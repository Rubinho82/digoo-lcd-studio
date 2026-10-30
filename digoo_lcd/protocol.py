"""Protocolo serial da telinha HONGTAI / LOVINGCOOL (USB 33c3:7791).

A tela aparece no Windows como "Dispositivo Serial USB (COMx)" (CDC-ACM,
descrição "MONITOR"). O baud é ignorado, mas usamos 2 Mbaud como o app oficial.

Dois formatos dividem a mesma porta:

  Comando:  55 AA | len_lo len_hi | opcode | payload | ck_lo ck_hi
            len = len(payload) + 7 ; ck = soma(bytes anteriores) & 0xFFFF
            Resposta: mesmo envelope, payload = JSON ou 1 byte de status.

  Imagem (firmware > 2.8):  len_le32 | jpeg | ck_lo ck_hi
            ck = soma(len_le32 + jpeg) & 0xFFFF
  Imagem (firmware <= 2.8): jpeg puro
  Painéis "SPI": RGB565 big-endian puro.

Regras que importam:
  * opcode 0x11 (keepalive) a cada ~1,5 s, senão a tela sai do modo ao vivo
    e passa a ignorar os quadros;
  * FF D9 FF D9 + 00 00 00 00 aborta um quadro pela metade (usar ao conectar);
  * cada quadro tem um limite de tamanho; estourar trava a tela.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

import serial
from serial.tools import list_ports

USB_VID = 0x33C3
USB_PIDS = (0x7791, 0x7788, 0x7792)
BAUD = 2_000_000

MAGIC = b"\x55\xaa"
CLEAR_SEQUENCE = b"\xff\xd9\xff\xd9"
CLEAR_TAIL = b"\x00\x00\x00\x00"
KEEPALIVE_INTERVAL = 1.0  # folga contra jitter de agendamento (a tela exige ~1,5 s)


class Opcode:
    RESTART = 0x01
    SET_BRIGHTNESS = 0x03
    GET_DEVICE_INFO = 0x06
    KEEPALIVE = 0x11
    CLOSE = 0x21
    SET_MOTOR = 0x25


ERROR_CODES = {
    "01": "operação falhou",
    "02": "sem memória",
    "03": "armazenamento interno cheio",
    "04": "cartão SD cheio",
    "05": "arquivo não existe",
    "06": "falha ao abrir arquivo",
    "07": "falha ao gravar arquivo",
}


class PanelError(RuntimeError):
    """A tela respondeu com erro ou a conexão caiu."""


def _checksum(data: bytes) -> bytes:
    return (sum(data) & 0xFFFF).to_bytes(2, "little")


def control_frame(opcode: int, payload: bytes = b"") -> bytes:
    body = MAGIC + (len(payload) + 7).to_bytes(2, "little") + bytes([opcode]) + payload
    return body + _checksum(body)


def image_frame(jpeg: bytes) -> bytes:
    body = len(jpeg).to_bytes(4, "little") + jpeg
    return body + _checksum(body)


def parse_reply(raw: bytes) -> dict:
    start = raw.find(MAGIC)
    if start < 0 or len(raw) - start < 8:
        raise PanelError(f"resposta curta/inválida: {raw[:32].hex()}")
    raw = raw[start:]
    length = int.from_bytes(raw[2:4], "little")
    if 7 <= length <= len(raw):
        raw = raw[:length]
    payload = raw[5:-2]
    try:
        return json.loads(payload.decode("utf8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        code = payload.hex()
        raise PanelError(f"erro da tela {code}: {ERROR_CODES.get(code, 'desconhecido')}") from None


@dataclass
class PanelInfo:
    width: int = 480
    height: int = 480
    angle: int = 0
    version: str = ""
    uid: str = ""
    model: str = ""
    region: str = ""
    brightness: int = 100
    storage_blocks: int = 0
    storage_block_size: int = 0
    storage_free_blocks: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def version_number(self) -> float:
        try:
            return float(str(self.version).replace("Ver", "").strip())
        except ValueError:
            return 0.0

    @property
    def uses_length_header(self) -> bool:
        return self.version_number > 2.8

    @property
    def is_spi(self) -> bool:
        m = self.model.lower()
        return "2.99" in m or "txw813-st7789-2.8inch" in m or "qspi" in m

    @property
    def fragile(self) -> bool:
        """TXW818-ST7701S 4" fw <=3.1: o app oficial limita este modelo (engasga com quadros grandes e rápidos;
        aqui ele chegou a parar de aceitar dados e voltar para a tela de fábrica)."""
        return "TXW818-ST7701S-4.0inch" in self.model and self.version_number <= 3.1

    @property
    def max_frame_kb(self) -> int:
        """Limite de tamanho do quadro por modelo (igual ao app oficial)."""
        if self.fragile:
            return 50
        longest = max(self.width, self.height)
        if "10.26" in self.model:
            return 350
        if "6.67" in self.model or longest >= 1024:
            return 260
        if "9.16" in self.model:
            return 120 if self.uses_length_header else 90
        return 80 if self.uses_length_header else 50

    @property
    def max_fps(self) -> int:
        if self.fragile:
            return 25
        longest = max(self.width, self.height)
        if "10.26" in self.model or "6.67" in self.model or longest >= 1024:
            return 60
        return 30

    @property
    def storage_text(self) -> str:
        if not self.storage_blocks or not self.storage_block_size:
            return "—"
        total = self.storage_blocks * self.storage_block_size / 1024 / 1024
        free = self.storage_free_blocks * self.storage_block_size / 1024 / 1024
        return f"{free:.1f} MB livres de {total:.1f} MB"


def find_panel() -> str | None:
    """Acha a porta COM da tela pelo VID/PID (ou pela descrição "MONITOR")."""
    ports = list(list_ports.comports())
    for p in ports:
        if p.vid == USB_VID and p.pid in USB_PIDS:
            return p.device
    for p in ports:
        text = f"{p.product or ''} {p.manufacturer or ''} {p.description or ''}".upper()
        if "HONGTAI" in text or ("MONITOR" in text and p.vid == USB_VID):
            return p.device
    return None


class Panel:
    """Conexão com a tela. Uso de uma única thread (o Engine)."""

    def __init__(self, port: str):
        self.port = port
        # 0,5 s: um write preso estoura rápido e a thread de keepalive recupera o lock a tempo (dentro da janela de 1,5 s)
        self._serial = serial.Serial(port, BAUD, timeout=0.05, write_timeout=0.5)
        self.info = PanelInfo()
        self._last_keepalive = 0.0
        self._lock = threading.RLock()  # quadro e keepalive vêm de threads diferentes: nunca se misturam

    # -- baixo nível ----------------------------------------------------
    def _write(self, data: bytes) -> None:
        try:
            with self._lock:
                self._serial.write(data)
                self._serial.flush()
        except (serial.SerialException, OSError) as exc:
            raise PanelError(f"falha ao escrever: {exc}") from exc

    def drain_input(self) -> bytes:
        """Descarta respostas acumuladas (ex.: ack dos keepalives)."""
        try:
            n = self._serial.in_waiting
            return self._serial.read(n) if n else b""
        except (serial.SerialException, OSError) as exc:
            raise PanelError(f"falha ao ler: {exc}") from exc

    def command(self, opcode: int, payload: bytes = b"", reply: bool = False, timeout: float = 3.0):
        if reply:
            self.drain_input()
        self._write(control_frame(opcode, payload))
        if not reply:
            return None
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            try:
                chunk = self._serial.read(4096)
            except (serial.SerialException, OSError) as exc:
                raise PanelError(f"falha ao ler: {exc}") from exc
            if chunk:
                buf += chunk
                start = buf.find(MAGIC)
                if start >= 0 and len(buf) - start >= 4:
                    need = int.from_bytes(buf[start + 2:start + 4], "little")
                    if len(buf) - start >= need:
                        break
        if not buf:
            raise PanelError(f"sem resposta ao comando 0x{opcode:02x}")
        return parse_reply(buf)

    # -- alto nível -----------------------------------------------------
    def clear(self) -> None:
        self._write(CLEAR_SEQUENCE)
        self._write(CLEAR_TAIL)

    def connect(self, probe_timeout: float = 3.0) -> PanelInfo:
        self.clear()
        time.sleep(0.3)
        reply = self.command(Opcode.GET_DEVICE_INFO, reply=True, timeout=probe_timeout) or {}
        data = reply.get("data") or reply
        self.info = PanelInfo(
            width=int(data.get("width", 480)),
            height=int(data.get("height", 480)),
            angle=int(data.get("angle", 0) or 0),
            version=str(data.get("version", "")),
            uid=str(data.get("uid", "")),
            model=str(data.get("model", "")),
            region=str(data.get("region", "")),
            brightness=int(data.get("brightness", 100)),
            storage_blocks=int(data.get("i_blocks", 0) or 0),
            storage_block_size=int(data.get("i_block_size", 0) or 0),
            storage_free_blocks=int(data.get("i_block_free", 0) or 0),
            raw=data,
        )
        if self.info.angle in (90, 270):
            self.info.width, self.info.height = self.info.height, self.info.width
        return self.info

    def set_brightness(self, level: int) -> None:
        self.command(Opcode.SET_BRIGHTNESS, bytes([max(0, min(100, int(level)))]))

    def keepalive(self, force: bool = False) -> None:
        # checa antes do lock: a thread de keepalive nem bloqueia na entrada quando não há nada a enviar
        if not force and time.monotonic() - self._last_keepalive < KEEPALIVE_INTERVAL:
            return
        with self._lock:
            now = time.monotonic()
            if force or now - self._last_keepalive >= KEEPALIVE_INTERVAL:
                self.command(Opcode.KEEPALIVE)
                self._last_keepalive = now

    def recover(self) -> None:
        """Destrava a tela sem desconectar: aborta o quadro pela metade, limpa o buffer de saída e reinjeta o
        keepalive — tudo sob o mesmo lock, para não colidir com um write de keepalive em andamento."""
        with self._lock:
            if hasattr(self._serial, "cancel_write"):
                self._serial.cancel_write()
            self._serial.reset_output_buffer()
            time.sleep(0.15)
            self.clear()
            time.sleep(0.2)
            self.drain_input()
            self.keepalive(force=True)

    def restart(self) -> None:
        self.command(Opcode.RESTART)

    def send_frame(self, payload: bytes) -> None:
        if self.info.is_spi or not self.info.uses_length_header:
            self._write(payload)
        else:
            self._write(image_frame(payload))

    def close(self, release: bool = True) -> None:
        """Fecha a conexão. Com release=True devolve a tela ao modo próprio dela."""
        try:
            if release:
                self.clear()
                if self.info.version_number >= 3.1:
                    self.command(Opcode.CLOSE)
        except Exception:
            pass
        try:
            with self._lock:  # não fechar enquanto a keepalive ainda escreve sob o lock
                self._serial.close()
        except Exception:
            pass
