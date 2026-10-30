"""Quadros prontos para a tela: vídeos e GIFs convertidos UMA vez para os JPEGs exatos que a telinha
recebe (480x480, já girados, 4:2:0, dentro do limite de tamanho, até 30 quadros/s).

Tocar depois disso é só ler do disco e mandar — sem decodificar, girar nem comprimir de novo.
Arquivo .djf: b"DJF1" + u16 largura + u16 altura + u16 rotação + u16 fps, depois
[u32 pts_ms][u32 tamanho][jpeg] por quadro.
"""

from __future__ import annotations

import bisect
import hashlib
import io
import logging
import queue
import struct
import threading
import time
from pathlib import Path

from PIL import Image

from .config import config_dir

log = logging.getLogger(__name__)

W = H = 480
MAX_FPS = 30
QUALITY = 92
MAX_FRAME_BYTES = 48 * 1024   # este modelo de tela engasga com quadros grandes (ver PanelInfo.fragile)
VERSION = "v3"
MAGIC = b"DJF1"
CACHE_DIR = config_dir() / "cache" / "video"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
MAX_CACHE_BYTES = 4 * 1024 ** 3

_jobs: queue.Queue = queue.Queue()
_pending: set[str] = set()
_lock = threading.Lock()
_worker: threading.Thread | None = None


def cache_path(src: Path, fit: str, rotation: int) -> Path | None:
    try:
        st = src.stat()
    except OSError:
        return None
    raw = f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|{fit}|{rotation % 360}|{QUALITY}|{VERSION}"
    return CACHE_DIR / f"{hashlib.sha1(raw.encode()).hexdigest()[:24]}.djf"


def get(src: Path, fit: str, rotation: int) -> Path | None:
    p = cache_path(src, fit, rotation)
    return p if p is not None and p.exists() else None


def pending(src: Path, fit: str, rotation: int) -> bool:
    p = cache_path(src, fit, rotation)
    with _lock:
        return p is not None and str(p) in _pending


def request(src: Path, fit: str, rotation: int) -> None:
    """Agenda a conversão (uma de cada vez, sem repetir)."""
    global _worker
    dst = cache_path(src, fit, rotation)
    if dst is None or dst.exists():
        return
    with _lock:
        if str(dst) in _pending:
            return
        _pending.add(str(dst))
        _jobs.put((src, fit, rotation % 360, dst))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True, name="frame-cache")
            _worker.start()


def _run():
    while True:
        # bloqueio permanente: worker daemon vivo pelo processo, sem corrida com request()
        src, fit, rot, dst = _jobs.get()
        t0 = time.time()
        try:
            n = _convert(src, fit, rot, dst)
            log.info("quadros prontos: %s (%d quadros, %.0f s, %.1f MB)", src.name, n, time.time() - t0,
                     dst.stat().st_size / 1048576)
            _prune()
        except Exception as exc:
            log.warning("não deu para preparar %s: %s", src.name, exc)
        finally:
            with _lock:
                _pending.discard(str(dst))


def _fit(frame, fit: str) -> Image.Image:
    sw, sh = frame.width, frame.height
    k = min(W / sw, H / sh) if fit == "contain" else max(W / sw, H / sh)
    nw, nh = max(2, round(sw * k)), max(2, round(sh * k))
    # AREA = média dos pixels de origem: a redução mais nítida e sem serrilhado
    img = frame.reformat(width=nw, height=nh, format="rgb24", interpolation="AREA").to_image()
    if fit == "contain":
        canvas = Image.new("RGB", (W, H), (0, 0, 0))
        canvas.paste(img, ((W - nw) // 2, (H - nh) // 2))
        return canvas
    x, y = (nw - W) // 2, (nh - H) // 2
    return img.crop((x, y, x + W, y + H))


def encode(img: Image.Image) -> bytes:
    q = QUALITY
    while True:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, subsampling=2)  # 4:2:0: o único que a tela mostra limpo
        data = buf.getvalue()
        if len(data) <= MAX_FRAME_BYTES or q <= 40:
            return data
        q -= 5


def _convert(src: Path, fit: str, rot: int, dst: Path) -> int:
    import av

    tmp = dst.with_suffix(".part")
    n = 0
    with av.open(str(src)) as inp, open(tmp, "wb") as out:
        ist = inp.streams.video[0]
        ist.thread_type = "AUTO"
        ist.codec_context.thread_count = 2  # não ocupar o processador todo enquanto converte
        rate = float(ist.average_rate or 0) or 30.0
        fps = min(MAX_FPS, max(1, round(rate)))
        out.write(MAGIC + struct.pack("<HHHH", W, H, rot, fps))
        step = 1.0 / fps
        last_t = -1.0
        first = None
        for frame in inp.decode(ist):
            t = frame.time if frame.time is not None else (last_t + step if last_t >= 0 else 0.0)
            if first is None:
                first = t
            t -= first
            if last_t >= 0 and t - last_t < step * 0.9:
                continue  # 60 fps → 30 fps
            last_t = t
            img = _fit(frame, fit)
            if rot:
                img = img.rotate(rot, expand=True)
            data = encode(img)
            out.write(struct.pack("<II", int(t * 1000), len(data)))
            out.write(data)
            n += 1
            time.sleep(0.001)  # cede a CPU para o resto do PC
    if n == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("nenhum quadro")
    tmp.replace(dst)
    return n


class FrameFile:
    """Lê um .djf: índice na memória (12 bytes por quadro), JPEGs lidos do disco sob demanda."""

    def __init__(self, path: Path):
        self.path = path
        self.f = open(path, "rb")
        head = self.f.read(12)
        if head[:4] != MAGIC:
            raise ValueError("arquivo de quadros inválido")
        self.w, self.h, self.rot, self.fps = struct.unpack("<HHHH", head[4:])
        self.pts: list[int] = []
        self.off: list[int] = []
        self.size: list[int] = []
        pos = 12
        while True:
            hdr = self.f.read(8)
            if len(hdr) < 8:
                break
            pts, size = struct.unpack("<II", hdr)
            pos += 8
            self.pts.append(pts)
            self.off.append(pos)
            self.size.append(size)
            pos += size
            self.f.seek(pos)
        if not self.pts:
            raise ValueError("arquivo de quadros vazio")
        self.duration = (self.pts[-1] + 1000 / max(1, self.fps)) / 1000

    def index_at(self, t: float) -> int:
        return max(0, bisect.bisect_right(self.pts, int(t * 1000)) - 1)

    def read(self, i: int) -> bytes:
        self.f.seek(self.off[i])
        return self.f.read(self.size[i])

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def _prune() -> None:
    files = sorted(list(CACHE_DIR.glob("*.djf")) + list(CACHE_DIR.glob("*.mkv")),
                   key=lambda p: p.stat().st_atime, reverse=True)
    total = 0
    for f in files:
        total += f.stat().st_size
        if total > MAX_CACHE_BYTES or f.suffix == ".mkv":  # .mkv = formato antigo (v1)
            try:
                f.unlink()
            except OSError:
                pass


def clear() -> int:
    n = 0
    for f in list(CACHE_DIR.glob("*.djf")) + list(CACHE_DIR.glob("*.mkv")) + list(CACHE_DIR.glob("*.part*")):
        try:
            n += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    return n


def size_bytes() -> int:
    return sum(f.stat().st_size for f in list(CACHE_DIR.glob("*.djf")) + list(CACHE_DIR.glob("*.mkv")))
