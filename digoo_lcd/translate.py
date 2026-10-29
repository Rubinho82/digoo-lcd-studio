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
    rd = d.get("responseData") or {}
    out = rd.get("translatedText")
    return (html.unescape(out).replace("\r", "") if out else None), rd.get("detectedLanguage")


def _mymemory(lines: list[str], target: str, media: dict) -> tuple[list[str], str | None]:
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
        return lines, src

    def piece(ch, tr=None):
        tr = tr if tr is not None else _mymemory_one("\n".join(ch), target)[0]
        parts = [p.strip() for p in (tr or "").split("\n")]
        if tr is None or len(parts) != len(ch):  # perdeu o alinhamento: traduz linha por linha
            parts = [(_mymemory_one(s, target)[0] or s).strip() for s in ch]
        return parts

    with ThreadPoolExecutor(max_workers=4) as pool:  # pedaços em paralelo: a música sai em segundos
        rest = list(pool.map(piece, chunks[1:]))
    out = piece(chunks[0], first)
    for parts in rest:
        out += parts
    return out, src


def _deepl(lines: list[str], target: str, media: dict, key: str) -> tuple[list[str], str | None]:
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
    return out, src


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


def _claude(lines: list[str], target: str, media: dict, key: str) -> tuple[list[str], str | None]:
    if not key:
        raise TranslateError("Cole a chave da API do Claude na aba Música.", retry=1e9)
    import anthropic  # pesado: só carrega quando alguém usa esse tradutor

    client = anthropic.Anthropic(api_key=key.strip(), timeout=180.0, max_retries=2)
    numbered = json.dumps([{"n": i, "t": s} for i, s in enumerate(lines)], ensure_ascii=False)
    song = f"{media.get('title', '')} — {media.get('artist', '')}".strip(" —")
    try:
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # se o modelo recusar, a própria API tenta o modelo reserva recomendado
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
            system=_SYSTEM.format(lang=_LANG_EN[target]),
            messages=[{"role": "user", "content": f"Song: {song}\n\nLines:\n{numbered}"}],
        )
    except anthropic.AuthenticationError as exc:
        raise TranslateError("Chave da API do Claude inválida.", retry=1e9) from exc
    except anthropic.PermissionDeniedError as exc:
        raise TranslateError("A chave do Claude não tem permissão (veja créditos/plano no console).",
                             retry=1e9) from exc
    except anthropic.RateLimitError as exc:
        raise TranslateError("Limite de uso da API do Claude; tento de novo em 1 minuto.") from exc
    except anthropic.APIStatusError as exc:
        raise TranslateError(f"API do Claude respondeu erro {exc.status_code}",
                             retry=60 if exc.status_code >= 500 else 1e9) from exc
    except anthropic.APIConnectionError as exc:
        raise TranslateError("Sem conexão com a API do Claude.") from exc
    if resp.stop_reason == "refusal":
        raise TranslateError("A IA não quis traduzir esta letra.", retry=1e9)
    if resp.stop_reason == "max_tokens":
        raise TranslateError("A letra é longa demais para traduzir de uma vez.", retry=1e9)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)
    by_n = {int(it["n"]): it["t"] for it in data.get("lines", []) if isinstance(it, dict) and "n" in it}
    return [by_n.get(i, s) for i, s in enumerate(lines)], data.get("source_language")


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
                out, src = _mymemory(uniq, target, media)
            elif provider == "deepl":
                out, src = _deepl(uniq, target, media, api_key)
            else:
                out, src = _claude(uniq, target, media, api_key)
            mapping = dict(zip(uniq, out))
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


def for_lyrics(lyr, media: dict | None, cfg) -> Translation | None:
    """Tradução da letra atual, alinhada linha a linha (None = tradução desligada ou sem letra)."""
    if lyr is None or getattr(lyr, "state", "") != "ok" or not getattr(cfg, "lyrics_translate", False):
        return None
    if (media or {}).get("demo"):
        return Translation("ok", [{"A letra da música aparece aqui": "The song lyrics show up here",
                                   "linha por linha, no tempo certo": "line by line, right on time"}.get(s, "")
                                  for _, s in lyr.lines])
    return _worker.get(lyr, media or {}, cfg)
