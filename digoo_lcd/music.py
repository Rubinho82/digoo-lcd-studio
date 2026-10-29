"""Música: espectro do som do PC + "tocando agora" (Spotify, YouTube…), e os desenhos disso na tela."""

from __future__ import annotations

import colorsys
import math
import time
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W = H = 480
TOAST_SECONDS = 8.0

LAYOUTS = {
    "capa": "Capa central",
    "vinil": "Disco de vinil",
    "cartao": "Cartão",
    "minimal": "Minimalista",
    "espectro": "Espectro gigante",
    "neon": "Onda neon",
}
WAVES = {"circulo": "Círculo", "barras": "Barras", "onda": "Onda", "espelho": "Barras espelhadas", "off": "Desligado"}


class AudioViz:
    """Suaviza as bandas que chegam do leitor (~30x/s): sobem rápido, descem devagar, com "tampinhas"."""

    def __init__(self, n: int = 48):
        self.n = n
        self.bands = [0.0] * n
        self.caps = [0.0] * n
        self.bass = 0.0
        self.level = 0.0
        self._last = time.monotonic()
        self.active = False

    def update(self, raw: tuple | None) -> None:
        now = time.monotonic()
        dt = min(0.2, now - self._last)
        self._last = now
        fresh = raw is not None and now - raw[0] < 0.4
        target = [v / 100 for v in raw[1]] if fresh else [0.0] * self.n
        if len(target) != self.n:
            target = (target + [0.0] * self.n)[: self.n]
        up, down = 1 - math.exp(-dt / 0.03), 1 - math.exp(-dt / 0.18)
        for i, t in enumerate(target):
            b = self.bands[i]
            b += (t - b) * (up if t > b else down)
            self.bands[i] = b
            self.caps[i] = b if b > self.caps[i] else max(0.0, self.caps[i] - dt * 0.45)
        bass_t = sum(target[:6]) / 6 if fresh else 0.0
        self.bass += (bass_t - self.bass) * (up if bass_t > self.bass else down)
        lv = (raw[2] / 100) if fresh else 0.0
        self.level += (lv - self.level) * (up if lv > self.level else down)
        self.active = fresh or max(self.bands) > 0.02


class MediaState:
    """Detecta troca de música (para o cartão aparecer por alguns segundos)."""

    def __init__(self):
        self.key = None
        self.changed_at = -1e9

    def update(self, media: dict | None) -> None:
        key = (media.get("title"), media.get("artist")) if media else None
        if key != self.key:
            if key and key[0]:
                self.changed_at = time.monotonic()
            self.key = key

    def toast_visible(self) -> bool:
        return time.monotonic() - self.changed_at < TOAST_SECONDS

    def toast_progress(self) -> float:
        """0→1 entrando, 1 parado, 1→0 saindo (animação do cartão)."""
        t = time.monotonic() - self.changed_at
        if t < 0.35:
            return t / 0.35
        if t > TOAST_SECONDS - 0.5:
            return max(0.0, (TOAST_SECONDS - t) / 0.5)
        return 1.0


def app_name(app_id: str) -> str:
    a = (app_id or "").lower()
    for k, v in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"),
                 ("opera", "Opera"), ("brave", "Brave"), ("vlc", "VLC"), ("zunemusic", "Media Player"),
                 ("deezer", "Deezer"), ("tidal", "TIDAL"), ("youtube", "YouTube Music"), ("discord", "Discord")):
        if k in a:
            return v
    return app_id.split("!")[-1].split("\\")[-1].replace(".exe", "") if app_id else ""


# ----------------------------------------------------------------------
# capa, cores e utilidades
# ----------------------------------------------------------------------
@lru_cache(maxsize=8)
def _cover(path: str, size: int) -> Image.Image | None:
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img).convert("RGB")
        return ImageOps.fit(img, (size, size), Image.LANCZOS)  # miniatura 16:9 do YouTube → quadrado do meio
    except Exception:
        return None


@lru_cache(maxsize=4)
def _cover_bg(path: str) -> Image.Image | None:
    c = _cover(path, 240)
    if c is None:
        return None
    bg = c.resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(28))
    return Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.55)


