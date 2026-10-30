"""Desenho dos temas do painel (480x480) com Pillow."""

from __future__ import annotations

import math
import threading
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import Config, METRICS, resource_dir
from .textdraw import Draw

W = H = 480
SS = 2  # supersampling dos anéis/arcos

# buffer de supersampling 960x960 compartilhado: criado uma vez e reaproveitado entre quadros/temas.
# _render_lock serializa todo o desenho, então um buffer único é seguro (evita realocar ~3,6 MB por quadro).
_SHARED_LAYER = None
_SHARED_LD = None

FONT_DIR = resource_dir() / "assets" / "fonts"
WIN_FONTS = Path(r"C:\Windows\Fonts")

FONTS = {
    "Rajdhani": {"static": {400: "Rajdhani-Medium.ttf", 500: "Rajdhani-Medium.ttf",
                            600: "Rajdhani-SemiBold.ttf", 700: "Rajdhani-Bold.ttf", 900: "Rajdhani-Bold.ttf"}},
    "Orbitron": {"variable": "Orbitron.ttf"},
    "Exo 2": {"variable": "Exo2.ttf"},
    "Share Tech Mono": {"static": {w: "ShareTechMono.ttf" for w in (400, 500, 600, 700, 900)}},
    "Bahnschrift": {"variable": str(WIN_FONTS / "bahnschrift.ttf")},
    "Segoe UI": {"static": {400: str(WIN_FONTS / "segoeui.ttf"), 500: str(WIN_FONTS / "seguisb.ttf"),
                            600: str(WIN_FONTS / "seguisb.ttf"), 700: str(WIN_FONTS / "segoeuib.ttf"),
                            900: str(WIN_FONTS / "segoeuib.ttf")}},
}

THEMES = {
    "aneis": "Anéis",
    "neon": "Neon",
    "grade": "Grade 6",
    "velocimetro": "Velocímetro",
    "graficos": "Gráficos",
    "minimal": "Minimalista",
    "relogio": "Relógio",
    "compacto": "Compacto (barra)",
    "musica": "Música (tocando agora)",
}

WEEKDAYS = ["SEGUNDA", "TERÇA", "QUARTA", "QUINTA", "SEXTA", "SÁBADO", "DOMINGO"]
MONTHS = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]


# ----------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------
@lru_cache(maxsize=256)
def font(family: str, size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    spec = FONTS.get(family) or FONTS["Rajdhani"]
    try:
        if "static" in spec:
            path = spec["static"][min(spec["static"], key=lambda w: abs(w - weight))]
            path = path if Path(path).is_absolute() else FONT_DIR / path
            return ImageFont.truetype(str(path), size)
        path = spec["variable"]
        path = path if Path(path).is_absolute() else FONT_DIR / path
        f = ImageFont.truetype(str(path), size)
        try:
            axes = f.get_variation_axes()
            vals = []
            for ax in axes:
                name = ax.get("name", b"")
                name = name.decode() if isinstance(name, bytes) else str(name)
                if name.lower().startswith("weight") or name.lower() == "wght":
                    vals.append(max(ax["minimum"], min(ax["maximum"], weight)))
                else:
                    vals.append(ax.get("default", ax["maximum"]))
            f.set_variation_by_axes(vals)
        except Exception:
            pass
        return f
    except OSError:
        return ImageFont.truetype(str(FONT_DIR / "Rajdhani-Bold.ttf"), size)


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or "#ffffff").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (255, 255, 255)


def mix(a, b, t: float):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def with_alpha(c, a: int):
    return (c[0], c[1], c[2], a)


def nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def temp_value(v, unit: str):
    if nan(v):
        return None
    return v * 9 / 5 + 32 if unit == "F" else v


def fmt_temp(v, unit="C") -> str:
    v = temp_value(v, unit)
    return "--" if v is None else f"{v:.0f}°"


def fmt_pct(v) -> str:
    return "--" if nan(v) else f"{v:.0f}%"


def fmt_rate(bps) -> str:
    if nan(bps):
        return "--"
    for unit, div in (("GB/s", 1024 ** 3), ("MB/s", 1024 ** 2), ("KB/s", 1024)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} B/s"


