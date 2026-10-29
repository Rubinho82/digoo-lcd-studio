"""Temas no estilo de outros programas de display de PC (anel único, lista de sensores, cartões…),
desenhados do zero aqui. Registrados no render ao importar."""

from __future__ import annotations

import math
import time

from PIL import Image, ImageFilter

from . import render
from .textdraw import Draw
from .render import H, W, fmt_metric, fmt_pct, metric_pct, temp_color, temp_value, with_alpha


def _grad_arc(c, cx, cy, r, width, start, end, c1, c2, steps=48):
    """Arco com degradê de uma cor para outra (desenhado em pedaços)."""
    if end <= start:
        return
    span = end - start
    n = max(2, int(steps * span / 360))
    for i in range(n):
        a0 = start + span * i / n
        a1 = start + span * (i + 1) / n + 0.6
        c.arc(cx, cy, r, width, a0, min(end, a1), render.mix(c1, c2, i / max(1, n - 1)), caps=False)
    c.arc(cx, cy, r, width, start, start + 0.5, c1)  # pontas arredondadas
    c.arc(cx, cy, r, width, end - 0.5, end, c2)


def theme_anel_unico(c):
    """Anel único: um valor gigante no meio de um anel grosso, alternando CPU e GPU a cada 6 s."""
    m, cfg = c.m, c.cfg
    gpu = int(time.time() / 6) % 2 == 1
    label, tk, lk = (cfg.gpu_label, "gpu_temp", "gpu_load") if gpu else (cfg.cpu_label, "cpu_temp", "cpu_load")
    c1, c2 = (c.acc2, c.acc) if gpu else (c.acc, c.acc2)
    frac = max(0.0, min(1.0, metric_pct(tk, m) or 0))
    c.arc(240, 240, 206, 28, 0, 360, c.muted, 38, caps=False)
    if frac > 0:
        _grad_arc(c, 240, 240, 206, 28, -90, -90 + 360 * max(0.02, frac), c1, c2)
    c.flush()
    c.text((240, 142), label, 34, c1, 700)
    tv = temp_value(m.get(tk), cfg.unit)
    c.value_unit(250, 300, "--" if tv is None else f"{tv:.0f}", f"°{cfg.unit}", 150, c.txt, 44)
    c.text((240, 350), f"USO {fmt_pct(m.get(lk))}", 28, c.muted, 600)
    for i in range(2):  # qual está na tela
        x = 228 + i * 24
        on = i == int(gpu)
        c.d.ellipse([x - 5, 390, x + 5, 400], fill=(c1 if on else c.muted) + (255 if on else 90,))


_LIST_ROWS = (("cpu_temp", "temp"), ("cpu_load", "uso"), ("cpu_clock", "clock"), ("cpu_power", "consumo"),
              ("gpu_temp", "temp"), ("gpu_hotspot", "memória"), ("gpu_load", "uso"), ("gpu_clock", "clock"),
              ("gpu_power", "consumo"), ("gpu_vram", "VRAM"), ("ram_load", "RAM"), ("disk_temp", "SSD"),
              ("net_down", "download"), ("net_up", "upload"))


def theme_lista(c):
    """Lista de sensores: tudo de uma vez em linhas, com barrinhas (estilo painel de sensores de PC)."""
    m, cfg = c.m, c.cfg
    c.d.rectangle([0, 0, W, H], fill=(8, 10, 14, 235))
    mono = render.font("Share Tech Mono", 19)
    now = m.get("now")
    title = (cfg.title or "SENSORES").upper() if cfg.show_title else "SENSORES"
    c.d.text((18, 24), title, font=mono, fill=c.acc + (255,), anchor="lm")
    if now:
        c.d.text((W - 18, 24), render.fmt_clock(cfg, now), font=mono, fill=c.txt + (255,), anchor="rm")
    c.d.line([(14, 44), (W - 14, 44)], fill=with_alpha(c.acc, 120), width=2)
    y, rh = 62, 29
    for i, (key, name) in enumerate(_LIST_ROWS):
        if key.startswith("cpu"):
            grp, col = cfg.cpu_label, c.acc
        elif key.startswith("gpu") and key != "gpu_vram":
            grp, col = cfg.gpu_label, c.acc2
        else:
            grp, col = "", (120, 230, 140)
        if i % 2 == 0:
            c.d.rectangle([12, y - rh / 2, W - 12, y + rh / 2], fill=(255, 255, 255, 9))
        c.d.text((22, y), f"{grp} {name}".strip().upper(), font=mono, fill=col + (255,), anchor="lm")
        val, unit = fmt_metric(key, m, cfg)
        c.d.text((W - 22, y), f"{val} {unit}".strip(), font=mono, fill=c.txt + (255,), anchor="rm")
        frac = metric_pct(key, m)
        if key in ("net_down", "net_up"):  # rede em escala log (1 B/s … 1 GB/s)
            v = m.get(key)
            frac = None if render.nan(v) else min(1.0, math.log10(max(1.0, v)) / 9)
        if frac is not None:
            c.bar([210, y - 4, 340, y + 4], max(0.0, min(1.0, frac)), col, bg_alpha=30, radius=2)
        y += rh


