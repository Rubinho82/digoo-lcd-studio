"""Interface (PySide6): painel de controle + ícone na bandeja."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QEvent, QSize, Qt, QTime, QTimer, Signal, QObject
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QRadioButton, QScrollArea, QScrollBar, QSlider, QSpinBox, QSystemTrayIcon,
    QTableWidget, QTableWidgetItem, QTabWidget, QTimeEdit, QVBoxLayout, QWidget,
)

from . import APP_NAME, __version__, config as config_mod, render, system
from .config import Config, METRICS, config_dir, resource_dir
from .effects import effect_label, is_effect
from .engine import Engine
from .gui_library import LibraryTab
from .sensors import SensorHub, pawnio_installed
from .sources import MEDIA_EXT, list_monitors

log = logging.getLogger(__name__)

ICON_PATH = resource_dir() / "assets" / "icon.ico"

STYLE = """
* { font-family: 'Segoe UI'; font-size: 10pt; color: #E6E9F2; }
QMainWindow, QWidget#root { background: #0B0E17; }
QTabWidget::pane { border: 1px solid #1F2638; border-radius: 10px; background: #111522; top: -1px; }
QTabBar::tab { background: transparent; padding: 8px 14px; margin-right: 2px; color: #8A93A6;
               border-top-left-radius: 8px; border-top-right-radius: 8px; }
QTabBar::tab:selected { background: #111522; color: #00E5FF; border: 1px solid #1F2638; border-bottom: none; }
QGroupBox { border: 1px solid #1F2638; border-radius: 10px; margin-top: 14px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: #00E5FF; font-weight: 600; }
QPushButton { background: #1A2033; border: 1px solid #2A3350; border-radius: 8px; padding: 7px 12px; }
QPushButton:hover { background: #222A42; border-color: #00E5FF; }
QPushButton:pressed { background: #0E1220; }
QPushButton:checked { background: #00E5FF; color: #05070D; border-color: #00E5FF; font-weight: 700; }
QPushButton#danger { border-color: #5A2433; }
QPushButton#danger:hover { border-color: #FF3B5C; }
QLineEdit, QSpinBox, QComboBox, QTimeEdit { background: #0E1220; border: 1px solid #2A3350; border-radius: 7px;
               padding: 5px 8px; selection-background-color: #00E5FF; selection-color: #000; }
QComboBox QAbstractItemView { background: #111522; border: 1px solid #2A3350; selection-background-color: #1F3B4D; }
QListWidget, QTableWidget { background: #0E1220; border: 1px solid #1F2638; border-radius: 8px; }
QListWidget::item:selected { background: #1F3B4D; border-radius: 6px; }
QHeaderView::section { background: #151A2A; border: none; padding: 5px; color: #8A93A6; }
QSlider::groove:horizontal { height: 6px; background: #1F2638; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #00E5FF; border-radius: 3px; }
QSlider::handle:horizontal { background: #FFFFFF; width: 16px; margin: -6px 0; border-radius: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #3A4566; border-radius: 5px; background: #0E1220; }
QCheckBox::indicator:hover { border-color: #00E5FF; }
QCheckBox::indicator:checked { background: #00E5FF; border-color: #00E5FF; }
QScrollArea { border: none; background: transparent; }
QLabel#muted { color: #8A93A6; }
QLabel#title { font-size: 16pt; font-weight: 700; color: #FFFFFF; }
QLabel#status { color: #8A93A6; }
QFrame#preview { background: #000; border: 2px solid #1F2638; border-radius: 16px; }
"""


def pil_to_pixmap(img: Image.Image, size: int | None = None) -> QPixmap:
    img = img.convert("RGB")
    if size:
        img = img.resize((size, size), Image.LANCZOS)
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color:#1F2638")
    return f


class WheelGuard(QObject):
    """Rodinha do mouse só mexe em caixas/listas/sliders depois de clicar neles.

    Sem isso, rolar a página de Configurações altera valores sem querer.
    """

    def eventFilter(self, obj, event):
        # QScrollBar também é QAbstractSlider, e é para ela que a área de rolagem repassa a rodinha:
        # bloquear ela travaria a rolagem de todas as listas.
        if (event.type() == QEvent.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox, QAbstractSlider))
                and not isinstance(obj, QScrollBar) and not obj.hasFocus()):
            parent = obj.parentWidget()
            if parent is not None:
                QApplication.sendEvent(parent, event)
            return True
        return False


class _Signals(QObject):
    pawnio_progress = Signal(float)
    pawnio_done = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, engine: Engine, hub: SensorHub):
        super().__init__()
        self.cfg = cfg
        self.engine = engine
        self.hub = hub
        self._quitting = False
        self._tray_hint_shown = False
        self._sig = _Signals()
        self._sig.pawnio_progress.connect(self._on_pawnio_progress)
        self._sig.pawnio_done.connect(self._on_pawnio_done)

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1240, 800)
        self.setAcceptDrops(True)
        self.setStyleSheet(STYLE)
        self._wheel_guard = WheelGuard(self)
        QApplication.instance().installEventFilter(self._wheel_guard)

        self._save_timer = QTimer(self, singleShot=True, interval=600, timeout=self._save)
        self._thumb_timer = QTimer(self, singleShot=True, interval=700, timeout=self._refresh_thumbs)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        lay = QHBoxLayout(root)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(16)
        lay.addWidget(self._build_left(), 0)
        lay.addWidget(self._build_right(), 1)

        self._build_tray()

        self._preview_timer = QTimer(self, interval=100, timeout=self._update_preview)
        self._preview_timer.start()
        self._status_timer = QTimer(self, interval=500, timeout=self._update_status)
        self._status_timer.start()
        self._sensor_timer = QTimer(self, interval=2000, timeout=self._update_sensor_table)
        self._sensor_timer.start()
        # na bandeja: devolve ao Windows a memória que a interface não está usando
        self._trim_timer = QTimer(self, interval=30000, timeout=self._trim_memory)
        self._trim_timer.start()
        QTimer.singleShot(2500, self._refresh_thumbs)
        QTimer.singleShot(3000, self._refresh_fan_choices)
        QTimer.singleShot(8000, self._update_pawnio_label)
        self._sync_mode_buttons()

    # ==================================================================
    # coluna esquerda: prévia + controles rápidos
    # ==================================================================
    def _build_left(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(400)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        t = QLabel(APP_NAME, objectName="title")
        v.addWidget(t)
        self.lbl_device = QLabel("procurando a tela…", objectName="muted")
        self.lbl_device.setWordWrap(True)
        v.addWidget(self.lbl_device)

        frame = QFrame(objectName="preview")
        frame.setFixedSize(400, 400)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(18, 18, 18, 18)
        self.preview = QLabel()
        self.preview.setFixedSize(360, 360)
        self.preview.setAlignment(Qt.AlignCenter)
        fl.addWidget(self.preview, 0, Qt.AlignCenter)
        v.addWidget(frame)

        self.lbl_stats = QLabel("", objectName="status")
        v.addWidget(self.lbl_stats)

        g = QGroupBox("Brilho")
        gl = QHBoxLayout(g)
        self.sld_brightness = QSlider(Qt.Horizontal, minimum=0, maximum=100, value=self.cfg.brightness)
        self.lbl_brightness = QLabel(f"{self.cfg.brightness}%")
        self.lbl_brightness.setFixedWidth(42)
        self.sld_brightness.valueChanged.connect(self._on_brightness)
        gl.addWidget(self.sld_brightness)
        gl.addWidget(self.lbl_brightness)
        v.addWidget(g)

        row = QHBoxLayout()
        self.btn_pause = QPushButton("Pausar tela")
        self.btn_pause.setToolTip("Devolve a tela para o conteúdo próprio dela (o que vinha de fábrica)")
        self.btn_pause.clicked.connect(self._toggle_pause)
        row.addWidget(self.btn_pause)
        b = QPushButton("Reconectar")
        b.clicked.connect(lambda: self.engine.command("reconnect"))
        row.addWidget(b)
        v.addLayout(row)
        v.addStretch(1)
        return w

    # ==================================================================
    # coluna direita: modo + abas
    # ==================================================================
    def _build_right(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        modes = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for key, label in (("painel", "Painel de sensores"), ("midia", "Fotos / GIF / Vídeo"), ("espelho", "Espelhar tela")):
            b = QPushButton(label, checkable=True)
            b.setMinimumHeight(40)
            b.clicked.connect(lambda _=False, k=key: self._set_mode(k))
            self.mode_group.addButton(b)
            self.mode_buttons[key] = b
            modes.addWidget(b)
        v.addLayout(modes)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_themes(), "Temas")
        self.tabs.addTab(self._tab_custom(), "Personalizar")
        self.tabs.addTab(self._tab_library(), "Biblioteca")
        self.tabs.addTab(self._tab_music(), "Música")
        self.tabs.addTab(self._tab_media(), "Playlist")
        self.tabs.addTab(self._tab_mirror(), "Espelho")
        self._sensor_tab = self._tab_sensors()
        self.tabs.addTab(self._sensor_tab, "Sensores")
        self.tabs.addTab(self._tab_settings(), "Configurações")
        v.addWidget(self.tabs, 1)
        return w

    @staticmethod
    def _scroll(inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(inner)
        inner.setStyleSheet("background: transparent;")
        return sa

    # -- Temas ----------------------------------------------------------
    def _tab_themes(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        g = QGroupBox("Onde mostrar o tema")
        gl = QGridLayout(g)
        self.rb_alone = QRadioButton("Sozinho na tela")
        self.rb_over = QRadioButton("Por cima da minha mídia (foto/GIF/vídeo da Playlist)")
        (self.rb_over if self.cfg.media_overlay else self.rb_alone).setChecked(True)
        self.rb_alone.toggled.connect(self._on_place_changed)
        gl.addWidget(self.rb_alone, 0, 0)
        gl.addWidget(self.rb_over, 0, 1, 1, 3)
        gl.addWidget(QLabel("Transparência do tema"), 1, 0)
        self.sld_trans = QSlider(Qt.Horizontal, minimum=0, maximum=90, value=int(self.cfg.overlay_transparency))
        self.lbl_trans = QLabel(f"{self.cfg.overlay_transparency}%")
        self.lbl_trans.setFixedWidth(42)
        self.sld_trans.valueChanged.connect(self._on_trans)
        gl.addWidget(self.sld_trans, 1, 1, 1, 2)
        gl.addWidget(self.lbl_trans, 1, 3)
        gl.addWidget(QLabel("Escurecer a mídia atrás"), 2, 0)
        self.sld_oscrim = QSlider(Qt.Horizontal, minimum=0, maximum=90, value=int(self.cfg.overlay_scrim * 100))
        self.lbl_oscrim = QLabel(f"{int(self.cfg.overlay_scrim * 100)}%")
        self.lbl_oscrim.setFixedWidth(42)
        self.sld_oscrim.valueChanged.connect(self._on_oscrim)
        gl.addWidget(self.sld_oscrim, 2, 1, 1, 2)
        gl.addWidget(self.lbl_oscrim, 2, 3)
        v.addWidget(g)
        self._sync_place_widgets()

        g = QGroupBox("Posição e tamanho — arraste na prévia à esquerda; rodinha do mouse = tamanho")
        gl = QGridLayout(g)
        self.layout_btns = {}
        for i, (key, label) in enumerate((("padrao", "Padrão"), ("auto", "Encaixe automático"),
                                          ("pilula", "Música em pílula"), ("livre", "Livre"))):
            b = QPushButton(label, checkable=True)
            b.clicked.connect(lambda _=False, k=key: self._set_layout(k))
            gl.addWidget(b, 0, i)
            self.layout_btns[key] = b
        self.lay_w = {}
        rows = (("theme_scale", "Tamanho do tema", 30, 100), ("theme_x", "Tema ←→", -240, 480),
                ("theme_y", "Tema ↑↓", -240, 480), ("music_scale", "Tamanho da música", 30, 160),
                ("music_x", "Música ←→", 0, 470), ("music_y", "Música ↑↓", 0, 470))
        for n, (key, label, lo, hi) in enumerate(rows):
            sl = QSlider(Qt.Horizontal, minimum=lo, maximum=hi)
            sl.valueChanged.connect(lambda val, k=key: self._on_layout_value(k, val))
            gl.addWidget(QLabel(label), 1 + n // 2, (n % 2) * 2)
            gl.addWidget(sl, 1 + n // 2, (n % 2) * 2 + 1)
            self.lay_w[key] = sl
        self.cb_mstyle = QComboBox()
        self.cb_mstyle.addItem("Cartão grande", "cartao")
        self.cb_mstyle.addItem("Pílula compacta", "pilula")
        self.cb_mstyle.currentIndexChanged.connect(lambda _: self._on_layout_value("music_style",
                                                                                   self.cb_mstyle.currentData()))
        gl.addWidget(QLabel("Música como"), 4, 0)
        gl.addWidget(self.cb_mstyle, 4, 1)
        v.addWidget(g)
        self._sync_layout_widgets()
        self.preview.installEventFilter(self)
        self.preview.setMouseTracking(True)
        self._drag = None

        v.addWidget(QLabel("Clique num tema para usar na tela. As miniaturas mostram seus dados de verdade.", objectName="muted"))
        self.theme_list = QListWidget()
        self.theme_list.setViewMode(QListWidget.IconMode)
        self.theme_list.setIconSize(QSize(170, 170))
        self.theme_list.setGridSize(QSize(196, 214))
        self.theme_list.setResizeMode(QListWidget.Adjust)
        self.theme_list.setMovement(QListWidget.Static)
        self.theme_list.setSpacing(6)
        for key, name in render.THEMES.items():
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, key)
            self.theme_list.addItem(it)
            if key == self.cfg.theme:
                it.setSelected(True)
        self.theme_list.itemClicked.connect(self._on_theme_clicked)
        self.theme_list.setMinimumHeight(660)
        v.addWidget(self.theme_list, 1)

        g = QGroupBox("O que aparece em cada quadro do tema \"Grade 6\"")
        gl = QGridLayout(g)
        self.slot_combos = []
        for i in range(6):
            cb = QComboBox()
            for k, label in METRICS.items():
                cb.addItem(label, k)
            cb.setCurrentIndex(max(0, cb.findData(self.cfg.grid_slots[i])))
            cb.currentIndexChanged.connect(lambda _=0, idx=i, c=cb: self._on_slot(idx, c.currentData()))
            gl.addWidget(QLabel(f"{i + 1}"), i // 2, (i % 2) * 2)
            gl.addWidget(cb, i // 2, (i % 2) * 2 + 1)
            self.slot_combos.append(cb)
        v.addWidget(g)
        return self._scroll(w)

    def _on_theme_clicked(self, item: QListWidgetItem):
        key = item.data(Qt.UserRole)
        if self.rb_over.isChecked():
            self.cfg.overlay_theme = key
            self.cfg.media_overlay = True
            if not self.cfg.media_paths:
                QMessageBox.information(self, "Tema por cima", "Sua Playlist está vazia. Escolha uma foto, GIF ou "
                                        "vídeo na aba Biblioteca (ou Playlist) e o tema aparece por cima.")
            self._set_mode("midia")
        else:
            self.cfg.theme = key
            self._set_mode("painel")

    def _current_geometry(self):
        e = self.engine
        ts, tx, ty = e._theme_geometry(self.cfg)
        ms, msc, mx, my = e._music_geometry(self.cfg)
        return {"theme_scale": ts, "theme_x": tx, "theme_y": ty, "music_style": ms, "music_scale": msc,
                "music_x": mx, "music_y": my}

    def _set_layout(self, mode: str):
        if mode == "livre" and self.cfg.layout_mode != "livre":
            for k, val in self._current_geometry().items():  # começa de onde está agora
                setattr(self.cfg, k, val)
        self.cfg.layout_mode = mode
        self._sync_layout_widgets()
        self._changed()

    def _on_layout_value(self, key: str, val):
        if getattr(self, "_syncing_layout", False):
            return
        if self.cfg.layout_mode != "livre":
            for k, v in self._current_geometry().items():
                setattr(self.cfg, k, v)
            self.cfg.layout_mode = "livre"
        if key in ("theme_scale", "music_scale"):
            val = val / 100
        setattr(self.cfg, key, val)
        self._sync_layout_widgets()
        self._changed()

    def _sync_layout_widgets(self):
        self._syncing_layout = True
        try:
            for k, b in self.layout_btns.items():
                b.setChecked(k == self.cfg.layout_mode)
            geo = self._current_geometry() if self.cfg.layout_mode != "livre" else \
                {k: getattr(self.cfg, k) for k in self.lay_w} | {"music_style": self.cfg.music_style}
            for k, sl in self.lay_w.items():
                v = geo[k]
                sl.setValue(int(v * 100) if k in ("theme_scale", "music_scale") else int(v))
            self.cb_mstyle.setCurrentIndex(max(0, self.cb_mstyle.findData(geo["music_style"])))
        finally:
            self._syncing_layout = False

    def eventFilter(self, obj, event):
        """Arrastar camadas na prévia (e mudar o tamanho com a rodinha)."""
        if obj is not getattr(self, "preview", None):
            return False
        et = event.type()
        k = 480 / max(1, self.preview.width())
        if et == QEvent.MouseButtonPress:
            pos = event.position()
            px, py = pos.x() * k, pos.y() * k
            for name in ("musica", "tema"):
                r = self.engine.layer_rects.get(name)
                if r and r[0] <= px <= r[0] + r[2] and r[1] <= py <= r[1] + r[3]:
                    self._set_layout("livre") if self.cfg.layout_mode != "livre" else None
                    key = "music" if name == "musica" else "theme"
                    self._drag = (key, px, py, getattr(self.cfg, key + "_x"), getattr(self.cfg, key + "_y"))
                    return True
        elif et == QEvent.MouseMove and self._drag:
            key, sx, sy, x0, y0 = self._drag
            pos = event.position()
            setattr(self.cfg, key + "_x", int(x0 + pos.x() * k - sx))
            setattr(self.cfg, key + "_y", int(y0 + pos.y() * k - sy))
            self._sync_layout_widgets()
            self._changed()
            return True
        elif et == QEvent.MouseButtonRelease and self._drag:
            self._drag = None
            return True
        elif et == QEvent.Wheel:
            pos = event.position()
            px, py = pos.x() * k, pos.y() * k
            for name in ("musica", "tema"):
                r = self.engine.layer_rects.get(name)
                if r and r[0] <= px <= r[0] + r[2] and r[1] <= py <= r[1] + r[3]:
                    if self.cfg.layout_mode != "livre":
                        self._set_layout("livre")
                    key = "music_scale" if name == "musica" else "theme_scale"
                    step = 0.05 if event.angleDelta().y() > 0 else -0.05
                    hi = 1.6 if name == "musica" else 1.0
                    setattr(self.cfg, key, round(max(0.3, min(hi, getattr(self.cfg, key) + step)), 2))
                    self._sync_layout_widgets()
                    self._changed()
                    return True
        return False

    def _on_place_changed(self, *_):
        over = self.rb_over.isChecked()
        self.cfg.media_overlay = over
        self._sync_place_widgets()
        sel = self.cfg.overlay_theme if over else self.cfg.theme
        for i in range(self.theme_list.count()) if hasattr(self, "theme_list") else []:
            it = self.theme_list.item(i)
            it.setSelected(it.data(Qt.UserRole) == sel)
        if over and self.cfg.media_paths:
            self._set_mode("midia")
        elif not over:
            self._set_mode("painel")
        else:
            self._changed()

    def _sync_place_widgets(self):
        on = self.rb_over.isChecked()
        for wdg in (self.sld_trans, self.lbl_trans, self.sld_oscrim, self.lbl_oscrim):
            wdg.setEnabled(on)

    def _on_trans(self, val: int):
        self.lbl_trans.setText(f"{val}%")
        self._set("overlay_transparency", val)

    def _on_oscrim(self, val: int):
        self.lbl_oscrim.setText(f"{val}%")
        self._set("overlay_scrim", val / 100)

    def _on_slot(self, idx: int, key: str):
        self.cfg.grid_slots[idx] = key
        self._changed(thumbs=True)

    def _refresh_thumbs(self):
        if hasattr(self, "library"):
            self.library.colors_changed()
        if hasattr(self, "music_list"):
            self._refresh_music_thumbs()
        m = self.hub.snapshot()
        for i in range(self.theme_list.count()):
            it = self.theme_list.item(i)
            try:
                img = render.render(self.cfg, m, it.data(Qt.UserRole))
                it.setIcon(QIcon(pil_to_pixmap(img, 170)))
            except Exception:
                log.exception("miniatura")

    # -- Personalizar ---------------------------------------------------
    def _tab_custom(self) -> QWidget:
        inner = QWidget()
        v = QVBoxLayout(inner)

        g = QGroupBox("Cores")
        gl = QGridLayout(g)
        self.color_buttons = {}
        names = [("accent", "Destaque 1 (CPU)"), ("accent2", "Destaque 2 (GPU)"), ("text", "Texto"),
                 ("muted", "Texto secundário"), ("background", "Fundo (borda)"), ("background2", "Fundo (centro)")]
        for i, (key, label) in enumerate(names):
            b = QPushButton()
            b.setFixedSize(64, 28)
            b.clicked.connect(lambda _=False, k=key: self._pick_color(k))
            self.color_buttons[key] = b
            gl.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            gl.addWidget(b, i // 2, (i % 2) * 2 + 1)
        self._paint_color_buttons()
        presets = QHBoxLayout()
        for name, acc, acc2 in (("Ciano/Rosa", "#00E5FF", "#FF2E88"), ("Verde/Roxo", "#39FF88", "#A259FF"),
                                ("Laranja/Azul", "#FF8A00", "#2E9BFF"), ("Vermelho", "#FF3B3B", "#FFB800"),
                                ("Branco", "#FFFFFF", "#9AA4B8")):
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, a=acc, a2=acc2: self._preset(a, a2))
            presets.addWidget(b)
        gl.addLayout(presets, 3, 0, 1, 4)
        v.addWidget(g)

        g = QGroupBox("Texto")
        fl = QFormLayout(g)
        self.cb_font = QComboBox()
        self.cb_font.addItems(list(render.FONTS))
        self.cb_font.setCurrentText(self.cfg.font)
        self.cb_font.currentTextChanged.connect(lambda t: self._set("font", t, thumbs=True))
        fl.addRow("Fonte", self.cb_font)
        self.ed_title = QLineEdit(self.cfg.title)
        self.ed_title.textChanged.connect(lambda t: self._set("title", t, thumbs=True))
        fl.addRow("Título no topo", self.ed_title)
        self.ed_cpu = QLineEdit(self.cfg.cpu_label)
        self.ed_cpu.textChanged.connect(lambda t: self._set("cpu_label", t, thumbs=True))
        fl.addRow("Nome da CPU", self.ed_cpu)
        self.ed_gpu = QLineEdit(self.cfg.gpu_label)
        self.ed_gpu.textChanged.connect(lambda t: self._set("gpu_label", t, thumbs=True))
        fl.addRow("Nome da GPU", self.ed_gpu)
        self.cb_unit = QComboBox()
        self.cb_unit.addItem("Celsius (°C)", "C")
        self.cb_unit.addItem("Fahrenheit (°F)", "F")
        self.cb_unit.setCurrentIndex(0 if self.cfg.unit == "C" else 1)
        self.cb_unit.currentIndexChanged.connect(lambda _: self._set("unit", self.cb_unit.currentData(), thumbs=True))
        fl.addRow("Temperatura", self.cb_unit)
        self.chk_24h = QCheckBox("Relógio 24 horas")
        self.chk_24h.setChecked(self.cfg.clock_24h)
        self.chk_24h.toggled.connect(lambda b: self._set("clock_24h", b, thumbs=True))
        fl.addRow("", self.chk_24h)
        self.cb_clockfmt = QComboBox()
        for key, label in render.CLOCK_FORMATS.items():
            self.cb_clockfmt.addItem(label, key)
        self.cb_clockfmt.setCurrentIndex(max(0, self.cb_clockfmt.findData(self.cfg.clock_format)))
        self.cb_clockfmt.currentIndexChanged.connect(lambda _: self._set("clock_format",
                                                                         self.cb_clockfmt.currentData(), thumbs=True))
        fl.addRow("Relógio do topo", self.cb_clockfmt)
        self.chk_title = QCheckBox("Mostrar o título no topo (ex.: DIGOO)")
        self.chk_title.setChecked(self.cfg.show_title)
        self.chk_title.toggled.connect(lambda on: self._set("show_title", on, thumbs=True))
        fl.addRow("", self.chk_title)
        self.chk_smooth = QCheckBox("Animação suave dos números e ponteiros")
        self.chk_smooth.setChecked(self.cfg.smooth)
        self.chk_smooth.toggled.connect(lambda b: self._set("smooth", b))
        fl.addRow("", self.chk_smooth)
        v.addWidget(g)

        g = QGroupBox("Imagem de fundo do painel")
        gl = QGridLayout(g)
        self.lbl_bg = QLabel(self._short(self.cfg.bg_image) or "nenhuma (usa o degradê das cores)", objectName="muted")
        gl.addWidget(self.lbl_bg, 0, 0, 1, 3)
        b = QPushButton("Escolher imagem…")
        b.clicked.connect(self._pick_bg)
        gl.addWidget(b, 1, 0)
        b = QPushButton("Remover")
        b.clicked.connect(lambda: self._set_bg(""))
        gl.addWidget(b, 1, 1)
        gl.addWidget(QLabel("Escurecer fundo"), 2, 0)
        self.sld_scrim = QSlider(Qt.Horizontal, minimum=0, maximum=90, value=int(self.cfg.bg_scrim * 100))
        self.sld_scrim.valueChanged.connect(lambda val: self._set("bg_scrim", val / 100, thumbs=True))
        gl.addWidget(self.sld_scrim, 2, 1, 1, 2)
        v.addWidget(g)
        v.addStretch(1)
        return self._scroll(inner)

    def _paint_color_buttons(self):
        for key, b in self.color_buttons.items():
            c = getattr(self.cfg.colors, key)
            b.setStyleSheet(f"background:{c}; border:1px solid #2A3350; border-radius:6px;")

    def _pick_color(self, key: str):
        cur = QColor(getattr(self.cfg.colors, key))
        c = QColorDialog.getColor(cur, self, "Escolher cor")
        if c.isValid():
            setattr(self.cfg.colors, key, c.name().upper())
            self._paint_color_buttons()
            self._changed(thumbs=True)

    def _preset(self, acc: str, acc2: str):
        self.cfg.colors.accent = acc
        self.cfg.colors.accent2 = acc2
        self._paint_color_buttons()
        self._changed(thumbs=True)

    def _pick_bg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Imagem de fundo", "", "Imagens (*.png *.jpg *.jpeg *.bmp *.webp)")
        if path:
            self._set_bg(path)

    def _set_bg(self, path: str):
        self.cfg.bg_image = path
        self.lbl_bg.setText(self._short(path) or "nenhuma (usa o degradê das cores)")
        self._changed(thumbs=True)

    # -- Mídia ----------------------------------------------------------
    def _tab_media(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Fotos, GIFs, vídeos e efeitos tocam em sequência. Pastas inteiras também valem. "
                           "Dica: arraste arquivos para a janela ou use a aba Biblioteca.", objectName="muted"))
        self.media_list = QListWidget()
        self.media_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.media_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.media_list.model().rowsMoved.connect(lambda *_: self._media_sync())
        for p in self.cfg.media_paths:
            self._media_append(p)
        v.addWidget(self.media_list, 1)
        row = QHBoxLayout()
        for label, fn in (("Adicionar arquivos…", self._media_add), ("Adicionar pasta…", self._media_add_dir),
                          ("Remover", self._media_remove), ("Subir", lambda: self._media_move(-1)),
                          ("Descer", lambda: self._media_move(1))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)

        g = QGroupBox("Reprodução")
        fl = QFormLayout(g)
        self.sp_interval = QSpinBox(minimum=1, maximum=3600, value=int(self.cfg.media_interval), suffix=" s")
        self.sp_interval.valueChanged.connect(lambda val: self._set("media_interval", val))
        fl.addRow("Tempo de cada item", self.sp_interval)
        sl = QHBoxLayout()
        self.sld_speed = QSlider(Qt.Horizontal, minimum=25, maximum=300, value=int(self.cfg.media_speed * 100))
        self.lbl_speed = QLabel(f"{self.cfg.media_speed:.2f}×")
        self.lbl_speed.setFixedWidth(48)
        self.sld_speed.valueChanged.connect(self._on_speed)
        sl.addWidget(self.sld_speed)
        sl.addWidget(self.lbl_speed)
        fl.addRow("Velocidade de GIF/vídeo", sl)
        self.cb_fit = QComboBox()
        self.cb_fit.addItem("Preencher (corta as bordas)", "cover")
        self.cb_fit.addItem("Caber inteiro (faixas pretas)", "contain")
        self.cb_fit.setCurrentIndex(0 if self.cfg.media_fit == "cover" else 1)
        self.cb_fit.currentIndexChanged.connect(lambda _: self._set("media_fit", self.cb_fit.currentData()))
        fl.addRow("Enquadramento", self.cb_fit)
        hint = QLabel("Para mostrar um tema com os sensores por cima da mídia, vá na aba Temas → "
                      "\"Por cima da minha mídia\" (dá para ajustar a transparência).", objectName="muted")
        hint.setWordWrap(True)
        fl.addRow(hint)
        v.addWidget(g)
        b = QPushButton("Mostrar mídia na tela")
        b.setMinimumHeight(36)
        b.clicked.connect(lambda: self._set_mode("midia"))
        v.addWidget(b)
        return w

    def _media_append(self, path: str):
        if is_effect(path):
            label = f"✦ Efeito: {effect_label(path)}"
        else:
            label = path
        it = QListWidgetItem(label)
        it.setData(Qt.UserRole, path)
        it.setToolTip(path)
        self.media_list.addItem(it)

    def _media_sync(self):
        self.cfg.media_paths = [self.media_list.item(i).data(Qt.UserRole) for i in range(self.media_list.count())]
        self._changed()

    def _on_speed(self, val: int):
        self.lbl_speed.setText(f"{val / 100:.2f}×")
        self._set("media_speed", val / 100)

    def _media_add(self):
        exts = " ".join(f"*{e}" for e in sorted(MEDIA_EXT))
        paths, _ = QFileDialog.getOpenFileNames(self, "Adicionar mídia", "", f"Mídia ({exts})")
        for p in paths:
            self._media_append(p)
        if paths:
            self._media_sync()

    def _media_add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Adicionar pasta")
        if d:
            self._media_append(d)
            self._media_sync()

    # -- Biblioteca -----------------------------------------------------
    # -- Música ----------------------------------------------------------
    def _tab_music(self) -> QWidget:
        from . import music

        self._music_mod = music
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.addWidget(QLabel("Mostra a música que está tocando no PC (Spotify, YouTube no navegador e outros players do "
                           "controle de mídia do Windows) e as ondas do som que sai das caixas/fone.",
                           objectName="muted", wordWrap=True))

        g = QGroupBox("Tocando agora")
        gl = QVBoxLayout(g)
        self.lbl_now = QLabel("—")
        self.lbl_now.setWordWrap(True)
        self.lbl_now.setStyleSheet("font-size:12pt;font-weight:600")
        self.lbl_audio = QLabel("", objectName="muted")
        gl.addWidget(self.lbl_now)
        gl.addWidget(self.lbl_audio)
        row = QHBoxLayout()
        b = QPushButton("Usar o tema Música na tela")
        b.setMinimumHeight(34)
        b.clicked.connect(self._use_music_theme)
        row.addWidget(b)
        b = QPushButton("Tema Música por cima da minha mídia")
        b.clicked.connect(self._use_music_overlay)
        row.addWidget(b)
        gl.addLayout(row)
        v.addWidget(g)

        g = QGroupBox("Modelo do tema Música (clique para usar)")
        gl = QVBoxLayout(g)
        self.music_list = QListWidget()
        self.music_list.setViewMode(QListWidget.IconMode)
        self.music_list.setIconSize(QSize(150, 150))
        self.music_list.setGridSize(QSize(172, 190))
        self.music_list.setResizeMode(QListWidget.Adjust)
        self.music_list.setMovement(QListWidget.Static)
        self.music_list.setFixedHeight(590)
        for key, name in music.LAYOUTS.items():
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, key)
            self.music_list.addItem(it)
            it.setSelected(key == self.cfg.music_layout)
        self.music_list.itemClicked.connect(self._on_music_layout)
        gl.addWidget(self.music_list)
        v.addWidget(g)

        g = QGroupBox("Letra da música (modelos \"Letra karaokê\" e \"Letra sobre a capa\")")
        fl = QFormLayout(g)
        self.chk_lyrics = QCheckBox("Buscar a letra sincronizada na internet (LRCLIB, grátis, sem cadastro)")
        self.chk_lyrics.setChecked(self.cfg.music_lyrics)
        self.chk_lyrics.toggled.connect(lambda on: self._set("music_lyrics", on))
        fl.addRow("", self.chk_lyrics)
        lr = QHBoxLayout()
        self.sld_lyr = QSlider(Qt.Horizontal, minimum=-50, maximum=50, value=int(round(self.cfg.lyrics_offset * 10)))
        self.lbl_lyr = QLabel(f"{self.cfg.lyrics_offset:+.1f} s")
        self.lbl_lyr.setFixedWidth(52)
        self.sld_lyr.valueChanged.connect(lambda val: (self.lbl_lyr.setText(f"{val / 10:+.1f} s"),
                                                       self._set("lyrics_offset", val / 10)))
        lr.addWidget(QLabel("atrasar"))
        lr.addWidget(self.sld_lyr)
        lr.addWidget(QLabel("adiantar"))
        lr.addWidget(self.lbl_lyr)
        fl.addRow("Acertar o tempo", lr)
        self.lbl_lyr_state = QLabel("", objectName="muted", wordWrap=True)
        fl.addRow("", self.lbl_lyr_state)
        v.addWidget(g)

        g = QGroupBox("Ondas do som")
        fl = QFormLayout(g)
        self.cb_wave = QComboBox()
        for key, label in music.WAVES.items():
            self.cb_wave.addItem(label, key)
        self.cb_wave.setCurrentIndex(max(0, self.cb_wave.findData(self.cfg.music_wave)))
        self.cb_wave.currentIndexChanged.connect(lambda _: self._set("music_wave", self.cb_wave.currentData(),
                                                                     thumbs=True))
        fl.addRow("Estilo (vale em todo lugar)", self.cb_wave)
        self.chk_wave_over = QCheckBox("Mostrar as ondas também por cima de outros temas, fotos e vídeos")
        self.chk_wave_over.setChecked(self.cfg.music_wave_overlay)
        self.chk_wave_over.toggled.connect(lambda on: self._set("music_wave_overlay", on))
        fl.addRow("", self.chk_wave_over)
        gr = QHBoxLayout()
        self.sld_gain = QSlider(Qt.Horizontal, minimum=30, maximum=300, value=int(self.cfg.music_gain * 100))
        self.lbl_gain = QLabel(f"{self.cfg.music_gain:.1f}×")
        self.lbl_gain.setFixedWidth(42)
        self.sld_gain.valueChanged.connect(lambda val: (self.lbl_gain.setText(f"{val / 100:.1f}×"),
                                                        self._set("music_gain", val / 100)))
        gr.addWidget(self.sld_gain)
        gr.addWidget(self.lbl_gain)
        fl.addRow("Sensibilidade", gr)
        self.chk_react = QCheckBox("Efeitos animados (Plasma, Galáxia, Túnel…) aceleram com o grave")
        self.chk_react.setChecked(self.cfg.music_react)
        self.chk_react.toggled.connect(lambda on: self._set("music_react", on))
        fl.addRow("", self.chk_react)
        v.addWidget(g)

        g = QGroupBox("O que aparece")
        fl = QFormLayout(g)
        checks = QGridLayout()
        self.music_checks = {}
        for n, (key, label) in enumerate((("music_show_cover", "Capa do álbum"), ("music_show_progress", "Barra de tempo"),
                                          ("music_show_app", "Nome do app (Spotify…)"), ("music_show_clock", "Relógio"))):
            ck = QCheckBox(label)
            ck.setChecked(bool(getattr(self.cfg, key)))
            ck.toggled.connect(lambda on, k=key: self._set(k, on, thumbs=True))
            checks.addWidget(ck, n // 2, n % 2)
            self.music_checks[key] = ck
        fl.addRow(checks)
        self.chk_autocol = QCheckBox("Cores automáticas tiradas da capa do álbum")
        self.chk_autocol.setChecked(self.cfg.music_auto_colors)
        self.chk_autocol.toggled.connect(lambda on: self._set("music_auto_colors", on, thumbs=True))
        fl.addRow("", self.chk_autocol)
        ol = QHBoxLayout()
        self.sld_mbg = QSlider(Qt.Horizontal, minimum=0, maximum=100, value=int(self.cfg.music_bg_opacity))
        self.lbl_mbg = QLabel(f"{self.cfg.music_bg_opacity}%")
        self.lbl_mbg.setFixedWidth(42)
        self.sld_mbg.valueChanged.connect(lambda val: (self.lbl_mbg.setText(f"{val}%"),
                                                       self._set("music_bg_opacity", val, thumbs=True)))
        ol.addWidget(QLabel("transparente"))
        ol.addWidget(self.sld_mbg)
        ol.addWidget(QLabel("sólido"))
        ol.addWidget(self.lbl_mbg)
        fl.addRow("Fundo (tema e cartão)", ol)
        self.cb_card = QComboBox()
        for label, val in (("Quando a música troca (8 segundos)", "troca"), ("Sempre enquanto toca", "sempre"),
                           ("Nunca", "nunca")):
            self.cb_card.addItem(label, val)
        self.cb_card.setCurrentIndex(max(0, self.cb_card.findData(self.cfg.music_card)))
        self.cb_card.currentIndexChanged.connect(lambda _: self._set("music_card", self.cb_card.currentData()))
        fl.addRow("Cartão por cima de outros temas", self.cb_card)
        v.addWidget(g)
        v.addWidget(QLabel("O som é lido direto do Windows (sem microfone) e nada é gravado. As ondas só mexem "
                           "enquanto tem som saindo.", objectName="muted", wordWrap=True))
        v.addStretch(1)

        self._music_timer = QTimer(self, interval=1000, timeout=self._update_music_status)
        self._music_timer.start()
        self._music_thumbs_timer = QTimer(self, singleShot=True, interval=500, timeout=self._refresh_music_thumbs)
        QTimer.singleShot(3000, self._refresh_music_thumbs)
        return self._scroll(inner)

    def _demo_music(self):
        """Música e som de exemplo para as miniaturas quando nada está tocando."""
        import math as _m
        mus = self._music_mod
        media = self.hub.media_snapshot()
        if not (media and media.get("title")):
            from .library import LIB_DIR
            covers = sorted(LIB_DIR.glob("*.jpg")) + sorted(LIB_DIR.glob("*.png"))
            media = {"title": "Nome da música", "artist": "Artista", "status": "Playing", "app": "Spotify.exe",
                     "position": 9, "duration": 200, "cover": str(covers[0]) if covers else "", "demo": True}
        viz = mus.AudioViz()
        viz.bands = [max(0.05, min(1.0, 0.85 - i * 0.012 + 0.25 * _m.sin(i / 3))) for i in range(viz.n)]
        viz.caps = [min(1.0, b + 0.06) for b in viz.bands]
        viz.active = True
        return media, viz

    def _refresh_music_thumbs(self):
        if not hasattr(self, "music_list"):
            return
        import datetime as _dt
        media, viz = self._demo_music()
        m = self.hub.snapshot()
        m.update({"media": media, "audio": viz, "now": _dt.datetime.now()})
        for i in range(self.music_list.count()):
            it = self.music_list.item(i)
            c = self.cfg.copy()
            c.music_layout = it.data(Qt.UserRole)
            try:
                it.setIcon(QIcon(pil_to_pixmap(render.render(c, m, "musica"), 150)))
            except Exception:
                log.exception("miniatura de música")

    def _on_music_layout(self, item: QListWidgetItem):
        self.cfg.music_layout = item.data(Qt.UserRole)
        on = (self.cfg.mode == "painel" and self.cfg.theme == "musica") or \
             (self.cfg.mode == "midia" and self.cfg.media_overlay and self.cfg.overlay_theme == "musica")
        if not on:
            if self.rb_over.isChecked() and self.cfg.media_paths:
                self._use_music_overlay()
            else:
                self._use_music_theme()
        else:
            self._changed()

    def _update_music_status(self):
        if not self.isVisible():
            return
        m = self.hub.media_snapshot()
        mus = self._music_mod
        if m and m.get("title"):
            st = "tocando" if m.get("status") == "Playing" else "pausado"
            app = mus.app_name(m.get("app", ""))
            dur = float(m.get("duration") or 0)
            tempo = f" · {mus.fmt_time(m.get('position') or 0)} / {mus.fmt_time(dur)}" if dur else ""
            self.lbl_now.setText(f"♫ {m['title']} — {m.get('artist') or m.get('album') or ''}   ({app}, {st}{tempo})")
        else:
            self.lbl_now.setText("Nada tocando agora. Dê play no Spotify, YouTube ou outro player.")
        if hasattr(self, "lbl_lyr_state"):
            from . import lyrics
            lyr = lyrics.for_media(m, self.cfg) if self.cfg.music_layout.startswith("letra") else None
            self.lbl_lyr_state.setText({
                None: "Clipes do YouTube também funcionam (o título \"Artista - Música (Official Video)\" é limpo "
                      "sozinho). Vídeos que não são música não têm letra.",
                "buscando": "Procurando a letra desta música…",
                "ok": "Letra encontrada" + (" com o tempo de cada linha." if lyr and lyr.synced else
                                            " (sem tempo: rola pela duração da música)."),
                "sem": "Não achei a letra desta música.",
                "instrumental": "Esta música é instrumental.",
                "desligado": "Busca de letras desligada.",
            }.get(lyr.state if lyr else None, ""))
        a = self.hub.audio_snapshot()
        if not self.hub.want_audio:
            self.lbl_audio.setText("Captura do som: desligada (liga sozinha quando você usa ondas, o tema Música "
                                   "ou efeitos que reagem ao som)")
        elif a and time.monotonic() - a[0] < 1:
            self.lbl_audio.setText(f"Captura do som: ouvindo (nível {a[2]})")
        else:
            self.lbl_audio.setText("Captura do som: ligada, mas não tem som saindo agora")

    def _use_music_theme(self):
        self.cfg.theme = "musica"
        self.rb_alone.setChecked(True)
        self._set_mode("painel")
        self._select_theme_item("musica")

    def _use_music_overlay(self):
        self.cfg.overlay_theme = "musica"
        self.rb_over.setChecked(True)
        self.cfg.media_overlay = True
        self._set_mode("midia")
        self._select_theme_item("musica")

    def _select_theme_item(self, key: str):
        for i in range(self.theme_list.count()):
            it = self.theme_list.item(i)
            it.setSelected(it.data(Qt.UserRole) == key)

    def _tab_library(self) -> QWidget:
        self.library = LibraryTab(self.cfg, self)
        self.library.use_now.connect(self._media_use_now)
        self.library.add_to_playlist.connect(self._media_add_paths)
        self.library.config_changed.connect(self._changed)
        return self.library

    def _media_use_now(self, paths: list):
        self.media_list.clear()
        for p in paths:
            self._media_append(p)
        self._media_sync()
        self._set_mode("midia")

    def _media_add_paths(self, paths: list):
        for p in paths:
            self._media_append(p)
        self._media_sync()

    # -- arrastar e soltar ------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir() or p.suffix.lower() in MEDIA_EXT:
                paths.append(str(p))
        if not paths:
            return
        self._media_add_paths(paths)
        self._set_mode("midia")
        self.tray.showMessage(APP_NAME, f"{len(paths)} item(ns) adicionados à playlist", QSystemTrayIcon.Information, 2000)

    def _media_remove(self):
        for it in self.media_list.selectedItems():
            self.media_list.takeItem(self.media_list.row(it))
        self._media_sync()

    def _media_move(self, delta: int):
        row = self.media_list.currentRow()
        new = row + delta
        if row < 0 or not 0 <= new < self.media_list.count():
            return
        it = self.media_list.takeItem(row)
        self.media_list.insertItem(new, it)
        self.media_list.setCurrentRow(new)
        self._media_sync()

    # -- Espelho --------------------------------------------------------
    def _tab_mirror(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Mostra na telinha o que está num monitor (ou só um pedaço dele).", objectName="muted"))
        g = QGroupBox("Origem")
        fl = QFormLayout(g)
        self.cb_monitor = QComboBox()
        try:
            for i, mon in enumerate(list_monitors(), start=1):
                self.cb_monitor.addItem(f"Monitor {i} — {mon['width']}×{mon['height']} em ({mon['left']}, {mon['top']})", i)
        except Exception as exc:
            self.cb_monitor.addItem(f"erro ao listar monitores: {exc}", 1)
        self.cb_monitor.setCurrentIndex(max(0, self.cb_monitor.findData(self.cfg.mirror_monitor)))
        self.cb_monitor.currentIndexChanged.connect(lambda _: self._set("mirror_monitor", self.cb_monitor.currentData()))
        fl.addRow("Monitor", self.cb_monitor)

        self.chk_region = QCheckBox("Só uma região (coordenadas da área de trabalho)")
        reg = self.cfg.mirror_region or [0, 0, 480, 480]
        self.chk_region.setChecked(bool(self.cfg.mirror_region))
        fl.addRow("", self.chk_region)
        rl = QHBoxLayout()
        self.region_spins = []
        for label, val in zip(("X", "Y", "Largura", "Altura"), reg):
            sp = QSpinBox(minimum=-20000, maximum=20000, value=int(val))
            sp.valueChanged.connect(self._region_changed)
            rl.addWidget(QLabel(label))
            rl.addWidget(sp)
            self.region_spins.append(sp)
        fl.addRow("Região", rl)
        self.chk_region.toggled.connect(self._region_changed)

        self.cb_mfit = QComboBox()
        self.cb_mfit.addItem("Caber inteiro (faixas pretas)", "contain")
        self.cb_mfit.addItem("Preencher (corta as bordas)", "cover")
        self.cb_mfit.setCurrentIndex(0 if self.cfg.mirror_fit == "contain" else 1)
        self.cb_mfit.currentIndexChanged.connect(lambda _: self._set("mirror_fit", self.cb_mfit.currentData()))
        fl.addRow("Enquadramento", self.cb_mfit)
        v.addWidget(g)
        b = QPushButton("Espelhar na tela")
        b.setMinimumHeight(36)
        b.clicked.connect(lambda: self._set_mode("espelho"))
        v.addWidget(b)
        v.addStretch(1)
        return w

    def _region_changed(self, *_):
        if self.chk_region.isChecked():
            self.cfg.mirror_region = [sp.value() for sp in self.region_spins]
        else:
            self.cfg.mirror_region = []
        self._changed()

    # -- Sensores -------------------------------------------------------
    def _tab_sensors(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        g = QGroupBox("Temperatura da CPU e ventoinhas da placa-mãe")
        gl = QVBoxLayout(g)
        self.lbl_pawnio = QLabel(wordWrap=True)
        gl.addWidget(self.lbl_pawnio)
        self.btn_pawnio = QPushButton("Instalar driver de sensores (PawnIO)…")
        self.btn_pawnio.clicked.connect(self._install_pawnio)
        gl.addWidget(self.btn_pawnio, 0, Qt.AlignLeft)
        v.addWidget(g)
        self._update_pawnio_label()

        g = QGroupBox("Escolhas")
        fl = QFormLayout(g)
        self.cb_gpu = QComboBox()
        for label, val in (("Automático (placa de vídeo dedicada)", "auto"), ("NVIDIA", "GpuNvidia"),
                           ("AMD", "GpuAmd"), ("Intel", "GpuIntel")):
            self.cb_gpu.addItem(label, val)
        self.cb_gpu.setCurrentIndex(max(0, self.cb_gpu.findData(self.cfg.gpu_select)))
        self.cb_gpu.currentIndexChanged.connect(lambda _: self._set("gpu_select", self.cb_gpu.currentData(), thumbs=True))
        fl.addRow("GPU mostrada", self.cb_gpu)
        self.cb_gpufan = QComboBox()
        for label, val in (("Automático (a placa decide)", 0), ("40%", 40), ("50%", 50), ("60%", 60), ("70%", 70),
                           ("80%", 80), ("90%", 90), ("100% (máximo)", 100)):
            self.cb_gpufan.addItem(label, val)
        self.cb_gpufan.setCurrentIndex(max(0, self.cb_gpufan.findData(int(self.cfg.gpu_fan or 0))))
        self.cb_gpufan.currentIndexChanged.connect(lambda _: self._set("gpu_fan", self.cb_gpufan.currentData()))
        self.cb_gpufan.setToolTip("Volta sozinha para o automático quando o app fecha; vai a 100% se a GPU passar de 83 °C")
        fl.addRow("Ventoinha da placa de vídeo", self.cb_gpufan)
        fl.addRow("", QLabel("As ventoinhas da CPU/watercooler não aceitam controle por programa nesta placa-mãe "
                             "(Gigabyte B850): ajuste a curva na BIOS (Smart Fan 6) ou no Gigabyte Control Center.",
                             objectName="muted", wordWrap=True))
        self.cb_pump = QComboBox()
        self.cb_fan = QComboBox()
        self.cb_pump.currentIndexChanged.connect(lambda _: self._set("pump_sensor", self.cb_pump.currentData() or ""))
        self.cb_fan.currentIndexChanged.connect(lambda _: self._set("fan_sensor", self.cb_fan.currentData() or ""))
        fl.addRow("Sensor da bomba", self.cb_pump)
        fl.addRow("Sensor do fan", self.cb_fan)
        self.sp_sensor = QSpinBox(minimum=1, maximum=10, value=max(1, int(self.cfg.sensor_interval)), suffix=" s")
        self.sp_sensor.valueChanged.connect(lambda val: self._set("sensor_interval", float(val)))
        fl.addRow("Atualizar sensores a cada", self.sp_sensor)
        v.addWidget(g)

        v.addWidget(QLabel("Todos os sensores lidos agora:", objectName="muted"))
        self.sensor_table = QTableWidget(0, 4)
        self.sensor_table.setHorizontalHeaderLabels(["Componente", "Tipo", "Sensor", "Valor"])
        self.sensor_table.verticalHeader().setVisible(False)
        self.sensor_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sensor_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.sensor_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        v.addWidget(self.sensor_table, 1)
        return w

    UNITS = {"Temperature": "°C", "Load": "%", "Fan": "RPM", "Power": "W", "Clock": "MHz",
             "Voltage": "V", "Data": "GB", "SmallData": "MB", "Control": "%", "Throughput": "B/s", "Level": "%"}

    def _update_sensor_table(self):
        if self.tabs.currentWidget() is not self._sensor_tab or not self.isVisible():
            return
        order = {"Temperature": 0, "Load": 1, "Fan": 2, "Control": 3, "Power": 4, "Clock": 5}
        rows = sorted(self.hub.all_sensors(), key=lambda s: (order.get(s["type"], 9), s["hwType"], s["hw"], s["name"]))
        self.sensor_table.setRowCount(len(rows))
        for i, s in enumerate(rows):
            vals = (s["hw"], s["type"], s["name"], f"{s['value']:.1f} {self.UNITS.get(s['type'], '')}")
            for j, val in enumerate(vals):
                it = self.sensor_table.item(i, j)
                if it is None:
                    self.sensor_table.setItem(i, j, QTableWidgetItem(val))
                else:
                    it.setText(val)

    def _refresh_fan_choices(self):
        fans = [s for s in self.hub.all_sensors() if s["type"] == "Fan" and not s["hwType"].startswith("Gpu")]
        for cb, current in ((self.cb_pump, self.cfg.pump_sensor), (self.cb_fan, self.cfg.fan_sensor)):
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("Automático", "")
            for s in fans:
                cb.addItem(f"{s['hw']} — {s['name']} ({s['value']:.0f} RPM)", s["id"])
            idx = cb.findData(current)
            if idx < 0 and current:
                cb.addItem(f"(não encontrado agora) {current}", current)
                idx = cb.count() - 1
            cb.setCurrentIndex(max(0, idx))
            cb.blockSignals(False)
        if not fans:
            QTimer.singleShot(10000, self._refresh_fan_choices)

    def _update_pawnio_label(self):
        installed = pawnio_installed()
        cpu_ok = self.hub.snapshot().get("cpu_temp") is not None
        if cpu_ok:
            txt = "✔ Temperatura da CPU funcionando."
        elif installed:
            txt = ("O driver PawnIO está instalado, mas a temperatura da CPU ainda não chegou. "
                   "Feche e abra o programa (ele precisa rodar como administrador).")
        else:
            txt = ("Sem o driver PawnIO o Windows não deixa ler a temperatura da CPU, o clock e as ventoinhas/bomba "
                   "da placa-mãe. A GPU, uso de CPU, RAM e discos funcionam sem ele.\n"
                   "PawnIO é o driver assinado e gratuito usado pelo LibreHardwareMonitor e pelo FanControl "
                   "(github.com/namazso/PawnIO.Setup).")
        self.lbl_pawnio.setText(txt)
        self.btn_pawnio.setVisible(not installed)

    def _install_pawnio(self):
        r = QMessageBox.question(
            self, "Instalar PawnIO",
            "Vou baixar o instalador oficial do PawnIO (github.com/namazso/PawnIO.Setup) e abrir ele.\n\n"
            "É um driver de kernel assinado, usado pelo LibreHardwareMonitor/FanControl para ler sensores "
            "da CPU e da placa-mãe. Você confirma a instalação na janela dele.\n\nContinuar?")
        if r != QMessageBox.Yes:
            return
        self._pawnio_dlg = QProgressDialog("Baixando PawnIO…", None, 0, 100, self)
        self._pawnio_dlg.setWindowTitle("PawnIO")
        self._pawnio_dlg.setMinimumDuration(0)
        self._pawnio_dlg.show()

        def work():
            try:
                path = system.download_pawnio(lambda f: self._sig.pawnio_progress.emit(f))
                self._sig.pawnio_progress.emit(1.0)
                code = system.run_installer(path)
                self._sig.pawnio_done.emit("" if code == 0 else f"o instalador terminou com código {code}")
            except Exception as exc:
                self._sig.pawnio_done.emit(str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_pawnio_progress(self, f: float):
        if getattr(self, "_pawnio_dlg", None):
            self._pawnio_dlg.setValue(int(f * 100))
            if f >= 1.0:
                self._pawnio_dlg.setLabelText("Siga o instalador do PawnIO…")

    def _on_pawnio_done(self, err: str):
        if getattr(self, "_pawnio_dlg", None):
            self._pawnio_dlg.close()
        if err:
            QMessageBox.warning(self, "PawnIO", f"Não deu para instalar: {err}")
        else:
            self.hub.restart_helper()
            QMessageBox.information(self, "PawnIO", "Driver instalado. Os sensores da CPU aparecem em alguns segundos.")
        QTimer.singleShot(6000, self._update_pawnio_label)
        QTimer.singleShot(6000, self._refresh_fan_choices)

    # -- Configurações --------------------------------------------------
    def _tab_settings(self) -> QWidget:
        inner = QWidget()
        v = QVBoxLayout(inner)

        g = QGroupBox("Tela")
        fl = QFormLayout(g)
        self.cb_rot = QComboBox()
        for label, val in (("0°", 0), ("90° (padrão desta tela)", 90), ("180°", 180), ("270°", 270)):
            self.cb_rot.addItem(label, val)
        self.cb_rot.setCurrentIndex(max(0, self.cb_rot.findData(self.cfg.rotation)))
        self.cb_rot.currentIndexChanged.connect(lambda _: self._set("rotation", self.cb_rot.currentData()))
        fl.addRow("Girar imagem", self.cb_rot)
        self.sp_fps = QSpinBox(minimum=1, maximum=30, value=int(self.cfg.fps), suffix=" quadros/s")
        self.sp_fps.valueChanged.connect(lambda val: self._set("fps", val))
        fl.addRow("Fluidez", self.sp_fps)
        self.sld_q = QSlider(Qt.Horizontal, minimum=50, maximum=98, value=int(self.cfg.jpeg_quality))
        self.lbl_q = QLabel(str(self.cfg.jpeg_quality))
        self.sld_q.valueChanged.connect(lambda val: (self.lbl_q.setText(str(val)), self._set("jpeg_quality", val)))
        ql = QHBoxLayout()
        ql.addWidget(self.sld_q)
        ql.addWidget(self.lbl_q)
        fl.addRow("Qualidade da imagem", ql)
        self.sp_maxkb = QSpinBox(minimum=0, maximum=400, value=int(self.cfg.max_kb), suffix=" KB", singleStep=10)
        self.sp_maxkb.setSpecialValueText("automático")
        self.sp_maxkb.setToolTip("Tamanho máximo de cada quadro. Se a imagem rasgar ou travar, diminua (ex.: 30 KB).")
        self.sp_maxkb.valueChanged.connect(lambda val: self._set("max_kb", val))
        fl.addRow("Limite por quadro", self.sp_maxkb)
        v.addWidget(g)

        g = QGroupBox("Modo noturno")
        fl = QFormLayout(g)
        self.chk_night = QCheckBox("Diminuir o brilho automaticamente")
        self.chk_night.setChecked(self.cfg.night_enabled)
        self.chk_night.toggled.connect(lambda b: self._set("night_enabled", b, brightness=True))
        fl.addRow("", self.chk_night)
        self.te_start = QTimeEdit(QTime.fromString(self.cfg.night_start, "HH:mm"))
        self.te_end = QTimeEdit(QTime.fromString(self.cfg.night_end, "HH:mm"))
        for te in (self.te_start, self.te_end):
            te.setDisplayFormat("HH:mm")
        self.te_start.timeChanged.connect(lambda t: self._set("night_start", t.toString("HH:mm"), brightness=True))
        self.te_end.timeChanged.connect(lambda t: self._set("night_end", t.toString("HH:mm"), brightness=True))
        tl = QHBoxLayout()
        tl.addWidget(QLabel("das"))
        tl.addWidget(self.te_start)
        tl.addWidget(QLabel("até"))
        tl.addWidget(self.te_end)
        tl.addStretch(1)
        fl.addRow("Horário", tl)
        self.sld_night = QSlider(Qt.Horizontal, minimum=0, maximum=100, value=self.cfg.night_brightness)
        self.sld_night.valueChanged.connect(lambda val: self._set("night_brightness", val, brightness=True))
        fl.addRow("Brilho à noite", self.sld_night)
        v.addWidget(g)

        g = QGroupBox("Programa")
        fl = QFormLayout(g)
        self.chk_auto = QCheckBox("Iniciar junto com o Windows")
        self.chk_auto.setChecked(system.autostart_enabled())
        self.chk_auto.toggled.connect(self._toggle_autostart)
        fl.addRow("", self.chk_auto)
        self.chk_min = QCheckBox("Abrir minimizado na bandeja")
        self.chk_min.setChecked(self.cfg.start_minimized)
        self.chk_min.toggled.connect(lambda b: self._set("start_minimized", b))
        fl.addRow("", self.chk_min)
        self.chk_release = QCheckBox("Ao sair, devolver a tela para o conteúdo de fábrica")
        self.chk_release.setChecked(self.cfg.release_on_exit)
        self.chk_release.toggled.connect(lambda b: self._set("release_on_exit", b))
        fl.addRow("", self.chk_release)
        row = QHBoxLayout()
        for label, fn in (("Limpar cache de vídeos", self._clear_video_cache),
                          ("Exportar config…", self._export_cfg), ("Importar config…", self._import_cfg),
                          ("Abrir pasta de dados", lambda: system.open_folder(config_dir()))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        fl.addRow(row)
        v.addWidget(g)

        g = QGroupBox("Sobre a tela conectada")
        fl = QFormLayout(g)
        self.lbl_info = {}
        for key, label in (("model", "Modelo"), ("version", "Firmware"), ("res", "Resolução"),
                           ("uid", "Número de série"), ("port", "Porta"), ("storage", "Memória interna")):
            lb = QLabel("—")
            lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.lbl_info[key] = lb
            fl.addRow(label, lb)
        b = QPushButton("Reiniciar a tela", objectName="danger")
        b.clicked.connect(self._restart_device)
        fl.addRow("", b)
        v.addWidget(g)
        v.addStretch(1)
        return self._scroll(inner)

    def _clear_video_cache(self):
        from . import videocache

        mb = videocache.size_bytes() / 1048576
        if QMessageBox.question(self, "Cache de vídeos",
                                f"O app guarda cópias leves (480×480) dos vídeos para gastar menos CPU.\n"
                                f"Agora ocupam {mb:.0f} MB. Apagar? Elas são refeitas sozinhas quando o vídeo tocar.") \
                == QMessageBox.Yes:
            videocache.clear()

    def _toggle_autostart(self, on: bool):
        ok, msg = system.set_autostart(on)
        if not ok:
            QMessageBox.warning(self, "Iniciar com o Windows", f"Não consegui alterar: {msg}")
            self.chk_auto.blockSignals(True)
            self.chk_auto.setChecked(system.autostart_enabled())
            self.chk_auto.blockSignals(False)

    def _restart_device(self):
        if QMessageBox.question(self, "Reiniciar a tela", "Reiniciar a telinha do watercooler agora?") == QMessageBox.Yes:
            self.engine.command("restart_device")

    def _export_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Exportar configuração", "digoo-lcd.json", "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(self.cfg.to_dict(), indent=2, ensure_ascii=False), encoding="utf8")

    def _import_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Importar configuração", "", "JSON (*.json)")
        if not path:
            return
        try:
            cfg = Config.from_dict(json.loads(Path(path).read_text(encoding="utf8")))
        except Exception as exc:
            QMessageBox.warning(self, "Importar", f"Arquivo inválido: {exc}")
            return
        config_mod.save(cfg)
        self.cfg = cfg
        self.engine.set_config(cfg)
        self.engine.command("brightness")
        self._rebuild_ui()
        QMessageBox.information(self, "Importar", "Configuração importada e aplicada.")

    def _rebuild_ui(self):
        """Recria os widgets com a config atual (usado após importar)."""
        tab = self.tabs.currentIndex()
        root = QWidget(objectName="root")
        lay = QHBoxLayout(root)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(16)
        lay.addWidget(self._build_left(), 0)
        lay.addWidget(self._build_right(), 1)
        self.setCentralWidget(root)
        self.tabs.setCurrentIndex(tab)
        self._sync_mode_buttons()
        QTimer.singleShot(200, self._refresh_thumbs)
        QTimer.singleShot(300, self._refresh_fan_choices)

    # ==================================================================
    # bandeja
    # ==================================================================
    def _build_tray(self):
        self.tray = QSystemTrayIcon(QIcon(str(ICON_PATH)), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.setStyleSheet("QMenu{background:#111522;border:1px solid #2A3350;padding:4px}"
                           "QMenu::item{padding:6px 18px;color:#E6E9F2}QMenu::item:selected{background:#1F3B4D}")
        menu.addAction("Abrir painel", self.show_window)
        menu.addSeparator()
        m_mode = menu.addMenu("Modo")
        for key, label in (("painel", "Painel de sensores"), ("midia", "Fotos / GIF / Vídeo"), ("espelho", "Espelhar tela")):
            m_mode.addAction(label, lambda k=key: self._set_mode(k))
        m_theme = menu.addMenu("Tema")
        for key, name in render.THEMES.items():
            m_theme.addAction(name, lambda k=key: self._tray_theme(k))
        m_b = menu.addMenu("Brilho")
        for val in (100, 75, 50, 25, 10, 0):
            m_b.addAction("Apagada" if val == 0 else f"{val}%", lambda v=val: self.sld_brightness.setValue(v))
        self.act_pause = QAction("Pausar tela", self, triggered=self._toggle_pause)
        menu.addAction(self.act_pause)
        menu.addSeparator()
        menu.addAction("Sair", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_window() if r in (
            QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger) else None)
        self.tray.show()
        self._tray_menu = menu

    def _tray_theme(self, key: str):
        self.cfg.theme = key
        for i in range(self.theme_list.count()):
            it = self.theme_list.item(i)
            it.setSelected(it.data(Qt.UserRole) == key)
        self._set_mode("painel")

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(APP_NAME, "Continuo rodando aqui na bandeja, mantendo a telinha atualizada.",
                                  QSystemTrayIcon.Information, 3000)

    def quit_app(self):
        self._quitting = True
        self.library.shutdown()
        self._save()
        self.tray.hide()
        QApplication.instance().quit()

    # ==================================================================
    # estado / atualização
    # ==================================================================
    def _set(self, attr: str, value, thumbs: bool = False, brightness: bool = False):
        setattr(self.cfg, attr, value)
        self._changed(thumbs=thumbs)
        if brightness:
            self.engine.command("brightness")

    def _changed(self, thumbs: bool = False):
        self.engine.set_config(self.cfg)
        self._save_timer.start()
        if thumbs:
            self._thumb_timer.start()

    def _save(self):
        try:
            config_mod.save(self.cfg)
        except Exception:
            log.exception("salvar config")

    def _set_mode(self, mode: str):
        self.cfg.mode = mode
        self._sync_mode_buttons()
        self._changed()

    def _sync_mode_buttons(self):
        for k, b in self.mode_buttons.items():
            b.setChecked(k == self.cfg.mode)

    def _on_brightness(self, val: int):
        self.lbl_brightness.setText(f"{val}%")
        self.cfg.brightness = val
        self._changed()
        self.engine.command("brightness")

    def _toggle_pause(self):
        if self.engine.paused:
            self.engine.command("resume")
        else:
            self.engine.command("pause")

    def _trim_memory(self):
        if self.isVisible():
            return
        import ctypes
        import gc

        gc.collect()
        try:
            ctypes.windll.psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
        except Exception:
            pass

    def hideEvent(self, e):
        super().hideEvent(e)
        QTimer.singleShot(3000, self._trim_memory)

    def _update_preview(self):
        if not self.isVisible():
            return
        img = self.engine.preview()
        if img is not None and img is not getattr(self, "_last_preview", None):
            self._last_preview = img
            self.preview.setPixmap(pil_to_pixmap(img, 360))

    def _update_status(self):
        e = self.engine
        paused = e.paused
        self.btn_pause.setText("Retomar tela" if paused else "Pausar tela")
        self.act_pause.setText("Retomar tela" if paused else "Pausar tela")
        if e.connected:
            info = e.info
            self.lbl_device.setText(f"● Conectado — {info.model or 'tela'} (fw {info.version}) em {e.port}")
            self.lbl_device.setStyleSheet("color:#39FF88")
            extra = f"   ·   {e.media_name}" if self.cfg.mode == "midia" and e.media_name else ""
            self.lbl_stats.setText(f"{e.fps_real:.0f} quadros/s   ·   {e.frame_kb:.0f} KB por quadro   ·   qualidade {e.quality}{extra}")
            self.tray.setToolTip(f"{APP_NAME} — conectado")
            self.lbl_info["model"].setText(info.model or "—")
            self.lbl_info["version"].setText(info.version or "—")
            self.lbl_info["res"].setText(f"{info.width} × {info.height} (ângulo {info.angle}°)")
            self.lbl_info["uid"].setText(info.uid or "—")
            self.lbl_info["port"].setText(e.port)
            self.lbl_info["storage"].setText(info.storage_text)
        else:
            self.lbl_device.setText(f"○ {e.status}")
            self.lbl_device.setStyleSheet("color:#FFB800" if paused else "color:#FF6B6B")
            self.lbl_stats.setText("prévia ao vivo (a tela não está recebendo)")
            self.tray.setToolTip(f"{APP_NAME} — {e.status}")

    @staticmethod
    def _short(path: str, n: int = 60) -> str:
        return path if len(path) <= n else "…" + path[-n:]