def fmt_metric(key: str, m: dict, cfg: Config) -> tuple[str, str]:
    """Retorna (valor, unidade) já formatados para uma métrica."""
    v = m.get(key)
    if key in ("cpu_temp", "gpu_temp", "gpu_hotspot", "disk_temp"):
        tv = temp_value(v, cfg.unit)
        return ("--" if tv is None else f"{tv:.0f}", f"°{cfg.unit}")
    if key in ("cpu_load", "gpu_load", "ram_load"):
        return ("--" if nan(v) else f"{v:.0f}", "%")
    if key in ("cpu_clock", "gpu_clock"):
        if nan(v):
            return "--", "MHz"
        return (f"{v / 1000:.2f}", "GHz") if v >= 1000 else (f"{v:.0f}", "MHz")
    if key in ("cpu_power", "gpu_power"):
        return ("--" if nan(v) else f"{v:.0f}", "W")
    if key == "gpu_vram":
        return ("--" if nan(v) else f"{v / 1024:.1f}", "GB")
    if key == "ram_used":
        return ("--" if nan(v) else f"{v:.1f}", "GB")
    if key in ("pump_rpm", "fan_rpm", "gpu_fan"):
        return ("--" if nan(v) else f"{v:.0f}", "RPM")
    if key in ("net_down", "net_up"):
        s = fmt_rate(v)
        parts = s.split(" ")
        return (parts[0], parts[1] if len(parts) > 1 else "")
    now = m.get("now")
    if key == "clock" and now:
        return (now.strftime("%H:%M") if cfg.clock_24h else now.strftime("%I:%M"), "" if cfg.clock_24h else now.strftime("%p"))
    if key == "date" and now:
        return (f"{now.day:02d} {MONTHS[now.month - 1]}", "")
    if key == "uptime":
        if nan(v):
            return "--", ""
        h = int(v // 3600)
        return (f"{h // 24}d {h % 24}h", "") if h >= 24 else (f"{h}h {int(v % 3600 // 60):02d}m", "")
    return ("--" if nan(v) else f"{v:.0f}", "")


def metric_pct(key: str, m: dict) -> float | None:
    """Fração 0..1 para barras, quando faz sentido."""
    v = m.get(key)
    if nan(v):
        return None
    if key in ("cpu_load", "gpu_load", "ram_load"):
        return v / 100
    if key in ("cpu_temp", "gpu_temp", "gpu_hotspot", "disk_temp"):
        return (v - 20) / 75
    if key == "gpu_vram":
        tot = m.get("gpu_vram_total")
        return v / tot if tot else None
    if key in ("pump_rpm",):
        return v / 3500
    if key in ("fan_rpm", "gpu_fan"):
        return v / 3000
    if key == "cpu_power":
        return v / 200
    if key == "gpu_power":
        return v / 300
    if key in ("cpu_clock",):
        return v / 5800
    if key in ("gpu_clock",):
        return v / 3200
    return None


def temp_color(v, cfg: Config):
    """Cor da temperatura: acento até ~50 °C, amarelo em 70, vermelho em 88."""
    acc = rgb(cfg.colors.accent)
    if nan(v):
        return rgb(cfg.colors.muted)
    if v <= 50:
        return acc
    if v <= 70:
        return mix(acc, (255, 196, 0), (v - 50) / 20)
    return mix((255, 196, 0), (255, 59, 59), (v - 70) / 18)


# ----------------------------------------------------------------------
# fundo
# ----------------------------------------------------------------------
@lru_cache(maxsize=8)
def _gradient(c1: str, c2: str) -> Image.Image:
    a, b = rgb(c1), rgb(c2)
    grad = Image.radial_gradient("L").resize((W * 2, H * 2))  # 0 no centro, 255 na borda
    grad = grad.crop((W // 2, H // 2 - 80, W // 2 + W, H // 2 - 80 + H))
    return Image.composite(Image.new("RGB", (W, H), a), Image.new("RGB", (W, H), b), grad)


@lru_cache(maxsize=4)
def _load_bg(path: str, mtime: float) -> Image.Image | None:
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img).convert("RGB")
        return ImageOps.fit(img, (W, H), Image.LANCZOS)
    except Exception:
        return None


def apply_scrim(img: Image.Image, scrim: float) -> Image.Image:
    if scrim <= 0:
        return img.copy()  # cópia: o Canvas desenha in place; sem isso sujaria o Image do @lru_cache de _load_bg
    return Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), max(0.0, min(0.95, scrim)))


def base_background(cfg: Config, media_frame: Image.Image | None = None,
                    scrim: float | None = None) -> Image.Image:
    if media_frame is not None:
        return apply_scrim(media_frame.convert("RGB"), cfg.overlay_scrim if scrim is None else scrim)
    if cfg.bg_image:
        p = Path(cfg.bg_image)
        if p.exists():
            bg = _load_bg(str(p), p.stat().st_mtime)
            if bg is not None:
                return apply_scrim(bg, cfg.bg_scrim)
    return _gradient(cfg.colors.background, cfg.colors.background2).copy()