def theme_cartoes(c):
    """Cartões com gráfico: 6 cartões, cada um com o valor e a curva dos últimos minutos."""
    m, cfg = c.m, c.cfg
    hist = m.get("history", {})
    render.header(c)
    cards = ((f"{cfg.cpu_label} TEMP", "cpu_temp", c.acc, 20, 100),
             (f"{cfg.cpu_label} USO", "cpu_load", c.acc, 0, 100),
             (f"{cfg.gpu_label} TEMP", "gpu_temp", c.acc2, 20, 100),
             (f"{cfg.gpu_label} USO", "gpu_load", c.acc2, 0, 100),
             ("RAM", "ram_load", (120, 230, 140), 0, 100),
             ("DOWNLOAD", "net_down", (255, 190, 60), 0, None))
    boxes = []
    for i, (label, key, col, vmin, vmax) in enumerate(cards):
        x0, y0 = 14 + (i % 2) * 230, 58 + (i // 2) * 140
        box = (x0, y0, x0 + 222, y0 + 132)
        boxes.append(box)
        c.rrect(box, 16, fill=(255, 255, 255, 14), outline=with_alpha(col, 70), width=2)
        vals = list(hist.get(key, []))[-90:]
        if vmax is None:
            good = [v for v in vals if not render.nan(v)]
            vmax = max(good) * 1.15 if good and max(good) > 0 else 1.0
        c.sparkline((box[0] + 10, box[1] + 76, box[2] - 10, box[3] - 10), vals, col, vmin, vmax, fill_alpha=45,
                    width=2)
    c.flush()
    for (label, key, col, *_), box in zip(cards, boxes):
        c.text((box[0] + 14, box[1] + 20), label, 18, col, 700, anchor="lm")
        val, unit = fmt_metric(key, m, cfg)
        color = temp_color(m.get(key), cfg) if key.endswith("temp") else c.txt
        c.value_unit(box[0] + 14, box[1] + 66, val, unit, 42, color, 18, anchor="left")


def theme_termometro(c):
    """Termômetros: dois tubos de vidro que enchem com a temperatura."""
    m, cfg = c.m, c.cfg
    render.header(c)
    top, bot = 84, 340
    for x, label, tk, lk, col in ((150, cfg.cpu_label, "cpu_temp", "cpu_load", c.acc),
                                  (330, cfg.gpu_label, "gpu_temp", "gpu_load", c.acc2)):
        t = m.get(tk)
        frac = max(0.0, min(1.0, metric_pct(tk, m) or 0))
        tc = temp_color(t, cfg)
        c.rrect([x - 22, top, x + 22, bot + 10], 22, fill=(255, 255, 255, 22), outline=(255, 255, 255, 90), width=2)
        c.d.ellipse([x - 44, bot - 6, x + 44, bot + 82], fill=(255, 255, 255, 22), outline=(255, 255, 255, 90),
                    width=2)
        lvl = bot - (bot - top - 16) * frac
        c.rrect([x - 13, lvl, x + 13, bot + 20], 13, fill=tc + (255,))
        c.d.ellipse([x - 35, bot + 3, x + 35, bot + 73], fill=tc + (255,))
        c.d.line([(x - 8, top + 18), (x - 8, bot - 10)], fill=(255, 255, 255, 70), width=4)  # brilho do vidro
        for k in range(8):  # escala 20..90 °C
            yy = bot - (bot - top - 16) * (k * 10) / 75
            c.d.line([(x + 28, yy), (x + 40 if k % 2 == 0 else x + 34, yy)], fill=(255, 255, 255, 140), width=2)
            if k % 2 == 0:
                c.text((x + 46, yy), f"{temp_value(20 + k * 10, cfg.unit):.0f}", 16, c.muted, 600, anchor="lm")
        tv = temp_value(t, cfg.unit)
        c.text((x, bot + 38), "--" if tv is None else f"{tv:.0f}°", 30, (255, 255, 255), 700)
        c.text((x - 60, 66), label, 26, col, 700, anchor="lm")
        c.text((x, 450), f"USO {fmt_pct(m.get(lk))}", 22, c.muted, 600)


def _needle(c, cx, cy, ang_deg, length, width, color, tail=0):
    a = math.radians(ang_deg)
    p1 = (cx - tail * math.cos(a), cy - tail * math.sin(a))
    p2 = (cx + length * math.cos(a), cy + length * math.sin(a))
    c.line_aa([p1, p2], color, width)


def theme_cronografo(c):
    """Relógio de ponteiros estilo cronógrafo, com mostradores pequenos de CPU, GPU e RAM."""
    m, cfg = c.m, c.cfg
    now = m.get("now")
    cx = cy = 240
    c.d.ellipse([18, 18, W - 18, H - 18], fill=(14, 16, 22, 235), outline=with_alpha(c.muted, 120), width=3)
    d = c._layer_draw()
    for i in range(60):
        a = math.radians(-90 + i * 6)
        r1 = 196 if i % 5 == 0 else 206
        col = with_alpha(c.txt if i % 5 == 0 else c.muted, 230)
        d.line([((cx + r1 * math.cos(a)) * 2, (cy + r1 * math.sin(a)) * 2),
                ((cx + 214 * math.cos(a)) * 2, (cy + 214 * math.sin(a)) * 2)], fill=col,
               width=(6 if i % 5 == 0 else 2) * 2)
    subs = ((150, 250, cfg.cpu_label, "cpu_temp", c.acc), (330, 250, cfg.gpu_label, "gpu_temp", c.acc2),
            (240, 350, "RAM", "ram_load", (120, 230, 140)))
    for sx, sy, label, key, col in subs:
        c.arc(sx, sy, 52, 4, 135, 405, c.muted, 90, caps=False)
        frac = max(0.0, min(1.0, metric_pct(key, m) or 0))
        c.arc(sx, sy, 52, 4, 135, 135 + 270 * frac, col, 255, caps=False)
        _needle(c, sx, sy, 135 + 270 * frac, 44, 3, col, 8)
    c.flush()
    for num, xy in (("12", (240, 70)), ("3", (410, 240)), ("9", (70, 240))):
        c.text(xy, num, 30, c.txt, 700)
    for sx, sy, label, key, col in subs:
        val, unit = fmt_metric(key, m, cfg)
        c.text((sx, sy - 18), label, 15, col, 700)
        c.text((sx, sy + 26), f"{val}{unit}", 17, c.txt, 700)
    if now:
        c.text((240, 150), f"{render.WEEK3[now.weekday()]} {now.day:02d}", 18, c.acc, 700)
        mins = now.minute + now.second / 60
        hrs = (now.hour % 12) + mins / 60
        _needle(c, cx, cy, -90 + hrs * 30, 112, 10, c.txt, 16)
        _needle(c, cx, cy, -90 + mins * 6, 170, 6, c.txt, 20)
        _needle(c, cx, cy, -90 + now.second * 6, 196, 2, c.acc, 30)
        c.flush()
    c.d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=c.acc + (255,))
    c.d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(20, 20, 24, 255))


