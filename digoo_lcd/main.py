"""Ponto de entrada: instância única, log, sensores, motor e janela."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import APP_NAME, config as config_mod
from .config import config_dir

SERVER_BASE = "DigooLCDStudio-single-instance"


def _user_scope() -> str:
    """SID do usuário atual, para escopar o named pipe por usuário (evita que outra conta ocupe o nome)."""
    import ctypes
    from ctypes import wintypes

    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError("OpenProcessToken")
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
                raise OSError("GetTokenInformation")
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
            str_ptr = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(str_ptr)):
                raise OSError("ConvertSidToStringSid")
            try:
                return str_ptr.value or "default"
            finally:
                kernel32.LocalFree(str_ptr)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        # fallback: nome de usuário (escopa por conta, menos único que o SID)
        import getpass

        try:
            return getpass.getuser()
        except Exception:
            return "default"


class _LogHandler(RotatingFileHandler):
    def handleError(self, record):
        # sem console o logging engole o erro em silêncio; registra em crash.log para dar para investigar
        import traceback

        try:
            with open(config_dir() / "crash.log", "a", encoding="utf8") as f:
                f.write("falha ao gravar app.log:\n" + traceback.format_exc() + "\n")
        except Exception:
            pass


def setup_logging() -> None:
    handler = _LogHandler(config_dir() / "app.log", maxBytes=1_000_000, backupCount=2, encoding="utf8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:
        root.addHandler(logging.StreamHandler())


def _attach_stdout() -> None:
    """No .exe sem console sys.stdout é None; liga no pipe que o processo pai passou."""
    if sys.stdout is not None:
        return
    import ctypes
    import msvcrt
    import os

    handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    if handle and handle != -1:
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
            sys.stdout = open(fd, "w", encoding="utf8", buffering=1)
        except OSError:
            pass


def diagnostics(video: str | None) -> int:
    """Testa tudo sem abrir a janela e grava diagnostico.txt na pasta de dados."""
    import platform
    import time

    from . import __version__, render
    from .protocol import Panel, find_panel
    from .sensors import HELPER, SensorHub, pawnio_installed
    from .sources import MediaPlayer
    from .system import is_admin

    lines = [f"{APP_NAME} {__version__} — {platform.platform()} — python {platform.python_version()}",
             f"admin: {is_admin()}", f"sensorhelper: {HELPER} existe={HELPER.exists()}",
             f"PawnIO instalado: {pawnio_installed()}"]
    port = find_panel()
    lines.append(f"porta da tela: {port}")
    if port:
        try:
            p = Panel(port)
            info = p.connect()
            lines.append(f"tela: {info.model} fw {info.version} {info.width}x{info.height} ângulo {info.angle} "
                         f"limite {info.max_frame_kb} KB uid {info.uid}")
            p.close(release=False)
        except Exception as exc:
            lines.append(f"tela: ERRO {exc}")
    hub = SensorHub(0.5)
    hub.start()
    time.sleep(4)
    m = hub.snapshot()
    hub.stop()
    for k, v in m.items():
        if k not in ("history", "now"):
            lines.append(f"  {k} = {v}")
    lines.append(f"sensores brutos: {len(hub.all_sensors())}")
    cfg = config_mod.Config()
    for theme in render.THEMES:
        t = time.perf_counter()
        render.render(cfg, m, theme)
        lines.append(f"tema {theme}: {(time.perf_counter() - t) * 1000:.0f} ms")
    try:
        import av

        lines.append(f"PyAV {av.__version__}")
        if video:
            mp = MediaPlayer()
            mp.configure([video], 5, "cover")
            t = time.time()
            frame = None
            while time.time() - t < 3 and frame is None:
                frame = mp.frame()
                time.sleep(0.05)
            mp.close()
            lines.append(f"vídeo {video}: {'OK ' + str(frame.size) if frame else 'SEM QUADROS'}")
    except Exception as exc:
        lines.append(f"PyAV: ERRO {exc}")
    out = config_dir() / "diagnostico.txt"
    out.write_text("\n".join(lines), encoding="utf8")
    if sys.stdout is not None:  # chamado pela interface (sem console) o stdout é None
        print("\n".join(lines))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="DigooLCD")
    parser.add_argument("--minimized", action="store_true", help="abre só na bandeja")
    parser.add_argument("--steam-ws", nargs="+", metavar="CMD", help=argparse.SUPPRESS)
    parser.add_argument("--diagnostico", nargs="?", const="", metavar="VIDEO",
                        help="testa tela/sensores/temas (e um vídeo opcional) e grava diagnostico.txt")
    args = parser.parse_args()

    if args.steam_ws:
        # Processo auxiliar da Steam: sem janela, sem instância única, fala JSON pelo stdout.
        _attach_stdout()
        from .steam_ws import helper_main

        return helper_main(args.steam_ws)

    setup_logging()
    log = logging.getLogger("digoo_lcd")
    if args.diagnostico is not None:
        return diagnostics(args.diagnostico or None)

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    # nome do pipe escopado ao usuário (SID): no PC multiusuário outra conta não ocupa o nome
    server_name = f"{SERVER_BASE}-{_user_scope()}"

    # Instância única: se já está aberto, só pede para a janela aparecer.
    sock = QLocalSocket()
    sock.connectToServer(server_name)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    # o atalho abre sem administrador: o "canal" precisa aceitar quem não está elevado
    server.setSocketOptions(QLocalServer.SocketOption.WorldAccessOption)
    server.listen(server_name)

    from .engine import Engine
    from .gui import MainWindow
    from .sensors import SensorHub
    from .system import is_admin

    cfg = config_mod.load()
    log.info("iniciando (admin=%s)", is_admin())
    # abertura rápida: só cria os objetos (barato) e pinta a janela; o trabalho pesado vem logo depois.
    hub = SensorHub(cfg.sensor_interval)
    engine = Engine(cfg, hub)

    win = MainWindow(cfg, engine, hub)

    def on_new_connection():
        c = server.nextPendingConnection()
        if not c:
            return

        def handle():
            if c.bytesAvailable():
                c.readAll()
                win.show_window()

        c.readyRead.connect(handle)
        c.disconnected.connect(c.deleteLater)
        handle()  # o atalho escreve e fecha na hora: o pedido pode já ter chegado

    server.newConnection.connect(on_new_connection)

    if not (args.minimized or cfg.start_minimized):
        win.show()

    def _boot():
        # inicialização pesada depois que a janela já pintou: sobe o sensorhelper e conecta à tela
        hub.start()
        engine.start()
        # conserta a tarefa de início com o Windows se o app foi movido de pasta (roda schtasks: em thread)
        import threading as _th

        from . import system as _system
        _th.Thread(target=_system.reconcile_autostart, daemon=True).start()

    QTimer.singleShot(0, _boot)

    def shutdown():
        log.info("encerrando")
        config_mod.save(win.cfg)
        engine.stop(release=win.cfg.release_on_exit)
        hub.stop()

    app.aboutToQuit.connect(shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