@lru_cache(maxsize=8)
def _cover_palette(path: str) -> tuple | None:
    """Duas cores vivas e diferentes tiradas da capa (para as "cores automáticas")."""
    c = _cover(path, 64)
    if c is None:
        return None
    q = c.quantize(12, method=Image.Quantize.MEDIANCUT).convert("RGB")
    counts = sorted(q.getcolors(64 * 64) or [], reverse=True)
    scored = []
    for n, rgb in counts:
        h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in rgb))
        scored.append((s * v * (n ** 0.35), h, rgb, s, v))
    scored.sort(reverse=True)
    picks = []
    for _, h, rgb, s, v in scored:
        if s < 0.25 or v < 0.3:
            continue
        if all(min(abs(h - ph), 1 - abs(h - ph)) > 0.08 for ph, _ in picks):
            # clareia para ficar legível em fundo escuro
            r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * 1.1), max(0.85, v))
            picks.append((h, (int(r * 255), int(g * 255), int(b * 255))))
        if len(picks) == 2:
            break
    if not picks:
        return None
    if len(picks) == 1:
        h = (picks[0][0] + 0.45) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.7, 1.0)
        picks.append((h, (int(r * 255), int(g * 255), int(b * 255))))
    return picks[0][1], picks[1][1]


@lru_cache(maxsize=8)
def _round_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size * 2 - 1, size * 2 - 1], radius * 2, fill=255)
    return m.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=8)
def _circle_mask(size: int) -> Image.Image:
    m = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(m).ellipse([0, 0, size * 2 - 1, size * 2 - 1], fill=255)
    return m.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=4)
def _symbol_font(size: int):
    try:
        return ImageFont.truetype(r"C:\Windows\Fonts\seguisym.ttf", size)  # tem a nota musical
    except OSError:
        return ImageFont.load_default()


def cover_image(media: dict | None, size: int) -> Image.Image | None:
    p = (media or {}).get("cover") or ""
    return _cover(p, size) if p and Path(p).exists() else None


