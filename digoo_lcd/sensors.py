"""Leitura de sensores.

Fonte principal: bin/sensorhelper/sensorhelper.exe (LibreHardwareMonitor),
que manda uma linha JSON por ciclo. Complementos/fallback: psutil (CPU, RAM,
rede, disco) e NVML direto via ctypes (GPU NVIDIA) se o helper não rodar.

A temperatura da CPU (e fans/bomba da placa-mãe) só aparece com o driver
PawnIO instalado — sem ele o LibreHardwareMonitor devolve 0.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import logging
import subprocess
import threading
import time
from collections import deque

import psutil

from .config import resource_dir

log = logging.getLogger(__name__)

HELPER = resource_dir() / "bin" / "sensorhelper" / "sensorhelper.exe"
CREATE_NO_WINDOW = 0x08000000
HISTORY_LEN = 180

HISTORY_KEYS = ("cpu_load", "gpu_load", "cpu_temp", "gpu_temp", "net_down", "net_up", "ram_load")


def pawnio_installed() -> bool:
    try:
        out = subprocess.run(["sc", "query", "PawnIO"], capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW, timeout=5)
        return out.returncode == 0
    except Exception:
        return False


# ----------------------------------------------------------------------
# NVML (fallback para GPU NVIDIA sem o helper)
# ----------------------------------------------------------------------
class _Nvml:
    def __init__(self):
        self.ok = False
        self.h = None
        try:
            self.lib = ctypes.WinDLL("nvml.dll")
        except OSError:
            try:
                self.lib = ctypes.WinDLL(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll")
            except OSError:
                return
        try:
            if self.lib.nvmlInit_v2() != 0:
                return
            h = ctypes.c_void_p()
            if self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
                return
            self.h = h
            self.ok = True
        except Exception:
            self.ok = False

    def read(self) -> dict:
        if not self.ok:
            return {}
        out = {}
        u = ctypes.c_uint()
        if self.lib.nvmlDeviceGetTemperature(self.h, 0, ctypes.byref(u)) == 0:
            out["gpu_temp"] = float(u.value)

        class Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        ut = Util()
        if self.lib.nvmlDeviceGetUtilizationRates(self.h, ctypes.byref(ut)) == 0:
            out["gpu_load"] = float(ut.gpu)

        class Mem(ctypes.Structure):
            _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

        m = Mem()
        if self.lib.nvmlDeviceGetMemoryInfo(self.h, ctypes.byref(m)) == 0:
            out["gpu_vram"] = m.used / 1024 / 1024
            out["gpu_vram_total"] = m.total / 1024 / 1024
        if self.lib.nvmlDeviceGetPowerUsage(self.h, ctypes.byref(u)) == 0:
            out["gpu_power"] = u.value / 1000.0
        if self.lib.nvmlDeviceGetClockInfo(self.h, 0, ctypes.byref(u)) == 0:
            out["gpu_clock"] = float(u.value)
        if self.lib.nvmlDeviceGetFanSpeed(self.h, ctypes.byref(u)) == 0:
            out["gpu_fan_pct"] = float(u.value)
        return out


# ----------------------------------------------------------------------
class SensorHub:
    """Agrega sensores e expõe snapshot() com chaves padronizadas."""

    def __init__(self, interval: float = 1.0):
        self.interval = max(0.25, float(interval))
        self.gpu_select = "auto"
        self.pump_sensor = ""
        self.fan_sensor = ""
        self.want_audio = False       # captura o som do PC (só quando algum recurso de música usa)
        self._audio_running = False
        self._audio = None            # (instante, [48 bandas 0..100], nível)
        self._media = None            # "tocando agora" do Windows
        self._media_time = 0.0

        self._lock = threading.Lock()
        self._raw: dict[str, dict] = {}
        self._raw_time = 0.0
        self._metrics: dict = {}
        self._history = {k: deque(maxlen=HISTORY_LEN) for k in HISTORY_KEYS}
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._nvml: _Nvml | None = None
        self.helper_status = "iniciando"
        self._last_net = None
        self._boot = psutil.boot_time()
        psutil.cpu_percent(None)

    # -- ciclo de vida ----------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._helper_loop, daemon=True, name="sensor-helper").start()
        threading.Thread(target=self._sample_loop, daemon=True, name="sensor-sample").start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_helper()

    def restart_helper(self) -> None:
        self._kill_helper()

    def _kill_helper(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.terminate()
            except Exception:
                pass

    # -- helper (LibreHardwareMonitor) -----------------------------------
    def _helper_loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            if not HELPER.exists():
                self.helper_status = "sensorhelper.exe não encontrado (usando modo básico)"
                return
            try:
                args = [str(HELPER), str(int(self.interval * 1000))]
                self._audio_running = self.want_audio
                if self.want_audio:
                    args.append("--audio")
                self._gpu_fan_sent = None  # leitor novo: reenvia a ventoinha
                self._proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW, bufsize=1, text=True, encoding="utf8",
                )
                self.helper_status = "ok"
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "audio" in msg:
                        a = msg["audio"] or {}
                        self._audio = (time.monotonic(), a.get("b") or [], a.get("lv", 0))
                        continue
                    if "media" in msg:
                        self._media = msg["media"]
                        self._media_time = time.monotonic()
                        continue
                    if "error" in msg:
                        log.warning("sensorhelper: %s", msg["error"])
                        continue
                    raw = {s["id"]: s for s in msg.get("sensors", [])}
                    with self._lock:
                        self._raw = raw
                        self._raw_time = time.time()
                    backoff = 2.0
            except Exception as exc:
                log.warning("sensorhelper falhou: %s", exc)
                self.helper_status = f"erro: {exc}"
            if self._stop.is_set():
                break
            self.helper_status = "reiniciando leitor de sensores"
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30)

    def set_gpu_fan(self, percent: int) -> None:
        """0 = automático (a placa decide); 30..100 = velocidade fixa. O leitor devolve o automático ao sair
        e põe 100% sozinho se a GPU passar de 83 °C."""
        p = self._proc
        if p is None or p.poll() is not None:
            self._gpu_fan_sent = None
            return
        if getattr(self, "_gpu_fan_sent", None) == percent:
            return
        try:
            p.stdin.write(f"gpufan {percent if percent else 'auto'}\n")
            p.stdin.flush()
            self._gpu_fan_sent = percent
        except Exception:
            self._gpu_fan_sent = None

    def set_audio(self, enabled: bool) -> None:
        """Liga/desliga a captura do som; reinicia o leitor se mudou."""
        self.want_audio = bool(enabled)
        p = self._proc
        if p is not None and p.poll() is None and self._audio_running != self.want_audio:
            self._kill_helper()

    def audio_snapshot(self):
        return self._audio

    def media_snapshot(self) -> dict | None:
        """Música tocando, com o tempo atualizado desde a última leitura."""
        m = self._media
        if not m:
            return None
        m = dict(m)
        if m.get("status") == "Playing":
            m["position"] = float(m.get("position") or 0) + (time.monotonic() - self._media_time)
            if m.get("duration"):
                m["position"] = min(m["position"], float(m["duration"]))
        return m

    def all_sensors(self) -> list[dict]:
        with self._lock:
            return sorted(self._raw.values(), key=lambda s: (s["hwType"], s["hw"], s["type"], s["name"]))

    # -- amostragem -------------------------------------------------------
    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            try:
                m = self._compute()
                with self._lock:
                    self._metrics = m
                    for k in HISTORY_KEYS:
                        v = m.get(k)
                        self._history[k].append(v if v is not None else float("nan"))
            except Exception:
                log.exception("erro ao amostrar sensores")
            self._stop.wait(self.interval)

    def snapshot(self) -> dict:
        with self._lock:
            m = dict(self._metrics)
            m["history"] = {k: list(v) for k, v in self._history.items()}
        now = dt.datetime.now()
        m["now"] = now
        return m

    # -- seleção dos sensores ---------------------------------------------
    @staticmethod
    def _pick(raw, hw_types, stype, names, positive=True):
        for name in names:
            for s in raw.values():
                if s["hwType"] in hw_types and s["type"] == stype and s["name"] == name:
                    v = s["value"]
                    if not positive or v > 0:
                        return float(v)
        return None

    def _gpu_type(self, raw) -> tuple[str, ...]:
        if self.gpu_select not in ("", "auto"):
            return (self.gpu_select,)
        present = {s["hwType"] for s in raw.values()}
        for t in ("GpuNvidia", "GpuAmd", "GpuIntel"):
            if t not in present:
                continue
            if t == "GpuAmd":
                tot = self._pick(raw, (t,), "SmallData", ["GPU Memory Total"]) or 0
                if tot < 2048 and "GpuNvidia" in present:
                    continue
            return (t,)
        return ("GpuNvidia", "GpuAmd", "GpuIntel")

    def _compute(self) -> dict:
        with self._lock:
            raw = dict(self._raw) if time.time() - self._raw_time < 10 else {}
        m: dict = {}
        cpu = ("Cpu",)

        # CPU
        m["cpu_temp"] = self._pick(raw, cpu, "Temperature",
                                   ["Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)", "Tdie",
                                    "Core Average", "CPU Cores", "Core Max"])
        m["cpu_load"] = self._pick(raw, cpu, "Load", ["CPU Total"], positive=False)
        if m["cpu_load"] is None:
            m["cpu_load"] = psutil.cpu_percent(None)
        m["cpu_clock"] = self._pick(raw, cpu, "Clock", ["Cores (Average)", "Cores (Average Effective)"])
        if m["cpu_clock"] is None:
            clocks = [s["value"] for s in raw.values()
                      if s["hwType"] == "Cpu" and s["type"] == "Clock" and s["name"].startswith("Core #")
                      and "Effective" not in s["name"] and s["value"] > 0]
            if clocks:
                m["cpu_clock"] = max(clocks)
        m["cpu_power"] = self._pick(raw, cpu, "Power", ["Package", "CPU Package"])

        # GPU
        g = self._gpu_type(raw)
        m["gpu_name"] = next((s["hw"] for s in raw.values() if s["hwType"] in g), "")
        m["gpu_temp"] = self._pick(raw, g, "Temperature", ["GPU Core"])
        m["gpu_hotspot"] = self._pick(raw, g, "Temperature", ["GPU Hot Spot", "GPU Memory Junction", "GPU Memory"])
        m["gpu_load"] = self._pick(raw, g, "Load", ["GPU Core", "D3D 3D"], positive=False)
        m["gpu_clock"] = self._pick(raw, g, "Clock", ["GPU Core"])
        m["gpu_power"] = self._pick(raw, g, "Power", ["GPU Package", "GPU Power", "GPU Core"])
        m["gpu_vram"] = self._pick(raw, g, "SmallData", ["GPU Memory Used", "D3D Dedicated Memory Used"])
        m["gpu_vram_total"] = self._pick(raw, g, "SmallData", ["GPU Memory Total", "D3D Dedicated Memory Total"])
        m["gpu_fan"] = self._pick(raw, g, "Fan", ["GPU Fan", "GPU Fan 1"], positive=False)

        if m["gpu_temp"] is None:  # helper fora do ar: NVML
            if self._nvml is None:
                self._nvml = _Nvml()
            for k, v in self._nvml.read().items():
                if m.get(k) is None:
                    m[k] = v

        # Bomba / fan da placa-mãe
        fans = {sid: s for sid, s in raw.items() if s["type"] == "Fan" and not s["hwType"].startswith("Gpu")}
        m["pump_rpm"] = self._fan_value(fans, self.pump_sensor, ("pump", "aio", "bomba"))
        m["fan_rpm"] = self._fan_value(fans, self.fan_sensor, ("cpu fan", "cpu_fan", "fan #1", "cpu"))

        # Disco: o mais quente entre as temperaturas principais
        disk = [s["value"] for s in raw.values()
                if s["hwType"] == "Storage" and s["type"] == "Temperature"
                and s["name"] in ("Composite Temperature", "Temperature") and 0 < s["value"] < 120]
        m["disk_temp"] = max(disk) if disk else None

        # RAM (psutil é exato)
        vm = psutil.virtual_memory()
        m["ram_load"] = vm.percent
        m["ram_used"] = vm.used / 1024 ** 3
        m["ram_total"] = vm.total / 1024 ** 3

        # Rede
        now = time.time()
        io = psutil.net_io_counters()
        if self._last_net:
            t0, r0, s0 = self._last_net
            dt_s = max(0.001, now - t0)
            m["net_down"] = max(0.0, (io.bytes_recv - r0) / dt_s)
            m["net_up"] = max(0.0, (io.bytes_sent - s0) / dt_s)
        else:
            m["net_down"] = m["net_up"] = 0.0
        self._last_net = (now, io.bytes_recv, io.bytes_sent)

        m["uptime"] = now - self._boot
        m["helper_ok"] = bool(raw)
        return m

    @staticmethod
    def _fan_value(fans: dict, chosen: str, hints: tuple[str, ...]):
        if chosen:
            s = fans.get(chosen)
            return float(s["value"]) if s else None
        for hint in hints:
            for s in fans.values():
                if hint in s["name"].lower() and s["value"] > 0:
                    return float(s["value"])
        return None
