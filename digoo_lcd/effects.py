"""Efeitos animados gerados ao vivo (numpy) — funcionam offline e usam as cores do tema.

Na playlist aparecem como "efeito:<nome>".
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import resource_dir

W = H = 480
LO = 240  # resolução interna dos efeitos "suaves" (ampliados depois)

EFFECTS = {
    "plasma": "Plasma",
    "matrix": "Chuva Matrix",
    "estrelas": "Viagem nas estrelas",
    "synthwave": "Synthwave",
    "fogo": "Fogo",
    "aurora": "Aurora",
    "tunel": "Túnel neon",
    "galaxia": "Galáxia",
}
PREFIX = "efeito:"


def is_effect(path: str) -> bool:
    return str(path).startswith(PREFIX)


def effect_label(path: str) -> str:
    return EFFECTS.get(str(path)[len(PREFIX):], str(path))


def _rgb(h: str):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def _palette(stops: list) -> np.ndarray:
    """LUT 256x3 interpolando as cores dadas."""
    stops = [np.asarray(s, dtype=np.float32) for s in stops]
    lut = np.zeros((256, 3), np.float32)
    n = len(stops) - 1
    for i in range(256):
        f = i / 255 * n
        k = min(int(f), n - 1)
        t = f - k
        lut[i] = stops[k] * (1 - t) + stops[k + 1] * t
    return lut.clip(0, 255).astype(np.uint8)


class Effect:
    def __init__(self, colors):
        self.acc = _rgb(colors.accent)
        self.acc2 = _rgb(colors.accent2)
        self.bg = _rgb(colors.background)
        y, x = np.mgrid[0:LO, 0:LO].astype(np.float32) / LO
        self.x, self.y = x, y
        self.cx, self.cy = x - 0.5, y - 0.5

    def frame(self, t: float) -> Image.Image:
        raise NotImplementedError

    @staticmethod
    def _up(arr: np.ndarray) -> Image.Image:
        return Image.fromarray(arr, "RGB").resize((W, H), Image.BILINEAR)


class Plasma(Effect):
    def __init__(self, colors):
        super().__init__(colors)
        self.lut = _palette([self.bg, self.acc, (255, 255, 255), self.acc2, self.bg])

    def frame(self, t):
        x, y, cx, cy = self.x * 10, self.y * 10, self.cx, self.cy
        v = (np.sin(x + t) + np.sin((y + t) / 2) + np.sin((x + y + t) / 2)
             + np.sin(np.sqrt(100 * ((cx + 0.3 * math.sin(t / 5)) ** 2 + (cy + 0.3 * math.cos(t / 3)) ** 2) + 1) + t))
        idx = ((v + 4) / 8 * 255 + t * 20).astype(np.int32) % 256
        return self._up(self.lut[idx])


class Tunel(Effect):
    def __init__(self, colors):
        super().__init__(colors)
        r = np.sqrt(self.cx ** 2 + self.cy ** 2) + 1e-3
        self.u0 = 0.25 / r
        self.v0 = np.arctan2(self.cy, self.cx) / math.pi
        self.shade = np.clip(r * 2.6, 0, 1)[..., None]
        self.lut = _palette([self.bg, self.acc, (255, 255, 255), self.acc2])

    def frame(self, t):
        u = self.u0 + t * 0.6
        v = self.v0 + t * 0.05
        pat = (np.sin(u * 18) * 0.5 + 0.5) * (np.cos(v * 8 * math.pi) * 0.5 + 0.5)
        col = self.lut[(pat * 255).astype(np.int32) % 256].astype(np.float32) * self.shade
        return self._up(col.clip(0, 255).astype(np.uint8))


class Aurora(Effect):
    def frame(self, t):
        x = self.x
        img = np.zeros((LO, LO, 3), np.float32) + self.bg * 0.6
        for k, (col, amp, spd, off) in enumerate(((self.acc, 0.10, 0.6, 0.35), (self.acc2, 0.08, 0.45, 0.5),
                                                  (self.acc * 0.5 + 127, 0.06, 0.8, 0.62))):
            curve = off + amp * np.sin(x * (4 + k) + t * spd) + 0.04 * np.sin(x * 13 - t * (1.3 + k * 0.2))
            d = self.y - curve
            band = np.exp(-(d * d) / 0.004) + 0.6 * np.exp(-np.clip(d, 0, None) / 0.12) * (d > 0) * 0.35
            shimmer = 0.85 + 0.15 * np.sin(x * 22 + t * 2 + k)
            img += (band * shimmer)[..., None] * col
        stars = (np.sin(self.x * 917.3) * np.sin(self.y * 513.7) > 0.995) * (self.y < 0.4)
        img += stars[..., None] * 200 * (0.6 + 0.4 * math.sin(t * 2))
        return self._up(img.clip(0, 255).astype(np.uint8))


class Fogo(Effect):
    def __init__(self, colors):
        super().__init__(colors)
        self.n = 160
        self.f = np.zeros((self.n, self.n), np.float32)
        self.lut = _palette([(0, 0, 0), (90, 0, 0), (220, 40, 0), (255, 140, 0), (255, 230, 120), (255, 255, 255)])
        self.rng = np.random.default_rng()
        for _ in range(60):  # já começa com as chamas altas
            self._step()

    def _step(self):
        f = self.f
        f[-1] = self.rng.uniform(0.6, 1.0, self.n)
        below = f[1:]
        avg = (below + np.roll(below, 1, 1) + np.roll(below, -1, 1) + np.vstack([f[2:], f[-1:]])) / 4.0
        f[:-1] = np.clip(avg - self.rng.uniform(0.0015, 0.011, avg.shape), 0, 1)

    def frame(self, t):
        f = self.f
        for _ in range(2):
            f[-1] = self.rng.uniform(0.6, 1.0, self.n)
            below = f[1:]
            avg = (below + np.roll(below, 1, 1) + np.roll(below, -1, 1) + np.vstack([f[2:], f[-1:]])) / 4.0
            f[:-1] = np.clip(avg - self.rng.uniform(0.0015, 0.011, avg.shape), 0, 1)
        arr = self.lut[(f * 255).astype(np.int32)]
        return Image.fromarray(arr, "RGB").resize((W, H), Image.BICUBIC)


class Estrelas(Effect):
    def __init__(self, colors):
        super().__init__(colors)
        self.rng = np.random.default_rng()
        self.n = 700
        self.p = self._spawn(self.n, far=False)
        self.last = None

    def _spawn(self, n, far=True):
        z = np.full(n, 1.0) if far else self.rng.uniform(0.05, 1.0, n)
        return np.stack([self.rng.uniform(-1, 1, n), self.rng.uniform(-1, 1, n), z], 1)

    def frame(self, t):
        dt = 0.016 if self.last is None else max(0.0, min(0.1, t - self.last))
        self.last = t
        p = self.p
        old = p.copy()
        p[:, 2] -= dt * 0.45
        dead = (p[:, 2] <= 0.02)
        p[dead] = self._spawn(int(dead.sum()))
        old[dead] = p[dead]
        img = Image.new("RGB", (W, H), tuple(int(c) for c in self.bg * 0.5))
        d = ImageDraw.Draw(img)
        sx = W / 2 + p[:, 0] / p[:, 2] * W * 0.25
        sy = H / 2 + p[:, 1] / p[:, 2] * H * 0.25
        ox = W / 2 + old[:, 0] / old[:, 2] * W * 0.25
        oy = H / 2 + old[:, 1] / old[:, 2] * H * 0.25
        b = np.clip(1.2 - p[:, 2], 0, 1)
        for i in np.argsort(-p[:, 2]):
            if not (0 <= sx[i] < W and 0 <= sy[i] < H):
                continue
            c = self.acc * (1 - b[i]) * 0.4 + np.array([255, 255, 255]) * b[i]
            col = tuple(int(v) for v in c.clip(0, 255))
            d.line([(ox[i], oy[i]), (sx[i], sy[i])], fill=col, width=1 + int(b[i] * 2.5))
        return img


class Galaxia(Effect):
    def __init__(self, colors):
        super().__init__(colors)
        rng = np.random.default_rng(7)
        n = 6000
        arm = rng.integers(0, 3, n)
        r = rng.power(0.6, n) * 0.95
        self.ang0 = arm * (2 * math.pi / 3) + r * 5.5 + rng.normal(0, 0.28, n) * (1.1 - r)
        self.r = r
        self.mix = np.clip(r, 0, 1)[:, None]
        self.buf = np.zeros((H, W, 3), np.float32)

    def frame(self, t):
        ang = self.ang0 + t * (0.25 / (self.r + 0.25))
        x = (W / 2 + np.cos(ang) * self.r * W * 0.47).astype(np.int32)
        y = (H / 2 + np.sin(ang) * self.r * H * 0.47 * 0.9).astype(np.int32)
        ok = (x >= 0) & (x < W) & (y >= 0) & (y < H)
        col = (self.acc * (1 - self.mix) + self.acc2 * self.mix)[ok]
        self.buf *= 0.86
        np.add.at(self.buf, (y[ok], x[ok]), col * 1.1)
        core = np.exp(-((np.arange(W) - W / 2) ** 2)[None, :] / 900 - ((np.arange(H) - H / 2) ** 2)[:, None] / 900)
        img = self.buf + core[..., None] * 160 + self.bg * 0.4
        return Image.fromarray(img.clip(0, 255).astype(np.uint8), "RGB")


class Matrix(Effect):
    GLYPHS = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄ0123456789ABCDEFZ:+-*<>"
    SIZE = 20

    def __init__(self, colors):
        super().__init__(colors)
        rng = np.random.default_rng()
        self.rng = rng
        try:
            font = ImageFont.truetype("msgothic.ttc", self.SIZE)
        except OSError:
            font = ImageFont.truetype(str(resource_dir() / "assets" / "fonts" / "ShareTechMono.ttf"), self.SIZE)
        self.atlas = []
        for g in self.GLYPHS:
            m = Image.new("L", (self.SIZE, self.SIZE), 0)
            ImageDraw.Draw(m).text((self.SIZE / 2, self.SIZE / 2), g, font=font, fill=255, anchor="mm")
            self.atlas.append(m)
        self.cols = W // self.SIZE
        self.rows = H // self.SIZE + 1
        self.head = rng.uniform(-self.rows, 0, self.cols)
        self.speed = rng.uniform(8, 22, self.cols)
        self.canvas = Image.new("RGB", (W, H), (0, 0, 0))
        self.last = None
        self.green = (40, 255, 110)

    def frame(self, t):
        dt = 0.05 if self.last is None else max(0.0, min(0.2, t - self.last))
        self.last = t
        arr = np.asarray(self.canvas, dtype=np.float32) * (0.90 ** (dt * 20))
        self.canvas = Image.fromarray(arr.astype(np.uint8), "RGB")
        prev = self.head.copy()
        self.head += self.speed * dt
        for c in range(self.cols):
            for row in range(int(prev[c]) + 1, int(self.head[c]) + 1):
                if 0 <= row < self.rows:
                    g = self.atlas[self.rng.integers(len(self.atlas))]
                    pos = (c * self.SIZE, row * self.SIZE)
                    self.canvas.paste(self.green, pos, g)
            r = int(self.head[c])
            if 0 <= r < self.rows:
                self.canvas.paste((235, 255, 240), (c * self.SIZE, r * self.SIZE), self.atlas[self.rng.integers(len(self.atlas))])
            if self.head[c] > self.rows + self.rng.uniform(0, 12):
                self.head[c] = self.rng.uniform(-8, 0)
                self.speed[c] = self.rng.uniform(8, 22)
        return self.canvas


class Synthwave(Effect):
    def __init__(self, colors):
        super().__init__(colors)
        # céu + sol fixos
        yy = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
        top, bottom = np.array([10, 4, 30], np.float32), self.acc2 * 0.55
        sky = np.broadcast_to(top * (1 - yy) + bottom * yy, (H, W, 3))
        self.sky = Image.fromarray(np.ascontiguousarray(sky).clip(0, 255).astype(np.uint8), "RGB")
        sun = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sun)
        cx, cy, r = W // 2, 250, 110
        for i in range(r * 2):
            y = cy - r + i
            half = int(math.sqrt(max(0, r * r - (y - cy) ** 2)))
            k = i / (2 * r)
            col = tuple(int(v) for v in (np.array([255, 230, 90]) * (1 - k) + self.acc2 * k))
            if y > cy - 10 and (y - cy) % 16 < 4 + (y - cy) // 14:
                continue  # listras do sol
            sd.line([(cx - half, y), (cx + half, y)], fill=col + (255,))
        glow = sun.filter(ImageFilter.GaussianBlur(18))
        self.sky.paste(glow, (0, 0), glow)
        self.sky.paste(sun, (0, 0), sun)
        md = ImageDraw.Draw(self.sky)
        pts = [(0, 300)]
        rng = np.random.default_rng(3)
        for x in range(0, W + 40, 40):
            pts.append((x, 300 - rng.uniform(10, 70)))
        pts += [(W, 300)]
        md.polygon(pts, fill=(14, 6, 32))
        self.horizon = 300

    def frame(self, t):
        img = self.sky.copy()
        d = ImageDraw.Draw(img, "RGBA")
        d.rectangle([0, self.horizon, W, H], fill=(8, 2, 22, 255))
        col = tuple(int(c) for c in self.acc) + (230,)
        hz = self.horizon
        for i in range(-12, 13):
            x_far = W / 2 + i * 14
            x_near = W / 2 + i * 120
            d.line([(x_far, hz), (x_near, H)], fill=col, width=2)
        phase = (t * 0.8) % 1.0
        for k in range(14):
            z = (k + 1 - phase)
            y = hz + (H - hz) * (1.0 / (z * 0.55 + 0.45)) ** 1.6 * 0.95
            if hz < y < H:
                d.line([(0, y), (W, y)], fill=col, width=2)
        return img


CLASSES = {
    "plasma": Plasma, "matrix": Matrix, "estrelas": Estrelas, "synthwave": Synthwave,
    "fogo": Fogo, "aurora": Aurora, "tunel": Tunel, "galaxia": Galaxia,
}


def make(path_or_key: str, colors) -> Effect:
    key = path_or_key[len(PREFIX):] if is_effect(path_or_key) else path_or_key
    return CLASSES.get(key, Plasma)(colors)
