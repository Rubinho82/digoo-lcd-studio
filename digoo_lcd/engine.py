"""Motor: gera os quadros e mantém a tela viva (thread única que escreve na porta)."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import logging
import math
import threading
import time

import serial
from PIL import Image

from . import render
from .config import Config
from .protocol import Panel, PanelError, PanelInfo, find_panel
from .sensors import SensorHub
from . import music, sources
from .sources import Encoded, MediaPlayer, ScreenMirror

log = logging.getLogger(__name__)

RECONNECT_DELAY = 2.0
CARD_TOP = 480 - 12 - 118 - 6   # topo do cartão "tocando agora" (as barras ficam logo acima)
PREVIEW_FPS_OFFLINE = 8


def _parse_hhmm(s: str) -> dt.time:
    try:
        h, m = s.strip().split(":")
        return dt.time(int(h) % 24, int(m) % 60)
    except Exception:
        return dt.time(0, 0)


def in_night_window(cfg: Config, now: dt.datetime | None = None) -> bool:
    if not cfg.night_enabled:
        return False
    now_t = (now or dt.datetime.now()).time()
    a, b = _parse_hhmm(cfg.night_start), _parse_hhmm(cfg.night_end)
    if a == b:
        return False
    return a <= now_t < b if a < b else (now_t >= a or now_t < b)


def _q(v):
    """Arredonda o suficiente para não redesenhar por diferenças invisíveis."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
        return None
    if abs(v) < 100:
        return round(v * 2) / 2
    return float(f"{v:.3g}")


