"""Desenho de texto com fonte reserva.

As fontes dos temas (Rajdhani, Orbitron…) só têm letras latinas. Título de música e letra podem vir com ♪,
japonês, coreano, russo, emoji… e o Pillow desenha um quadradinho vazio no lugar do que a fonte não tem.
Aqui o texto é quebrado em pedaços: o que a fonte do tema tem sai nela, o resto sai na primeira fonte do
Windows que tiver o caractere (do mesmo tamanho). Use `Draw(img, "RGBA")` no lugar de `ImageDraw.Draw`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import ImageDraw, ImageFont

_WIN = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
# ordem: latim estendido/cirílico/grego, símbolos (♪ ★ ▶), japonês, coreano, chinês, índico, tailandês, africano
FALLBACKS = [_WIN / n for n in ("segoeuib.ttf", "seguisym.ttf", "YuGothB.ttc", "malgunbd.ttf", "msyhbd.ttc",
                                "Nirmala.ttc", "LeelaUIb.ttf", "LeelawUI.ttf", "ebrimabd.ttf", "gadugib.ttf",
                                "seguiemj.ttf", "simsun.ttc")]
FALLBACKS = [p for p in FALLBACKS if p.exists()]
EMOJI = str(_WIN / "seguiemj.ttf")
_DROP = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF} | set(range(0xFE00, 0xFE10))  # invisíveis e seletores de emoji


@lru_cache(maxsize=64)
def _probe(path: str):
    try:
        return ImageFont.truetype(path, 24)
    except OSError:
        return None


@lru_cache(maxsize=64)
def _notdef(path: str):
    f = _probe(path)
    return None if f is None else _shape(f, "\U000FFFFD")  # caractere que nenhuma fonte tem → desenho do "vazio"


def _shape(f, ch):
    m = f.getmask(ch)
    return m.size, bytes(m)


@lru_cache(maxsize=20000)
def has_glyph(path: str, ch: str) -> bool:
    f = _probe(path)
    if f is None:
        return False
    return _shape(f, ch) != _notdef(path)


@lru_cache(maxsize=256)
def _sized(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


def _font_for(base_path: str, ch: str) -> str | None:
    if ch.isspace() or has_glyph(base_path, ch):
        return base_path
    for p in FALLBACKS:
        if has_glyph(str(p), ch):
            return str(p)
    return None


def runs(text: str, font) -> list[tuple[str, object]]:
    """[(pedaço, fonte)] — vazio se nada precisar de troca é tratado por quem chama."""
    base = getattr(font, "path", None)
    if not base or not text:
        return [(text, font)]
    out: list[list] = []
    for ch in text:
        if ord(ch) in _DROP:
            continue
        if ord(ch) < 0x80:  # ASCII: todas as fontes dos temas têm; poupa a checagem
            p = base
        elif out and _combining(ch):
            out[-1][0] += ch
            continue
        else:
            p = _font_for(base, ch)
            if p is None:
                continue  # nenhuma fonte tem: melhor sumir do que virar quadradinho
        if out and out[-1][1] == p:
            out[-1][0] += ch
        else:
            out.append([ch, p])
    res = []
    for s, p in out:
        f = font if p == base else (_sized(p, int(font.size)) or font)
        res.append((s, f))
    return res or [("", font)]


def _combining(ch: str) -> bool:
    import unicodedata

    return unicodedata.combining(ch) != 0


class FallbackDraw(ImageDraw.ImageDraw):
    def _split(self, text, font):
        if not isinstance(text, str) or "\n" in text or not getattr(font, "path", None):
            return None
        if text.isascii():
            return None
        r = runs(text, font)
        if len(r) == 1 and r[0][1] is font and r[0][0] == text:
            return None
        return r

    def _layout(self, xy, r, anchor):
        anchor = anchor or "la"
        widths = [super(FallbackDraw, self).textlength(s, font=f) for s, f in r]
        total = sum(widths)
        h = anchor[0]
        x = xy[0] - (total if h == "r" else total / 2 if h == "m" else 0)
        pos = []
        for w in widths:
            pos.append(x)
            x += w
        return pos, "l" + anchor[1]

    def text(self, xy, text, fill=None, font=None, anchor=None, *args, **kw):
        r = self._split(text, font)
        if r is None:
            return super().text(xy, text, fill, font, anchor, *args, **kw)
        xs, anc = self._layout(xy, r, anchor)
        for (s, f), x in zip(r, xs):
            extra = dict(kw)
            if getattr(f, "path", "") == EMOJI and self.im.mode in ("RGB", "RGBA"):
                extra["embedded_color"] = True  # emoji colorido
            super().text((x, xy[1]), s, fill, f, anc, **extra)

    def textlength(self, text, font=None, *args, **kw):
        r = self._split(text, font)
        if r is None:
            return super().textlength(text, font, *args, **kw)
        return sum(super(FallbackDraw, self).textlength(s, font=f) for s, f in r)

    def textbbox(self, xy, text, font=None, anchor=None, *args, **kw):
        r = self._split(text, font)
        if r is None:
            return super().textbbox(xy, text, font, anchor, *args, **kw)
        xs, anc = self._layout(xy, r, anchor)
        boxes = [super(FallbackDraw, self).textbbox((x, xy[1]), s, font=f, anchor=anc, **kw)
                 for (s, f), x in zip(r, xs) if s]
        if not boxes:
            return (xy[0], xy[1], xy[0], xy[1])
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def Draw(im, mode=None) -> FallbackDraw:  # noqa: N802 (mesmo nome do ImageDraw.Draw)
    return FallbackDraw(im, mode)
