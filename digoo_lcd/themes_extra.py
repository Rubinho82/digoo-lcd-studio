"""Temas extras (originais), registrados no render e no tema Música ao importar."""

from __future__ import annotations

import time

from . import render
from .render import H, W, fmt_metric, fmt_pct, metric_pct, temp_color, temp_value, with_alpha


# ----------------------------------------------------------------------
# sistema
# ----------------------------------------------------------------------
def theme_terminal(c):
    """Terminal retrô: texto verde de monitor CRT, com linhas de varredura."""
    m, cfg = c.m, c.cfg
    green, dim = (64, 255, 120), (30, 120, 60)
    c.d.rectangle([0, 0, W, H], fill=(2, 10, 4, 255))
    f = render.font("Share Tech Mono", 26)
    now = m.get("now")
    lines = [
        f"> {cfg.title or 'SYSTEM'}  {render.fmt_clock(cfg, now) if now else ''}",
        "",
        f"CPU   {fmt_metric('cpu_temp', m, cfg)[0]:>4}°  {fmt_pct(m.get('cpu_load')):>5}",
        f"GPU   {fmt_metric('gpu_temp', m, cfg)[0]:>4}°  {fmt_pct(m.get('gpu_load')):>5}",
        f"RAM   {fmt_pct(m.get('ram_load')):>11}",
        f"VRAM  {fmt_metric('gpu_vram', m, cfg)[0]:>8} GB",
        f"CLK   {fmt_metric('cpu_clock', m, cfg)[0]:>8} GHz",
        f"PWR   {fmt_metric('gpu_power', m, cfg)[0]:>9} W",
        "",
        "> _" if int(time.time() * 2) % 2 else ">",
    ]
    for i, ln in enumerate(lines):
        c.d.text((26, 30 + i * 40), ln, font=f, fill=green + (255,))
    for i, key in enumerate(("cpu_load", "gpu_load", "ram_load")):  # barras em blocos ASCII
        frac = max(0.0, min(1.0, (m.get(key) or 0) / 100))
        n = int(frac * 14)
        c.d.text((300, 110 + i * 40 if i < 2 else 190), "#" * n + "." * (14 - n), font=render.font("Share Tech Mono", 20),
                 fill=dim + (255,))
    for y in range(0, H, 3):  # varredura CRT
        c.d.line([(0, y), (W, y)], fill=(0, 0, 0, 70))


def theme_hud(c):
    """HUD cyberpunk: painéis em ângulo, amarelo e ciano."""
    m, cfg = c.m, c.cfg
    yel, cya = (255, 214, 0), c.acc
    c.d.rectangle([0, 0, W, H], fill=(8, 8, 14, 255))
    for y in range(0, H, 24):
        c.d.line([(0, y), (W, y)], fill=(255, 255, 255, 10))

    def panel(x0, y0, x1, y1, col):
        pts = [(x0 + 18, y0), (x1, y0), (x1, y1 - 18), (x1 - 18, y1), (x0, y1), (x0, y0 + 18)]
        c.d.polygon(pts, fill=(col[0], col[1], col[2], 22), outline=col + (230,))

    now = m.get("now")
    c.d.polygon([(0, 0), (W, 0), (W, 50), (W - 30, 64), (0, 64)], fill=yel + (255,))
    c.text((20, 32), (cfg.title or "NET//RUN").upper() if cfg.show_title else "NET//RUN", 28, (10, 10, 14), 700, anchor="lm")
    if now:
        c.text((W - 44, 32), render.fmt_clock(cfg, now), 26, (10, 10, 14), 700, anchor="rm")
    for i, (label, tk, lk, col) in enumerate(((cfg.cpu_label, "cpu_temp", "cpu_load", cya),
                                              (cfg.gpu_label, "gpu_temp", "gpu_load", yel))):
        x0 = 16 + i * 232
        panel(x0, 86, x0 + 216, 290, col)
        c.text((x0 + 18, 112), label, 24, col, 700, anchor="lm")
        tv, tu = fmt_metric(tk, m, cfg)
        c.value_unit(x0 + 18, 210, tv, tu, 78, (255, 255, 255), 26, anchor="left")
        frac = (m.get(lk) or 0) / 100
        for s in range(10):  # segmentos
            on = s < round(frac * 10)
            c.d.rectangle([x0 + 18 + s * 18, 240, x0 + 30 + s * 18, 262], fill=(col if on else (60, 60, 70)) + (255,))
        c.text((x0 + 198, 112), fmt_pct(m.get(lk)), 22, (220, 220, 230), 600, anchor="rm")
    panel(16, 306, W - 16, 464, cya)
    for i, (label, key) in enumerate((("RAM", "ram_load"), ("VRAM", "gpu_vram"), ("REDE", "net_down"))):
        y = 340 + i * 42
        val, unit = fmt_metric(key, m, cfg)
        c.text((36, y), label, 22, cya, 700, anchor="lm")
        c.text((W - 36, y), f"{val} {unit}", 22, (255, 255, 255), 700, anchor="rm")
        frac = metric_pct(key, m)
        if frac is not None:
            c.bar([130, y - 4, 330, y + 4], frac, yel)


