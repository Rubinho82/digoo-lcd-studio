"""Integração com o Windows: iniciar com o sistema, admin e driver PawnIO."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

TASK_NAME = "DigooLCDStudio"
CREATE_NO_WINDOW = 0x08000000
PAWNIO_URL = "https://github.com/namazso/PawnIO.Setup/releases/latest/download/PawnIO_setup.exe"
# Nome esperado no certificado que assina o PawnIO_setup.exe (defesa contra troca do asset 'latest').
PAWNIO_PUBLISHER = "namazso"
RELEASES_API = "https://api.github.com/repos/Rubinho82/digoo-lcd-studio/releases/latest"


def _version_tuple(v: str) -> tuple:
    """"v1.2.10" -> (1, 2, 10). Ignora sufixos não numéricos em cada parte."""
    parts = []
    for p in str(v).strip().lstrip("vV").split("."):
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def check_update(current_version: str):
    """Consulta o release mais novo no GitHub. Devolve {'latest', 'url'} se houver versão
    mais nova, senão None. NUNCA levanta exceção (None em qualquer falha); chamada em thread."""
    try:
        from .library import SSL_CTX

        req = urllib.request.Request(
            RELEASES_API,
            headers={"User-Agent": "DigooLCDStudio", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=6, context=SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "")
        if tag and _version_tuple(tag) > _version_tuple(current_version):
            return {"latest": tag.lstrip("vV"), "url": data.get("html_url") or ""}
        return None
    except Exception:
        return None


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


def _unxml(s: str) -> str:
    return (s.replace("&quot;", '"').replace("&gt;", ">").replace("&lt;", "<")
             .replace("&amp;", "&"))


def _task_command() -> str | None:
    """Lê o <Command> da tarefa agendada via schtasks /XML, ou None se não der."""
    try:
        r = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
        if r.returncode != 0:
            return None
        m = re.search(r"<Command>(.*?)</Command>", r.stdout, re.DOTALL)
        return _unxml(m.group(1).strip()) if m else None
    except Exception:
        return None


def reconcile_autostart() -> None:
    """Se a tarefa de início aponta para um .exe diferente do que está rodando (ex.: usuário
    atualizou trocando a pasta), recria a tarefa apontando para o atual. Silencioso: qualquer
    falha (sem admin, schtasks indisponível) é ignorada para não travar o boot."""
    try:
        if not autostart_enabled():
            return
        current = _task_command()
        want = launch_command()[0]
        if current and os.path.normcase(os.path.normpath(current)) != os.path.normcase(os.path.normpath(want)):
            set_autostart(True)
    except Exception:
        pass


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


def _win_verify_trust(path: Path) -> bool:
    """WinVerifyTrust: True só se o .exe está assinado e a cadeia é válida/confiável."""
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.c_void_p)]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    # WINTRUST_ACTION_GENERIC_VERIFY_V2
    guid = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    fi = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), str(path), None, None)
    wd = WINTRUST_DATA()
    wd.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    wd.dwUIChoice = 2            # WTD_UI_NONE
    wd.fdwRevocationChecks = 0   # WTD_REVOKE_NONE
    wd.dwUnionChoice = 1         # WTD_CHOICE_FILE
    wd.pFile = ctypes.pointer(fi)
    wd.dwStateAction = 1         # WTD_STATEACTION_VERIFY
    wd.dwProvFlags = 0x100       # WTD_SAFER_FLAG
    wintrust = ctypes.windll.wintrust
    rc = wintrust.WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(wd))
    wd.dwStateAction = 2         # WTD_STATEACTION_CLOSE (libera o estado)
    wintrust.WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(wd))
    return rc == 0


def _signer_name(path: Path) -> str:
    """Nome amigável (CN) do signatário do .exe via CryptQueryObject/CertGetNameString; '' se falhar."""
    from ctypes import wintypes

    class CRYPTOAPI_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    class CRYPT_ALGORITHM_IDENTIFIER(ctypes.Structure):
        _fields_ = [("pszObjId", ctypes.c_char_p), ("Parameters", CRYPTOAPI_BLOB)]

    # Só os campos iniciais importam (Issuer/SerialNumber); o resto é ignorado.
    class CMSG_SIGNER_INFO(ctypes.Structure):
        _fields_ = [("dwVersion", wintypes.DWORD), ("Issuer", CRYPTOAPI_BLOB),
                    ("SerialNumber", CRYPTOAPI_BLOB)]

    class CERT_INFO(ctypes.Structure):
        _fields_ = [("dwVersion", wintypes.DWORD), ("SerialNumber", CRYPTOAPI_BLOB),
                    ("SignatureAlgorithm", CRYPT_ALGORITHM_IDENTIFIER), ("Issuer", CRYPTOAPI_BLOB)]

    crypt32 = ctypes.windll.crypt32
    # Devolve ponteiro (PCCERT_CONTEXT): sem isto o ctypes trunca para int em 64 bits.
    crypt32.CertFindCertificateInStore.restype = ctypes.c_void_p
    enc = wintypes.DWORD()
    content = wintypes.DWORD()
    fmt = wintypes.DWORD()
    h_store = ctypes.c_void_p()
    h_msg = ctypes.c_void_p()
    ok = crypt32.CryptQueryObject(
        1,                                          # CERT_QUERY_OBJECT_FILE
        ctypes.c_wchar_p(str(path)),
        0x400,                                      # CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED
        2,                                          # CERT_QUERY_FORMAT_FLAG_BINARY
        0, ctypes.byref(enc), ctypes.byref(content), ctypes.byref(fmt),
        ctypes.byref(h_store), ctypes.byref(h_msg), None)
    if not ok:
        return ""
    cert = None
    try:
        size = wintypes.DWORD()
        if not crypt32.CryptMsgGetParam(h_msg, 6, 0, None, ctypes.byref(size)):  # CMSG_SIGNER_INFO_PARAM
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if not crypt32.CryptMsgGetParam(h_msg, 6, 0, buf, ctypes.byref(size)):
            return ""
        signer = ctypes.cast(buf, ctypes.POINTER(CMSG_SIGNER_INFO)).contents
        ci = CERT_INFO()
        ci.Issuer = signer.Issuer
        ci.SerialNumber = signer.SerialNumber
        cert = crypt32.CertFindCertificateInStore(
            h_store, enc.value, 0, 0x000B0000,      # CERT_FIND_SUBJECT_CERT
            ctypes.byref(ci), None)
        if not cert:
            return ""
        # CERT_NAME_SIMPLE_DISPLAY_TYPE = 4
        n = crypt32.CertGetNameStringW(cert, 4, 0, None, None, 0)
        if n <= 1:
            return ""
        name = ctypes.create_unicode_buffer(n)
        crypt32.CertGetNameStringW(cert, 4, 0, None, name, n)
        return name.value or ""
    finally:
        if cert:
            crypt32.CertFreeCertificateContext(cert)
        if h_store:
            crypt32.CertCloseStore(h_store, 0)
        if h_msg:
            crypt32.CryptMsgClose(h_msg)


def _verify_signature(path: Path) -> tuple[bool, str]:
    """(ok, nome_ou_motivo). Fail-closed: qualquer erro na verificação reprova o binário."""
    try:
        if not _win_verify_trust(path):
            return False, "não está assinada ou a cadeia é inválida"
        name = _signer_name(path)
        if not name:
            return False, "sem nome de signatário no certificado"
        if PAWNIO_PUBLISHER.lower() not in name.lower():
            return False, f"signatário inesperado: {name}"
        return True, name
    except Exception as exc:
        return False, f"falha ao verificar ({exc})"


def run_installer(path: Path) -> int:
    """Confere a assinatura Authenticode antes de rodar — é um instalador de driver (ring0).
    Se a assinatura não bater, apaga o arquivo e levanta RuntimeError (a GUI mostra o aviso)."""
    ok, who = _verify_signature(Path(path))
    if not ok:
        try:
            Path(path).unlink()
        except Exception:
            pass
        raise RuntimeError(f"assinatura do PawnIO não confere ({who}); download apagado por segurança")
    return subprocess.run([str(path)]).returncode


def open_folder(path: Path) -> None:
    os.startfile(str(path))  # noqa: S606
