"""Fontes de imagem: playlist de mídia (foto/GIF/vídeo) e espelho de tela."""

from __future__ import annotations

import io
import logging
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

log = logging.getLogger(__name__)

W = H = 480
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".ogv"}
MEDIA_EXT = IMAGE_EXT | GIF_EXT | VIDEO_EXT


def fit_image(img: Image.Image, mode: str, resample=Image.BILINEAR) -> Image.Image:
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (0, 0, 0))
        bg.paste(rgba, (0, 0), rgba)  # transparência (stickers) vira fundo preto
        img = bg
    else:
        img = img.convert("RGB")
    if img.size == (W, H):
        return img
    if mode == "contain":
        return ImageOps.pad(img, (W, H), resample, color=(0, 0, 0))
    return ImageOps.fit(img, (W, H), resample)


def expand_paths(paths: list[str]) -> list[str]:
    """Abre pastas e descarta o que não existe. Efeitos ("efeito:x") passam direto."""
    from .effects import is_effect

    out: list[str] = []
    for p in paths:
        if is_effect(p):
            out.append(p)
            continue
        path = Path(p)
        if path.is_dir():
            out += [str(f) for f in sorted(path.iterdir()) if f.suffix.lower() in MEDIA_EXT]
        elif path.exists() and path.suffix.lower() in MEDIA_EXT:
            out.append(str(path))
    return out


# ----------------------------------------------------------------------
class _Still:
    def __init__(self, path: Path, fit: str):
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        self.frame = fit_image(img, fit, Image.LANCZOS)

    def frame_at(self, t: float) -> Image.Image:
        return self.frame

    def finished(self, t: float, interval: float) -> bool:
        return t >= interval

    def close(self):
        pass


class _Gif:
    """GIF curto: quadros já no tamanho da tela, guardados comprimidos (~40 KB cada, não 675 KB)."""

    MAX_FRAMES = 240  # GIFs maiores vão pelo decodificador de vídeo (e ganham cópia leve em cache)

    def __init__(self, path: Path, fit: str, speed: float = 1.0):
        self.speed = speed
        img = Image.open(path)
        self.frames: list[bytes] = []
        self.times: list[float] = []
        total = 0.0
        for i, fr in enumerate(ImageSequence.Iterator(img)):
            if i >= self.MAX_FRAMES:
                break
            dur = max(20, int(fr.info.get("duration", 80) or 80)) / 1000
            buf = io.BytesIO()
            fit_image(fr.convert("RGBA"), fit, Image.LANCZOS).save(buf, "JPEG", quality=95, subsampling=0)
            self.frames.append(buf.getvalue())
            total += dur
            self.times.append(total)
        self.total = total or 1.0
        self._idx = -1
        self._img: Image.Image | None = None

    def frame_at(self, t: float) -> Image.Image:
        t = (t * self.speed) % self.total
        idx = len(self.frames) - 1
        for i, end in enumerate(self.times):
            if t < end:
                idx = i
                break
        if idx != self._idx:
            img = Image.open(io.BytesIO(self.frames[idx]))
            img.load()
            self._img, self._idx = img, idx
        return self._img

    def finished(self, t: float, interval: float) -> bool:
        # GIF curto repete até cobrir o intervalo; GIF longo toca uma vez inteiro.
        return t >= max(interval, self.total / self.speed)

    def close(self):
        pass


