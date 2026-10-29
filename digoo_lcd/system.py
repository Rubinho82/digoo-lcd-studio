"""Integração com o Windows: iniciar com o sistema, admin e driver PawnIO."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

TASK_NAME = "DigooLCDStudio"
CREATE_NO_WINDOW = 0x08000000
PAWNIO_URL = "https://github.com/namazso/PawnIO.Setup/releases/latest/download/PawnIO_setup.exe"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def launch_command() -> tuple[str, str]:
    """(programa, argumentos) para abrir o app minimizado."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--minimized"
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    main = Path(__file__).resolve().parent.parent / "DigooLCD.pyw"
    return str(pyw if pyw.exists() else exe), f'"{main}" --minimized'


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW, timeout=20)


def autostart_enabled() -> bool:
    try:
        return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0
    except Exception:
        return False


def set_autostart(enabled: bool) -> tuple[bool, str]:
    if not enabled:
        r = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
        return (r.returncode == 0 or not autostart_enabled()), (r.stderr or r.stdout).strip()
    prog, args = launch_command()
    # Tarefa no logon com privilégio máximo: abre como admin sem pedir UAC.
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Digoo LCD Studio — telinha do watercooler</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{os.environ.get("USERDOMAIN", "")}\\{os.environ.get("USERNAME", "")}</UserId><Delay>PT10S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>6</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{_xml(prog)}</Command><Arguments>{_xml(args)}</Arguments></Exec></Actions>
</Task>"""
    path = Path(tempfile.gettempdir()) / "digoolcd_task.xml"
    path.write_text(xml, encoding="utf-16")
    try:
        r = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(path), "/F")
    finally:
        try:
            path.unlink()
        except Exception:
            pass
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def download_pawnio(progress=None) -> Path:
    dest = Path(tempfile.gettempdir()) / "PawnIO_setup.exe"
    req = urllib.request.Request(PAWNIO_URL, headers={"User-Agent": "DigooLCDStudio"})
    from .library import SSL_CTX

    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    return dest


def run_installer(path: Path) -> int:
    """Roda o instalador (UI própria dele) e espera terminar."""
    return subprocess.run([str(path)]).returncode


def open_folder(path: Path) -> None:
    os.startfile(str(path))  # noqa: S606
