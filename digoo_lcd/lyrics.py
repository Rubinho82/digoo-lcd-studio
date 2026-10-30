"""Letras sincronizadas da música que está tocando.

Fonte: LRCLIB (lrclib.net) — banco aberto e gratuito, sem chave. A busca roda numa thread só, uma música por
vez, e o resultado fica guardado em disco (cache\\lyrics) para não buscar de novo. Funciona com o Spotify
(título e artista certinhos) e com clipes no YouTube (o título "Artista - Música (Official Video)" é limpo).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import config_dir

log = logging.getLogger(__name__)

API = "https://lrclib.net/api"
UA = "DigooLCDStudio (https://github.com/Rubinho82/digoo-lcd-studio)"
CACHE_DIR = config_dir() / "cache" / "lyrics"
NOT_FOUND_RETRY = 3 * 86400   # letra que não existia: tenta de novo depois de 3 dias
ERROR_RETRY = 15              # falha de rede: janela curta para um timeout pontual ser tentado de novo logo

_BROWSERS = ("chrome", "msedge", "firefox", "opera", "brave", "vivaldi")
_JUNK = re.compile(
    r"\s*[\(\[【「][^\)\]】」]*?(official|oficial|video|vídeo|clipe|clip|lyric|letra|legendad|audio|áudio|"
    r"visuali[sz]er|\bhd\b|\b4k\b|\bmv\b|m/v|ao vivo|\blive\b|remaster|tradução|traducao|color coded|"
    r"prod\.|explicit)[^\)\]】」]*[\)\]】」]", re.I)
_TAIL = re.compile(r"\s+[-–]\s+.*\b(remaster\w*|version|versão|ao vivo|live|mono|stereo|edit|mix)\b.*$", re.I)
_CHANNEL = re.compile(r"\s*(-\s*topic|vevo|official|oficial|music|tv|records)\s*$", re.I)
_LRC = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


class Lyrics:
    """state: buscando | ok | sem | instrumental | erro. lines = [(segundos, texto)].
    'sem' = a música não tem letra; 'erro' = falha de rede ao buscar (distinto de 'sem')."""

    def __init__(self, state: str, lines=None, synced: bool = False):
        self.state = state
        self.lines: list[tuple[float, str]] = lines or []
        self.synced = synced
        self._times = [t for t, _ in self.lines]

    def at(self, pos: float, duration: float = 0.0) -> tuple[int, float, float]:
        """(linha atual, quanto da linha já passou 0..1, segundos desde que a linha começou).
        Linha -1 = antes da primeira. Letra sem tempo é espalhada pela duração da música."""
        n = len(self.lines)
        if not n:
            return -1, 0.0, 0.0
        if not self.synced:
            if duration <= 0:
                return 0, 0.0, 99.0
            per = duration / n
            i = min(n - 1, int(pos / per))
            return i, (pos - i * per) / per, pos - i * per
        i = bisect.bisect_right(self._times, pos) - 1
        if i < 0:
            return -1, 0.0, 0.0
        t0 = self._times[i]
        t1 = self._times[i + 1] if i + 1 < n else t0 + 5.0
        return i, max(0.0, min(1.0, (pos - t0) / max(0.05, t1 - t0))), pos - t0


DEMO = Lyrics("ok", [(0, "A letra da música aparece aqui"), (4, "linha por linha, no tempo certo"),
                     (8, "igual karaokê, enquanto toca"), (12, "no Spotify ou no YouTube"),
                     (16, "♪")], synced=True)


def parse_lrc(text: str) -> list[tuple[float, str]]:
    out = []
    for raw in (text or "").splitlines():
        tags = _LRC.findall(raw)
        if not tags:
            continue
        words = _LRC.sub("", raw).strip()
        for mm, ss in tags:
            out.append((int(mm) * 60 + float(ss), words))
    out.sort(key=lambda x: x[0])
    return out


def _clean(title: str) -> str:
    t = _JUNK.sub("", title or "")
    t = _TAIL.sub("", t)
    t = re.sub(r"\s*[|｜].*$", "", t)  # "Música | Clipe Oficial | Canal"
    return re.sub(r"\s{2,}", " ", t).strip(" -–\"'")


def candidates(media: dict) -> list[tuple[str, str]]:
    """Pares (música, artista) para tentar, do mais provável ao menos."""
    title, artist = media.get("title") or "", media.get("artist") or ""
    app = (media.get("app") or "").lower()
    browser = any(b in app for b in _BROWSERS)
    if artist and not browser:  # Spotify e players de música já mandam título e artista certos
        return [(_clean(title), artist)] if _clean(title) else []
    out = []
    for sep in (" - ", " – ", " — "):  # clipe: "Artista - Música (Official Video)"
        if sep in title:
            a, t = title.split(sep, 1)
            out.append((_clean(t), _clean(a)))
            break
    if artist and not out:
        out.append((_clean(title), _CHANNEL.sub("", artist).strip()))
    seen, uniq = set(), []
    for t, a in out:
        if t and (t.lower(), a.lower()) not in seen:
            seen.add((t.lower(), a.lower()))
            uniq.append((t, a))
    return uniq


def _get(path: str, params: dict):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    from .library import SSL_CTX

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as r:
                return json.loads(r.read().decode("utf8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code < 500 or attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))  # o servidor devolve 503 de vez em quando; na hora seguinte passa
        except (urllib.error.URLError, socket.timeout):  # HTTPError é subclasse: capturar depois
            if attempt == 2:  # queda de conexão ou timeout: tenta de novo algumas vezes antes de desistir
                raise
            time.sleep(1.0 * (attempt + 1))


def _pick(results, duration: float):
    """Prefere a que tem tempo; entre elas, a de duração mais parecida com a que está tocando."""
    if not results:
        return None
    if isinstance(results, dict):
        results = [results]

    def score(r):
        d = abs(float(r.get("duration") or 0) - duration) if duration else 0
        return (0 if r.get("syncedLyrics") else 1, 0 if d < 4 else 1, d)

    def lines(r):
        return sum(1 for ln in (r.get("syncedLyrics") or r.get("plainLyrics") or "").splitlines()
                   if _LRC.sub("", ln).strip())

    # o banco é aberto e tem registros lixo (ex.: só "probe"): letra de verdade tem pelo menos umas linhas
    good = [r for r in results if r.get("instrumental") or lines(r) >= 5]
    if duration:
        good = [r for r in good if not r.get("duration") or abs(float(r["duration"]) - duration) < 25] or good
    return min(good, key=score) if good else None


def fetch(media: dict) -> dict | None:
    """Busca na LRCLIB. Devolve o registro escolhido ou None se não achou (erro de rede sobe exceção)."""
    dur = float(media.get("duration") or 0)
    for track, artist in candidates(media):
        params = {"track_name": track, "artist_name": artist}
        if dur:
            params["duration"] = int(round(dur))
        hit = _pick(_get("get", params), dur)
        if hit:
            return hit
        hit = _pick(_get("search", {"track_name": track, "artist_name": artist}), dur)
        if hit:
            return hit
    cands = candidates(media)
    if cands:
        t, a = cands[0]
        return _pick(_get("search", {"q": f"{t} {a}".strip()}), dur)
    return None


def _to_lyrics(rec: dict | None) -> Lyrics:
    if not rec:
        return Lyrics("sem")
    if rec.get("instrumental"):
        return Lyrics("instrumental")
    synced = parse_lrc(rec.get("syncedLyrics") or "")
    if synced:
        return Lyrics("ok", synced, synced=True)
    plain = [ln.strip() for ln in (rec.get("plainLyrics") or "").splitlines() if ln.strip()]
    return Lyrics("ok", [(0.0, ln) for ln in plain], synced=False) if plain else Lyrics("sem")


class _Finder:
    def __init__(self):
        self._lock = threading.Lock()
        self._mem: dict[str, Lyrics] = {}
        self._errors: dict[str, float] = {}
        self._busy: str | None = None

    @staticmethod
    def key(media: dict) -> str:
        return f"{(media.get('title') or '').strip().lower()}|{(media.get('artist') or '').strip().lower()}"

    def get(self, media: dict) -> Lyrics:
        k = self.key(media)
        with self._lock:
            hit = self._mem.get(k)
            if hit is not None:
                return hit
            if time.monotonic() - self._errors.get(k, -1e9) < ERROR_RETRY:
                return Lyrics("erro")  # falha de rede recente: distinto de "sem" (música sem letra)
            disk = self._load(k)
            if disk is not None:
                self._mem[k] = disk
                return disk
            if self._busy is None:
                self._busy = k
                threading.Thread(target=self._work, args=(k, dict(media)), daemon=True,
                                 name="letras").start()
        return Lyrics("buscando")

    def _path(self, k: str):
        return CACHE_DIR / (hashlib.sha1(k.encode("utf8")).hexdigest()[:20] + ".json")

    def _load(self, k: str) -> Lyrics | None:
        p = self._path(k)
        try:
            d = json.loads(p.read_text("utf8"))
        except (OSError, ValueError):
            return None
        if d.get("none"):
            return Lyrics("sem") if time.time() - d.get("t", 0) < NOT_FOUND_RETRY else None
        return _to_lyrics(d)

    def _work(self, k: str, media: dict) -> None:
        try:
            rec = fetch(media)
            lyr = _to_lyrics(rec)
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                keep = {f: rec.get(f) for f in ("syncedLyrics", "plainLyrics", "instrumental")} if rec else \
                    {"none": True, "t": time.time()}
                self._path(k).write_text(json.dumps(keep, ensure_ascii=False), "utf8")
            except OSError:
                pass
            with self._lock:
                self._mem[k] = lyr
            log.info("letra %s: %s", media.get("title"), lyr.state + (" (com tempo)" if lyr.synced else ""))
        except Exception as exc:
            log.warning("letra: falha ao buscar (%s)", exc)
            with self._lock:
                self._errors[k] = time.monotonic()
        finally:
            with self._lock:
                self._busy = None


_finder = _Finder()


def for_media(media: dict | None, cfg=None) -> Lyrics | None:
    """Letra da música atual (None = nada tocando ou letras desligadas)."""
    if not media or not media.get("title"):
        return None
    if media.get("demo"):
        return DEMO
    if cfg is not None and not getattr(cfg, "music_lyrics", True):
        return Lyrics("desligado")
    return _finder.get(media)