class _Video:
    """Decodifica com PyAV numa thread, no ritmo do próprio vídeo."""

    def __init__(self, path: Path, fit: str, loop: bool, speed: float = 1.0):
        self.speed = speed
        self.path = path
        self.fit = fit
        self.loop = loop
        self.latest: Image.Image | None = None
        self._raw = None      # último quadro decodificado; só é redimensionado quando a tela pede
        self.done = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="video-decoder")
        self._thread.start()

    def _scale(self, frame) -> Image.Image:
        sw, sh = frame.width, frame.height
        if self.fit == "contain":
            k = min(W / sw, H / sh)
        else:
            k = max(W / sw, H / sh)
        nw, nh = max(2, int(round(sw * k))), max(2, int(round(sh * k)))
        img = frame.reformat(width=nw, height=nh, format="rgb24").to_image()
        if self.fit == "contain":
            canvas = Image.new("RGB", (W, H), (0, 0, 0))
            canvas.paste(img, ((W - nw) // 2, (H - nh) // 2))
            return canvas
        x, y = (nw - W) // 2, (nh - H) // 2
        return img.crop((x, y, x + W, y + H))

    def _run(self):
        import av

        while not self._stop.is_set():
            try:
                with av.open(str(self.path)) as container:
                    stream = container.streams.video[0]
                    stream.thread_type = "AUTO"
                    tb = float(stream.time_base or 0) or (1 / 30)
                    start = time.monotonic()
                    first_pts = None
                    for frame in container.decode(stream):
                        if self._stop.is_set():
                            return
                        pts = frame.pts if frame.pts is not None else 0
                        if first_pts is None:
                            first_pts = pts
                        due = (pts - first_pts) * tb / self.speed
                        delay = due - (time.monotonic() - start)
                        if delay < -0.25:
                            continue  # atrasado: pula quadro
                        if delay > 0:
                            self._stop.wait(min(delay, 1.0))
                        self._raw = frame
            except Exception as exc:
                log.warning("vídeo %s: %s", self.path.name, exc)
                self.done = True
                return
            if not self.loop:
                self.done = True
                return

    def frame_at(self, t: float) -> Image.Image | None:
        raw = self._raw
        if raw is not None:
            self._raw = None
            try:
                self.latest = self._scale(raw)
            except Exception as exc:
                log.warning("vídeo %s: %s", self.path.name, exc)
        return self.latest

    def finished(self, t: float, interval: float) -> bool:
        return self.done

    def close(self):
        self._stop.set()


class Encoded:
    """Quadro já no formato da tela (JPEG girado). Só vira imagem se alguém precisar (prévia/sobreposição)."""

    __slots__ = ("data", "rot", "key", "_img")

    def __init__(self, data: bytes, rot: int, key):
        self.data, self.rot, self.key = data, rot, key
        self._img: Image.Image | None = None

    @property
    def image(self) -> Image.Image:
        if self._img is None:
            img = Image.open(io.BytesIO(self.data))
            img.load()
            self._img = img.rotate(-self.rot, expand=True) if self.rot else img
        return self._img


class _Frames:
    """Toca um .djf: a cada quadro só lê ~40 KB do disco."""

    def __init__(self, path: Path, speed: float, is_gif: bool):
        from .videocache import FrameFile

        self.ff = FrameFile(path)
        self.speed = speed
        self.is_gif = is_gif
        self._idx = -1
        self._enc: Encoded | None = None
        self._uid = id(self)

    def frame_at(self, t: float) -> Encoded:
        i = self.ff.index_at((t * self.speed) % self.ff.duration)
        if i != self._idx:
            self._enc = Encoded(self.ff.read(i), self.ff.rot, (self._uid, i))
            self._idx = i
        return self._enc

    def finished(self, t: float, interval: float) -> bool:
        play = self.ff.duration / self.speed
        return t >= (max(interval, play) if self.is_gif else play)

    def close(self):
        self.ff.close()


AUDIO_BASS = [0.0]  # grave atual (0..1); o motor atualiza quando "efeitos reagem ao som" está ligado


class _EffectSrc:
    def __init__(self, key: str, colors):
        from .effects import make

        self.fx = make(key, colors)
        self._t_last = None
        self._t_fx = 0.0

    def frame_at(self, t: float) -> Image.Image:
        # o tempo do efeito corre mais rápido quando o grave bate
        dt = 0.0 if self._t_last is None else max(0.0, t - self._t_last)
        self._t_last = t
        self._t_fx += dt * (1.0 + 2.5 * AUDIO_BASS[0])
        return self.fx.frame(self._t_fx)

    def finished(self, t: float, interval: float) -> bool:
        return t >= interval

    def close(self):
        pass


class MediaPlayer:
    def __init__(self):
        self.items: list[str] = []
        self.colors = None
        self.speed = 1.0
        self.rotation = 0
        self._live: tuple | None = None   # (caminho, é_gif) tocando ao vivo, esperando os quadros prontos
        self._next_check = 0.0
        self.interval = 15.0
        self.fit = "cover"
        self._key = None
        self._idx = -1
        self._cur = None
        self._t0 = 0.0
        self._last: Image.Image | None = None
        self.current_name = ""

    def configure(self, paths: list[str], interval: float, fit: str, colors=None, speed: float = 1.0,
                  rotation: int = 0) -> None:
        ckey = tuple(vars(colors).values()) if colors is not None else None
        key = (tuple(paths), float(interval), fit, ckey, float(speed), int(rotation) % 360)
        if key == self._key:
            return
        self._key = key
        self.items = expand_paths(paths)
        self.interval = max(1.0, float(interval))
        self.fit = fit
        self.colors = colors
        self.speed = max(0.1, min(4.0, float(speed)))
        self.rotation = int(rotation) % 360
        self._close_cur()
        self._idx = -1

    def _close_cur(self):
        if self._cur is not None:
            self._cur.close()
        self._cur = None
        self._live = None

    def _open(self, item: str):
        from .config import Colors
        from .effects import is_effect

        if is_effect(item):
            return _EffectSrc(item, self.colors or Colors())
        from . import videocache

        path = Path(item)
        ext = path.suffix.lower()
        loop = len(self.items) == 1
        is_video = ext in VIDEO_EXT
        frames = 0
        if not is_video:
            img = Image.open(path)
            frames = getattr(img, "n_frames", 1)
            if frames <= 1:
                return _Still(path, self.fit)
        # animado: quadros prontos se já existem; senão toca ao vivo e prepara em segundo plano
        ready = videocache.get(path, self.fit, self.rotation)
        if ready:
            try:
                return _Frames(ready, self.speed, is_gif=not is_video)
            except Exception as exc:
                log.warning("quadros prontos ilegíveis (%s); refazendo", exc)
                ready.unlink(missing_ok=True)
        videocache.request(path, self.fit, self.rotation)
        self._live = (path, not is_video)
        if is_video or frames > _Gif.MAX_FRAMES:
            return _Video(path, self.fit, loop=loop, speed=self.speed)
        return _Gif(path, self.fit, self.speed)

    def _advance(self):
        self._close_cur()
        for _ in range(len(self.items)):
            self._idx = (self._idx + 1) % len(self.items)
            path = self.items[self._idx]
            try:
                self._live = None
                self._cur = self._open(path)
                self._t0 = time.monotonic()
                self.current_name = _display_name(path)
                return
            except Exception as exc:
                log.warning("não abriu %s: %s", path, exc)
        self._cur = None

    def frame(self) -> Image.Image | None:
        if not self.items:
            return None
        if self._cur is None:
            self._advance()
            if self._cur is None:
                return None
        from . import videocache

        t = time.monotonic() - self._t0
        # tocando ao vivo? assim que os quadros prontos terminarem, troca sem parar a reprodução
        if self._live and time.monotonic() >= self._next_check:
            self._next_check = time.monotonic() + 1.0
            src, is_gif = self._live
            ready = videocache.get(src, self.fit, self.rotation)
            if ready:
                try:
                    new = _Frames(ready, self.speed, is_gif)
                    self._cur.close()
                    self._cur, self._live = new, None
                except Exception as exc:
                    log.warning("quadros prontos ilegíveis: %s", exc)
        if len(self.items) > 1 and self._cur.finished(t, self.interval):
            self._advance()
            if self._cur is None:
                return self._last
            t = 0.0
        fr = self._cur.frame_at(t)
        if fr is not None:
            self._last = fr
        return self._last

    def close(self):
        self._close_cur()


def _display_name(item: str) -> str:
    from .effects import effect_label, is_effect

    return effect_label(item) if is_effect(item) else Path(item).name


# ----------------------------------------------------------------------
def list_monitors() -> list[dict]:
    import mss

    with mss.mss() as s:
        return [dict(m) for m in s.monitors[1:]]


class ScreenMirror:
    """Captura um monitor (ou região). O objeto mss é criado na thread que usa."""

    def __init__(self):
        self._mss = None
        self._owner = None

    def grab(self, monitor_index: int, region: list, fit: str) -> Image.Image | None:
        import mss

        me = threading.get_ident()
        if self._mss is None or self._owner != me:
            self._mss = mss.mss()
            self._owner = me
        mons = self._mss.monitors
        if region and len(region) == 4 and region[2] > 0 and region[3] > 0:
            box = {"left": int(region[0]), "top": int(region[1]), "width": int(region[2]), "height": int(region[3])}
        else:
            idx = monitor_index if 1 <= monitor_index < len(mons) else 1
            box = mons[idx]
        shot = self._mss.grab(box)
        img = Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return fit_image(img, fit, Image.BILINEAR)

    def close(self):
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None
