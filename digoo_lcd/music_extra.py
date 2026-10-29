"""Mais modelos do tema Música (letra karaokê, fita cassete, aparelho de som…), registrados ao importar."""

from __future__ import annotations

import math
import time

from PIL import Image, ImageDraw

from . import lyrics, music
from .music import H, W, _ellipsize, _round_mask, draw_bars, draw_spectrum, fmt_time
from .render import font, mix


def _wrap(d, text: str, fnt, max_w: float, max_lines: int = 2) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if d.textlength(trial, font=fnt) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [_ellipsize(d, " ".join(lines[max_lines - 1:]), fnt, max_w)]
    return lines


def _text(x, xy, text, size, color, alpha=255, weight=700, anchor="mm"):
    """Texto com transparência (o x.T sempre desenha opaco)."""
    c = x.c
    if alpha >= 255:
        c.d.text(xy, text, font=c.f(size, weight), fill=tuple(color) + (255,), anchor=anchor, **x.sk)
    else:
        c.text_alpha(xy, text, c.f(size, weight), color, int(alpha), anchor, **x.sk)


def _sweep(x, cx, y, text, size, frac, hi, anchor="mm"):
    """Pinta a parte já cantada da linha com a cor de destaque (efeito karaokê)."""
    if frac <= 0 or not text:
        return
    c = x.c
    f = c.f(size, 700)
    w = c.d.textlength(text, font=f)
    x0 = cx - w / 2 if anchor == "mm" else cx
    layer = Image.new("RGBA", (int(w) + 12, size * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((6, size), text, font=f, fill=tuple(hi) + (255,), anchor="lm", **x.sk)
    part = layer.crop((0, 0, int(6 + w * min(1.0, frac)), layer.height))
    c.img.paste(part, (int(x0) - 6, int(y - size)), part)
    c.d = ImageDraw.Draw(c.img, "RGBA")


def _lyr_state(x):
    """(letra, posição) ou (None, mensagem) quando não dá para mostrar."""
    if not x.has:
        return None, "Toque uma música no Spotify ou YouTube"
    lyr = lyrics.for_media(x.media, x.cfg)
    if lyr is None:
        return None, ""
    msg = {"buscando": "Procurando a letra…", "sem": "Letra não encontrada",
           "instrumental": "♪  Instrumental  ♪", "desligado": "Letras desligadas na aba Música"}.get(lyr.state)
    if msg or not lyr.lines:
        return None, msg or "Letra não encontrada"
    pos = float(x.media.get("position") or 0) + float(getattr(x.cfg, "lyrics_offset", 0.0) or 0.0)
    return lyr, pos


def _ease(since: float, synced: bool) -> float:
    if not synced:
        return 1.0
    e = max(0.0, min(1.0, since / 0.35))
    return 1 - (1 - e) ** 3


def _line_text(lyr, k):
    return (lyr.lines[k][1] or "♪") if 0 <= k < len(lyr.lines) else ""


def _header(x, y=18, size=64):
    c = x.c
    cov = x.cover(size)
    tx = 20
    if cov is not None:
        c.img.paste(cov, (20, y), _round_mask(size, 12))
        c.d = ImageDraw.Draw(c.img, "RGBA")
        tx = 20 + size + 14
    x.T((tx, y + size * 0.32), x.title(), 24, c.txt, 700, anchor="lm", max_w=W - tx - 20)
    x.T((tx, y + size * 0.72), x.artist(), 17, c.muted, 600, anchor="lm", max_w=W - tx - 20)


# ----------------------------------------------------------------------
# letras
# ----------------------------------------------------------------------
def _layout_letra(x):
    """Karaokê: a linha atual grande, pintando conforme é cantada; as próximas embaixo."""
    c = x.c
    _header(x)
    lyr, pos = _lyr_state(x)
    top, bottom, cy, gap = 100, 424, 250, 72
    if lyr is None:
        x.T((240, cy), pos, 26, c.muted, 600, max_w=W - 40)
    else:
        dur = float(x.media.get("duration") or 0)
        i, frac, since = lyr.at(pos, dur)
        shift = (1 - _ease(since, lyr.synced)) * gap
        fcur = c.f(32, 700)
        for k in range(i - 2, i + 4):
            y = cy + (k - i) * gap + shift
            if y < top or y > bottom:
                continue
            txt = _line_text(lyr, k) if k >= 0 else ("♪" if k == -1 else "")
            if not txt:
                continue
            if k == i or (i < 0 and k == -1):
                lines = _wrap(c.d, txt, fcur, W - 40, 2)
                fr = frac if lyr.synced else 0.0
                total = sum(len(s) for s in lines) or 1
                done = 0
                for n, ln in enumerate(lines):
                    yy = y + (n - (len(lines) - 1) / 2) * 38
                    _text(x, (240, yy), ln, 32, c.txt, 255)
                    _sweep(x, 240, yy, ln, 32, (fr * total - done) / max(1, len(ln)), c.acc)
                    done += len(ln)
            else:
                dist = abs(k - i)
                a = 170 if dist == 1 else 95
                _text(x, (240, y), _ellipsize(c.d, txt, c.f(24, 600), W - 40), 24, c.txt, a, 600)
    x.progress(40, W - 40, 440, times=False)
    if x.viz is not None and x.wave != "off":
        draw_bars(c, x.viz, 450, 476, "barras" if x.wave == "circulo" else x.wave, 200, 40, W - 40)


def _layout_letra_capa(x):
    """Letra grande alinhada à esquerda sobre a capa desfocada, rolando sozinha."""
    c = x.c
    c.d.rectangle([0, 0, W, H], fill=(0, 0, 0, int(80 * x.op)))
    lyr, pos = _lyr_state(x)
    if lyr is None:
        x.T((240, 200), pos, 28, c.txt, 700, max_w=W - 40)
    else:
        dur = float(x.media.get("duration") or 0)
        i, frac, since = lyr.at(pos, dur)
        fb = c.f(34, 700)
        blocks = {}
        for k in range(max(-1, i - 3), i + 6):
            txt = _line_text(lyr, k) if k >= 0 else "♪"
            if txt:
                blocks[k] = _wrap(c.d, txt, fb, W - 56, 3)

        def height(k):
            return len(blocks.get(k, [""])) * 40 + 22

        prev_h = height(i - 1) if (i - 1) in blocks else 0
        shift = (1 - _ease(since, lyr.synced)) * prev_h
        y = 190 + shift - (height(i) if i in blocks else 0) / 2
        ys = {i: y}
        yy = y
        for k in range(i - 1, i - 4, -1):  # acima
            if k not in blocks:
                break
            yy -= height(k)
            ys[k] = yy
        yy = y + (height(i) if i in blocks else 0)
        for k in range(i + 1, i + 6):  # abaixo
            if k not in blocks:
                break
            ys[k] = yy
            yy += height(k)
        for k, y0 in ys.items():
            if k not in blocks:
                continue
            cur = k == i
            for n, ln in enumerate(blocks[k]):
                ly = y0 + n * 40 + 20
                if ly < 36 or ly > 384:
                    continue
                _text(x, (28, ly), ln, 34, c.txt, 255 if cur else (110 if k < i else 85), anchor="lm")
    c.d.rectangle([0, 396, W, H], fill=(0, 0, 0, int(110 * x.op)))
    cov = x.cover(52)
    tx = 24
    if cov is not None:
        c.img.paste(cov, (24, 404), _round_mask(52, 10))
        c.d = ImageDraw.Draw(c.img, "RGBA")
        tx = 90
    x.T((tx, 418), x.title(), 21, c.txt, 700, anchor="lm", max_w=W - tx - 24)
    x.T((tx, 442), x.artist(), 16, c.muted, 600, anchor="lm", max_w=W - tx - 24)
    x.progress(24, W - 24, 464, times=False)


# ----------------------------------------------------------------------
# aparelhos
# ----------------------------------------------------------------------
_reel = [0.0, time.monotonic()]


def _layout_cassete(x):
    """Fita cassete: os carretéis giram e a fita passa de um lado para o outro com o tempo da música."""
    c = x.c
    now = time.monotonic()
    if x.has and x.media.get("status") == "Playing":
        _reel[0] = (_reel[0] + (now - _reel[1]) * 120) % 360
    _reel[1] = now
    c.d.rounded_rectangle([28, 60, W - 28, 330], 24, fill=(36, 36, 42, 255), outline=(78, 78, 90, 255), width=3)
    for sx, sy in ((44, 76), (W - 44, 76), (44, 314), (W - 44, 314)):  # parafusos
        c.d.ellipse([sx - 6, sy - 6, sx + 6, sy + 6], fill=(90, 90, 100, 255))
        c.d.line([(sx - 4, sy), (sx + 4, sy)], fill=(40, 40, 46, 255), width=2)
    c.d.rounded_rectangle([56, 84, W - 56, 206], 10, fill=(242, 234, 214, 255))
    c.d.rectangle([56, 170, W - 56, 180], fill=c.acc + (255,))
    c.d.rectangle([56, 182, W - 56, 190], fill=c.acc2 + (255,))
    fl = c.f(26, 700)
    c.d.text((240, 114), _ellipsize(c.d, x.title(), fl, W - 140), font=fl, fill=(40, 36, 44, 255), anchor="mm")
    fa = c.f(18, 600)
    c.d.text((240, 146), _ellipsize(c.d, x.artist(), fa, W - 140), font=fa, fill=(90, 84, 90, 255), anchor="mm")
    c.d.text((70, 98), "A", font=c.f(20, 700), fill=(40, 36, 44, 255), anchor="mm")
    c.d.rounded_rectangle([118, 214, W - 118, 290], 36, fill=(16, 16, 20, 255), outline=(70, 70, 80, 255), width=2)
    dur = float(x.media.get("duration") or 0)
    p = (float(x.media.get("position") or 0) / dur) if dur > 0 else 0.3
    d = c._layer_draw()
    for cx, amount, sign in ((176, 1 - p, 1), (304, p, -1)):
        cy = 252
        rt = 16 + 20 * amount
        d.ellipse([(cx - rt) * 2, (cy - rt) * 2, (cx + rt) * 2, (cy + rt) * 2], fill=(74, 48, 34, 255))
        d.ellipse([(cx - 15) * 2, (cy - 15) * 2, (cx + 15) * 2, (cy + 15) * 2], fill=(235, 235, 240, 255))
        for s in range(6):
            a = math.radians(_reel[0] * sign + s * 60)
            d.line([((cx + 5 * math.cos(a)) * 2, (cy + 5 * math.sin(a)) * 2),
                    ((cx + 13 * math.cos(a)) * 2, (cy + 13 * math.sin(a)) * 2)], fill=(60, 60, 66, 255), width=6)
    c.flush()
    c.d.polygon([(130, 330), (150, 300), (W - 150, 300), (W - 130, 330)], fill=(52, 52, 60, 255))
    draw_spectrum(c, x.viz, "barras" if x.wave == "circulo" else x.wave, (350, 418, 30, W - 30))
    x.progress(30, W - 30, 430)
    x.app_line((240, 466), size=16)
    x.clock((W - 30, 30), anchor="rm")


def _vfd(x, box, text, size, col, speed=50):
    """Texto de visor fluorescente; rola sozinho se não couber."""
    c = x.c
    x0, y0, x1, y1 = box
    f = font("Share Tech Mono", size, 700)
    w = c.d.textlength(text, font=f)
    layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    if w <= x1 - x0:
        ld.text(((x1 - x0) / 2, (y1 - y0) / 2), text, font=f, fill=col + (255,), anchor="mm")
    else:
        off = (time.monotonic() * speed) % (w + 60)
        for k in (0, 1):
            ld.text((-off + k * (w + 60), (y1 - y0) / 2), text, font=f, fill=col + (255,), anchor="lm")
    c.img.paste(layer, (x0, y0), layer)
    c.d = ImageDraw.Draw(c.img, "RGBA")


def _layout_aparelho(x):
    """Aparelho de som: visor fluorescente e equalizador de LEDs verde/amarelo/vermelho."""
    c = x.c
    vfd = (90, 255, 225)
    c.d.rounded_rectangle([8, 8, W - 8, H - 8], 26, fill=(14, 14, 17, 255), outline=(60, 60, 68, 255), width=3)
    c.d.rounded_rectangle([24, 24, W - 24, 124], 10, fill=(4, 16, 18, 255), outline=(30, 60, 60, 255), width=2)
    _vfd(x, (34, 30, W - 34, 78), x.title().upper(), 34, vfd)
    _vfd(x, (34, 80, W - 34, 116), x.artist().upper(), 22, mix(vfd, (0, 0, 0), 0.35), speed=35)
    cols, rows = 16, 14
    gx0, gx1, gy0, gy1 = 30, W - 30, 146, 402
    cw = (gx1 - gx0) / cols
    rh = (gy1 - gy0) / rows
    bands = [0.0] * cols
    caps = [0.0] * cols
    if x.viz is not None:
        b, cp = music._gain_bands(c, x.viz)
        per = len(b) // cols
        bands = [max(b[i * per:(i + 1) * per]) for i in range(cols)]
        caps = [max(cp[i * per:(i + 1) * per]) for i in range(cols)]
    for i in range(cols):
        lit = round(bands[i] * rows)
        cap = min(rows - 1, round(caps[i] * rows))
        for r in range(rows):
            col = (40, 230, 90) if r < 8 else (255, 210, 40) if r < 11 else (255, 60, 50)
            on = r < lit or (r == cap and cap > 0)
            y1 = gy1 - r * rh
            c.d.rectangle([gx0 + i * cw + 3, y1 - rh + 3, gx0 + (i + 1) * cw - 3, y1 - 1],
                          fill=col + ((255,) if on else (34,)))
    dur = float(x.media.get("duration") or 0)
    pos = float(x.media.get("position") or 0)
    t = f"{fmt_time(pos)} / {fmt_time(dur)}" if (x.has and dur > 0) else "--:--"
    c.d.text((34, 436), t, font=font("Share Tech Mono", 26, 700), fill=vfd + (255,), anchor="lm")
    if x.has and x.show_app:
        app = music.app_name(x.media.get("app", "")).upper()
        dim = mix(vfd, (0, 0, 0), 0.3) + (255,)
        fa = font("Share Tech Mono", 22, 700)
        c.d.text((W - 34, 436), app, font=fa, fill=dim, anchor="rm")
        ix = W - 34 - c.d.textlength(app, font=fa) - 26  # ▶ / ❚❚ desenhados (a fonte do visor não tem)
        if x.media.get("status") == "Playing":
            c.d.polygon([(ix, 427), (ix + 14, 436), (ix, 445)], fill=dim)
        else:
            c.d.rectangle([ix, 427, ix + 4, 445], fill=dim)
            c.d.rectangle([ix + 9, 427, ix + 13, 445], fill=dim)


_GB = [(15, 56, 15), (48, 98, 48), (139, 172, 15), (155, 188, 15)]


def _layout_pixel(x):
    """Videogame 8-bit: desenhado em 160×160 e ampliado sem suavizar, nas 4 cores de portátil antigo."""
    c = x.c
    S = 160
    img = Image.new("RGBA", (S, S), _GB[3] + (255,))
    d = ImageDraw.Draw(img)
    d.fontmode = "1"  # sem suavização: letras em pixels
    d.rectangle([2, 2, S - 3, S - 3], outline=_GB[0])
    cov = x.cover(160)
    if cov is not None:
        small = cov.convert("L").resize((40, 40), Image.BILINEAR)
        small = small.point(lambda v: 0 if v < 64 else 85 if v < 128 else 170 if v < 192 else 255)
        lut = {0: _GB[0], 85: _GB[1], 170: _GB[2], 255: _GB[3]}
        px = Image.new("RGB", (40, 40))
        px.putdata([lut[v] for v in small.getdata()])
        img.paste(px, (8, 8))
    else:
        d.rectangle([8, 8, 47, 47], fill=_GB[1])
        d.text((28, 28), "♪", font=music._symbol_font(24), fill=_GB[3], anchor="mm")
    fs = font("Share Tech Mono", 11, 700)
    fx = font("Share Tech Mono", 9, 700)
    d.text((54, 12), _ellipsize(d, x.title(), fs, S - 62), font=fs, fill=_GB[0])
    d.text((54, 28), _ellipsize(d, x.artist(), fx, S - 62), font=fx, fill=_GB[1])
    if x.has:
        st = "> PLAY" if x.media.get("status") == "Playing" else "|| PAUSE"
        d.text((54, 40), st, font=fx, fill=_GB[0])
    cols, base, top = 16, 128, 58
    bands = [0.0] * cols
    if x.viz is not None:
        b, _ = music._gain_bands(c, x.viz)
        per = len(b) // cols
        bands = [max(b[i * per:(i + 1) * per]) for i in range(cols)]
    for i in range(cols):
        blocks = round(bands[i] * (base - top) / 4)
        x0 = 9 + i * 9
        for k in range(blocks):
            d.rectangle([x0, base - k * 4 - 3, x0 + 6, base - k * 4 - 1], fill=_GB[0] if k < 9 else _GB[1])
    dur = float(x.media.get("duration") or 0)
    pos = float(x.media.get("position") or 0)
    if x.has and dur > 0:
        n = int(26 * pos / dur)
        for k in range(26):
            d.rectangle([9 + k * 5 + 1, 136, 9 + k * 5 + 3, 139], fill=_GB[0] if k <= n else _GB[2])
        d.text((9, 145), fmt_time(pos), font=fx, fill=_GB[0])
        d.text((S - 9, 145), fmt_time(dur), font=fx, fill=_GB[0], anchor="ra")
    big = img.resize((W, H), Image.NEAREST)
    c.img.paste(big, (0, 0), big)
    c.d = ImageDraw.Draw(c.img, "RGBA")


def _layout_ondas(x):
    """Ondas suaves: quatro ondas que se cruzam, cada uma puxada por uma faixa do som."""
    c = x.c
    t = time.monotonic()
    if x.viz is not None:
        b, _ = music._gain_bands(c, x.viz)
    else:
        b = [0.0] * 48
    low, mid, high = sum(b[:8]) / 8, sum(b[8:24]) / 16, sum(b[24:]) / max(1, len(b) - 24)
    waves = ((low, 1.1, 1.6, c.acc), (mid, 1.9, -2.2, c.acc2), (high, 2.8, 2.9, mix(c.acc, c.acc2, 0.5)),
             ((low + mid) / 2, 1.5, -1.2, c.txt))
    cy = 250
    for amt, freq, speed, col in waves:
        amp = 14 + 120 * min(1.0, amt * 1.4)
        pts = []
        for px in range(0, W + 1, 6):
            env = math.sin(math.pi * px / W) ** 2
            pts.append((px, cy + amp * env * math.sin(freq * 2 * math.pi * px / W + t * speed)))
        c.line_aa(pts, col, 12, 40)
        c.line_aa(pts, col, 3, 230)
    c.flush()
    if x.has:
        c.glow_text((240, 74), _ellipsize(c.d, x.title(), c.f(38, 700), W - 40), 38, c.txt, 700, radius=8,
                    strength=1)
    else:
        x.T((240, 74), x.title(), 34, c.txt, 700)
    x.T((240, 114), x.artist(), 22 if x.has else 16, c.acc, 600, max_w=W - 40)
    x.clock((240, 400), size=26)
    x.progress(40, W - 40, 428)


# ----------------------------------------------------------------------
# polaroide e rádio
# ----------------------------------------------------------------------
def _layout_polaroid(x):
    """Polaroide: capa numa foto instantânea levemente inclinada, título escrito embaixo."""
    c = x.c
    cov = x.cover(250)
    card = Image.new("RGBA", (290, 340), (245, 242, 236, 255))
    if cov is not None:
        card.paste(cov, (20, 20))
    else:
        ImageDraw.Draw(card).rectangle([20, 20, 270, 270], fill=c.acc + (255,))
    d = ImageDraw.Draw(card)
    d.text((145, 300), _ellipsize(d, x.title(), c.f(26, 700), 260), font=c.f(26, 700), fill=(40, 40, 46),
           anchor="mm")
    ang = -4 + 2 * math.sin(time.time() * 0.6) if x.media.get("status") == "Playing" else -4
    rot = card.rotate(ang, Image.BICUBIC, expand=True)
    shadow = Image.new("RGBA", rot.size, (0, 0, 0, 0))
    shadow.putalpha(rot.getchannel("A").point(lambda v: v // 3))
    c.img.paste(shadow, (W // 2 - rot.width // 2 + 8, 30 + 10), shadow)
    c.img.paste(rot, (W // 2 - rot.width // 2, 30), rot)
    c.d = ImageDraw.Draw(c.img, "RGBA")
    x.T((240, 410), x.artist(), 24 if x.has else 16, c.txt, 600, max_w=W - 40)
    x.progress(50, W - 50, 434, times=False)
    draw_spectrum(c, x.viz, x.wave if x.wave != "circulo" else "onda", (444, 476), None)


def _layout_radio(x):
    """Rádio antigo: dial de frequência que anda com a música, alto-falante pulsando com o grave."""
    c = x.c
    wood = (92, 58, 34)
    c.d.rounded_rectangle([20, 40, W - 20, H - 40], 30, fill=wood + (255,), outline=(60, 36, 20, 255), width=6)
    c.d.rounded_rectangle([44, 64, W - 44, 170], 14, fill=(236, 214, 160, 255))
    for i in range(0, 21):  # escala do dial
        xx = 64 + i * (W - 128) / 20
        c.d.line([(xx, 140), (xx, 160 if i % 5 else 128)], fill=(80, 60, 40, 255), width=2)
    dur = float(x.media.get("duration") or 0)
    frac = (float(x.media.get("position") or 0) / dur) if dur > 0 else 0.5
    nx = 64 + frac * (W - 128)
    c.d.line([(nx, 74), (nx, 162)], fill=(200, 30, 30, 255), width=4)
    x.T((240, 98), x.title(), 28, (60, 40, 25), 700, max_w=W - 120)
    x.T((240, 124), x.artist(), 18, (100, 75, 50), 600, max_w=W - 120)
    cx, cy = 160, 300
    r = 92 + (x.viz.bass * 10 if x.viz else 0)
    c.d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(40, 26, 16, 255))
    for k in range(5):  # grade do alto-falante
        rr = r - 10 - k * 16
        if rr > 4:
            c.d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=(110, 80, 50, 255), width=3)
    cov = x.cover(120)
    if cov is not None:
        c.img.paste(cov, (300, 240), _round_mask(120, 12))
        c.d = ImageDraw.Draw(c.img, "RGBA")
    if x.viz is not None and x.wave != "off":
        draw_bars(c, x.viz, 380, 420, "barras", 230, 290, W - 50)
    x.app_line((370, 225), size=16)


music.LAYOUTS.update({"letra": "Letra (karaokê)", "letra_capa": "Letra sobre a capa", "polaroid": "Polaroide",
                      "radio": "Rádio antigo", "cassete": "Fita cassete", "aparelho": "Aparelho de som (LEDs)",
                      "pixel": "Videogame 8-bit", "ondas": "Ondas suaves"})
music.LAYOUT_FUNCS.update({"letra": _layout_letra, "letra_capa": _layout_letra_capa, "polaroid": _layout_polaroid,
                           "radio": _layout_radio, "cassete": _layout_cassete, "aparelho": _layout_aparelho,
                           "pixel": _layout_pixel, "ondas": _layout_ondas})
# modelos que mexem sozinhos (redesenhar ~25x/s enquanto toca, mesmo sem mudar o som)
music.ANIMATED.update({"letra", "letra_capa", "polaroid", "cassete", "aparelho", "ondas"})