def theme_radar(c):
    """Radar: o feixe gira e cada sensor é um ponto — quanto mais longe do centro, mais alto o valor."""
    m, cfg = c.m, c.cfg
    green = c.acc
    cx, cy, R = 240, 254, 196
    c.d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=(0, 18, 14, 220))
    for r in (R / 3, 2 * R / 3, R):
        c.d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=with_alpha(green, 90), width=2)
    c.d.line([(cx - R, cy), (cx + R, cy)], fill=with_alpha(green, 60), width=1)
    c.d.line([(cx, cy - R), (cx, cy + R)], fill=with_alpha(green, 60), width=1)
    sweep = (time.time() * 90) % 360
    d = c._layer_draw()
    for k in range(18):  # rastro do feixe
        a1 = sweep - k * 3
        d.pieslice([(cx - R) * 2, (cy - R) * 2, (cx + R) * 2, (cy + R) * 2], a1 - 3, a1,
                   fill=with_alpha(green, int(110 * (1 - k / 18) ** 2)))
    c.flush()
    a = math.radians(sweep)
    c.d.line([(cx, cy), (cx + R * math.cos(a), cy + R * math.sin(a))], fill=with_alpha(green, 255), width=3)
    pts = ((cfg.cpu_label, "cpu_load", 200), (cfg.gpu_label, "gpu_load", 300), ("RAM", "ram_load", 60),
           ("VRAM", "gpu_vram", 130), (cfg.cpu_label, "cpu_temp", 250), (cfg.gpu_label, "gpu_temp", 20))
    for label, key, ang in pts:
        frac = max(0.05, min(1.0, metric_pct(key, m) or 0))
        rr = R * frac * 0.92
        px, py = cx + rr * math.cos(math.radians(ang)), cy + rr * math.sin(math.radians(ang))
        since = (sweep - ang) % 360  # o ponto acende quando o feixe passa e vai apagando
        glow = max(0.3, 1 - since / 300)
        col = render.mix((0, 0, 0), (255, 255, 255) if since < 20 else green, glow)
        c.d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=col + (255,))
        val, unit = fmt_metric(key, m, cfg)
        tcol = render.mix(green, (255, 255, 255), 0.6 if since < 20 else 0.0)
        c.text((px + 10, py - 12), f"{label} {val}{unit}", 16, render.mix((0, 0, 0), tcol, max(0.65, glow)), 700,
               anchor="lm")
    render.header(c, y=26)