def fmt_time(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def _grad(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _ellipsize(d, text: str, font, max_w: float) -> str:
    if d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _opt(cfg, name, default):
    return getattr(cfg, name, default)


def apply_palette(c, media: dict | None) -> None:
    """Se "cores automáticas" estiver ligado, troca as cores de destaque pelas da capa."""
    if not _opt(c.cfg, "music_auto_colors", False) or not media:
        return
    p = media.get("cover") or ""
    pal = _cover_palette(p) if p and Path(p).exists() else None
    if pal:
        c.acc, c.acc2 = pal


def _gain_bands(c, viz: AudioViz):
    g = max(0.3, min(3.0, float(_opt(c.cfg, "music_gain", 1.0))))
    return [min(1.0, b * g) for b in viz.bands], [min(1.0, b * g) for b in viz.caps]


# ----------------------------------------------------------------------
# espectro (qualquer estilo)
# ----------------------------------------------------------------------
def draw_ring(c, viz: AudioViz, cx: float, cy: float, r: float, length: float = 52, width: int = 6, alpha=235):
    """Espectro em círculo, espelhado (graves embaixo dos dois lados)."""
    bands, _ = _gain_bands(c, viz)
    n = viz.n
    d = c._layer_draw()
    for i in range(n * 2):
        k = i if i < n else 2 * n - 1 - i
        ang = math.pi / 2 + (i + 0.5) * (2 * math.pi / (n * 2))
        r1, r2 = r + 2, r + 4 + length * bands[k]
        col = _grad(c.acc, c.acc2, k / (n - 1)) + (alpha,)
        d.line([((cx + r1 * math.cos(ang)) * 2, (cy + r1 * math.sin(ang)) * 2),
                ((cx + r2 * math.cos(ang)) * 2, (cy + r2 * math.sin(ang)) * 2)], fill=col, width=width)
    c.flush()


def draw_bars(c, viz: AudioViz, top: int = 330, bottom: int = 476, style: str = "barras", alpha: int = 235,
              x0: int = 8, x1: int = W - 8, glow: bool = False):
    """Espectro numa faixa horizontal; style: barras | onda | espelho."""
    n = viz.n
    bands, caps = _gain_bands(c, viz)
    if style == "onda":
        pts = [(x0 + (x1 - x0) * i / (n - 1), bottom - (bottom - top) * bands[i]) for i in range(n)]
        if glow:
            c.line_aa(pts, c.acc, 14, 50)
            c.line_aa(pts, c.acc, 8, 90)
        else:
            c.poly_aa(pts + [(x1, bottom), (x0, bottom)], c.acc, 70)
        c.line_aa(pts, c.acc if not glow else (255, 255, 255), 4 if not glow else 3, alpha)
        c.line_aa([(x, bottom - (bottom - y) * 0.6) for x, y in pts], c.acc2, 2, max(0, alpha - 60))
        c.flush()
        return
    gap = 3
    bw = (x1 - x0 - gap * (n - 1)) / n
    espelho = style == "espelho"
    mid = (top + bottom) / 2 if espelho else bottom
    span = (bottom - top) / (2 if espelho else 1)
    for i in range(n):
        bx = x0 + i * (bw + gap)
        hgt = max(2.0, span * bands[i])
        col = _grad(c.acc, c.acc2, i / (n - 1)) + (alpha,)
        if espelho:
            c.d.rounded_rectangle([bx, mid - hgt, bx + bw, mid + hgt], radius=min(3, bw / 2), fill=col)
        else:
            c.d.rounded_rectangle([bx, bottom - hgt, bx + bw, bottom], radius=min(3, bw / 2), fill=col)
            cap_y = bottom - span * caps[i] - 4
            c.d.rectangle([bx, cap_y, bx + bw, cap_y + 2], fill=(255, 255, 255, 200))


def draw_spectrum(c, viz: AudioViz | None, style: str, band: tuple, ring: tuple | None = None):
    """Desenha no estilo escolhido: band=(top,bottom[,x0,x1]) para barras/onda; ring=(cx,cy,r,len) para círculo."""
    if viz is None or style == "off":
        return
    if style == "circulo":
        if ring is None:
            ring = (240, 240, 150, 70)
        draw_ring(c, viz, *ring)
        return
    top, bottom, *xs = band
    draw_bars(c, viz, top, bottom, style, 235, *(xs or []))


# ----------------------------------------------------------------------
# cartão "tocando agora" (por cima de outros temas/vídeos)
# ----------------------------------------------------------------------
def draw_card(c, media: dict, y_anim: float = 1.0):
    if not media or not media.get("title"):
        return
    apply_palette(c, media)
    h = 118
    y0 = int(H - 12 - h + (1 - y_anim) * (h + 20))
    op = max(0, min(100, int(_opt(c.cfg, "music_bg_opacity", 60)))) / 100
    if op > 0:
        c.d.rounded_rectangle([12, y0, W - 12, y0 + h], 20, fill=(8, 10, 18, int(240 * op)))
    sk = {"stroke_width": 2, "stroke_fill": (0, 0, 0, 220)} if op < 0.45 else {}
    cov = cover_image(media, 94) if _opt(c.cfg, "music_show_cover", True) else None
    x_text = 26
    if cov is not None:
        c.img.paste(cov, (24, y0 + 12), _round_mask(94, 14))
        c.d = ImageDraw.Draw(c.img, "RGBA")
        x_text = 132
    fw, fa, fs = c.f(30, 700), c.f(22, 600), c.f(16, 600)
    title = _ellipsize(c.d, media.get("title", ""), fw, W - x_text - 26)
    artist = _ellipsize(c.d, media.get("artist", "") or media.get("album", ""), fa, W - x_text - 26)
    c.d.text((x_text, y0 + 30), title, font=fw, fill=c.txt + (255,), anchor="lm", **sk)
    c.d.text((x_text, y0 + 60), artist, font=fa, fill=c.muted + (255,), anchor="lm", **sk)
    dur = float(media.get("duration") or 0)
    pos = float(media.get("position") or 0)
    bx0, bx1, by = x_text, W - 26, y0 + 88
    if _opt(c.cfg, "music_show_progress", True):
        c.bar([bx0, by, bx1, by + 6], (pos / dur) if dur > 0 else None, c.acc)
        if dur > 0:
            c.d.text((bx0, by + 18), f"{fmt_time(pos)} / {fmt_time(dur)}", font=fs, fill=c.muted + (255,),
                     anchor="lm", **sk)
    if _opt(c.cfg, "music_show_app", True):
        status = "tocando" if media.get("status") == "Playing" else "pausado"
        app = app_name(media.get("app", ""))
        c.d.text((bx1, by + 18), f"{app} · {status}" if app else status, font=fs, fill=c.muted + (255,),
                 anchor="rm", **sk)


def draw_pill(c, media: dict, x: int = 10, y: int = 10, w: int = 300):
    """Música compacta: capa redonda pequena + título e artista numa pílula."""
    if not media or not media.get("title"):
        return
    apply_palette(c, media)
    op = max(0, min(100, int(_opt(c.cfg, "music_bg_opacity", 60)))) / 100
    if op > 0:
        c.d.rounded_rectangle([x, y, x + w, y + 58], 29, fill=(8, 10, 18, int(240 * op)))
    sk = {"stroke_width": 2, "stroke_fill": (0, 0, 0, 220)} if op < 0.45 else {}
    cov = cover_image(media, 46) if _opt(c.cfg, "music_show_cover", True) else None
    tx = x + 20
    if cov is not None:
        c.img.paste(cov, (x + 6, y + 6), _circle_mask(46))
        c.d = ImageDraw.Draw(c.img, "RGBA")
        tx = x + 62
    f1, f2 = c.f(22, 700), c.f(16, 600)
    c.d.text((tx, y + 20), _ellipsize(c.d, media.get("title", ""), f1, w - (tx - x) - 16), font=f1,
             fill=c.txt + (255,), anchor="lm", **sk)
    sub = media.get("artist", "") or media.get("album", "")
    if _opt(c.cfg, "music_show_progress", True) and float(media.get("duration") or 0) > 0:
        sub = f"{sub} · {fmt_time(media.get('position') or 0)}"
    c.d.text((tx, y + 41), _ellipsize(c.d, sub, f2, w - (tx - x) - 16), font=f2, fill=c.muted + (255,),
             anchor="lm", **sk)


# ----------------------------------------------------------------------
# tema "Música" e seus modelos
# ----------------------------------------------------------------------
class _Ctx:
    """O que todos os modelos usam: textos com contorno quando o fundo é transparente, opções etc."""

    def __init__(self, c):
        self.c = c
        self.cfg = c.cfg
        self.media = c.m.get("media") or {}
        self.viz: AudioViz | None = c.m.get("audio")
        self.has = bool(self.media.get("title"))
        self.op = max(0, min(100, int(_opt(c.cfg, "music_bg_opacity", 60)))) / 100
        self.sk = {"stroke_width": 2, "stroke_fill": (0, 0, 0, 220)} if self.op < 0.45 else {}
        self.wave = _opt(c.cfg, "music_wave", "circulo")
        self.show_cover = _opt(c.cfg, "music_show_cover", True)
        self.show_progress = _opt(c.cfg, "music_show_progress", True)
        self.show_app = _opt(c.cfg, "music_show_app", True)
        self.show_clock = _opt(c.cfg, "music_show_clock", False)

    def T(self, xy, text, size, color, weight=700, anchor="mm", max_w=None):
        c = self.c
        f = c.f(size, weight)
        if max_w:
            text = _ellipsize(c.d, text, f, max_w)
        c.d.text(xy, text, font=f, fill=color + (255,), anchor=anchor, **self.sk)

    def title(self):
        return self.media.get("title", "") if self.has else "Nada tocando"

    def artist(self):
        if self.has:
            return self.media.get("artist", "") or self.media.get("album", "")
        return "Spotify, YouTube e outros players aparecem aqui"

    def progress(self, x0, x1, y, times=True):
        if not (self.has and self.show_progress):
            return
        c = self.c
        dur = float(self.media.get("duration") or 0)
        pos = float(self.media.get("position") or 0)
        if dur <= 0:
            return
        c.bar([x0, y, x1, y + 6], pos / dur, c.acc)
        if times:
            self.T((x0, y + 24), fmt_time(pos), 18, c.muted, 600, anchor="lm")
            self.T((x1, y + 24), fmt_time(dur), 18, c.muted, 600, anchor="rm")

    def app_line(self, xy, anchor="mm", size=18):
        if not (self.has and self.show_app):
            return
        app = app_name(self.media.get("app", ""))
        status = "tocando" if self.media.get("status") == "Playing" else "pausado"
        self.T(xy, f"{app} · {status}" if app else status, size, self.c.acc, 700, anchor=anchor)

    def clock(self, xy, anchor="mm", size=24):
        now = self.c.m.get("now")
        if self.show_clock and now:
            self.T(xy, now.strftime("%H:%M") if self.cfg.clock_24h else now.strftime("%I:%M"), size, self.c.txt,
                   700, anchor=anchor)

    def cover(self, size):
        return cover_image(self.media, size) if (self.has and self.show_cover) else None

    def note(self, cx, cy, r):
        c = self.c
        c.d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 18), outline=c.acc + (160,), width=3)
        c.d.text((cx, cy), "\u266b", font=_symbol_font(int(r * 1.1)), fill=c.acc + (255,), anchor="mm")