def _render_key(m: dict, theme: str):
    now = m.get("now") or dt.datetime.now()
    if theme == "relogio":
        t = (now.hour, now.minute, now.second, now.microsecond // 100_000)  # anel dos segundos anda liso
    else:
        t = (now.hour, now.minute, now.day)
    vals = tuple((k, _q(v)) for k, v in sorted(m.items())
                 if k not in ("history", "now", "uptime", "media", "audio"))
    hist = tuple((k, len(v), _q(v[-1]) if v else None) for k, v in sorted((m.get("history") or {}).items()))
    up = int((m.get("uptime") or 0) // 60)
    return theme, t, vals, hist, up


def to_rgb565(img: Image.Image) -> bytes:
    import numpy as np

    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    v = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
    return v.astype(">u2").tobytes()


class Engine(threading.Thread):
    def __init__(self, cfg: Config, hub: SensorHub):
        super().__init__(daemon=True, name="lcd-engine")
        self.hub = hub
        self._cfg = cfg.copy()
        self._cfg_lock = threading.Lock()
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._cmds: list[str] = []

        self.panel: Panel | None = None
        self.status = "procurando a tela…"
        self.connected = False
        self.info = PanelInfo()
        self.port = ""
        self.paused = False
        self.fps_real = 0.0
        self.frame_kb = 0.0
        self.quality = cfg.jpeg_quality
        self.brightness_now = -1
        self.media_name = ""

        self._preview: Image.Image | None = None
        self._preview_lock = threading.Lock()
        self._smoother = render.Smoother()
        self._media = MediaPlayer()
        self._mirror = ScreenMirror()
        self._last_hash = None
        self._last_send = 0.0
        self._last_key = None       # o que estava na tela da última vez que o painel foi desenhado
        self._last_img: Image.Image | None = None
        self._last_render = 0.0
        self._layer_img = None          # tema como camada transparente (cache)
        self._layer_key = None
        self._layer_t = 0.0
        self._mlayer_img = None         # música como camada transparente (cache)
        self._mlayer_key = None
        self.layer_rects: dict = {}     # onde cada camada está na tela (para arrastar na prévia)
        self._timeouts: list[float] = []
        self._viz = music.AudioViz()
        self._mstate = music.MediaState()
        self._media_now: dict | None = None
        self._dim = 1.0                   # brilho por software (1 = sem escurecer)
        self._lut: list[int] = []
        self._lut_for = -1.0
        self._last_dim = 1.0
        self._last_obj = None             # imagem que gerou o último JPEG
        self._last_payload: bytes | None = None
        self._release_on_stop = True

    # -- API para a interface -------------------------------------------
    @property
    def cfg(self) -> Config:
        with self._cfg_lock:
            return self._cfg

    def set_config(self, cfg: Config) -> None:
        with self._cfg_lock:
            self._cfg = cfg.copy()
        self._last_hash = None
        self._last_key = None
        self._layer_key = None
        self._mlayer_key = None
        self._last_obj = None  # rotação/qualidade podem ter mudado: refaz o JPEG
        self._wake.set()

    def command(self, name: str) -> None:
        self._cmds.append(name)
        self._wake.set()

    def preview(self) -> Image.Image | None:
        with self._preview_lock:
            p = self._preview
        # quadro pronto só é decodificado se a janela estiver pedindo a prévia
        return p.image if isinstance(p, Encoded) else p

    def stop(self, release: bool = True) -> None:
        self._release_on_stop = release
        self._halt.set()
        self._wake.set()
        self.join(timeout=5)

    def _recover_timeout(self) -> bool:
        """A tela parou de aceitar dados: limpa o envio, aborta o quadro pela metade e volta ao modo ao vivo."""
        now = time.monotonic()
        self._timeouts = [t for t in self._timeouts if now - t < 60] + [now]
        p = self.panel
        if p is None or len(self._timeouts) > 4:
            return False
        try:
            ser = p._serial
            if hasattr(ser, "cancel_write"):
                ser.cancel_write()
            ser.reset_output_buffer()
            time.sleep(0.15)
            p.clear()
            time.sleep(0.2)
            p.drain_input()
            p.keepalive(force=True)
            self._last_hash = None
            self._last_obj = None
            return True
        except Exception:
            return False

    def _keepalive_loop(self) -> None:
        while not self._halt.is_set():
            p = self.panel
            if p is not None and not self.paused:
                try:
                    p.keepalive()
                except Exception:
                    pass  # quem trata a queda é o loop principal
            self._halt.wait(0.5)

    # -- loop principal -------------------------------------------------
    def run(self) -> None:
        threading.Thread(target=self._keepalive_loop, daemon=True, name="lcd-keepalive").start()
        next_try = 0.0
        frames = 0
        t_fps = time.monotonic()
        while not self._halt.is_set():
            cfg = self.cfg
            self._apply_hub(cfg)
            self._handle_commands()

            if not self.paused and self.panel is None and time.monotonic() >= next_try:
                if not self._connect(cfg):
                    next_try = time.monotonic() + RECONNECT_DELAY

            t0 = time.monotonic()
            try:
                self._viz.update(self.hub.audio_snapshot())
                self._media_now = self.hub.media_snapshot()
                self._mstate.update(self._media_now)
                sources.AUDIO_BASS[0] = self._viz.bass if cfg.music_react else 0.0
                img = self._compose(cfg)
                img = self._music_layers(cfg, img)
            except Exception:
                log.exception("erro ao desenhar quadro")
                img = None
            if img is not None:
                with self._preview_lock:
                    self._preview = img

            if self.panel is not None and not self.paused and img is not None:
                try:
                    self._sync_brightness(cfg)
                    self._send(cfg, img)
                    self.panel.keepalive()
                    self.panel.drain_input()
                    frames += 1
                except (PanelError, serial.SerialException, OSError) as exc:
                    if "timeout" in str(exc).lower() and self._recover_timeout():
                        log.info("a tela engasgou e foi destravada sem desconectar")
                    else:
                        log.warning("conexão com a tela perdida: %s", exc)
                        self._drop(f"conexão perdida: {exc}")
                    next_try = time.monotonic() + RECONNECT_DELAY

            now = time.monotonic()
            if now - t_fps >= 1.0:
                self.fps_real = frames / (now - t_fps)
                frames = 0
                t_fps = now

            fps = int(cfg.fps)
            if isinstance(img, Encoded):
                fps = max(fps, 30)  # quadro pronto custa quase nada: vídeo/GIF a 30 fps, mais liso
            fps = max(1, min(fps, self.info.max_fps if self.connected else 60))
            if not self.connected or self.paused:
                fps = min(fps, PREVIEW_FPS_OFFLINE)
            wait = 1.0 / fps - (time.monotonic() - t0)
            if wait > 0:
                self._wake.wait(wait)
                self._wake.clear()

        self._shutdown()

    @staticmethod
    def _music_theme_on(cfg: Config) -> bool:
        return (cfg.mode == "painel" and cfg.theme == "musica") or \
               (cfg.mode == "midia" and cfg.media_overlay and cfg.overlay_theme == "musica")

    def _music_needed(self, cfg: Config) -> bool:
        fx = cfg.mode == "midia" and cfg.music_react and any(str(p).startswith("efeito:") for p in cfg.media_paths)
        waves_over = cfg.music_wave_overlay and cfg.music_wave != "off"
        return self._music_theme_on(cfg) or waves_over or fx

    def _apply_hub(self, cfg: Config) -> None:
        self.hub.gpu_select = cfg.gpu_select
        self.hub.pump_sensor = cfg.pump_sensor
        self.hub.fan_sensor = cfg.fan_sensor
        self.hub.interval = max(0.25, float(cfg.sensor_interval))
        self.hub.set_audio(self._music_needed(cfg))
        self.hub.set_gpu_fan(int(cfg.gpu_fan or 0))

    def _handle_commands(self) -> None:
        while self._cmds:
            cmd = self._cmds.pop(0)
            try:
                if cmd == "reconnect":
                    self._drop("reconectando…", release=False)
                elif cmd == "restart_device" and self.panel:
                    self.panel.restart()
                    self._drop("tela reiniciando…", release=False)
                elif cmd == "pause":
                    self.paused = True
                    self._drop("pausado — a tela mostra o conteúdo próprio dela", release=True)
                elif cmd == "resume":
                    self.paused = False
                    self.status = "procurando a tela…"
                elif cmd == "brightness":
                    self.brightness_now = -1
            except Exception as exc:
                log.warning("comando %s falhou: %s", cmd, exc)

    def _connect(self, cfg: Config) -> bool:
        port = find_panel()
        if not port:
            self.status = "tela não encontrada — confira o cabo USB do watercooler"
            return False
        try:
            panel = Panel(port)
        except (serial.SerialException, OSError) as exc:
            msg = str(exc)
            if "Acesso negado" in msg or "Access is denied" in msg or "PermissionError" in msg:
                self.status = f"{port} ocupada por outro programa (feche o app original/outro software)"
            else:
                self.status = f"não abriu {port}: {exc}"
            return False
        try:
            info = panel.connect()
            try:
                panel.set_brightness(100)
                time.sleep(0.1)
                panel.drain_input()
            except Exception:
                pass
            panel.keepalive(force=True)
        except Exception as exc:
            panel.close(release=False)
            self.status = f"a tela não respondeu em {port}: {exc}"
            return False
        self.panel = panel
        self.info = info
        self.port = port
        self.connected = True
        self.brightness_now = -1
        self.quality = cfg.jpeg_quality
        self._last_hash = None
        self.status = f"conectado em {port}"
        log.info("conectado: %s %s fw %s", port, info.model, info.version)
        return True

    def _drop(self, status: str, release: bool = False) -> None:
        if self.panel is not None:
            self.panel.close(release=release)
        self.panel = None
        self.connected = False
        self.status = status

    def _shutdown(self) -> None:
        if self.panel is not None:
            self.panel.close(release=self._release_on_stop)
            self.panel = None
        self._media.close()
        self._mirror.close()

    # -- quadros --------------------------------------------------------
    def _metrics(self, cfg: Config) -> dict:
        m = self.hub.snapshot()
        m["media"] = self._media_now
        m["audio"] = self._viz
        if cfg.smooth:
            fps = max(1, cfg.fps)
            m = self._smoother(m, factor=min(1.0, 5.0 / fps))
        return m

    def _compose(self, cfg: Config) -> Image.Image | None:
        if cfg.mode == "midia":
            self._media.configure(cfg.media_paths, cfg.media_interval, cfg.media_fit,
                                  cfg.colors, cfg.media_speed, cfg.rotation)
            frame = self._media.frame()
            self.media_name = self._media.current_name
            if frame is None:
                return self._dashboard(cfg, self._metrics(cfg), "relogio")
            if cfg.media_overlay:
                bg = (frame.image if isinstance(frame, Encoded) else frame).convert("RGB")
                if cfg.layout_mode != "padrao":
                    return self._layered(cfg, render.apply_scrim(bg, cfg.overlay_scrim), cfg.overlay_theme)
                over = render.render(cfg, self._metrics(cfg), cfg.overlay_theme, media_frame=bg)
                t = max(0, min(95, int(cfg.overlay_transparency))) / 100
                return Image.blend(over, bg, t) if t > 0 else over
            return frame
        if cfg.mode == "espelho":
            try:
                return self._mirror.grab(cfg.mirror_monitor, cfg.mirror_region, cfg.mirror_fit)
            except Exception as exc:
                log.warning("espelho falhou: %s", exc)
                return None
        if cfg.layout_mode != "padrao" and cfg.theme != "musica":
            return self._layered(cfg, render.base_background(cfg), cfg.theme)
        return self._dashboard(cfg, self._metrics(cfg), cfg.theme)

    # -- camadas (mover/redimensionar) ----------------------------------
    def _card_state(self, cfg: Config):
        media = self._media_now
        if self._music_theme_on(cfg) or not (media and media.get("title")):
            return False, 1.0
        if cfg.music_card == "sempre" and media.get("status") == "Playing":
            return True, 1.0
        if cfg.music_card in ("troca", "sempre") and self._mstate.toast_visible():
            return True, self._mstate.toast_progress()
        return False, 1.0

    def _theme_geometry(self, cfg: Config):
        if cfg.layout_mode == "auto":
            return (0.72, 67, 2) if self._card_state(cfg)[0] else (1.0, 0, 0)
        if cfg.layout_mode == "livre":
            return max(0.3, min(1.0, float(cfg.theme_scale))), int(cfg.theme_x), int(cfg.theme_y)
        if cfg.layout_mode == "pilula":
            return 0.86, 34, 66  # abre espaço no topo para a pílula
        return 1.0, 0, 0

    def _music_geometry(self, cfg: Config):
        if cfg.layout_mode == "pilula":
            return "pilula", 1.0, 90, 4
        if cfg.layout_mode == "livre":
            return cfg.music_style, max(0.3, min(1.6, float(cfg.music_scale))), int(cfg.music_x), int(cfg.music_y)
        return "cartao", 1.0, 12, 350

    def _layered(self, cfg: Config, base: Image.Image, theme: str) -> Image.Image:
        m = self._metrics(cfg)
        key = _render_key(m, theme)
        if cfg.clock_format in ("hora_seg", "completo"):
            key = (key, m["now"].second)
        if theme == "musica":
            key = (key, tuple(round(b, 2) for b in self._viz.bands), (self._media_now or {}).get("title"))
        now = time.monotonic()
        gap = 1 / 25 if theme == "musica" and self._viz.active else 0.1
        if self._layer_img is None or (key != self._layer_key and now - self._layer_t >= gap):
            self._layer_img = render.render_layer(cfg, m, theme)
            self._layer_key, self._layer_t = key, now
        scale, x, y = self._theme_geometry(cfg)
        lay = self._layer_img
        t = max(0, min(95, int(cfg.overlay_transparency))) / 100 if cfg.mode == "midia" else 0
        if t > 0:
            a = lay.getchannel("A").point(lambda v: int(v * (1 - t)))
            lay = lay.copy()
            lay.putalpha(a)
        if scale < 0.999:
            lay = lay.resize((int(480 * scale), int(480 * scale)), Image.LANCZOS)
        out = base.convert("RGBA")
        sx, sy, dx, dy = max(0, -x), max(0, -y), max(0, x), max(0, y)  # pode sair um pouco da borda
        w, h = min(lay.width - sx, 480 - dx), min(lay.height - sy, 480 - dy)
        if w > 0 and h > 0:
            out.alpha_composite(lay.crop((sx, sy, sx + w, sy + h)), (dx, dy))
        self.layer_rects["tema"] = (x, y, lay.width, lay.height)
        return out.convert("RGB")

    def _music_widget(self, cfg: Config, img: Image.Image, y_anim: float) -> Image.Image:
        style, scale, x, y = self._music_geometry(cfg)
        media = self._media_now or {}
        key = (style, media.get("title"), media.get("artist"), int(media.get("position") or 0), media.get("status"),
               media.get("cover"), cfg.music_bg_opacity, cfg.music_show_cover, cfg.music_show_progress,
               cfg.music_show_app, cfg.music_auto_colors, round(y_anim, 2))
        if key != self._mlayer_key or self._mlayer_img is None:
            if style == "pilula":
                lay = render.matte(cfg, {}, lambda c: music.draw_pill(c, media, 10, 10, 300))
            else:
                lay = render.matte(cfg, {}, lambda c: music.draw_card(c, media, 1.0))
            bbox = lay.getchannel("A").getbbox()
            self._mlayer_img = lay.crop(bbox) if bbox else lay
            self._mlayer_key = key
        lay = self._mlayer_img
        if scale != 1.0:
            lay = lay.resize((max(1, int(lay.width * scale)), max(1, int(lay.height * scale))), Image.LANCZOS)
        if y_anim < 1.0:  # entra/sai deslizando
            a = lay.getchannel("A").point(lambda v: int(v * y_anim))
            lay = lay.copy()
            lay.putalpha(a)
        out = img.convert("RGBA")
        px, py = max(0, min(480 - 8, x)), max(0, min(480 - 8, y))
        out.alpha_composite(lay.crop((0, 0, min(lay.width, 480 - px), min(lay.height, 480 - py))), (px, py))
        self.layer_rects["musica"] = (px, py, lay.width, lay.height)
        return out.convert("RGB")

    def _music_layers(self, cfg: Config, img):
        """Cartão "tocando agora" e/ou espectro por cima do que estiver na tela."""
        if img is None or cfg.mode == "espelho" or self._music_theme_on(cfg):
            return img
        media = self._media_now
        card, y_anim = False, 1.0
        if media and media.get("title"):
            if cfg.music_card == "sempre" and media.get("status") == "Playing":
                card = True
            elif cfg.music_card in ("troca", "sempre") and self._mstate.toast_visible():
                card, y_anim = True, self._mstate.toast_progress()
        bars = cfg.music_wave_overlay and cfg.music_wave != "off" and self._viz.active
        if not (card or bars):
            return img
        base = (img.image if isinstance(img, Encoded) else img).convert("RGB").copy()
        c = render.Canvas(base, cfg, {})
        if bars:
            ring = (240, 170, 90, 60) if card else (240, 240, 150, 70)
            music.draw_spectrum(c, self._viz, cfg.music_wave, (250 if card else 330, CARD_TOP if card else 476), ring)
            c.flush()
        if card and cfg.layout_mode != "padrao":
            c.flush()
            return self._music_widget(cfg, c.img, y_anim)
        if card:
            music.draw_card(c, media, y_anim)
        c.flush()
        return c.img

    def _dashboard(self, cfg: Config, m: dict, theme: str) -> Image.Image:
        """Só redesenha o painel quando algo visível mudou (desenhar custa ~18 ms)."""
        key = _render_key(m, theme)
        if cfg.clock_format in ("hora_seg", "completo"):
            key = (key, m["now"].second)
        if theme == "musica":  # barras e capa mexem a cada quadro: a chave inclui o som e a música
            med = self._media_now or {}
            playing = med.get("status") == "Playing"
            key = (key, tuple(round(b, 2) for b in self._viz.bands), round(self._viz.bass, 2), med.get("title"),
                   int(med.get("position") or 0), med.get("status"), med.get("cover"),
                   int(time.monotonic() * 25) if playing and cfg.music_layout == "vinil" else 0)  # vinil girando
        now = time.monotonic()
        # no máximo 10 redesenhos por segundo: a animação dos números continua lisa no olho
        min_gap = 1 / 30 if theme == "musica" and self._viz.active else 0.1
        if self._last_img is None or (key != self._last_key and now - self._last_render >= min_gap):
            self._last_img = render.render(cfg, m, theme)
            self._last_key = key
            self._last_render = now
        return self._last_img

    def _sync_brightness(self, cfg: Config) -> None:
        """Brilho por software: esta tela aceita o comando 0x03 (e até guarda o valor), mas a luz de fundo
        não muda — testado. Então a imagem é escurecida antes de ir, com curva de gama para o controle
        ficar "linear" para o olho (15% no controle ≈ 15% de luz)."""
        target = cfg.night_brightness if in_night_window(cfg) else cfg.brightness
        self.brightness_now = max(0, min(100, int(target)))
        self._dim = 1.0 if self.brightness_now >= 100 else (self.brightness_now / 100) ** (1 / 2.2)

    def _dim_lut(self) -> list[int]:
        if self._lut_for != self._dim:
            self._lut = [int(i * self._dim + 0.5) for i in range(256)] * 3
            self._lut_for = self._dim
        return self._lut

    def _send(self, cfg: Config, img: Image.Image) -> None:
        panel = self.panel
        assert panel is not None
        rot = int(cfg.rotation) % 360
        now = time.monotonic()
        if isinstance(img, Encoded):
            if (self._dim >= 1.0 and img.rot == rot and (panel.info.width, panel.info.height) == (480, 480)
                    and not panel.info.is_spi):
                # caminho rápido: o JPEG já está pronto, é só mandar
                if img.key == self._last_hash and now - self._last_send < 1.0:
                    return
                panel.send_frame(img.data)
                self._last_hash, self._last_send = img.key, now
                self.frame_kb = len(img.data) / 1024
                self.quality = 92
                return
            img = img.image
        # mesma imagem de antes (foto parada, painel sem mudança): reaproveita o JPEG já feito
        if img is self._last_obj and self._last_payload is not None and self._last_dim == self._dim:
            if now - self._last_send >= 1.0:
                panel.send_frame(self._last_payload)
                self._last_send = now
            return
        src_obj = img
        if self._dim < 1.0:
            img = img.convert("RGB").point(self._dim_lut())
        if rot:
            img = img.rotate(rot, expand=True)
        if img.size != (panel.info.width, panel.info.height):
            img = img.resize((panel.info.width, panel.info.height), Image.BILINEAR)

        h = hashlib.blake2b(img.tobytes(), digest_size=8).digest()
        if h == self._last_hash and now - self._last_send < 1.0:
            return  # nada mudou: só o keepalive segura a tela

        if panel.info.is_spi:
            payload = to_rgb565(img)
        else:
            kb = int(cfg.max_kb or 0)
            kb = max(20, kb) if kb else panel.info.max_frame_kb  # abaixo de 20 KB a imagem vira borrão
            budget = kb * 1024
            payload = self._encode(img, budget, cfg.jpeg_quality)
        panel.send_frame(payload)
        self._last_hash = h
        self._last_send = now
        self._last_obj, self._last_payload, self._last_dim = src_obj, payload, self._dim
        self.frame_kb = len(payload) / 1024

    def _encode(self, img: Image.Image, budget: int, max_q: int) -> bytes:
        q = min(self.quality, max_q)
        while True:
            buf = io.BytesIO()
            # Sempre 4:2:0: o decodificador da tela (TXW818) mostra 4:4:4 pixelado — testado A/B
            img.save(buf, "JPEG", quality=q, subsampling=2)
            data = buf.getvalue()
            if len(data) <= budget or q <= 30:
                break
            q -= 5
        # adapta: se sobrou folga, tenta subir a qualidade aos poucos
        if len(data) < budget * 0.6 and q < max_q:
            q = min(max_q, q + 2)
        self.quality = q
        return data
