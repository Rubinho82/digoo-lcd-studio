"""Tradução das letras sincronizadas.

Três tradutores, do mais simples ao melhor:
- MyMemory: grátis e sem cadastro, com limite diário (~5 mil caracteres ≈ 2–3 músicas);
- DeepL: chave grátis (500 mil caracteres/mês), tradução muito boa;
- Claude (IA): chave paga (centavos por música), a mais natural — entende gíria, duplo sentido e o tom.

A música inteira vai junta (o tradutor entende o contexto), cada linha repetida só uma vez, e o resultado
fica em cache no disco. Roda numa thread, uma música por vez.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import config_dir

log = logging.getLogger(__name__)

PROVIDERS = {
    "mymemory": "MyMemory — grátis, sem cadastro (limite de ~2 a 3 músicas por dia)",
    "deepl": "DeepL — chave grátis, tradução muito boa (500 mil caracteres por mês)",
    "claude": "Claude (IA) — a mais natural, entende gíria e contexto (chave paga, centavos por música)",
}
LANGS = {"pt": "Português (Brasil)", "en": "Inglês", "es": "Espanhol", "fr": "Francês", "it": "Italiano",
         "de": "Alemão", "ja": "Japonês", "ko": "Coreano"}
_LANG_EN = {"pt": "Brazilian Portuguese", "en": "English", "es": "Spanish", "fr": "French", "it": "Italian",
            "de": "German", "ja": "Japanese", "ko": "Korean"}
_DEEPL = {"pt": "PT-BR", "en": "EN-US", "es": "ES", "fr": "FR", "it": "IT", "de": "DE", "ja": "JA", "ko": "KO"}
_MYMEMORY = {"pt": "pt-BR", "en": "en-US", "es": "es-ES", "fr": "fr-FR", "it": "it-IT", "de": "de-DE", "ja": "ja-JP",
             "ko": "ko-KR"}
CLAUDE_MODEL = "claude-opus-5-5"
CACHE_DIR = config_dir() / "cache" / "lyrics"
UA = "DigooLCDStudio (https://github.com/Rubinho82/digoo-lcd-studio)"


class TranslateError(RuntimeError):
    """Erro com mensagem para mostrar na interface. retry = segundos até tentar de novo."""

    def __init__(self, msg: str, retry: float = 60):
        super().__init__(msg)
        self.retry = retry


class Translation:
    """state: traduzindo | ok | mesma (já está no idioma) | erro. lines = alinhado com a letra."""

    def __init__(self, state: str, lines=None, msg: str = ""):
        self.state = state
        self.lines: list[str] = lines or []
        self.msg = msg


def _ssl():
    from .library import SSL_CTX

    return SSL_CTX


def _same_lang(src: str | None, target: str) -> bool:
    return bool(src) and src.lower().split("-")[0] == target


# ----------------------------------------------------------------------
# tradutores: recebem as linhas únicas e devolvem (traduções na mesma ordem, idioma de origem)
# ----------------------------------------------------------------------
def _mymemory_one(text: str, target: str) -> tuple[str | None, str | None]:
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
        {"q": text, "langpair": f"autodetect|{_MYMEMORY[target]}"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20,
                                    context=_ssl()) as r:
            d = json.loads(r.read().decode("utf8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise TranslateError("O limite diário grátis do MyMemory acabou. Amanhã volta, ou use DeepL/Claude.",
                                 retry=3600) from exc
        raise TranslateError(f"MyMemory respondeu erro {exc.code}") from exc
    if d.get("quotaFinished"):
        raise TranslateError("O limite diário grátis do MyMemory acabou. Amanhã volta, ou use DeepL/Claude.",
                             retry=3600)
    try:  # o MyMemory devolve erro com HTTP 200 e responseStatus != 200 (pode vir como string)
        status = int(str(d.get("responseStatus", 200)) or 200)
    except (TypeError, ValueError):
        status = 200
    if status == 429:
        raise TranslateError("Muitos pedidos ao MyMemory; tento de novo em instantes.", retry=10)
    if status != 200:  # 403, langpair inválido etc.: a mensagem de erro vem dentro de translatedText
        raise TranslateError(f"MyMemory recusou o pedido (status {status}).", retry=1e9)
    rd = d.get("responseData") or {}
    out = rd.get("translatedText")
    return (html.unescape(out).replace("\r", "") if out else None), rd.get("detectedLanguage")


def _mymemory(lines: list[str], target: str, media: dict) -> tuple[list[str], str | None, bool]:
    chunks, cur = [], []
    for s in lines:  # o MyMemory aceita até 500 bytes por pedido
        if cur and len("\n".join(cur + [s]).encode("utf8")) > 450:
            chunks.append(cur)
            cur = []
        cur.append(s)
    if cur:
        chunks.append(cur)
    from concurrent.futures import ThreadPoolExecutor

    first, src = _mymemory_one("\n".join(chunks[0]), target)  # o primeiro diz o idioma de origem
    if _same_lang(src, target):
        return lines, src, False

    degraded = []  # linhas que voltaram sem tradução (ficou o original): não vale gravar no cache

    def piece(ch, tr=None):
        tr = tr if tr is not None else _mymemory_one("\n".join(ch), target)[0]
        parts = [p.strip() for p in (tr or "").split("\n")]
        if tr is None or len(parts) != len(ch):  # perdeu o alinhamento: traduz linha por linha
            parts = []
            for s in ch:
                one = _mymemory_one(s, target)[0]
                if not one:  # não traduziu: fica o original (degradado)
                    degraded.append(True)
                    one = s
                parts.append(one.strip())
        return parts

    with ThreadPoolExecutor(max_workers=2) as pool:  # pedaços em paralelo sem forçar o rate-limit curto
        rest = list(pool.map(piece, chunks[1:]))
    out = piece(chunks[0], first)
    for parts in rest:
        out += parts
    return out, src, bool(degraded)


def _deepl(lines: list[str], target: str, media: dict, key: str) -> tuple[list[str], str | None, bool]:
    if not key:
        raise TranslateError("Cole a chave do DeepL na aba Música.", retry=1e9)
    host = "api-free.deepl.com" if key.strip().endswith(":fx") else "api.deepl.com"
    out, src = [], None
    context = "\n".join(lines)[:4000]
    for i in range(0, len(lines), 50):  # até 50 textos por pedido
        body = json.dumps({"text": lines[i:i + 50], "target_lang": _DEEPL[target], "context": context}).encode()
        req = urllib.request.Request(f"https://{host}/v2/translate", data=body, method="POST",
                                     headers={"Authorization": f"DeepL-Auth-Key {key.strip()}",
                                              "Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30, context=_ssl()) as r:
                d = json.loads(r.read().decode("utf8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise TranslateError("Chave do DeepL inválida.", retry=1e9) from exc
            if exc.code == 456:
                raise TranslateError("A cota do DeepL deste mês acabou.", retry=3600) from exc
            raise TranslateError(f"DeepL respondeu erro {exc.code}") from exc
        tr = d.get("translations") or []
        if len(tr) != len(lines[i:i + 50]):
            raise TranslateError("DeepL devolveu um número diferente de linhas")
        out += [t.get("text", "") for t in tr]
        src = src or (tr[0].get("detected_source_language") if tr else None)
    return out, src, False


_SYSTEM = (
    "You translate song lyrics for a karaoke screen that shows each translated line while the original is "
    "being sung. Translate into {lang}. Translate the meaning and the feeling, not word for word: turn slang, "
    "idioms and wordplay into natural equivalents a native speaker would really say, keep the tone of the song "
    "(romantic, angry, playful, sad), and keep each line about as short as the original so it can be read in "
    "time. Keep proper names, brand names and vocalizations (oh, yeah, la la, na na) as they are. Do not add "
    "notes, explanations or censorship. Return exactly one translated line for every input line, using the same "
    "number n. If the lyrics are already in {lang}, return the lines unchanged."
)
_SCHEMA = {
    "type": "object",
    "properties": {
        "source_language": {"type": "string", "description": "ISO 639-1 code of the lyrics' main language"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "t": {"type": "string"}},
                "required": ["n", "t"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["source_language", "lines"],
    "additionalProperties": False,
}


def _claude(lines: list[str], target: str, media: dict, key: str) -> tuple[list[str], str | None, bool]:
    if not key:
        raise TranslateError("Cole a chave da API do Claude na aba Música.", retry=1e9)
    # POST direto na API (urllib + SSL_CTX como os outros tradutores): evita arrastar o SDK anthropic
    # (httpx/httpcore/aiohttp/pydantic/opentelemetry…) para dentro do pacote só por um tradutor opcional pago.
    numbered = json.dumps([{"n": i, "t": s} for i, s in enumerate(lines)], ensure_ascii=False)
    song = f"{media.get('title', '')} — {media.get('artist', '')}".strip(" —")
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 16000,
        "fallbacks": "default",  # se o modelo recusar, a própria API tenta o modelo reserva recomendado
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
        "system": _SYSTEM.format(lang=_LANG_EN[target]),
        "messages": [{"role": "user", "content": f"Song: {song}\n\nLines:\n{numbered}"}],
    }, ensure_ascii=False).encode("utf8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key.strip(), "anthropic-version": "2023-06-01",
                 "anthropic-beta": "server-side-fallback-2026-07-01",
                 "content-type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=180, context=_ssl()) as r:
            data = json.loads(r.read().decode("utf8"))
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 401:
            raise TranslateError("Chave da API do Claude inválida.", retry=1e9) from exc
        if code == 403:
            raise TranslateError("A chave do Claude não tem permissão (veja créditos/plano no console).",
                                 retry=1e9) from exc
        if code == 429:
            raise TranslateError("Limite de uso da API do Claude; tento de novo em 1 minuto.") from exc
        raise TranslateError(f"API do Claude respondeu erro {code}",
                             retry=60 if code >= 500 else 1e9) from exc
    except urllib.error.URLError as exc:  # HTTPError é subclasse de URLError: capturar depois
        raise TranslateError("Sem conexão com a API do Claude.") from exc
    if data.get("stop_reason") == "refusal":
        raise TranslateError("A IA não quis traduzir esta letra.", retry=1e9)
    if data.get("stop_reason") == "max_tokens":
        raise TranslateError("A letra é longa demais para traduzir de uma vez.", retry=1e9)
    text = next((b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"), "")
    parsed = json.loads(text)
    by_n = {int(it["n"]): it["t"] for it in parsed.get("lines", []) if isinstance(it, dict) and "n" in it}
    missing = any(i not in by_n for i in range(len(lines)))  # faltou linha: ficou o original (degradado)
    return [by_n.get(i, s) for i, s in enumerate(lines)], parsed.get("source_language"), missing


# ----------------------------------------------------------------------
class _Worker:
    def __init__(self):
        self._lock = threading.Lock()
        self._mem: dict[str, tuple[dict, str | None]] = {}
        self._errors: dict[str, tuple[float, float, str]] = {}  # chave -> (quando, espera, mensagem)
        self._busy: str | None = None

    def get(self, lyr, media: dict, cfg) -> Translation:
        provider = getattr(cfg, "lyrics_provider", "mymemory")
        target = getattr(cfg, "lyrics_lang", "pt")
        if provider not in PROVIDERS or target not in LANGS:
            return Translation("erro", msg="tradutor ou idioma inválido")
        uniq = list(dict.fromkeys(s.strip() for _, s in lyr.lines if s.strip()))
        if not uniq:
            return Translation("mesma")
        key = hashlib.sha1(f"{provider}|{target}|".encode() + "\n".join(uniq).encode("utf8")).hexdigest()[:24]
        api_key = {"deepl": getattr(cfg, "deepl_key", ""), "claude": getattr(cfg, "claude_key", "")}.get(provider, "")
        err_key = key + hashlib.sha1(api_key.encode()).hexdigest()[:8]
        with self._lock:
            hit = self._mem.get(key) or self._load(key)
            if hit is not None:
                self._mem[key] = hit
                mapping, src = hit
                if _same_lang(src, target):
                    return Translation("mesma")
                return Translation("ok", [mapping.get(s.strip(), "") if s.strip() else "" for _, s in lyr.lines])
            err = self._errors.get(err_key)
            if err and time.monotonic() - err[0] < err[1]:
                return Translation("erro", msg=err[2])
            if self._busy is None:
                self._busy = key
                threading.Thread(target=self._work, args=(key, err_key, provider, target, uniq, dict(media), api_key),
                                 daemon=True, name="traducao").start()
        return Translation("traduzindo")

    def _path(self, key: str):
        return CACHE_DIR / f"tr_{key}.json"

    def _load(self, key: str):
        try:
            d = json.loads(self._path(key).read_text("utf8"))
            return d["map"], d.get("src")
        except (OSError, ValueError, KeyError):
            return None

    def _work(self, key, err_key, provider, target, uniq, media, api_key):
        t0 = time.monotonic()
        try:
            if provider == "mymemory":
                out, src, partial = _mymemory(uniq, target, media)
            elif provider == "deepl":
                out, src, partial = _deepl(uniq, target, media, api_key)
            else:
                out, src, partial = _claude(uniq, target, media, api_key)
            mapping = dict(zip(uniq, out))
            if not partial:  # tradução degradada (linhas sem traduzir) fica só na memória, não vai pro disco
                try:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    self._path(key).write_text(json.dumps({"map": mapping, "src": src}, ensure_ascii=False), "utf8")
                except OSError:
                    pass
            with self._lock:
                self._mem[key] = (mapping, src)
            log.info("tradução %s (%s→%s, %s): %d linhas em %.1fs", media.get("title"), src, target, provider,
                     len(uniq), time.monotonic() - t0)
        except TranslateError as exc:
            log.warning("tradução: %s", exc)
            with self._lock:
                self._errors[err_key] = (time.monotonic(), exc.retry, str(exc))
        except Exception as exc:  # resposta inesperada: não derruba o app, tenta de novo depois
            log.exception("tradução falhou")
            with self._lock:
                self._errors[err_key] = (time.monotonic(), 120, f"falhou ({exc.__class__.__name__})")
        finally:
            with self._lock:
                self._busy = None


_worker = _Worker()


def clear_cache() -> int:
    """Apaga os caches de tradução (tr_*.json em CACHE_DIR) e limpa o cache em memória. Devolve quantos apagou."""
    n = 0
    try:
        for p in CACHE_DIR.glob("tr_*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    with _worker._lock:
        _worker._mem.clear()
        _worker._errors.clear()
    return n


def for_lyrics(lyr, media: dict | None, cfg) -> Translation | None:
    """Tradução da letra atual, alinhada linha a linha (None = tradução desligada ou sem letra)."""
    if lyr is None or getattr(lyr, "state", "") != "ok" or not getattr(cfg, "lyrics_translate", False):
        return None
    if (media or {}).get("demo"):
        return Translation("ok", [{"A letra da música aparece aqui": "The song lyrics show up here",
                                   "linha por linha, no tempo certo": "line by line, right on time"}.get(s, "")
                                  for _, s in lyr.lines])
    return _worker.get(lyr, media or {}, cfg)