# ----------------------------------------------------------------------
# primitivas
# ----------------------------------------------------------------------
class Canvas:
    def __init__(self, base: Image.Image, cfg: Config, m: dict):
        self.img = base
        self.d = Draw(self.img, "RGBA")
        self.cfg = cfg
        self.m = m
        self.fam = cfg.font
        self.acc = rgb(cfg.colors.accent)
        self.acc2 = rgb(cfg.colors.accent2)
        self.txt = rgb(cfg.colors.text)
        self.muted = rgb(cfg.colors.muted)
        self._layer: Image.Image | None = None
        self._ld: ImageDraw.ImageDraw | None = None
        self.has_media = False  # fundo é vídeo/foto/imagem (alguns temas desenham fundo próprio se não for)

    def f(self, size, weight=700):
        return font(self.fam, size, weight)

    def text(self, xy, s, size, color=None, weight=700, anchor="mm", alpha=255):
        c = color or self.txt
        if alpha < 255:
            self.text_alpha(xy, s, self.f(size, weight), c, alpha, anchor)
            return
        self.d.text(xy, s, font=self.f(size, weight), fill=with_alpha(c, alpha), anchor=anchor)

    def text_alpha(self, xy, s, fnt, color, alpha, anchor="mm", **kw):
        """Texto semitransparente. O Pillow ignora o alfa de texto desenhado direto numa imagem RGB, então
        desenha numa camada do tamanho do texto e cola com a transparência."""
        x0, y0, x1, y1 = self.d.textbbox(xy, s, font=fnt, anchor=anchor, stroke_width=kw.get("stroke_width", 0))
        x0, y0 = int(x0) - 1, int(y0) - 1
        if x1 <= x0 or y1 <= y0:
            return
        layer = Image.new("RGBA", (int(x1) - x0 + 2, int(y1) - y0 + 2), (0, 0, 0, 0))
        Draw(layer).text((xy[0] - x0, xy[1] - y0), s, font=fnt, fill=with_alpha(color, 255),
                                   anchor=anchor, **kw)
        if alpha < 255:
            layer.putalpha(layer.getchannel("A").point(lambda v: v * alpha // 255))
        self.img.paste(layer, (x0, y0), layer)
        self.d = Draw(self.img, "RGBA")

    def value_unit(self, x, y, value, unit, size, color=None, unit_size=None, anchor="center"):
        """Número grande + unidade pequena, alinhados pela linha de base."""
        fv = self.f(size, 700)
        fu = self.f(unit_size or max(12, size // 3), 600)
        wv = self.d.textlength(value, font=fv)
        wu = self.d.textlength(unit, font=fu) if unit else 0
        gap = 4 if unit else 0
        total = wv + gap + wu
        if anchor == "center":
            x0 = x - total / 2
        elif anchor == "right":
            x0 = x - total
        else:
            x0 = x
        self.d.text((x0, y), value, font=fv, fill=color or self.txt, anchor="ls")
        if unit:
            self.d.text((x0 + wv + gap, y), unit, font=fu, fill=self.muted, anchor="ls")

    # -- camada supersampled para arcos ---------------------------------
    def _layer_draw(self):
        global _SHARED_LAYER, _SHARED_LD
        if self._layer is None:
            if _SHARED_LAYER is None:  # aloca uma vez; já nasce limpo e o flush() o devolve limpo
                _SHARED_LAYER = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
                _SHARED_LD = Draw(_SHARED_LAYER, "RGBA")
            self._layer = _SHARED_LAYER
            self._ld = _SHARED_LD
        return self._ld

    def arc(self, cx, cy, r, width, start, end, color, alpha=255, caps=True):
        if end <= start:
            return
        d = self._layer_draw()
        s = SS
        box = [(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s]
        col = with_alpha(color, alpha)
        d.arc(box, start, end, fill=col, width=int(width * s))
        if caps:
            rr = r - width / 2
            for ang in (start, end):
                a = math.radians(ang)
                px, py = cx + rr * math.cos(a), cy + rr * math.sin(a)
                w2 = width / 2
                d.ellipse([(px - w2) * s, (py - w2) * s, (px + w2) * s, (py + w2) * s], fill=col)

    def line_aa(self, points, color, width=3, alpha=255):
        d = self._layer_draw()
        pts = [(x * SS, y * SS) for x, y in points]
        if len(pts) >= 2:
            d.line(pts, fill=with_alpha(color, alpha), width=int(width * SS), joint="curve")

    def poly_aa(self, points, color, alpha=255):
        d = self._layer_draw()
        if len(points) >= 3:
            d.polygon([(x * SS, y * SS) for x, y in points], fill=with_alpha(color, alpha))

    def rrect(self, box, radius, fill=None, outline=None, width=1):
        self.d.rounded_rectangle(box, radius, fill=fill, outline=outline, width=width)

    def bar(self, box, frac, color, bg_alpha=40, radius=None):
        x0, y0, x1, y1 = box
        r = radius if radius is not None else (y1 - y0) // 2
        self.d.rounded_rectangle(box, r, fill=with_alpha(self.muted, bg_alpha))
        if frac is not None and frac > 0:
            xe = x0 + (x1 - x0) * max(0.0, min(1.0, frac))
            if xe - x0 >= 2 * r:
                self.d.rounded_rectangle([x0, y0, xe, y1], r, fill=color)
            else:
                self.d.ellipse([x0, y0, x0 + (y1 - y0), y1], fill=color)

    def flush(self):
        if self._layer is not None:
            # média 2x2 = antialiasing clássico de supersampling, 4x mais rápido que Lanczos
            small = self._layer.reduce(SS)
            self.img.paste(small, (0, 0), small)
            self._layer.paste((0, 0, 0, 0), (0, 0, W * SS, H * SS))  # limpa o buffer compartilhado p/ reuso
            self._layer = None
            self._ld = None
            self.d = Draw(self.img, "RGBA")

    def glow_text(self, xy, s, size, color, weight=700, anchor="mm", radius=10, strength=2):
        fnt = self.f(size, weight)
        # só a região do texto (+ margem do brilho) é desenhada e borrada, não a tela inteira
        x0, y0, x1, y1 = self.d.textbbox(xy, s, font=fnt, anchor=anchor)
        m = radius * 3
        box = (max(0, int(x0) - m), max(0, int(y0) - m), min(W, int(x1) + m), min(H, int(y1) + m))
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        layer = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
        Draw(layer).text((xy[0] - box[0], xy[1] - box[1]), s, font=fnt,
                                   fill=with_alpha(color, 255), anchor=anchor)
        glow = layer.filter(ImageFilter.GaussianBlur(radius))
        for _ in range(strength):
            self.img.paste(glow, box[:2], glow)
        self.img.paste(layer, box[:2], layer)
        self.d = Draw(self.img, "RGBA")

    def sparkline(self, box, values, color, vmin=0.0, vmax=100.0, fill_alpha=50, width=3):
        x0, y0, x1, y1 = box
        vals = [v for v in values if not nan(v)]
        if len(vals) < 2:
            return
        n = len(values)
        span = (vmax - vmin) or 1
        pts = []
        for i, v in enumerate(values):
            if nan(v):
                continue
            x = x0 + (x1 - x0) * i / max(1, n - 1)
            y = y1 - (y1 - y0) * max(0.0, min(1.0, (v - vmin) / span))
            pts.append((x, y))
        if len(pts) < 2:
            return
        self.poly_aa(pts + [(pts[-1][0], y1), (pts[0][0], y1)], color, fill_alpha)
        self.line_aa(pts, color, width)


WEEK3 = ["SEG", "TER", "QUA", "QUI", "SEX", "SÁB", "DOM"]
CLOCK_FORMATS = {"hora": "Hora", "hora_seg": "Hora com segundos", "data": "Só a data",
                 "data_hora": "Data e hora", "completo": "Dia da semana, data e hora (com segundos)"}


def fmt_clock(cfg, now) -> str:
    f = getattr(cfg, "clock_format", "hora")
    h = "%H:%M" if cfg.clock_24h else "%I:%M"
    if f in ("hora_seg", "completo"):
        h += ":%S"
    t = now.strftime(h) + ("" if cfg.clock_24h else now.strftime(" %p"))
    if f == "data":
        return now.strftime("%d/%m/%Y")
    if f == "data_hora":
        return now.strftime("%d/%m  ") + t
    if f == "completo":
        return f"{WEEK3[now.weekday()]}, {now:%d/%m} · {t}"
    return t


def header(c: Canvas, y=30):
    now = c.m.get("now")
    if getattr(c.cfg, "show_title", True) and c.cfg.title:
        c.text((28, y), c.cfg.title.upper(), 24, c.acc, 700, anchor="lm")
    if now:
        c.text((W - 28, y), fmt_clock(c.cfg, now), 24, c.txt, 600, anchor="rm")


# ----------------------------------------------------------------------
# temas
# ----------------------------------------------------------------------
def theme_aneis(c: Canvas):
    m, cfg = c.m, c.cfg
    header(c)
    rings = [
        (124, 196, cfg.cpu_label, m.get("cpu_temp"), m.get("cpu_load"), c.acc),
        (356, 196, cfg.gpu_label, m.get("gpu_temp"), m.get("gpu_load"), c.acc2),
    ]
    for cx, cy, label, temp, load, color in rings:
        R = 104
        c.arc(cx, cy, R, 15, 135, 405, c.muted, 55, caps=True)
        frac = metric_pct("cpu_temp", {"cpu_temp": temp}) if not nan(temp) else 0
        tc = temp_color(temp, cfg) if color == c.acc else mix(color, temp_color(temp, cfg), 0.5 if not nan(temp) and temp > 60 else 0)
        if frac:
            c.arc(cx, cy, R, 15, 135, 135 + 270 * max(0.02, min(1, frac)), tc)
        R2 = 80
        c.arc(cx, cy, R2, 6, 135, 405, c.muted, 45, caps=True)
        if not nan(load):
            c.arc(cx, cy, R2, 6, 135, 135 + 270 * max(0.01, min(1, load / 100)), color, 230)
    c.flush()
    for cx, cy, label, temp, load, color in rings:
        tv = temp_value(temp, cfg.unit)
        c.text((cx, cy - 44), label, 26, color, 700)
        c.value_unit(cx + 6, cy + 30, "--" if tv is None else f"{tv:.0f}", f"°{cfg.unit}", 66, c.txt, 24)
        c.text((cx, cy + 58), f"USO {fmt_pct(load)}", 20, c.muted, 600)

    # rodapé: RAM / VRAM / extra
    y = 334
    rows = [
        ("RAM", m.get("ram_load"), f"{m.get('ram_used', 0):.1f} / {m.get('ram_total', 0):.0f} GB", c.acc),
        ("VRAM", metric_pct("gpu_vram", m), "--" if nan(m.get("gpu_vram")) else
         f"{m['gpu_vram'] / 1024:.1f} / {(m.get('gpu_vram_total') or 0) / 1024:.0f} GB", c.acc2),
    ]
    for label, frac, txt, color in rows:
        frac = frac / 100 if label == "RAM" and frac is not None else frac
        c.text((28, y), label, 20, c.muted, 700, anchor="lm")
        c.text((W - 28, y), txt, 20, c.txt, 600, anchor="rm")
        c.bar([28, y + 16, W - 28, y + 26], frac, color)
        y += 50
    if not nan(m.get("pump_rpm")):
        extra_l = ("BOMBA", fmt_metric("pump_rpm", m, cfg))
    elif not nan(m.get("cpu_clock")):
        extra_l = ("CPU", fmt_metric("cpu_clock", m, cfg))
    else:
        extra_l = ("SSD", fmt_metric("disk_temp", m, cfg))
    extra_r = ("GPU", fmt_metric("gpu_power", m, cfg))
    c.text((28, 448), f"{extra_l[0]}  {extra_l[1][0]} {extra_l[1][1]}", 22, c.txt, 600, anchor="lm")
    c.text((W - 28, 448), f"{extra_r[0]}  {extra_r[1][0]} {extra_r[1][1]}", 22, c.txt, 600, anchor="rm")


def theme_neon(c: Canvas):
    m, cfg = c.m, c.cfg
    hist = m.get("history", {})
    blocks = [
        (0, cfg.cpu_label, m.get("cpu_temp"), m.get("cpu_load"), hist.get("cpu_load", []), c.acc,
         fmt_metric("cpu_clock", m, cfg)),
        (240, cfg.gpu_label, m.get("gpu_temp"), m.get("gpu_load"), hist.get("gpu_load", []), c.acc2,
         fmt_metric("gpu_power", m, cfg)),
    ]
    for y0, label, temp, load, h, color, extra in blocks:
        c.sparkline([0, y0 + 168, W, y0 + 236], h[-90:], color, 0, 100, fill_alpha=45, width=2)
    c.flush()
    c.d.line([(30, 240), (W - 30, 240)], fill=with_alpha(c.muted, 90), width=2)
    for y0, label, temp, load, h, color, extra in blocks:
        tv = temp_value(temp, cfg.unit)
        c.text((30, y0 + 26), label, 28, color, 700, anchor="lm")
        c.text((W - 30, y0 + 26), f"{extra[0]} {extra[1]}", 24, c.muted, 600, anchor="rm")
        c.glow_text((26, y0 + 150), "--" if tv is None else f"{tv:.0f}°", 112, color, 700, anchor="ls", radius=9)
        c.text((W - 30, y0 + 118), fmt_pct(load), 54, c.txt, 700, anchor="rs")
        c.text((W - 30, y0 + 144), "USO", 20, c.muted, 600, anchor="rs")


def theme_grade(c: Canvas):
    m, cfg = c.m, c.cfg
    pad, gap = 14, 12
    tw = (W - pad * 2 - gap) // 2
    th = (H - pad * 2 - gap * 2) // 3
    colors = [c.acc, c.acc2]
    for i, key in enumerate(cfg.grid_slots[:6]):
        col, row = i % 2, i // 2
        x0 = pad + col * (tw + gap)
        y0 = pad + row * (th + gap)
        color = colors[col]
        c.rrect([x0, y0, x0 + tw, y0 + th], 18, fill=(255, 255, 255, 14), outline=with_alpha(color, 70), width=2)
        label = METRICS.get(key, key).upper()
        label = label.replace("— ", "").replace("TEMPERATURA", "TEMP")
        c.text((x0 + 16, y0 + 24), label, 19, color, 700, anchor="lm")
        val, unit = fmt_metric(key, m, cfg)
        vcol = temp_color(m.get(key), cfg) if "temp" in key and key != "cpu_temp_x" else c.txt
        c.value_unit(x0 + 16, y0 + 100, val, unit, 60, vcol, 22, anchor="left")
        frac = metric_pct(key, m)
        if frac is not None:
            c.bar([x0 + 16, y0 + th - 22, x0 + tw - 16, y0 + th - 14], frac, color)


def theme_velocimetro(c: Canvas):
    m, cfg = c.m, c.cfg
    header(c)
    gauges = [
        (124, 210, cfg.cpu_label, m.get("cpu_temp"), m.get("cpu_load"), c.acc),
        (356, 210, cfg.gpu_label, m.get("gpu_temp"), m.get("gpu_load"), c.acc2),
    ]
    for cx, cy, label, temp, load, color in gauges:
        R = 100
        c.arc(cx, cy, R, 4, 150, 390, c.muted, 90, caps=False)
        # zona quente
        c.arc(cx, cy, R, 4, 150 + 240 * 0.75, 390, (255, 59, 59), 200, caps=False)
        d = c._layer_draw()
        for i in range(0, 11):
            ang = math.radians(150 + 24 * i)
            r1, r2 = R - 8, R - (22 if i % 5 == 0 else 15)
            p1 = (cx + r1 * math.cos(ang), cy + r1 * math.sin(ang))
            p2 = (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))
            d.line([(p1[0] * SS, p1[1] * SS), (p2[0] * SS, p2[1] * SS)],
                   fill=with_alpha(c.txt, 200), width=(3 if i % 5 == 0 else 2) * SS)
        frac = 0 if nan(load) else max(0.0, min(1.0, load / 100))
        c.arc(cx, cy, R + 12, 8, 150, 150 + 240 * max(0.01, frac), color, 240)
        ang = math.radians(150 + 240 * frac)
        tip = (cx + (R - 26) * math.cos(ang), cy + (R - 26) * math.sin(ang))
        perp = ang + math.pi / 2
        base = [(cx + 6 * math.cos(perp), cy + 6 * math.sin(perp)), (cx - 6 * math.cos(perp), cy - 6 * math.sin(perp))]
        c.poly_aa([base[0], tip, base[1]], color)
        d.ellipse([(cx - 11) * SS, (cy - 11) * SS, (cx + 11) * SS, (cy + 11) * SS], fill=with_alpha(c.txt, 255))
    c.flush()
    for cx, cy, label, temp, load, color in gauges:
        c.text((cx, cy - 38), label, 24, color, 700)
        c.text((cx, cy + 58), fmt_pct(load), 34, c.txt, 700)
        tv = temp_value(temp, cfg.unit)
        c.value_unit(cx, cy + 128, "--" if tv is None else f"{tv:.0f}", f"°{cfg.unit}", 52,
                     temp_color(temp, cfg), 22)
    c.text((W // 2, 452), f"RAM {fmt_pct(m.get('ram_load'))}   ·   REDE {fmt_rate(m.get('net_down'))}", 22, c.muted, 600)


def theme_graficos(c: Canvas):
    m, cfg = c.m, c.cfg
    hist = m.get("history", {})
    header(c)
    panels = [
        (64, cfg.cpu_label, "cpu_temp", "cpu_load", c.acc),
        (268, cfg.gpu_label, "gpu_temp", "gpu_load", c.acc2),
    ]
    for y0, label, tk, lk, color in panels:
        box = [24, y0 + 60, W - 24, y0 + 190]
        c.rrect([16, y0, W - 16, y0 + 196], 18, fill=(255, 255, 255, 12), outline=with_alpha(color, 60), width=2)
        for gy in range(1, 4):
            yy = box[1] + (box[3] - box[1]) * gy / 4
            c.d.line([(box[0], yy), (box[2], yy)], fill=with_alpha(c.muted, 30), width=1)
        c.sparkline(box, hist.get(lk, [])[-120:], color, 0, 100, fill_alpha=55, width=3)
        c.sparkline(box, hist.get(tk, [])[-120:], c.txt, 20, 100, fill_alpha=0, width=2)
    c.flush()
    for y0, label, tk, lk, color in panels:
        c.text((32, y0 + 30), label, 28, color, 700, anchor="lm")
        tv, tu = fmt_metric(tk, m, cfg)
        c.value_unit(250, y0 + 44, tv, tu, 42, temp_color(m.get(tk), cfg), 18, anchor="right")
        lv, lu = fmt_metric(lk, m, cfg)
        c.value_unit(W - 32, y0 + 44, lv, lu, 42, c.txt, 18, anchor="right")
    c.text((W // 2, 470), "linha branca = temperatura · área = uso", 16, c.muted, 500)


def theme_minimal(c: Canvas):
    m, cfg = c.m, c.cfg
    temp = m.get("cpu_temp")
    frac = metric_pct("cpu_temp", m) or 0
    c.arc(240, 240, 226, 10, 0, 360, c.muted, 40, caps=False)
    if frac:
        c.arc(240, 240, 226, 10, -90, -90 + 360 * max(0.01, min(1, frac)), temp_color(temp, cfg))
    c.flush()
    now = m.get("now")
    if now:
        c.text((240, 96), now.strftime("%H:%M") if cfg.clock_24h else now.strftime("%I:%M"), 40, c.muted, 600)
    c.text((240, 158), cfg.cpu_label, 30, c.acc, 700)
    tv = temp_value(temp, cfg.unit)
    c.value_unit(250, 300, "--" if tv is None else f"{tv:.0f}", f"°{cfg.unit}", 150, c.txt, 44)
    gv, gu = fmt_metric("gpu_temp", m, cfg)
    c.text((240, 370), f"{cfg.gpu_label}  {gv}{gu}", 34, c.acc2, 700)
    c.text((240, 410), f"USO {fmt_pct(m.get('cpu_load'))} · {fmt_pct(m.get('gpu_load'))}", 22, c.muted, 600)


def theme_relogio(c: Canvas):
    m, cfg = c.m, c.cfg
    now = m.get("now")
    if not now:
        return
    sec = now.second + now.microsecond / 1e6
    c.arc(240, 240, 222, 6, 0, 360, c.muted, 40, caps=False)
    c.arc(240, 240, 222, 6, -90, -90 + 360 * (sec / 60), c.acc, 255)
    for i in range(60):
        ang = math.radians(-90 + i * 6)
        r1 = 206 if i % 5 == 0 else 210
        d = c._layer_draw()
        p1 = (240 + r1 * math.cos(ang), 240 + r1 * math.sin(ang))
        p2 = (240 + 214 * math.cos(ang), 240 + 214 * math.sin(ang))
        d.line([(p1[0] * SS, p1[1] * SS), (p2[0] * SS, p2[1] * SS)],
               fill=with_alpha(c.txt if i % 5 == 0 else c.muted, 200), width=2 * SS)
    c.flush()
    hhmm = now.strftime("%H:%M") if cfg.clock_24h else now.strftime("%I:%M")
    c.glow_text((240, 222), hhmm, 128, c.txt, 700, radius=8, strength=1)
    c.text((240, 300), f"{WEEKDAYS[now.weekday()]}, {now.day:02d} {MONTHS[now.month - 1]}", 30, c.acc, 700)
    cv, cu = fmt_metric("cpu_temp", m, cfg)
    gv, gu = fmt_metric("gpu_temp", m, cfg)
    c.text((176, 364), cfg.cpu_label, 20, c.muted, 700)
    c.text((176, 392), f"{cv}{cu}", 32, temp_color(m.get("cpu_temp"), cfg), 700)
    c.text((304, 364), cfg.gpu_label, 20, c.muted, 700)
    c.text((304, 392), f"{gv}{gu}", 32, temp_color(m.get("gpu_temp"), cfg), 700)


def theme_compacto(c: Canvas):
    """Barra discreta para usar por cima de fotos/vídeos."""
    m, cfg = c.m, c.cfg
    now = m.get("now")
    if now:
        hhmm = fmt_clock(cfg, now)
        tw = c.d.textlength(hhmm, font=c.f(30, 700))
        c.rrect([W - 34 - tw, 14, W - 14, 62], 16, fill=(0, 0, 0, 140))
        c.text((W - 24, 39), hhmm, 30, c.txt, 700, anchor="rm")
    c.rrect([14, H - 132, W - 14, H - 14], 22, fill=(0, 0, 0, 150))
    half = (W - 28) // 2
    for i, (label, tk, lk, color) in enumerate([
        (cfg.cpu_label, "cpu_temp", "cpu_load", c.acc), (cfg.gpu_label, "gpu_temp", "gpu_load", c.acc2)]):
        x0 = 14 + i * half
        c.text((x0 + 22, H - 108), label, 22, color, 700, anchor="lm")
        tv, tu = fmt_metric(tk, m, cfg)
        c.value_unit(x0 + 22, H - 46, tv, tu, 56, temp_color(m.get(tk), cfg), 20, anchor="left")
        c.text((x0 + half - 22, H - 64), fmt_pct(m.get(lk)), 30, c.txt, 700, anchor="rm")
        c.bar([x0 + 22, H - 36, x0 + half - 22, H - 28], (m.get(lk) or 0) / 100, color)
    c.d.line([(W // 2, H - 118), (W // 2, H - 28)], fill=with_alpha(c.muted, 80), width=2)


ANIMATED_THEMES: set = set()  # temas que se mexem sozinhos (feixe, ponteiro…): o motor redesenha mais vezes

THEME_FUNCS = {
    "aneis": theme_aneis,
    "neon": theme_neon,
    "grade": theme_grade,
    "velocimetro": theme_velocimetro,
    "graficos": theme_graficos,
    "minimal": theme_minimal,
    "relogio": theme_relogio,
    "compacto": theme_compacto,
    "musica": lambda c: _music().theme_musica(c),
}


def _music():
    from . import music  # import tardio: music.py usa o Canvas daqui

    return music


# ----------------------------------------------------------------------
class Smoother:
    """Suaviza números para os ponteiros/anéis andarem em vez de pular."""

    KEYS = ("cpu_temp", "cpu_load", "gpu_temp", "gpu_load", "ram_load", "gpu_vram",
            "cpu_power", "gpu_power", "pump_rpm", "fan_rpm", "cpu_clock", "gpu_clock")

    def __init__(self):
        self.state: dict = {}

    def __call__(self, m: dict, factor: float = 0.25) -> dict:
        out = dict(m)
        for k in self.KEYS:
            v = m.get(k)
            if nan(v):
                self.state.pop(k, None)
                continue
            prev = self.state.get(k)
            cur = v if prev is None else prev + (v - prev) * factor
            self.state[k] = cur
            out[k] = cur
        return out


_render_lock = threading.Lock()
_BLACK = Image.new("RGB", (W, H), (0, 0, 0))
_WHITE = Image.new("RGB", (W, H), (255, 255, 255))


def matte(cfg: Config, metrics: dict, draw) -> Image.Image:
    """Desenha algo como camada RGBA de fundo transparente: desenha sobre preto e sobre branco e calcula a
    transparência exata pela diferença (funciona com brilho, desfoque, mistura — tudo que os temas usam)."""
    import numpy as np

    outs = []
    with _render_lock:
        for base in (_BLACK, _WHITE):
            c = Canvas(base.copy(), cfg, metrics)
            draw(c)
            c.flush()
            outs.append(np.asarray(c.img.convert("RGB"), np.float32))
    b, w = outs
    a = np.clip(1.0 - (w - b).mean(axis=2, keepdims=True) / 255.0, 0.0, 1.0)
    rgb = np.where(a > 0.004, b / np.maximum(a, 0.004), 0.0)
    return Image.fromarray(np.concatenate([np.clip(rgb, 0, 255), a * 255], axis=2).astype(np.uint8), "RGBA")


def render_layer(cfg: Config, metrics: dict, theme: str) -> Image.Image:
    """Tema como camada transparente (para mover/redimensionar por cima de vídeo/foto)."""
    c = cfg.copy()
    c.overlay_scrim = 0.0
    fn = THEME_FUNCS.get(theme, theme_aneis)
    return matte(c, metrics, fn)


def render(cfg: Config, metrics: dict, theme: str | None = None,
           media_frame: Image.Image | None = None) -> Image.Image:
    theme = theme or cfg.theme
    with _render_lock:  # motor e miniaturas da interface desenham em threads diferentes
        base = base_background(cfg, media_frame)
        c = Canvas(base, cfg, metrics)
        c.has_media = media_frame is not None or bool(cfg.bg_image)
        THEME_FUNCS.get(theme, theme_aneis)(c)
        c.flush()
        return c.img


# temas extras se registram sozinhos (import no fim: render já está completo aqui)
from . import themes_extra  # noqa: E402,F401
