"""Wallpaper Engine: Workshop da Steam (navegar, assinar, baixar) e wallpapers já instalados.

Busca: página pública do Workshop (só para pegar os IDs) + ISteamRemoteStorage/GetPublishedFileDetails
(sem chave). Assinar/baixar: steam_api64.dll do próprio Wallpaper Engine, num processo separado
(`DigooLCD --steam-ws subscribe <id>`), usando a Steam já logada — sem senha, igual o WE faz.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import winreg
from dataclasses import dataclass, field
from pathlib import Path

from .library import SSL_CTX, Item, Provider, ProviderError

APP_ID = 431960
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
CREATE_NO_WINDOW = 0x08000000

# Estados do ISteamUGC::GetItemState
ST_SUBSCRIBED, ST_INSTALLED, ST_NEEDS_UPDATE, ST_DOWNLOADING, ST_PENDING = 1, 4, 8, 16, 32


# ----------------------------------------------------------------------
# onde estão a Steam e o Wallpaper Engine
# ----------------------------------------------------------------------
def steam_dir() -> Path | None:
    for hive, key, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                           (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
        try:
            with winreg.OpenKey(hive, key) as k:
                p = Path(winreg.QueryValueEx(k, val)[0])
                if p.exists():
                    return p
        except OSError:
            pass
    return None


def library_dirs() -> list[Path]:
    sd = steam_dir()
    if not sd:
        return []
    out = [sd]
    vdf = sd / "steamapps" / "libraryfolders.vdf"
    try:
        for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf8", errors="replace")):
            p = Path(m.group(1).replace("\\\\", "\\"))
            if p.exists() and all(p.resolve() != o.resolve() for o in out):
                out.append(p)
    except OSError:
        pass
    return out


def we_dir() -> Path | None:
    for lib in library_dirs():
        p = lib / "steamapps" / "common" / "wallpaper_engine"
        if p.exists():
            return p
    return None


def workshop_dirs() -> list[Path]:
    return [d for d in (lib / "steamapps" / "workshop" / "content" / str(APP_ID) for lib in library_dirs()) if d.exists()]


def steam_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"], capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW, timeout=10).stdout
        return "steam.exe" in out.lower()
    except Exception:
        return False


def start_steam() -> None:
    sd = steam_dir()
    if sd and (sd / "steam.exe").exists():
        subprocess.Popen([str(sd / "steam.exe"), "-silent"], creationflags=CREATE_NO_WINDOW)


# ----------------------------------------------------------------------
# wallpapers locais (assinados + os que vêm com o WE + os seus)
# ----------------------------------------------------------------------
@dataclass
class LocalWallpaper:
    id: str
    title: str
    type: str          # video | scene | web | application
    folder: Path
    tags: list = field(default_factory=list)
    rating: str = ""
    source: str = "workshop"   # workshop | padrão | meus

    @property
    def project(self) -> dict:
        try:
            return json.loads((self.folder / "project.json").read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    @property
    def video_file(self) -> Path | None:
        if self.type != "video":
            return None
        f = self.project.get("file")
        p = self.folder / f if f else None
        if p and p.exists():
            return p
        vids = sorted(self.folder.glob("*.mp4"), key=lambda x: -x.stat().st_size) + sorted(self.folder.glob("*.webm"))
        return vids[0] if vids else None

    @property
    def preview(self) -> Path | None:
        pv = self.project.get("preview")
        cands = ([self.folder / pv] if pv else []) + [self.folder / n for n in ("preview.gif", "preview.jpg", "preview.png")]
        return next((c for c in cands if c.exists()), None)

    def playable(self) -> Path | None:
        """O que mandar para a telinha: o vídeo, ou a prévia (cenas/web não rodam fora do WE)."""
        return self.video_file or self.preview


def _read_project(folder: Path, source: str) -> LocalWallpaper | None:
    pj = folder / "project.json"
    if not pj.exists():
        return None
    try:
        j = json.loads(pj.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    wtype = str(j.get("type") or "").lower()
    if not wtype:
        wtype = "application" if str(j.get("file", "")).lower().endswith(".exe") else "scene"
    return LocalWallpaper(id=folder.name, title=str(j.get("title") or folder.name), type=wtype, folder=folder,
                          tags=list(j.get("tags") or []), rating=str(j.get("contentrating") or ""), source=source)


def local_wallpapers() -> list[LocalWallpaper]:
    out: list[LocalWallpaper] = []
    for d in workshop_dirs():
        for f in d.iterdir():
            if f.is_dir():
                w = _read_project(f, "workshop")
                if w:
                    out.append(w)
    wd = we_dir()
    if wd:
        for sub, src in (("myprojects", "meus"), ("defaultprojects", "padrão")):
            base = wd / "projects" / sub
            if base.exists():
                for f in sorted(base.iterdir()):
                    if f.is_dir():
                        w = _read_project(f, src)
                        if w and w.type != "application":
                            out.append(w)
    order = {"workshop": 0, "meus": 1, "padrão": 2}
    out.sort(key=lambda w: (order.get(w.source, 3), w.type != "video", -w.folder.stat().st_mtime))
    return out


# ----------------------------------------------------------------------
# Workshop online
# ----------------------------------------------------------------------
WS_TAGS = {"Anime": "Anime", "Game": "Jogos", "Landscape": "Paisagem", "Sci-Fi": "Ficção científica",
           "Cyberpunk": "Cyberpunk", "Fantasy": "Fantasia", "Pixel art": "Pixel art", "Retro": "Retrô",
           "Nature": "Natureza", "Abstract": "Abstrato", "Relaxing": "Relaxante", "Music": "Música",
           "Vehicle": "Carros", "Animal": "Animais", "Girls": "Garotas", "Cartoon": "Desenho",
           "Technology": "Tecnologia", "Medieval": "Medieval", "Memes": "Memes", "MMD": "MMD"}
WS_SORTS = {"trend": "Em alta (semana)", "totaluniquesubscribers": "Mais assinados", "mostrecent": "Mais recentes",
            "lastupdated": "Atualizados"}
WS_TYPES = {"": "Todos os tipos", "Video": "Só vídeos", "Scene": "Só cenas", "Web": "Só web"}
TYPE_PT = {"Video": "Vídeo", "Scene": "Cena", "Web": "Web"}


def _details(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    form = {"itemcount": len(ids)}
    for i, x in enumerate(ids):
        form[f"publishedfileids[{i}]"] = x
    req = urllib.request.Request(
        "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        data=urllib.parse.urlencode(form).encode(), headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            data = json.loads(r.read().decode("utf8"))
    except Exception as exc:
        raise ProviderError(f"Steam não respondeu: {exc}") from exc
    return {d["publishedfileid"]: d for d in data.get("response", {}).get("publishedfiledetails", [])
            if d.get("result") == 1}


class Workshop(Provider):
    key = "workshop"
    name = "Wallpaper Engine"
    credit = "Steam Workshop · Wallpaper Engine"
    page_size = 30

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.tag = "Anime"
        self.sort = "trend"
        self.wtype = ""
        self.categories = list(WS_TAGS.values())

    def set_category(self, label: str):
        self.tag = next((k for k, v in WS_TAGS.items() if v == label), label)

    def _page(self, query: str, offset: int) -> list[Item]:
        page = offset // self.page_size + 1
        sort = "textsearch" if query else self.sort
        p = [("appid", APP_ID), ("section", "readytouseitems"), ("numperpage", self.page_size), ("p", page),
             ("browsesort", sort), ("actualsort", sort), ("requiredtags[]", "Everyone")]
        if query:
            p.append(("searchtext", query))
        if sort == "trend":
            p.append(("days", 7))
        if self.tag:
            p.append(("requiredtags[]", self.tag))
        if self.wtype:
            p.append(("requiredtags[]", self.wtype))
        url = "https://steamcommunity.com/workshop/browse/?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "pt-BR,pt;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                html = r.read().decode("utf8", "replace")
        except Exception as exc:
            raise ProviderError(f"Workshop não respondeu: {exc}") from exc
        ids = list(dict.fromkeys(re.findall(r"filedetails/\?id=(\d+)", html)))
        det = _details(ids)
        out = []
        for i in ids:
            d = det.get(i)
            if not d:
                continue
            tags = [t.get("tag", "") for t in d.get("tags", [])]
            wtype = next((t for t in tags if t in ("Video", "Scene", "Web", "Application")), "Scene")
            if wtype == "Application":
                continue
            prev = d.get("preview_url", "")
            subs = f"{int(d.get('subscriptions') or 0):,}".replace(",", ".")
            out.append(Item(
                id=i, provider=self.key, title=d.get("title") or i,
                thumb_url=(prev + "?imw=256&imh=256&ima=fit&impolicy=Letterbox&imcolor=%23000000&letterbox=true")
                if prev else "",
                media_url=prev, kind="video" if wtype == "Video" else "gif",
                size=int(d.get("file_size") or 0),
                page_url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={i}",
                credit=f"{TYPE_PT.get(wtype, wtype)} · {subs} assinaturas",
                extra={"workshop_id": i, "we_type": wtype, "tags": tags}))
        return out

    def trending(self, offset=0):
        return self._page("", offset)

    def search(self, query, offset=0):
        return self._page(query, offset)


# ----------------------------------------------------------------------
# processo auxiliar que fala com a Steam (steam_api64.dll do Wallpaper Engine)
# ----------------------------------------------------------------------
def helper_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--steam-ws", *args]
    main = Path(__file__).resolve().parent.parent / "DigooLCD.pyw"
    return [sys.executable, str(main), "--steam-ws", *args]


def _emit(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def helper_main(args: list[str]) -> int:
    """`--steam-ws subscribe <id>` | `unsubscribe <id>` | `status`. Fala JSON por linha no stdout."""
    wd = we_dir()
    dll_path = wd / "bin" / "steam_api64.dll" if wd else None
    if not dll_path or not dll_path.exists():
        _emit(error="Wallpaper Engine não encontrado nas bibliotecas da Steam")
        return 2
    os.environ["SteamAppId"] = str(APP_ID)
    os.environ["SteamGameId"] = str(APP_ID)
    dll = ctypes.CDLL(str(dll_path))
    dll.SteamAPI_InitFlat.argtypes = [ctypes.c_char_p]
    dll.SteamAPI_InitFlat.restype = ctypes.c_int
    err = ctypes.create_string_buffer(1024)

    started = False
    deadline = time.time() + 90
    while dll.SteamAPI_InitFlat(err) != 0:
        if not started and not steam_running():
            _emit(status="abrindo a Steam…")
            start_steam()
            started = True
        if time.time() > deadline:
            _emit(error=f"não consegui falar com a Steam: {err.value.decode(errors='replace')}")
            return 3
        time.sleep(3)

    dll.SteamAPI_SteamUGC_v020.restype = ctypes.c_void_p
    ugc = dll.SteamAPI_SteamUGC_v020()
    u64, vp = ctypes.c_uint64, ctypes.c_void_p

    def fn(name, argtypes, restype):
        f = getattr(dll, "SteamAPI_ISteamUGC_" + name)
        f.argtypes, f.restype = [vp, *argtypes], restype
        return f

    get_state = fn("GetItemState", [u64], ctypes.c_uint32)
    subscribe = fn("SubscribeItem", [u64], u64)
    unsubscribe = fn("UnsubscribeItem", [u64], u64)
    download = fn("DownloadItem", [u64, ctypes.c_bool], ctypes.c_bool)
    dl_info = fn("GetItemDownloadInfo", [u64, ctypes.POINTER(u64), ctypes.POINTER(u64)], ctypes.c_bool)
    inst_info = fn("GetItemInstallInfo", [u64, ctypes.POINTER(u64), ctypes.c_char_p, ctypes.c_uint32,
                                          ctypes.POINTER(ctypes.c_uint32)], ctypes.c_bool)
    run_callbacks = dll.SteamAPI_RunCallbacks

    def installed_folder(item) -> str:
        size, ts = u64(), ctypes.c_uint32()
        buf = ctypes.create_string_buffer(1024)
        if inst_info(ugc, item, ctypes.byref(size), buf, 1024, ctypes.byref(ts)):
            return buf.value.decode("utf8", "replace")
        return ""

    try:
        cmd = args[0] if args else "status"
        if cmd == "status":
            _emit(ok=True)
            return 0
        item = int(args[1])
        if cmd == "unsubscribe":
            unsubscribe(ugc, item)
            for _ in range(20):
                run_callbacks()
                time.sleep(0.25)
            _emit(done=True, state=get_state(ugc, item))
            return 0

        # assina + baixa (a Steam salva em steamapps/workshop/content/431960/<id>)
        if not get_state(ugc, item) & ST_SUBSCRIBED:
            subscribe(ugc, item)
        download(ugc, item, True)
        deadline = time.time() + 3600
        last = -1.0
        while time.time() < deadline:
            run_callbacks()
            state = get_state(ugc, item)
            done_b, total_b = u64(), u64()
            dl_info(ugc, item, ctypes.byref(done_b), ctypes.byref(total_b))
            if state & ST_INSTALLED and not state & (ST_NEEDS_UPDATE | ST_DOWNLOADING | ST_PENDING):
                folder = installed_folder(item)
                if folder:
                    _emit(done=True, folder=folder)
                    return 0
            frac = done_b.value / total_b.value if total_b.value else 0.0
            if abs(frac - last) >= 0.01:
                _emit(progress=frac, downloaded=done_b.value, total=total_b.value, state=state)
                last = frac
            time.sleep(0.3)
        _emit(error="tempo esgotado esperando a Steam baixar")
        return 4
    finally:
        dll.SteamAPI_Shutdown()


def run_helper(args: list[str], on_line) -> int:
    """Roda o auxiliar e repassa cada linha JSON para on_line(dict)."""
    p = subprocess.Popen(helper_command(*args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, text=True, encoding="utf8", creationflags=CREATE_NO_WINDOW)
    for line in p.stdout:
        line = line.strip()
        if line.startswith("{"):
            try:
                on_line(json.loads(line))
            except json.JSONDecodeError:
                pass
    return p.wait()