_vidro_cache: dict = {}


def _vidro_bg(c1, c2):
    k = (c1, c2)
    if k not in _vidro_cache:
        img = Image.new("RGB", (W, H), (10, 10, 18))
        d = Draw(img)
        d.ellipse([-60, -40, 260, 280], fill=c1)
        d.ellipse([240, 200, 560, 520], fill=c2)
        d.ellipse([180, -80, 420, 140], fill=render.mix(c1, c2, 0.5))
        _vidro_cache.clear()
        _vidro_cache[k] = img.filter(ImageFilter.GaussianBlur(70))
    return _vidro_cache[k]


def theme_vidro(c):
    """Vidro fosco: cartões translúcidos que desfocam o que está atrás (fica lindo por cima de vídeo)."""
    m, cfg = c.m, c.cfg
    c.muted = (225, 225, 235)  # unidades legíveis sobre o vidro claro
    if not c.has_media:
        c.img.paste(_vidro_bg(c.acc, c.acc2), (0, 0))
    boxes = ((16, 60, 232, 290), (248, 60, 464, 290), (16, 306, 464, 464))
    for box in boxes:
        reg = c.img.crop(box).convert("RGB").filter(ImageFilter.GaussianBlur(14))
        reg = Image.blend(reg, Image.new("RGB", reg.size, (255, 255, 255)), 0.12)
        mask = Image.new("L", reg.size, 0)
        Draw(mask).rounded_rectangle([0, 0, reg.width - 1, reg.height - 1], 22, fill=255)
        c.img.paste(reg, box[:2], mask)
    c.d = Draw(c.img, "RGBA")
    for box in boxes:
        c.rrect(box, 22, outline=(255, 255, 255, 90), width=2)
    render.header(c)
    white = (255, 255, 255)
    for box, label, tk, lk in ((boxes[0], cfg.cpu_label, "cpu_temp", "cpu_load"),
                               (boxes[1], cfg.gpu_label, "gpu_temp", "gpu_load")):
        x0, y0, x1, _ = box
        c.text((x0 + 20, y0 + 30), label, 26, white, 700, anchor="lm")
        tv, tu = fmt_metric(tk, m, cfg)
        c.value_unit((x0 + x1) / 2, y0 + 140, tv, tu, 84, white, 26)
        c.text(((x0 + x1) / 2, y0 + 178), f"USO {fmt_pct(m.get(lk))}", 22, (235, 235, 245), 600)
        c.bar([x0 + 24, y0 + 200, x1 - 24, y0 + 208], (m.get(lk) or 0) / 100, white, bg_alpha=60)
    x0, y0, x1, _ = boxes[2]
    for i, (label, key) in enumerate((("RAM", "ram_load"), ("VRAM", "gpu_vram"), ("REDE", "net_down"))):
        cxx = x0 + (x1 - x0) * (i + 0.5) / 3
        c.text((cxx, y0 + 40), label, 20, (235, 235, 245), 700)
        val, unit = fmt_metric(key, m, cfg)
        c.value_unit(cxx, y0 + 102, val, unit, 40, white, 18)
        frac = metric_pct(key, m)
        if frac is not None:
            c.bar([cxx - 52, y0 + 124, cxx + 52, y0 + 130], frac, white, bg_alpha=60)