def _layout_capa(x: _Ctx):
    c = x.c
    cx, cy, R = 240, 172 if not x.show_clock else 182, 98
    draw_spectrum(c, x.viz, x.wave, (300, 330), (cx, cy, R + 8, 52))
    cov = x.cover(R * 2)
    if cov is not None:
        size = int(R * 2 * (1.0 + (x.viz.bass * 0.05 if x.viz else 0)))  # capa "respira" com o grave
        cv = cov.resize((size, size), Image.BILINEAR) if size != R * 2 else cov
        c.img.paste(cv, (cx - size // 2, cy - size // 2), _round_mask(size, 26))
        c.d = ImageDraw.Draw(c.img, "RGBA")
    else:
        x.note(cx, cy, R)
    x.clock((240, 30))
    x.T((240, 362), x.title(), 34, c.txt, 700, max_w=W - 40)
    x.T((240, 396), x.artist(), 24 if x.has else 18, c.muted, 600, max_w=W - 40)
    x.progress(40, W - 40, 426)
    x.app_line((240, 450))


_vinyl_angle = [0.0, time.monotonic()]


def _layout_vinil(x: _Ctx):
    c = x.c
    cx, cy, R = 240, 190, 128
    now = time.monotonic()
    if x.has and x.media.get("status") == "Playing":  # 33⅓ rpm de verdade = 200°/s
        _vinyl_angle[0] = (_vinyl_angle[0] + (now - _vinyl_angle[1]) * 200) % 360
    _vinyl_angle[1] = now
    draw_spectrum(c, x.viz, x.wave, (330, 360), (cx, cy, R + 6, 44))
    d = c._layer_draw()
    d.ellipse([(cx - R) * 2, (cy - R) * 2, (cx + R) * 2, (cy + R) * 2], fill=(12, 12, 14, 255))
    for rr in range(R - 6, 56, -7):  # sulcos
        d.ellipse([(cx - rr) * 2, (cy - rr) * 2, (cx + rr) * 2, (cy + rr) * 2], outline=(40, 40, 46, 255), width=2)
    hl = math.radians(_vinyl_angle[0] * 0.25 - 40)  # brilho que "anda" no disco
    d.arc([(cx - R + 10) * 2, (cy - R + 10) * 2, (cx + R - 10) * 2, (cy + R - 10) * 2],
          math.degrees(hl), math.degrees(hl) + 40, fill=(255, 255, 255, 40), width=18)
    c.flush()
    lab = 108
    cov = x.cover(lab)
    if cov is not None:
        rot = cov.rotate(-_vinyl_angle[0], Image.BILINEAR)
        c.img.paste(rot, (cx - lab // 2, cy - lab // 2), _circle_mask(lab))
    else:
        c.d.ellipse([cx - lab // 2, cy - lab // 2, cx + lab // 2, cy + lab // 2], fill=c.acc + (255,))
    c.d = ImageDraw.Draw(c.img, "RGBA")
    c.d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=(20, 20, 24, 255))
    # braço da agulha
    c.line_aa([(430, 40), (410, 64), (345, 205)], (200, 200, 210), 6)
    c.flush()
    c.d.ellipse([418, 28, 442, 52], fill=(170, 170, 180, 255))
    x.clock((40, 30), anchor="lm")
    x.T((240, 362), x.title(), 32, c.txt, 700, max_w=W - 40)
    x.T((240, 394), x.artist(), 22 if x.has else 18, c.muted, 600, max_w=W - 40)
    x.progress(40, W - 40, 422)
    x.app_line((240, 448))


def _layout_cartao(x: _Ctx):
    c = x.c
    S = 190
    cov = x.cover(S)
    if cov is not None:
        c.img.paste(cov, (24, 36), _round_mask(S, 18))
        c.d = ImageDraw.Draw(c.img, "RGBA")
    else:
        x.note(24 + S // 2, 36 + S // 2, S // 2)
    tx = 24 + S + 18
    tw = W - tx - 20
    # título em até 2 linhas
    words, lines, cur = x.title().split(), [], ""
    f = c.f(34, 700)
    for wd in words:
        trial = (cur + " " + wd).strip()
        if c.d.textlength(trial, font=f) <= tw or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    lines.append(cur)
    if len(lines) > 2:
        lines = [lines[0], _ellipsize(c.d, " ".join(lines[1:]), f, tw)]
    y = 66
    for ln in lines:
        x.T((tx, y), ln, 34, c.txt, 700, anchor="lm", max_w=tw)
        y += 38
    x.T((tx, y + 6), x.artist(), 22 if x.has else 16, c.muted, 600, anchor="lm", max_w=tw)
    x.app_line((tx, y + 44), anchor="lm", size=17)
    x.clock((tx, 200), anchor="lm", size=26)
    x.progress(24, W - 24, 252)
    draw_spectrum(c, x.viz, x.wave, (300, 470), (240, 385, 52, 34))


def _layout_minimal(x: _Ctx):
    c = x.c
    x.clock((240, 40), size=30)
    f = c.f(46, 700)
    words, lines, cur = x.title().split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if c.d.textlength(trial, font=f) <= W - 50 or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    lines.append(cur)
    lines = lines[:3]
    y0 = 190 - (len(lines) - 1) * 26
    for i, ln in enumerate(lines):
        x.T((240, y0 + i * 52), ln, 46, c.txt, 700, max_w=W - 40)
    x.T((240, y0 + len(lines) * 52 + 6), x.artist(), 24 if x.has else 18, c.acc, 600, max_w=W - 40)
    x.progress(60, W - 60, 330, times=False)
    draw_spectrum(c, x.viz, "onda" if x.wave == "circulo" else x.wave, (370, 466), None)


def _layout_espectro(x: _Ctx):
    c = x.c
    cov = x.cover(84)
    tx = 20
    if cov is not None:
        c.img.paste(cov, (20, 20), _round_mask(84, 12))
        c.d = ImageDraw.Draw(c.img, "RGBA")
        tx = 120
    x.T((tx, 44), x.title(), 28, c.txt, 700, anchor="lm", max_w=W - tx - 20)
    x.T((tx, 76), x.artist(), 20 if x.has else 15, c.muted, 600, anchor="lm", max_w=W - tx - 20)
    x.clock((W - 20, 120), anchor="rm", size=22)
    draw_spectrum(c, x.viz, x.wave, (130, 440), (240, 290, 70, 90))
    x.progress(20, W - 20, 452, times=False)


def _layout_neon(x: _Ctx):
    c = x.c
    if x.has:
        c.glow_text((240, 70), _ellipsize(c.d, x.title(), c.f(40, 700), W - 40), 40, c.acc, 700, radius=8,
                    strength=1)
    else:
        x.T((240, 70), x.title(), 36, c.acc, 700)
    x.T((240, 112), x.artist(), 22 if x.has else 16, c.txt, 600, max_w=W - 40)
    if x.viz is not None and x.wave != "off":
        if x.wave == "circulo":
            draw_ring(c, x.viz, 240, 260, 70, 70)
        else:
            draw_bars(c, x.viz, 170, 350, x.wave, 235, 8, W - 8, glow=True)
    x.clock((240, 400), size=28)
    x.progress(40, W - 40, 430)


LAYOUT_FUNCS = {
    "capa": _layout_capa, "vinil": _layout_vinil, "cartao": _layout_cartao,
    "minimal": _layout_minimal, "espectro": _layout_espectro, "neon": _layout_neon,
}


def theme_musica(c):
    """Tema "Música": o modelo escolhido na aba Música (capa, vinil, cartão…)."""
    x = _Ctx(c)
    apply_palette(c, x.media)
    cover_path = x.media.get("cover") or ""
    bg = _cover_bg(cover_path) if x.has and cover_path and Path(cover_path).exists() else None
    if bg is not None and x.op > 0:
        # 0% = transparente (o vídeo/foto atrás aparece inteiro), 100% = capa desfocada cobrindo tudo
        c.img.paste(Image.blend(c.img.convert("RGB"), bg, x.op), (0, 0))
        c.d = ImageDraw.Draw(c.img, "RGBA")
    LAYOUT_FUNCS.get(_opt(c.cfg, "music_layout", "capa"), _layout_capa)(x)
