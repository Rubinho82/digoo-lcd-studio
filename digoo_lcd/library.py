"""Biblioteca de GIFs/vídeos: busca online (GIPHY, Tenor, anime, Wikimedia) e acervo local."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import config_dir

UA = "DigooLCDStudio/1.1 (+cooler screen app)"
LIB_DIR = config_dir() / "biblioteca"
THUMB_DIR = config_dir() / "cache" / "thumbs"
INDEX_PATH = LIB_DIR / "biblioteca.json"
for _d in (LIB_DIR, THUMB_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _ssl_context() -> ssl.SSLContext:
    # A lista de certificados do Windows/OpenSSL pode estar velha (já rejeitou a Wikimedia);
    # o pacote certifi traz a lista atual da Mozilla.
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()

VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".ogv")


@dataclass
class Item:
    id: str
    provider: str
    title: str
    thumb_url: str
    media_url: str
    kind: str = "gif"           # gif | video
    width: int = 0
    height: int = 0
    size: int = 0               # bytes, quando o provedor informa
    page_url: str = ""
    credit: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.provider}_{self.id}"

    @property
    def ext(self) -> str:
        path = urllib.parse.urlparse(self.media_url).path.lower()
        m = re.search(r"\.(gif|mp4|webm|webp|png|jpe?g|mov|ogv)$", path)
        return "." + m.group(1) if m else (".mp4" if self.kind == "video" else ".gif")


class ProviderError(RuntimeError):
    pass


# esperas entre tentativas; o nº de tentativas é len(_RETRY_BACKOFF)+1
_RETRY_BACKOFF = (0.5, 1.5)


def _retry_after(exc: urllib.error.HTTPError, default: float) -> float:
    # respeita o cabeçalho Retry-After (em segundos) quando vier; senão usa o backoff
    try:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        if ra and ra.strip().isdigit():
            return min(float(ra.strip()), 30.0)
    except Exception:
        pass
    return default


def http_json(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(len(_RETRY_BACKOFF) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read().decode("utf8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ProviderError("chave de API inválida ou ausente") from exc
            # 429/5xx costumam ser passageiros: tenta de novo antes de desistir
            if (exc.code == 429 or 500 <= exc.code < 600) and attempt < len(_RETRY_BACKOFF):
                time.sleep(_retry_after(exc, _RETRY_BACKOFF[attempt]))
                continue
            if exc.code == 429:
                raise ProviderError("muitas buscas seguidas — espere um pouco") from exc
            raise ProviderError(f"erro HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < len(_RETRY_BACKOFF):
                time.sleep(_RETRY_BACKOFF[attempt])
                continue
            reason = getattr(exc, "reason", exc)
            raise ProviderError(f"sem conexão: {reason}") from exc
    raise ProviderError("sem conexão")  # inatingível


def http_download(url: str, dest: Path, progress=None, timeout: float = 30) -> Path:
    # só http/https: bloqueia file:/ftp:/data: vindos de uma media_url manipulada do provedor
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ProviderError("URL de mídia inválida")
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(len(_RETRY_BACKOFF) + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": UA}
        if have:  # já temos parte do arquivo: pede para continuar de onde parou
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                # 206 = servidor aceitou retomar; 200 = não suporta Range → recomeça do zero
                resume = bool(have) and getattr(r, "status", 200) == 206
                if not resume:
                    have = 0
                clen = int(r.headers.get("Content-Length") or 0)
                total = (have + clen) if clen else 0
                done = have
                with open(tmp, "ab" if resume else "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            progress(done / total)
            tmp.replace(dest)
            return dest
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and tmp.exists():  # Range inválido: joga o .part fora e recomeça
                tmp.unlink(missing_ok=True)
            retriable = exc.code in (416, 429) or 500 <= exc.code < 600
            if not retriable or attempt >= len(_RETRY_BACKOFF):
                raise ProviderError(f"erro HTTP {exc.code}") from exc
            time.sleep(_retry_after(exc, _RETRY_BACKOFF[attempt]))
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= len(_RETRY_BACKOFF):
                reason = getattr(exc, "reason", exc)
                raise ProviderError(f"sem conexão: {reason}") from exc
            time.sleep(_RETRY_BACKOFF[attempt])
    return dest  # inatingível


def fetch_preview(url: str) -> Path:
    """Baixa (com cache) a prévia grande de um resultado, com a extensão do conteúdo real (GIF/JPG/…)."""
    h = hashlib.sha1(url.encode()).hexdigest()[:20]
    for old in THUMB_DIR.glob(f"prev_{h}.*"):
        if old.suffix != ".part":
            return old
    tmp = THUMB_DIR / f"prev_{h}.bin"
    http_download(url, tmp, timeout=30)
    dest = tmp.with_suffix(_sniff_ext(tmp) or ".jpg")
    tmp.replace(dest)
    return dest


def fetch_thumb(url: str) -> Path:
    """Baixa (com cache) a miniatura de um resultado."""
    h = hashlib.sha1(url.encode()).hexdigest()[:20]
    ext = ".gif" if ".gif" in url.lower() else ".jpg"
    path = THUMB_DIR / f"{h}{ext}"
    if not path.exists():
        http_download(url, path, timeout=15)
    return path


# ----------------------------------------------------------------------
# provedores
# ----------------------------------------------------------------------
class Provider:
    key = ""
    name = ""
    needs_key = False
    key_help = ""
    credit = ""
    categories: list[str] = []
    page_size = 30
    paginates = True

    def __init__(self, api_key: str = "", rating: str = "pg-13", lang: str = "pt"):
        self.api_key = (api_key or "").strip()
        self.rating = rating
        self.lang = lang

    def trending(self, offset: int = 0) -> list[Item]:
        raise NotImplementedError

    def search(self, query: str, offset: int = 0) -> list[Item]:
        raise NotImplementedError

    def _check_key(self):
        if self.needs_key and not self.api_key:
            raise ProviderError(f"cole sua chave grátis do {self.name} no botão \"Chaves de API\"")


GENERAL_CATEGORIES = ["neon", "anime", "games", "espaço", "cyberpunk", "pixel art", "carros", "gatos",
                      "memes", "loop", "abstrato", "chuva", "fogo", "oceano", "lo-fi", "vaporwave"]


class Giphy(Provider):
    key = "giphy"
    name = "GIPHY"
    needs_key = True
    key_help = "https://developers.giphy.com/dashboard/?create=true"
    credit = "Powered by GIPHY"
    categories = GENERAL_CATEGORIES

    def __init__(self, *a, stickers: bool = False, **kw):
        super().__init__(*a, **kw)
        self.stickers = stickers

    def _base(self):
        return f"https://api.giphy.com/v1/{'stickers' if self.stickers else 'gifs'}"

    def _parse(self, data: dict) -> list[Item]:
        out = []
        for g in data.get("data", []):
            im = g.get("images", {})
            orig = im.get("original", {})
            thumb = (im.get("fixed_width_small") or im.get("fixed_width") or im.get("preview_gif") or {}).get("url", "")
            mp4 = orig.get("mp4") or (im.get("original_mp4") or {}).get("mp4")
            prev = (im.get("downsized_medium") or im.get("fixed_height") or im.get("fixed_width") or {}).get("url", "")
            # Stickers têm fundo transparente: o GIF preserva isso, o MP4 não.
            media = orig.get("url") if self.stickers or not mp4 else mp4
            if not media:
                continue
            out.append(Item(
                id=g["id"], provider=self.key + ("_st" if self.stickers else ""), title=g.get("title") or "GIF",
                thumb_url=thumb, media_url=media,
                kind="gif" if urllib.parse.urlparse(media).path.endswith(".gif") else "video",
                width=int(orig.get("width") or 0), height=int(orig.get("height") or 0),
                size=int(orig.get("mp4_size") or orig.get("size") or 0),
                page_url=g.get("url", ""), credit=self.credit, extra={"preview": prev}))
        return out

    def trending(self, offset=0):
        self._check_key()
        q = urllib.parse.urlencode({"api_key": self.api_key, "limit": self.page_size, "offset": offset,
                                    "rating": self.rating, "bundle": "messaging_non_clips"})
        return self._parse(http_json(f"{self._base()}/trending?{q}"))

    def search(self, query, offset=0):
        self._check_key()
        q = urllib.parse.urlencode({"api_key": self.api_key, "q": query, "limit": self.page_size,
                                    "offset": offset, "rating": self.rating, "lang": self.lang,
                                    "bundle": "messaging_non_clips"})
        return self._parse(http_json(f"{self._base()}/search?{q}"))


class Tenor(Provider):
    key = "tenor"
    name = "Tenor"
    needs_key = True
    key_help = "https://developers.google.com/tenor/guides/quickstart"
    credit = "Via Tenor"
    categories = GENERAL_CATEGORIES

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._next: dict[str, str] = {}

    def _parse(self, data: dict, ctx: str) -> list[Item]:
        self._next[ctx] = data.get("next", "")
        out = []
        for r in data.get("results", []):
            mf = r.get("media_formats", {})
            mp4 = mf.get("mp4") or mf.get("loopedmp4") or {}
            gif = mf.get("gif") or {}
            thumb = (mf.get("tinygif") or mf.get("nanogif") or gif).get("url", "")
            media = mp4 or gif
            if not media.get("url"):
                continue
            dims = media.get("dims") or [0, 0]
            out.append(Item(
                id=r["id"], provider=self.key, title=r.get("content_description") or r.get("title") or "GIF",
                thumb_url=thumb, media_url=media["url"], kind="video" if mp4 else "gif",
                width=int(dims[0]), height=int(dims[1]), size=int(media.get("size") or 0),
                page_url=r.get("itemurl", ""), credit=self.credit, extra={"preview": gif.get("url", "")}))
        return out

    def _params(self, ctx: str, offset: int, **extra):
        p = {"key": self.api_key, "client_key": "digoolcd", "limit": self.page_size,
             "media_filter": "mp4,gif,tinygif", "contentfilter": "medium", "locale": "pt_BR", **extra}
        if offset and self._next.get(ctx):
            p["pos"] = self._next[ctx]
        return urllib.parse.urlencode(p)

    def trending(self, offset=0):
        self._check_key()
        return self._parse(http_json("https://tenor.googleapis.com/v2/featured?" + self._params("trend", offset)), "trend")

    def search(self, query, offset=0):
        self._check_key()
        ctx = "s:" + query
        return self._parse(http_json("https://tenor.googleapis.com/v2/search?" + self._params(ctx, offset, q=query)), ctx)


class Anime(Provider):
    """nekos.best — GIFs de anime por categoria, sem chave (cada busca traz uma leva aleatória)."""

    key = "anime"
    name = "Anime"
    credit = "nekos.best"
    LABELS = {"dance": "dançando", "happy": "feliz", "smile": "sorriso", "wave": "tchau", "wink": "piscadinha",
              "thumbsup": "joinha", "laugh": "risada", "blush": "vergonha", "highfive": "toca aqui",
              "hug": "abraço", "pat": "cafuné", "cuddle": "carinho", "nod": "concordando", "sleep": "dormindo",
              "think": "pensando", "yeet": "arremesso", "shrug": "sei lá", "lurk": "espiando",
              "peck": "beijinho", "run": "correndo"}
    categories = list(LABELS.values())
    page_size = 20

    def _get(self, cat: str) -> list[Item]:
        data = http_json(f"https://nekos.best/api/v2/{cat}?amount={self.page_size}")
        out = []
        for r in data.get("results", []):
            url = r.get("url", "")
            dims = r.get("dimensions") or {}
            iid = Path(urllib.parse.urlparse(url).path).stem
            out.append(Item(id=iid, provider=self.key, title=r.get("anime_name") or self.LABELS.get(cat, cat),
                            thumb_url=url, media_url=url, kind="gif",
                            width=int(dims.get("width") or 0), height=int(dims.get("height") or 0),
                            page_url=url, credit=self.credit))
        return out

    def trending(self, offset=0):
        return self._get("dance")

    def search(self, query, offset=0):
        q = query.strip().lower()
        for cat, label in self.LABELS.items():
            if q in (cat, label):
                return self._get(cat)
        matches = [c for c, lb in self.LABELS.items() if q and (q in c or q in lb)]
        if matches:
            return self._get(matches[0])
        raise ProviderError("no Anime dá para buscar: " + ", ".join(self.LABELS.values()))


class Wikimedia(Provider):
    """Wikimedia Commons — GIFs e vídeos livres (Creative Commons), sem chave."""

    key = "wikimedia"
    name = "Wikimedia"
    credit = "Wikimedia Commons (licença livre)"
    categories = ["animação", "loop", "neon", "espaço", "terra girando", "fractal", "oceano", "aurora",
                  "timelapse", "galáxia", "pixel art", "ilusão de ótica", "fogo", "chuva"]

    def __init__(self, *a, videos: bool = False, **kw):
        super().__init__(*a, **kw)
        self.videos = videos

    def _query(self, text: str, offset: int) -> list[Item]:
        mime = "filemime:video/webm" if self.videos else "filemime:image/gif"
        p = {"action": "query", "generator": "search", "gsrsearch": f"{text} {mime}", "gsrnamespace": 6,
             "gsrlimit": self.page_size, "gsroffset": offset, "prop": "imageinfo",
             "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 220,
             "iiextmetadatafilter": "Artist|LicenseShortName", "format": "json"}
        data = http_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(p))
        pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda x: x.get("index", 0))
        out = []
        for pg in pages:
            ii = (pg.get("imageinfo") or [{}])[0]
            if not ii.get("url"):
                continue
            meta = ii.get("extmetadata") or {}
            artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()[:60]
            lic = (meta.get("LicenseShortName") or {}).get("value", "")
            title = pg.get("title", "").removeprefix("File:").rsplit(".", 1)[0]
            out.append(Item(id=str(pg["pageid"]), provider=self.key, title=title,
                            thumb_url=ii.get("thumburl", ""), media_url=ii["url"],
                            kind="video" if self.videos else "gif",
                            width=int(ii.get("width") or 0), height=int(ii.get("height") or 0),
                            size=int(ii.get("size") or 0), page_url=ii.get("descriptionurl", ""),
                            credit=" · ".join(x for x in (artist, lic) if x) or self.credit,
                            extra={"preview": ii["url"] if not self.videos and int(ii.get("size") or 0) < 6_000_000
                                   else ii.get("thumburl", "")}))
        return out

    def trending(self, offset=0):
        return self._query("animation loop", offset)

    def search(self, query, offset=0):
        return self._query(query, offset)


# chave → (nome na tela, fábrica a partir da Config)
PROVIDERS = {
    "giphy": ("GIPHY", lambda cfg: Giphy(cfg.giphy_key, cfg.gif_rating)),
    "giphy_st": ("GIPHY Stickers", lambda cfg: Giphy(cfg.giphy_key, cfg.gif_rating, stickers=True)),
    "tenor": ("Tenor", lambda cfg: Tenor(cfg.tenor_key)),
    "anime": ("Anime", lambda cfg: Anime()),
    "wikimedia": ("Wikimedia GIFs", lambda cfg: Wikimedia()),
    "wikimedia_v": ("Wikimedia vídeos", lambda cfg: Wikimedia(videos=True)),
}


# ----------------------------------------------------------------------
# acervo local
# ----------------------------------------------------------------------
@dataclass
class Entry:
    file: str
    title: str = ""
    source: str = "arquivo"
    page_url: str = ""
    credit: str = ""
    favorite: bool = False
    added: float = field(default_factory=time.time)


class Library:
    def __init__(self):
        self._lock = threading.Lock()
        self.entries: dict[str, Entry] = {}
        self.load()

    def load(self):
        from .sources import MEDIA_EXT

        try:
            raw = json.loads(INDEX_PATH.read_text(encoding="utf8"))
            entries = {k: Entry(**v) for k, v in raw.items()}
        except Exception:
            entries = {}
        # arquivos colocados direto na pasta também entram
        for f in LIB_DIR.iterdir():
            if f.is_file() and f.suffix.lower() in MEDIA_EXT and f.name not in entries:
                entries[f.name] = Entry(file=f.name, title=f.stem, added=f.stat().st_mtime)
        self.entries = {k: e for k, e in entries.items() if (LIB_DIR / e.file).exists()}

    def save(self):
        with self._lock:
            tmp = INDEX_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: asdict(v) for k, v in self.entries.items()}, indent=1,
                                      ensure_ascii=False), encoding="utf8")
            tmp.replace(INDEX_PATH)

    @staticmethod
    def path(entry: Entry) -> Path:
        return LIB_DIR / entry.file

    def has(self, item: Item) -> Path | None:
        stem = _safe(item.uid)
        for name in list(self.entries):
            if Path(name).stem == stem and (LIB_DIR / name).exists():
                return LIB_DIR / name
        return None

    def download(self, item: Item, progress=None) -> Path:
        existing = self.has(item)
        if existing:
            return existing
        name = _safe(item.uid) + item.ext
        dest = LIB_DIR / name
        http_download(item.media_url, dest, progress, timeout=60)
        real = _sniff_ext(dest)
        if real and real != dest.suffix.lower():  # URL sem extensão (ex.: prévias da Steam)
            new = dest.with_suffix(real)
            dest.replace(new)
            dest, name = new, new.name
        with self._lock:
            self.entries[name] = Entry(file=name, title=item.title, source=item.provider,
                                       page_url=item.page_url, credit=item.credit)
        self.save()
        return dest

    def import_files(self, paths: list[str]) -> list[Path]:
        out = []
        for p in paths:
            src = Path(p)
            if not src.is_file():
                continue
            dest = LIB_DIR / src.name
            n = 1
            while dest.exists():
                dest = LIB_DIR / f"{src.stem}_{n}{src.suffix}"
                n += 1
            shutil.copy2(src, dest)
            self.entries[dest.name] = Entry(file=dest.name, title=src.stem)
            out.append(dest)
        self.save()
        return out

    def remove(self, name: str):
        e = self.entries.pop(name, None)
        if e:
            try:
                (LIB_DIR / e.file).unlink()
            except FileNotFoundError:
                pass
        self.save()

    def toggle_favorite(self, name: str) -> bool:
        e = self.entries[name]
        e.favorite = not e.favorite
        self.save()
        return e.favorite

    def sorted_entries(self, favorites_only: bool = False) -> list[tuple[str, Entry]]:
        items = [(k, e) for k, e in self.entries.items() if e.favorite or not favorites_only]
        return sorted(items, key=lambda kv: (not kv[1].favorite, -kv[1].added))


def _sniff_ext(path: Path) -> str:
    head = path.read_bytes()[:12]
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
    return ""


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:80]


def local_thumb(path: Path, size: int = 150) -> Path | None:
    """Miniatura estática (primeiro quadro) para vídeos/imagens locais."""
    from PIL import Image, ImageOps

    try:
        out = THUMB_DIR / f"local_{hashlib.sha1(str(path).encode()).hexdigest()[:16]}_{int(path.stat().st_mtime)}.jpg"
    except FileNotFoundError:
        return None
    if out.exists():
        return out
    try:
        if path.suffix.lower() in VIDEO_SUFFIXES:
            import av

            with av.open(str(path)) as c:
                img = next(c.decode(video=0)).to_image()
        else:
            img = Image.open(path)
            img.seek(0)
        img = ImageOps.fit(img.convert("RGB"), (size, size), Image.BILINEAR)
        img.save(out, "JPEG", quality=85)
        return out
    except Exception:
        return None
