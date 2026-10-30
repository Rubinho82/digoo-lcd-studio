"""Aba "Biblioteca": busca de GIFs/vídeos online, acervo local e efeitos animados."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QImageReader, QMovie, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import effects
from .config import Config
from .library import (LIB_DIR, PROVIDERS, Entry, Item, Library, ProviderError, fetch_preview, fetch_thumb,
                      local_thumb)
from .sources import MEDIA_EXT
from .steam_ws import (WS_SORTS, WS_TAGS as WS_TAGS_PT, WS_TYPES, LocalWallpaper, Workshop, local_wallpapers,
                       run_helper)

log = logging.getLogger(__name__)

TILE = 128
PREVIEW = 250  # lado da prévia grande
SPECIAL = {"we_local": "Wallpaper Engine — instalados", "efeitos": "Efeitos animados",
           "local": "Minha biblioteca"}
# fontes online: Workshop do Wallpaper Engine primeiro
SOURCES = {"workshop": ("Wallpaper Engine — Workshop", lambda cfg: Workshop()), **PROVIDERS}
BIG_DOWNLOAD = 150 * 1024 * 1024


def _open_external(url: str):
    # só abre links http/https; ignora file:/javascript:/esquemas inesperados do SO
    u = QUrl(url)
    if u.scheme().lower() in ("http", "https"):
        QDesktopServices.openUrl(u)


def _pil_pixmap(img: Image.Image, size: int) -> QPixmap:
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
    q = QImage(img.tobytes("raw", "RGB"), size, size, size * 3, QImage.Format_RGB888)
    return QPixmap.fromImage(q.copy())


class _Bridge(QObject):
    """Leva callbacks das threads de rede para a thread da interface."""
    call = Signal(object)


class Tile(QFrame):
    clicked = Signal(object)
    double_clicked = Signal(object)

    def __init__(self, payload, title: str, badge: str = ""):
        super().__init__()
        self.payload = payload
        self.setFixedSize(TILE + 10, TILE + 34)
        self.setCursor(Qt.PointingHandCursor)
        self._movie: QMovie | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(3)
        self.img = QLabel("…")
        self.img.setFixedSize(TILE, TILE)
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setStyleSheet("background:#05070D;border-radius:8px;color:#55607A")
        v.addWidget(self.img)
        t = QLabel(self.fontMetrics().elidedText(((badge + " ") if badge else "") + title, Qt.ElideRight, TILE))
        t.setStyleSheet("font-size:8pt;color:#AEB6C8")
        v.addWidget(t)
        self.set_selected(False)

    def set_selected(self, on: bool):
        self.setStyleSheet("Tile{background:%s;border:2px solid %s;border-radius:10px}" % (
            ("#13283A", "#00E5FF") if on else ("#0E1220", "#1F2638")))

    def set_pixmap(self, pm: QPixmap):
        self.img.setPixmap(pm.scaled(TILE, TILE, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def set_file(self, path: Path):
        if path.suffix.lower() == ".gif":
            reader = QImageReader(str(path))
            sz = reader.size()
            mv = QMovie(str(path))
            if sz.isValid() and sz.width() > 0:
                k = min(TILE / sz.width(), TILE / sz.height())
                mv.setScaledSize(QSize(max(1, int(sz.width() * k)), max(1, int(sz.height() * k))))
            mv.setCacheMode(QMovie.CacheNone)
            self._movie = mv
            self.img.setMovie(mv)
            mv.start()
        else:
            pm = QPixmap(str(path))
            if not pm.isNull():
                self.set_pixmap(pm)
            else:
                self.img.setText("sem prévia")

    def set_error(self, text="✕"):
        self.img.setText(text)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self)

    def mouseDoubleClickEvent(self, e):
        self.double_clicked.emit(self)

    def stop(self):
        if self._movie:
            self._movie.stop()

    def set_paused(self, paused: bool):
        if self._movie:
            self._movie.setPaused(paused)


class KeysDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chaves de API (grátis)")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        info = QLabel(
            "<b>GIPHY</b> e <b>Tenor</b> exigem uma chave gratuita, ligada a uma conta sua:<br><br>"
            "<b>GIPHY:</b> entre em developers.giphy.com → <i>Create an App</i> → escolha <b>API</b> → "
            "dê um nome (ex.: \"Telinha\") → copie a <i>API Key</i> e cole abaixo.<br>"
            "<b>Tenor:</b> no Google Cloud Console ative a <i>Tenor API</i> e crie uma chave em "
            "<i>APIs e serviços → Credenciais</i>.<br><br>"
            "Anime e Wikimedia funcionam sem chave nenhuma.")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        v.addWidget(info)
        f = QFormLayout()
        self.giphy = QLineEdit(cfg.giphy_key)
        self.giphy.setPlaceholderText("cole a API Key do GIPHY")
        self.giphy.setEchoMode(QLineEdit.Password)
        self.tenor = QLineEdit(cfg.tenor_key)
        self.tenor.setPlaceholderText("cole a chave do Tenor (Google)")
        self.tenor.setEchoMode(QLineEdit.Password)
        self.rating = QComboBox()
        for label, val in (("Livre (G)", "g"), ("Leve (PG)", "pg"), ("Moderado (PG-13)", "pg-13"), ("Adulto (R)", "r")):
            self.rating.addItem(label, val)
        self.rating.setCurrentIndex(max(0, self.rating.findData(cfg.gif_rating)))
        f.addRow("GIPHY", self.giphy)
        f.addRow("Tenor", self.tenor)
        f.addRow("Filtro de conteúdo", self.rating)
        v.addLayout(f)
        # as chaves ficam mascaradas; o usuário marca para conferir o que colou
        self.chk_show = QCheckBox("Mostrar chaves")
        self.chk_show.toggled.connect(self._toggle_echo)
        v.addWidget(self.chk_show)
        row = QHBoxLayout()
        for label, url in (("Abrir site do GIPHY", "https://developers.giphy.com/dashboard/?create=true"),
                           ("Abrir guia do Tenor", "https://developers.google.com/tenor/guides/quickstart")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            row.addWidget(b)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _toggle_echo(self, show: bool):
        mode = QLineEdit.Normal if show else QLineEdit.Password
        self.giphy.setEchoMode(mode)
        self.tenor.setEchoMode(mode)


class LibraryTab(QWidget):
    use_now = Signal(list)        # caminhos → tocar já
    add_to_playlist = Signal(list)
    config_changed = Signal()

    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.lib = Library()
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.bridge = _Bridge()
        self.bridge.call.connect(lambda fn: fn())
        self.tiles: list[Tile] = []
        self.selected: Tile | None = None
        self.gen = 0
        self.offset = 0
        self.query = ""
        self.provider = None
        self.source = cfg.library_provider if (cfg.library_provider in SOURCES or cfg.library_provider in SPECIAL) else "workshop"
        self.busy_steam = False

        v = QVBoxLayout(self)
        v.setSpacing(8)

        top = QHBoxLayout()
        self.cb_source = QComboBox()
        self.cb_source.addItem(SOURCES["workshop"][0], "workshop")
        for k, label in SPECIAL.items():
            self.cb_source.addItem(label, k)
        for k, (label, _) in PROVIDERS.items():
            self.cb_source.addItem(label, k)
        self.cb_source.setCurrentIndex(max(0, self.cb_source.findData(self.source)))
        self.cb_source.currentIndexChanged.connect(self._on_source)
        top.addWidget(self.cb_source)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("buscar GIFs… (ex.: neon, gato, anime, espaço)")
        self.ed_search.returnPressed.connect(self._do_search)
        top.addWidget(self.ed_search, 1)
        b = QPushButton("Buscar")
        b.clicked.connect(self._do_search)
        top.addWidget(b)
        b = QPushButton("Em alta")
        b.clicked.connect(self._do_trending)
        top.addWidget(b)
        b = QPushButton("Chaves de API…")
        b.clicked.connect(self._edit_keys)
        top.addWidget(b)
        v.addLayout(top)

        self.ws_bar = QWidget()
        wl = QHBoxLayout(self.ws_bar)
        wl.setContentsMargins(0, 0, 0, 0)
        self.cb_sort = QComboBox()
        for k, label in WS_SORTS.items():
            self.cb_sort.addItem(label, k)
        self.cb_type = QComboBox()
        for k, label in WS_TYPES.items():
            self.cb_type.addItem(label, k)
        self.cb_sort.currentIndexChanged.connect(self._ws_filters)
        self.cb_type.currentIndexChanged.connect(self._ws_filters)
        self.lbl_tag = QLabel("")
        self.lbl_tag.setStyleSheet("color:#00E5FF;font-weight:600")
        wl.addWidget(QLabel("Ordem"))
        wl.addWidget(self.cb_sort)
        wl.addWidget(QLabel("Tipo"))
        wl.addWidget(self.cb_type)
        wl.addWidget(self.lbl_tag)
        wl.addStretch(1)
        hint = QLabel("▶ vídeo toca completo · cenas usam a prévia animada")
        hint.setStyleSheet("color:#8A93A6;font-size:8pt")
        wl.addWidget(hint)
        v.addWidget(self.ws_bar)

        self.chips_area = QScrollArea()
        self.chips_area.setFixedHeight(46)
        self.chips_area.setWidgetResizable(True)
        self.chips_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.chips_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(self.chips_area)

        self.local_bar = QWidget()
        lb = QHBoxLayout(self.local_bar)
        lb.setContentsMargins(0, 0, 0, 0)
        self.chk_fav = QCheckBox("Só favoritos")
        self.chk_fav.toggled.connect(lambda _: self._show_local())
        lb.addWidget(self.chk_fav)
        lb.addStretch(1)
        for label, fn in (("Importar arquivos…", self._import), ("Abrir pasta", lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(LIB_DIR))))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            lb.addWidget(b)
        v.addWidget(self.local_bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background:transparent")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        self.cols = 4
        self.scroll.viewport().installEventFilter(self)

        mid = QHBoxLayout()
        mid.addWidget(self.scroll, 1)
        panel = QFrame()
        panel.setFixedWidth(PREVIEW + 24)
        panel.setStyleSheet("QFrame{background:#0E1220;border:1px solid #1F2638;border-radius:10px}")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(12, 12, 12, 12)
        t = QLabel("Prévia")
        t.setStyleSheet("border:none;color:#00E5FF;font-weight:700")
        pl.addWidget(t)
        self.prev_img = QLabel("clique num item\npara ver a prévia")
        self.prev_img.setFixedSize(PREVIEW, PREVIEW)
        self.prev_img.setAlignment(Qt.AlignCenter)
        self.prev_img.setStyleSheet("background:#05070D;border:none;border-radius:8px;color:#55607A")
        pl.addWidget(self.prev_img)
        self.prev_info = QLabel("")
        self.prev_info.setWordWrap(True)
        self.prev_info.setStyleSheet("border:none;color:#AEB6C8;font-size:9pt")
        pl.addWidget(self.prev_info)
        note = QLabel("Ver a prévia não baixa nada. Só baixa/assina quando você clica em \"Mostrar na tela\".")
        note.setWordWrap(True)
        note.setStyleSheet("border:none;color:#55607A;font-size:8pt")
        pl.addWidget(note)
        pl.addStretch(1)
        mid.addWidget(panel)
        v.addLayout(mid, 1)
        self._prev_movie: QMovie | None = None
        self._prev_gen = 0

        self.btn_more = QPushButton("Carregar mais")
        self.btn_more.clicked.connect(self._load_more)
        v.addWidget(self.btn_more)

        bar = QFrame()
        bar.setStyleSheet("QFrame{background:#0E1220;border:1px solid #1F2638;border-radius:10px}")
        bl = QHBoxLayout(bar)
        info = QVBoxLayout()
        self.lbl_sel = QLabel("Selecione um item (duplo clique = mostrar na tela)")
        self.lbl_sel.setStyleSheet("border:none;font-weight:600")
        self.lbl_credit = QLabel("")
        self.lbl_credit.setStyleSheet("border:none;color:#8A93A6;font-size:8pt")
        info.addWidget(self.lbl_sel)
        info.addWidget(self.lbl_credit)
        bl.addLayout(info, 1)
        self.btn_use = QPushButton("Mostrar na tela")
        self.btn_use.setStyleSheet("background:#00E5FF;color:#05070D;font-weight:700;border:none")
        self.btn_use.clicked.connect(lambda: self._act("use"))
        self.btn_add = QPushButton("+ Playlist")
        self.btn_add.clicked.connect(lambda: self._act("add"))
        self.btn_save = QPushButton("Salvar")
        self.btn_save.clicked.connect(lambda: self._act("save"))
        self.btn_del = QPushButton("Apagar")
        self.btn_del.clicked.connect(lambda: self._act("delete"))
        self.btn_steam = QPushButton("Assinar na Steam")
        self.btn_steam.setToolTip("Assina e baixa pela sua Steam (também aparece no Wallpaper Engine do PC)")
        self.btn_steam.clicked.connect(lambda: self._act("subscribe"))
        bl.addWidget(self.btn_steam)
        self.btn_page = QPushButton("Página")
        self.btn_page.clicked.connect(lambda: self._act("page"))
        for b in (self.btn_use, self.btn_add, self.btn_save, self.btn_del, self.btn_page):
            bl.addWidget(b)
        v.addWidget(bar)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#8A93A6")
        v.addWidget(self.lbl_status)

        # o conteúdo só é carregado quando a aba aparece pela 1ª vez (no boot o app fica na bandeja
        # e não precisa baixar nada do Workshop)
        self._loaded = False

    # -- origem / navegação ----------------------------------------------
    def _on_source(self, *_):
        self.source = self.cb_source.currentData()
        self.cfg.library_provider = self.source
        self.config_changed.emit()
        online = self.source in SOURCES
        self.provider = SOURCES[self.source][1](self.cfg) if online else None
        self.ed_search.setEnabled(online)
        self.local_bar.setVisible(self.source == "local")
        self.ws_bar.setVisible(self.source == "workshop")
        if self.source == "workshop":
            self._ws_filters(fetch=False)
        self._build_chips()
        if self.source == "efeitos":
            self._show_effects()
        elif self.source == "local":
            self._show_local()
        elif self.source == "we_local":
            self._show_we_local()
        else:
            self._do_trending()

    def _ws_filters(self, *_, fetch=True):
        if not isinstance(self.provider, Workshop):
            return
        self.provider.sort = self.cb_sort.currentData()
        self.provider.wtype = self.cb_type.currentData()
        tag = WS_TAGS_PT.get(self.provider.tag, self.provider.tag)
        self.lbl_tag.setText(f"· categoria: {tag}" if tag else "· todas as categorias")
        if fetch:
            self.ed_search.clear()
            self._do_trending()

    def _build_chips(self):
        w = QWidget()
        w.setStyleSheet("background:transparent")
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        cats = self.provider.categories if self.provider else []
        for c in cats:
            b = QPushButton(c)
            b.setStyleSheet("padding:4px 10px;border-radius:12px")
            b.clicked.connect(lambda _=False, q=c: self._chip(q))
            h.addWidget(b)
        if not cats:
            note = {"efeitos": "Animações geradas ao vivo com as suas cores — funcionam sem internet.",
                    "we_local": "Wallpapers que você assinou no Workshop + os que vêm com o Wallpaper Engine.",
                    "local": f"Tudo que você salvou/baixou fica em {LIB_DIR}"}.get(self.source, "")
            lbl = QLabel(note)
            lbl.setStyleSheet("color:#8A93A6")
            h.addWidget(lbl)
        h.addStretch(1)
        self.chips_area.setWidget(w)

    def _chip(self, label: str):
        if isinstance(self.provider, Workshop):
            if WS_TAGS_PT.get(self.provider.tag) == label:
                self.provider.tag = ""  # clicar de novo tira o filtro
            else:
                self.provider.set_category(label)
            self._ws_filters()
        else:
            self.ed_search.setText(label)
            self._do_search()

    def _clear(self):
        self.gen += 1
        for t in self.tiles:
            t.stop()
            t.deleteLater()
        self.tiles.clear()
        self.selected = None
        self.lbl_sel.setText("Selecione um item (duplo clique = mostrar na tela)")
        self.lbl_credit.setText("")
        self._update_actions()
        if hasattr(self, "prev_img"):
            self._prev_gen += 1
            self._prev_clear("clique num item\npara ver a prévia")
            self.prev_info.setText("")

    def eventFilter(self, obj, event):
        # quantas colunas cabem: refaz a grade quando a largura muda
        if obj is self.scroll.viewport() and event.type() == event.Type.Resize:
            cols = max(2, (self.scroll.viewport().width() + 4) // (TILE + 18))
            if cols != self.cols:
                self.cols = cols
                for i, t in enumerate(self.tiles):
                    self.grid.addWidget(t, i // cols, i % cols)
        return False

    def _add_tile(self, tile: Tile):
        n = len(self.tiles)
        self.grid.addWidget(tile, n // self.cols, n % self.cols)
        tile.clicked.connect(self._select)
        tile.double_clicked.connect(lambda t: (self._select(t), self._act("use")))
        self.tiles.append(tile)

    def _select(self, tile: Tile):
        if self.selected and self.selected is not tile:
            try:
                self.selected.set_selected(False)
            except RuntimeError:
                pass
        self.selected = tile
        tile.set_selected(True)
        p = tile.payload
        if isinstance(p, Item):
            size = f" · {p.size / 1024 / 1024:.1f} MB" if p.size else ""
            dims = f" · {p.width}×{p.height}" if p.width else ""
            self.lbl_sel.setText(p.title[:70])
            self.lbl_credit.setText(f"{p.credit}{dims}{size}")
        elif isinstance(p, LocalWallpaper):
            kind = {"video": "Vídeo", "scene": "Cena (prévia animada)", "web": "Web (prévia)"}.get(p.type, p.type)
            self.lbl_sel.setText(p.title[:70])
            self.lbl_credit.setText(f"{kind} · {', '.join(p.tags) or p.source} · {p.folder}")
        elif isinstance(p, tuple):  # (nome, Entry)
            name, e = p
            self.lbl_sel.setText(("★ " if e.favorite else "") + (e.title or name)[:70])
            self.lbl_credit.setText(f"{e.source} · {name}" + (f" · {e.credit}" if e.credit else ""))
        else:
            self.lbl_sel.setText(effects.effect_label(p))
            self.lbl_credit.setText("Efeito gerado ao vivo · usa as cores da aba Personalizar")
        self._update_actions()
        self._show_preview(tile)

    # -- prévia grande ------------------------------------------------------
    def _prev_clear(self, text: str = ""):
        if self._prev_movie:
            self._prev_movie.stop()
            self._prev_movie = None
        self.prev_img.clear()
        self.prev_img.setText(text)

    def _prev_file(self, gen: int, path: Path | None):
        if gen != self._prev_gen:
            return
        self._prev_clear("" if path else "sem prévia")
        if not path:
            return
        reader = QImageReader(str(path))
        reader.setDecideFormatFromContent(True)
        if reader.supportsAnimation() and reader.imageCount() != 1:
            sz = reader.size()
            mv = QMovie(str(path))
            mv.setCacheMode(QMovie.CacheNone)
            if sz.isValid() and sz.width() > 0:
                k = min(PREVIEW / sz.width(), PREVIEW / sz.height())
                mv.setScaledSize(QSize(max(1, int(sz.width() * k)), max(1, int(sz.height() * k))))
            self._prev_movie = mv
            self.prev_img.setMovie(mv)
            mv.start()
        else:
            pm = QPixmap(str(path))
            if pm.isNull():
                self.prev_img.setText("sem prévia")
            else:
                self.prev_img.setPixmap(pm.scaled(PREVIEW, PREVIEW, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _show_preview(self, tile: Tile):
        self._prev_gen += 1
        gen = self._prev_gen
        p = tile.payload
        self._prev_clear("carregando…")
        if isinstance(p, Item):
            kind = "vídeo" if p.kind == "video" else "GIF"
            if p.provider == "workshop":
                kind = {"Video": "vídeo (toca completo)", "Scene": "cena (mostra esta prévia)",
                        "Web": "web (mostra esta prévia)"}.get(p.extra.get("we_type"), kind)
            size = f" · {p.size / 1024 / 1024:.1f} MB" if p.size else ""
            self.prev_info.setText(f"<b>{p.title[:80]}</b><br>{kind}{size}<br>{p.credit}")
            url = p.extra.get("preview") or (p.media_url if p.provider in ("workshop", "anime") else "") or p.thumb_url

            def work():
                try:
                    path = fetch_preview(url) if url else None
                except Exception:
                    path = None
                self.bridge.call.emit(lambda: self._prev_file(gen, path))

            self.pool.submit(work)
        elif isinstance(p, LocalWallpaper):
            kinds = {"video": "vídeo (toca completo)", "scene": "cena (mostra a prévia)", "web": "web (mostra a prévia)"}
            self.prev_info.setText(f"<b>{p.title[:80]}</b><br>{kinds.get(p.type, p.type)} · "
                                   f"{', '.join(p.tags) or p.source}")
            if p.preview:
                self._prev_file(gen, p.preview)
            elif p.video_file:
                vf = p.video_file

                def work_local():
                    th = local_thumb(vf, PREVIEW)
                    self.bridge.call.emit(lambda: self._prev_file(gen, th))

                self.pool.submit(work_local)
            else:
                self._prev_file(gen, None)
        elif isinstance(p, tuple):
            name, e = p
            path = self.lib.path(e)
            self.prev_info.setText(f"<b>{(e.title or name)[:80]}</b><br>{e.source} · {name}")
            if path.suffix.lower() in (".gif", ".webp", ".png", ".jpg", ".jpeg", ".bmp"):
                self._prev_file(gen, path)
            else:
                def work_lib():
                    th = local_thumb(path, PREVIEW)
                    self.bridge.call.emit(lambda: self._prev_file(gen, th))

                self.pool.submit(work_lib)
        else:
            self.prev_info.setText(f"<b>{effects.effect_label(p)}</b><br>efeito gerado ao vivo com as suas cores")
            pm = tile.img.pixmap()
            self._prev_clear()
            if pm is not None and not pm.isNull():
                self.prev_img.setPixmap(pm.scaled(PREVIEW, PREVIEW, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _update_actions(self):
        p = self.selected.payload if self.selected else None
        has = p is not None
        self.btn_use.setEnabled(has)
        self.btn_add.setEnabled(has)
        self.btn_save.setVisible(isinstance(p, tuple) or (isinstance(p, Item) and p.provider != "workshop"))
        self.btn_save.setText("★ Favorito" if isinstance(p, tuple) else "Salvar")
        self.btn_del.setVisible(isinstance(p, tuple))
        self.btn_page.setVisible(isinstance(p, Item) or (isinstance(p, tuple) and bool(p[1].page_url))
                                 or (isinstance(p, LocalWallpaper) and p.source == "workshop"))
        self.btn_steam.setVisible(isinstance(p, Item) and p.provider == "workshop")
        if isinstance(p, LocalWallpaper):
            self.btn_del.setVisible(p.source == "workshop")
            self.btn_del.setText("Cancelar assinatura")
        else:
            self.btn_del.setText("Apagar")

    def status(self, text: str):
        self.lbl_status.setText(text)

    # -- efeitos / local --------------------------------------------------
    def _show_effects(self):
        self._clear()
        self.btn_more.hide()
        for key, label in effects.EFFECTS.items():
            tile = Tile(effects.PREFIX + key, label)
            self._add_tile(tile)
            try:
                fx = effects.make(key, self.cfg.colors)
                img = None
                for i in range(12):
                    img = fx.frame(1.0 + i / 6)
                tile.set_pixmap(_pil_pixmap(img, TILE))
            except Exception as exc:
                log.warning("prévia do efeito %s: %s", key, exc)
                tile.set_error()
        self.status(f"{len(effects.EFFECTS)} efeitos — duplo clique para ver na tela")

    def _show_local(self):
        if self.source != "local":
            return
        self._clear()
        self.btn_more.hide()
        self.lib.load()
        entries = self.lib.sorted_entries(self.chk_fav.isChecked())
        gen = self.gen
        for name, e in entries:
            tile = Tile((name, e), e.title or name, "★" if e.favorite else "")
            self._add_tile(tile)
            path = self.lib.path(e)
            if path.suffix.lower() == ".gif":
                tile.set_file(path)
            else:
                self.pool.submit(self._bg_local_thumb, gen, tile, path)
        total = sum(self.lib.path(e).stat().st_size for _, e in entries if self.lib.path(e).exists())
        self.status(f"{len(entries)} itens · {total / 1024 / 1024:.0f} MB em {LIB_DIR}" if entries else
                    "Biblioteca vazia — salve itens das buscas ou use \"Importar arquivos…\"")

    def _show_we_local(self):
        self._clear()
        self.btn_more.hide()
        try:
            items = local_wallpapers()
        except Exception as exc:
            self.status(f"não achei o Wallpaper Engine: {exc}")
            return
        gen = self.gen
        for w in items:
            badge = "▶" if w.type == "video" else ""
            tile = Tile(w, w.title, badge)
            self._add_tile(tile)
            pv = w.preview
            if pv is not None:
                tile.set_file(pv)
            elif w.video_file:
                self.pool.submit(self._bg_local_thumb, gen, tile, w.video_file)
            else:
                tile.set_error("sem prévia")
        n_ws = sum(1 for w in items if w.source == "workshop")
        self.status(f"{len(items)} wallpapers ({n_ws} assinados no Workshop) — ▶ = vídeo completo"
                    if items else "nenhum wallpaper do Wallpaper Engine encontrado")

    def refresh_we_local(self):
        if self.source == "we_local":
            self._show_we_local()

    def _bg_local_thumb(self, gen, tile, path):
        thumb = local_thumb(path, TILE)
        self.bridge.call.emit(lambda: self._set_tile_file(gen, tile, thumb))

    def _set_tile_file(self, gen, tile, path):
        if gen != self.gen:
            return
        try:
            if path:
                tile.set_file(path)
            else:
                tile.set_error("sem prévia")
        except RuntimeError:
            pass  # tile já foi destruída

    def refresh_local(self):
        if self.source == "local":
            self._show_local()

    def _import(self):
        exts = " ".join(f"*{e}" for e in sorted(MEDIA_EXT))
        paths, _ = QFileDialog.getOpenFileNames(self, "Importar para a biblioteca", "", f"Mídia ({exts})")
        if paths:
            self.lib.import_files(paths)
            self._show_local()

    # -- busca online -------------------------------------------------------
    def _do_search(self):
        if not self.provider:
            return
        q = self.ed_search.text().strip()
        if not q:
            return self._do_trending()
        self.query = q
        self._fetch(reset=True)

    def _do_trending(self):
        if not self.provider:
            return
        self.query = ""
        self._fetch(reset=True)

    def _load_more(self):
        self._fetch(reset=False)

    def _fetch(self, reset: bool):
        if reset:
            self._clear()
            self.offset = 0
        gen = self.gen
        prov, query, offset = self.provider, self.query, self.offset
        self.btn_more.hide()
        self.status(f"buscando {'“' + query + '”' if query else 'em alta'} no {prov.name}…")

        def work():
            try:
                items = prov.search(query, offset) if query else prov.trending(offset)
                self.bridge.call.emit(lambda: self._show_results(gen, items))
            except ProviderError as exc:
                msg = str(exc)
                self.bridge.call.emit(lambda: self._fail(gen, msg))
            except Exception as exc:
                log.exception("busca")
                msg = f"erro: {exc}"
                self.bridge.call.emit(lambda: self._fail(gen, msg))

        self.pool.submit(work)

    def _fail(self, gen, msg):
        if gen != self.gen:
            return
        self.status(msg)
        if self.provider and self.provider.needs_key and not self.provider.api_key:
            self._edit_keys()

    def _show_results(self, gen, items: list[Item]):
        if gen != self.gen:
            return
        seen = {t.payload.uid for t in self.tiles if isinstance(t.payload, Item)}
        new = [it for it in items if it.uid not in seen]
        for it in new:
            badge = "▶" if it.kind == "video" else ""
            tile = Tile(it, it.title, badge)
            self._add_tile(tile)
            if it.thumb_url:
                self.pool.submit(self._bg_thumb, gen, tile, it.thumb_url)
            else:
                tile.set_error("sem prévia")
        self.offset += len(items)
        self.btn_more.setVisible(bool(items))
        credit = f" · {self.provider.credit}" if self.provider.credit else ""
        self.status(f"{len(self.tiles)} resultados{credit}" if self.tiles else "nada encontrado")

    def _bg_thumb(self, gen, tile, url):
        path = None
        for attempt in range(2):  # um soluço de rede não deve deixar a tile em ✕ até refazer a busca
            try:
                path = fetch_thumb(url)
                break
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
        self.bridge.call.emit(lambda: self._set_tile_file(gen, tile, path))

    # -- ações ----------------------------------------------------------------
    def _act(self, action: str):
        if not self.selected:
            return
        p = self.selected.payload
        if isinstance(p, str):  # efeito
            if action == "use":
                self.use_now.emit([p])
            elif action == "add":
                self.add_to_playlist.emit([p])
                self.status(f"{effects.effect_label(p)} adicionado à playlist")
            return
        if isinstance(p, LocalWallpaper):
            path = p.playable()
            if action in ("use", "add"):
                if not path:
                    self.status("esse wallpaper não tem vídeo nem prévia para mostrar")
                    return
                (self.use_now if action == "use" else self.add_to_playlist).emit([str(path)])
                self.status(f"“{p.title[:40]}” {'na tela' if action == 'use' else 'adicionado à playlist'}"
                            + ("" if p.type == "video" else " (prévia animada da cena)"))
            elif action == "page":
                QDesktopServices.openUrl(QUrl(f"https://steamcommunity.com/sharedfiles/filedetails/?id={p.id}"))
            elif action == "delete":
                if QMessageBox.question(self, "Cancelar assinatura",
                                        f"Cancelar a assinatura de “{p.title}” na Steam?\n"
                                        "Ele sai do Wallpaper Engine também.") == QMessageBox.Yes:
                    self._steam(["unsubscribe", p.id], p.title, after=lambda _f: self.refresh_we_local())
            return
        if isinstance(p, tuple):
            name, e = p
            path = str(self.lib.path(e))
            if action == "use":
                self.use_now.emit([path])
            elif action == "add":
                self.add_to_playlist.emit([path])
                self.status(f"{e.title or name} adicionado à playlist")
            elif action == "save":
                fav = self.lib.toggle_favorite(name)
                self.status("marcado como favorito" if fav else "removido dos favoritos")
                self._show_local()
            elif action == "delete":
                if QMessageBox.question(self, "Apagar", f"Apagar \"{e.title or name}\" da biblioteca?") == QMessageBox.Yes:
                    self.lib.remove(name)
                    self._show_local()
            elif action == "page" and e.page_url:
                _open_external(e.page_url)
            return
        item: Item = p
        if action == "page":
            _open_external(item.page_url or item.media_url)
            return
        if item.provider == "workshop":
            self._ws_action(item, action)
            return
        if item.size > 60 * 1024 * 1024 and not self.lib.has(item):
            if QMessageBox.question(self, "Arquivo grande",
                                    f"Esse arquivo tem {item.size / 1024 / 1024:.0f} MB. Baixar mesmo assim?") != QMessageBox.Yes:
                return
        self._download(item, action)

    def _download(self, item: Item, action: str):
        self.status(f"baixando “{item.title[:40]}”…")

        def prog(f):
            self.bridge.call.emit(lambda: self.status(f"baixando “{item.title[:40]}”… {f * 100:.0f}%"))

        def work():
            try:
                path = self.lib.download(item, prog)
                self.bridge.call.emit(lambda: self._downloaded(item, action, path))
            except Exception as exc:
                msg = f"falha ao baixar: {exc}"
                self.bridge.call.emit(lambda: self.status(msg))

        self.pool.submit(work)

    def _downloaded(self, item: Item, action: str, path: Path):
        where = "assinado no seu Wallpaper Engine" if item.provider == "workshop" else "salvo na sua biblioteca"
        if action == "use":
            self.use_now.emit([str(path)])
            self.status(f"“{item.title[:40]}” na tela ({where})")
        elif action == "add":
            self.add_to_playlist.emit([str(path)])
            self.status(f"“{item.title[:40]}” adicionado à playlist")
        else:
            self.status(f"“{item.title[:40]}” salvo na sua biblioteca")

    # -- Workshop / Steam ------------------------------------------------------
    def _ws_action(self, item: Item, action: str):
        wtype = item.extra.get("we_type", "Scene")
        if action == "subscribe" or wtype == "Video":
            if action != "subscribe" and item.size > BIG_DOWNLOAD:
                if QMessageBox.question(self, "Vídeo grande",
                                        f"Esse vídeo tem {item.size / 1024 / 1024:.0f} MB. A Steam vai assinar e "
                                        "baixar ele na sua conta. Continuar?") != QMessageBox.Yes:
                    return

            def after(folder):
                if action == "subscribe" or not folder:
                    return
                from .steam_ws import _read_project

                w = _read_project(Path(folder), "workshop")
                path = w.playable() if w else None
                if path:
                    self._downloaded(item, action, path)
                else:
                    self.status("baixou, mas não achei o vídeo dentro da pasta")

            self._steam(["subscribe", item.extra["workshop_id"]], item.title, after)
            return
        # cenas/web não rodam fora do Wallpaper Engine → usa a prévia animada (sem precisar da Steam)
        self._download(item, action)

    def _steam(self, args: list, title: str, after=None):
        if self.busy_steam:
            self.status("a Steam ainda está cuidando do item anterior…")
            return
        self.busy_steam = True
        verb = "cancelando assinatura de" if args[0] == "unsubscribe" else "Steam: assinando e baixando"
        self.status(f"{verb} “{title[:40]}”…")

        def on_line(msg: dict):
            if "progress" in msg and msg.get("total"):
                t = (f"{verb} “{title[:30]}”… {msg['progress'] * 100:.0f}% "
                     f"({msg['downloaded'] / 1048576:.0f}/{msg['total'] / 1048576:.0f} MB)")
                self.bridge.call.emit(lambda: self.status(t))
            elif "status" in msg:
                t = msg["status"]
                self.bridge.call.emit(lambda: self.status(t))
            elif "error" in msg:
                t = "Steam: " + msg["error"]
                self.bridge.call.emit(lambda: self.status(t))
            elif msg.get("done"):
                folder = msg.get("folder", "")

                def fin():
                    self.status(f"pronto: “{title[:40]}”" + (" — também está no seu Wallpaper Engine" if folder else ""))
                    if after:
                        after(folder)
                self.bridge.call.emit(fin)

        def work():
            try:
                run_helper(args, on_line)
            except Exception as exc:
                msg = f"falha ao falar com a Steam: {exc}"
                self.bridge.call.emit(lambda: self.status(msg))
            finally:
                self.busy_steam = False

        self.pool.submit(work)

    def _edit_keys(self):
        dlg = KeysDialog(self.cfg, self)
        if dlg.exec() == QDialog.Accepted:
            self.cfg.giphy_key = dlg.giphy.text().strip()
            self.cfg.tenor_key = dlg.tenor.text().strip()
            self.cfg.gif_rating = dlg.rating.currentData()
            self.config_changed.emit()
            if self.source in SOURCES:
                self._on_source()

    # miniaturas animadas só gastam CPU enquanto a aba está na tela
    def showEvent(self, e):
        super().showEvent(e)
        if not self._loaded:
            self._loaded = True
            self._on_source()
        for t in self.tiles:
            t.set_paused(False)

    def hideEvent(self, e):
        super().hideEvent(e)
        for t in self.tiles:
            t.set_paused(True)

    def colors_changed(self):
        if self._loaded and self.source == "efeitos":
            self._show_effects()

    def shutdown(self):
        from .steam_ws import stop_helpers

        stop_helpers()  # sem isso o app "fechava" mas ficava vivo até um download grande da Steam acabar
        self.pool.shutdown(wait=False, cancel_futures=True)