def theme_rpg(c):
    """Janela de RPG 8-bit: os sensores viram barras de vida, desenhado em pixels grandes."""
    m, cfg = c.m, c.cfg
    S = 160
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = Draw(img)
    d.fontmode = "1"  # sem suavização: letras em pixels
    blue, white = (24, 32, 120), (248, 248, 248)
    d.rectangle([3, 3, S - 4, S - 4], fill=blue + (255,), outline=white, width=2)
    d.rectangle([6, 6, S - 7, S - 7], outline=(150, 150, 200))
    f = render.font("Share Tech Mono", 11, 700)
    fb = render.font("Share Tech Mono", 13, 700)
    now = m.get("now")
    d.text((12, 11), "STATUS", font=fb, fill=(255, 220, 80))
    if now:
        d.text((S - 12, 12), now.strftime("%H:%M"), font=f, fill=white, anchor="ra")
    rows = ((cfg.cpu_label, "cpu_temp", "cpu_load"), (cfg.gpu_label, "gpu_temp", "gpu_load"),
            ("RAM", None, "ram_load"), ("VRAM", None, "gpu_vram"))
    y = 34
    for label, tk, lk in rows:
        frac = max(0.0, min(1.0, metric_pct(lk, m) or 0))
        hp = 1 - frac  # a "vida" que sobra
        col = (80, 220, 80) if hp > 0.5 else (240, 200, 40) if hp > 0.2 else (230, 50, 40)
        d.text((12, y), label[:4].upper(), font=f, fill=white)
        right = (fmt_metric(tk, m, cfg)[0] + "° " if tk else "") + f"{frac * 100:.0f}/100"
        d.text((S - 12, y), right, font=f, fill=white, anchor="ra")
        d.rectangle([12, y + 14, S - 13, y + 19], fill=(10, 10, 40), outline=white)
        if hp > 0:
            d.rectangle([13, y + 15, 13 + int((S - 27) * hp), y + 18], fill=col)
        y += 29
    big = img.resize((W, H), Image.NEAREST)
    c.img.paste(big, (0, 0), big)
    c.d = Draw(c.img, "RGBA")


render.THEMES.update({"anel_unico": "Anel único (alterna)", "lista": "Lista de sensores",
                      "cartoes": "Cartões com gráfico", "termometro": "Termômetros", "cronografo": "Cronógrafo",
                      "radar": "Radar", "vidro": "Vidro fosco", "rpg": "RPG 8-bit"})
render.THEME_FUNCS.update({"anel_unico": theme_anel_unico, "lista": theme_lista, "cartoes": theme_cartoes,
                           "termometro": theme_termometro, "cronografo": theme_cronografo, "radar": theme_radar,
                           "vidro": theme_vidro, "rpg": theme_rpg})
render.ANIMATED_THEMES.update({"radar", "cronografo", "anel_unico"})