def theme_barras(c):
    """Barras verticais grandes: CPU, GPU, RAM e VRAM lado a lado."""
    m, cfg = c.m, c.cfg
    render.header(c)
    items = [(cfg.cpu_label, "cpu_load", "cpu_temp", c.acc), (cfg.gpu_label, "gpu_load", "gpu_temp", c.acc2),
             ("RAM", "ram_load", None, (120, 230, 140)), ("VRAM", "gpu_vram", None, (255, 190, 60))]
    bw, gap, top, bottom = 84, 28, 120, 400
    x0 = (W - (4 * bw + 3 * gap)) / 2
    for i, (label, key, tkey, col) in enumerate(items):
        x = x0 + i * (bw + gap)
        frac = metric_pct(key, m) or 0
        c.rrect([x, top, x + bw, bottom], 14, fill=with_alpha(c.muted, 35))
        h = (bottom - top) * max(0.02, min(1.0, frac))
        steps = 18
        for s in range(steps):  # degradê de baixo pra cima
            y1 = bottom - h * s / steps
            y0 = bottom - h * (s + 1) / steps
            k = s / steps
            cc = render.mix(col, (255, 255, 255), k * 0.35)
            c.d.rectangle([x, y0, x + bw, y1 + 1], fill=cc + (235,))
        c.text((x + bw / 2, top - 22), f"{frac * 100:.0f}%", 26, c.txt, 700)
        c.text((x + bw / 2, bottom + 26), label, 22, col, 700)
        if tkey:
            tv = temp_value(m.get(tkey), cfg.unit)
            c.text((x + bw / 2, bottom + 56), "--" if tv is None else f"{tv:.0f}°", 26, temp_color(m.get(tkey), cfg), 700)


def theme_led(c):
    """Dígitos de LED: números enormes com brilho, estilo painel digital."""
    m, cfg = c.m, c.cfg
    c.d.rectangle([0, 0, W, H], fill=(6, 4, 4, 255))
    for i, (label, key, col) in enumerate(((cfg.cpu_label, "cpu_temp", (255, 60, 40)),
                                           (cfg.gpu_label, "gpu_temp", (60, 220, 255)))):
        y = 150 + i * 200
        tv = temp_value(m.get(key), cfg.unit)
        txt = "--" if tv is None else f"{tv:.0f}"
        c.glow_text((W - 110, y), txt, 130, col, 700, anchor="rm", radius=10, strength=1)
        c.text((W - 96, y - 44), f"°{cfg.unit}", 30, col, 700, anchor="lm")
        c.text((30, y - 60), label, 28, col, 700, anchor="lm")
        c.text((30, y - 26), fmt_pct(m.get("cpu_load" if i == 0 else "gpu_load")), 26, (200, 200, 200), 600, anchor="lm")
    now = m.get("now")
    if now:
        c.text((W / 2, 452), render.fmt_clock(cfg, now), 30, (255, 170, 60), 700)


render.THEMES.update({"terminal": "Terminal retrô", "hud": "HUD cyberpunk", "barras": "Barras verticais",
                      "led": "Dígitos de LED"})
render.THEME_FUNCS.update({"terminal": theme_terminal, "hud": theme_hud, "barras": theme_barras, "led": theme_led})

from . import music_extra, themes_display  # noqa: E402,F401  (mais modelos de música e mais temas)
