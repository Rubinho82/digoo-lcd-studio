"""Configuração persistente (JSON em %APPDATA%\\DigooLCD\\config.json)."""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def resource_dir() -> Path:
    """Pasta com assets/ e bin/ (funciona rodando do código ou do .exe)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home())) / "DigooLCD"
    base.mkdir(parents=True, exist_ok=True)
    return base


CONFIG_PATH = config_dir() / "config.json"

MODES = ("painel", "midia", "espelho")

# Métricas que os temas e a "Grade" podem mostrar.
METRICS = {
    "cpu_temp": "CPU — temperatura",
    "cpu_load": "CPU — uso",
    "cpu_clock": "CPU — clock",
    "cpu_power": "CPU — consumo",
    "gpu_temp": "GPU — temperatura",
    "gpu_hotspot": "GPU — temp. memória",
    "gpu_load": "GPU — uso",
    "gpu_clock": "GPU — clock",
    "gpu_power": "GPU — consumo",
    "gpu_vram": "GPU — VRAM usada",
    "gpu_fan": "GPU — ventoinha",
    "ram_load": "RAM — uso",
    "ram_used": "RAM — usada (GB)",
    "disk_temp": "SSD — temperatura",
    "pump_rpm": "Bomba — RPM",
    "fan_rpm": "Fan — RPM",
    "net_down": "Rede — download",
    "net_up": "Rede — upload",
    "clock": "Hora",
    "date": "Data",
    "uptime": "Tempo ligado",
}


@dataclass
class Colors:
    accent: str = "#00E5FF"
    accent2: str = "#FF2E88"
    text: str = "#FFFFFF"
    muted: str = "#8A93A6"
    background: str = "#05070D"
    background2: str = "#141A2E"


@dataclass
class Config:
    mode: str = "painel"
    theme: str = "aneis"

    # Tela
    brightness: int = 80
    rotation: int = 90          # graus anti-horário aplicados antes de enviar
    fps: int = 20
    jpeg_quality: int = 95
    max_kb: int = 0             # 0 = limite automático do modelo

    # Painel
    colors: Colors = field(default_factory=Colors)
    font: str = "Rajdhani"
    unit: str = "C"
    clock_24h: bool = True
    cpu_label: str = "CPU"
    gpu_label: str = "GPU"
    title: str = "DIGOO"
    bg_image: str = ""
    bg_scrim: float = 0.45
    grid_slots: list = field(default_factory=lambda: [
        "cpu_temp", "gpu_temp", "cpu_load", "gpu_load", "ram_load", "pump_rpm"])
    smooth: bool = True

    # Mídia
    media_paths: list = field(default_factory=list)
    media_interval: int = 15
    media_fit: str = "cover"    # cover | contain
    media_overlay: bool = False
    overlay_theme: str = "compacto"
    overlay_transparency: int = 0   # % de transparência do tema por cima da mídia (0 = sólido)
    overlay_scrim: float = 0.3      # quanto escurecer a mídia atrás do tema

    # Música (som do PC + "tocando agora")
    music_card: str = "troca"       # nunca | troca (8 s quando a música muda) | sempre (enquanto toca)
    music_bars: str = "off"         # (antigo — virou music_wave + music_wave_overlay)
    music_layout: str = "capa"      # modelo do tema Música: capa | vinil | cartao | minimal | espectro | neon
    music_wave: str = "circulo"     # ondas em todo lugar: off | circulo | barras | onda | espelho
    music_wave_overlay: bool = False  # ondas também por cima de outros temas/vídeos
    music_gain: float = 1.0         # sensibilidade das ondas
    music_auto_colors: bool = False  # cores tiradas da capa do álbum
    music_show_cover: bool = True
    music_show_progress: bool = True
    music_show_app: bool = True
    music_show_clock: bool = False
    music_react: bool = True        # efeitos animados aceleram com o grave
    music_bg_opacity: int = 60      # fundo do tema Música e do cartão: 0 = transparente, 100 = sólido
    music_lyrics: bool = True       # buscar letra sincronizada (LRCLIB) para os modelos de letra
    lyrics_offset: float = 0.0      # segundos; positivo = letra adiantada
    lyrics_translate: bool = False  # traduzir a letra
    lyrics_lang: str = "pt"         # idioma da tradução (translate.LANGS)
    lyrics_provider: str = "mymemory"  # mymemory | deepl | claude
    lyrics_show: str = "ambos"      # ambos (original + tradução) | traducao (só a tradução)
    deepl_key: str = ""
    claude_key: str = ""

    # Posição e tamanho das camadas (tema e música): padrao | auto | pilula | livre
    layout_mode: str = "padrao"
    theme_scale: float = 1.0
    theme_x: int = 0
    theme_y: int = 0
    music_style: str = "cartao"     # cartao | pilula
    music_scale: float = 1.0
    music_x: int = 12
    music_y: int = 350

    # Relógio / título / ventoinha
    clock_format: str = "hora"      # hora | hora_seg | data | data_hora | completo
    show_title: bool = True
    gpu_fan: int = 0                # 0 = automático; 30..100 = % fixo
    media_speed: float = 1.0

    # Biblioteca online
    giphy_key: str = ""
    tenor_key: str = ""
    gif_rating: str = "pg-13"   # g | pg | pg-13 | r
    library_provider: str = "anime"

    # Espelho
    mirror_monitor: int = 1
    mirror_region: list = field(default_factory=list)  # [x, y, w, h] ou vazio
    mirror_fit: str = "contain"

    # Sensores
    gpu_select: str = "auto"
    pump_sensor: str = ""       # id do sensor do LibreHardwareMonitor ou ""
    fan_sensor: str = ""
    sensor_interval: float = 1.0

    # Agenda noturna
    night_enabled: bool = False
    night_start: str = "23:00"
    night_end: str = "07:00"
    night_brightness: int = 15

    # App
    config_version: int = 2
    start_minimized: bool = False
    release_on_exit: bool = True

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        cfg = cls()
        names = {f.name for f in fields(cls)}
        for k, v in (data or {}).items():
            if k == "colors" and isinstance(v, dict):
                cfg.colors = Colors(**{ck: cv for ck, cv in v.items() if ck in Colors.__dataclass_fields__})
            elif k in names:
                setattr(cfg, k, v)
        if cfg.mode not in MODES:
            cfg.mode = "painel"
        cfg.grid_slots = (list(cfg.grid_slots) + Config().grid_slots)[:6]
        if "music_wave" not in (data or {}) and cfg.music_bars not in ("", "off"):
            cfg.music_wave, cfg.music_wave_overlay = cfg.music_bars, True  # opção antiga de ondas por cima
        if int((data or {}).get("config_version", 1)) < 2:
            # v1 mandava JPEG 88 com cor em meia resolução; a tela aguenta bem mais.
            if cfg.jpeg_quality == 88:
                cfg.jpeg_quality = 95
            cfg.config_version = 2
        return cfg

    def copy(self) -> "Config":
        return Config.from_dict(json.loads(json.dumps(self.to_dict())))


_lock = threading.Lock()


def load() -> Config:
    try:
        return Config.from_dict(json.loads(CONFIG_PATH.read_text(encoding="utf8")))
    except FileNotFoundError:
        return Config()
    except Exception:
        # Config corrompida: guarda uma cópia e começa do padrão.
        try:
            CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".bad.json"))
        except Exception:
            pass
        return Config()


def save(cfg: Config) -> None:
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False), encoding="utf8")
        os.replace(tmp, CONFIG_PATH)
