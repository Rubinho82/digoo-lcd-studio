"""Abre o Digoo LCD Studio sem janela de console (python DigooLCD.pyw)."""

import os

# Antes de qualquer import: o OpenBLAS do numpy reserva ~32 MB por núcleo (516 MB neste PC) só por
# ser importado. O app não faz álgebra linear, então 1 thread basta e a reserva cai para ~11 MB.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import ctypes  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

PIPE = r"\\.\pipe\DigooLCDStudio-single-instance"  # o canal do QLocalServer do app aberto
TASK = "DigooLCDStudio"
CREATE_NO_WINDOW = 0x08000000


def _signal_running() -> bool:
    """Pede para o app que já está aberto mostrar a janela. Não precisa de administrador."""
    for _ in range(10):
        try:
            with open(PIPE, "wb", buffering=0) as f:
                f.write(b"show")
            return True
        except FileNotFoundError:
            return False  # nenhum app aberto
        except OSError:
            time.sleep(0.1)  # canal ocupado: tenta de novo
    return False


def _start_via_task() -> bool:
    """Liga o app pela tarefa de início com o Windows (já tem administrador, sem pedir UAC)."""
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, creationflags=CREATE_NO_WINDOW,
                           timeout=15)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    for _ in range(40):  # a tarefa abre minimizado: espera o app subir e pede a janela
        time.sleep(0.5)
        if _signal_running():
            return True
    return True


def _ensure_admin() -> None:
    # Os sensores de CPU/placa-mãe precisam de administrador.
    if ctypes.windll.shell32.IsUserAnAdmin() or "--steam-ws" in sys.argv:
        return
    if _signal_running() or _start_via_task():
        sys.exit(0)
    frozen = getattr(sys, "frozen", False)
    args = sys.argv[1:] if frozen else sys.argv
    params = " ".join(f'"{a}"' for a in args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if rc > 32:
        sys.exit(0)
    # Usuário recusou o UAC: segue sem admin (GPU/RAM/uso continuam funcionando).


def _crash_log(text: str) -> None:
    """Grava direto em arquivo, sem depender do logging (útil quando o app sobe pelo Agendador)."""
    try:
        import datetime

        folder = Path(os.environ.get("APPDATA", Path.home())) / "DigooLCD"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "crash.log", "a", encoding="utf8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {sys.argv[1:]} {text}\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        # _ensure_admin() dentro do try: falhas antes da janela (import do PySide6,
        # QLocalServer.listen, elevação) passam a ter feedback visível em vez de sumir.
        _ensure_admin()
        from digoo_lcd.main import main

        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback

        _crash_log("\n" + traceback.format_exc())
        # App é --windowed (sem console): sem isto o raise não aparece em lugar nenhum.
        try:
            log_path = Path(os.environ.get("APPDATA", Path.home())) / "DigooLCD" / "crash.log"
            ctypes.windll.user32.MessageBoxW(
                None,
                "O Digoo LCD Studio não conseguiu abrir.\n\n"
                f"Os detalhes foram salvos em:\n{log_path}\n\n"
                "Para um relatório completo, rode no Prompt:\n"
                "DigooLCD.exe --diagnostico",
                "Digoo LCD Studio — erro ao abrir",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
        raise
