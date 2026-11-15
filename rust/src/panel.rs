//! Conexão com a tela: acha a porta pelo VID/PID, conversa o protocolo, envia quadros e keepalive.

use crate::protocol::{self, opcode, PanelInfo};
use serialport::{SerialPort, SerialPortType};
use std::io::{Read, Write};
use std::time::{Duration, Instant};

pub struct Panel {
    port: Box<dyn SerialPort>,
    pub info: PanelInfo,
    last_keepalive: Instant,
}

/// Acha a porta COM da tela pelo VID/PID (ou pela descrição).
pub fn find_panel() -> Option<String> {
    let ports = serialport::available_ports().ok()?;
    for p in &ports {
        if let SerialPortType::UsbPort(u) = &p.port_type {
            if u.vid == protocol::USB_VID && protocol::USB_PIDS.contains(&u.pid) {
                return Some(p.port_name.clone());
            }
        }
    }
    // fallback: descrição "HONGTAI" / "MONITOR"
    for p in &ports {
        if let SerialPortType::UsbPort(u) = &p.port_type {
            let text = format!(
                "{} {}",
                u.product.clone().unwrap_or_default(),
                u.manufacturer.clone().unwrap_or_default()
            )
            .to_uppercase();
            if text.contains("HONGTAI") || (text.contains("MONITOR") && u.vid == protocol::USB_VID) {
                return Some(p.port_name.clone());
            }
        }
    }
    None
}

impl Panel {
    pub fn open(port_name: &str) -> std::io::Result<Panel> {
        let port = serialport::new(port_name, protocol::BAUD)
            .timeout(Duration::from_millis(500))
            .open()
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
        Ok(Panel {
            port,
            info: PanelInfo::default(),
            last_keepalive: Instant::now() - Duration::from_secs(10),
        })
    }

    fn write(&mut self, data: &[u8]) -> std::io::Result<()> {
        self.port.write_all(data)?;
        self.port.flush()
    }

    fn drain_input(&mut self) {
        if let Ok(n) = self.port.bytes_to_read() {
            if n > 0 {
                let mut buf = vec![0u8; n as usize];
                let _ = self.port.read(&mut buf);
            }
        }
    }

    pub fn clear(&mut self) -> std::io::Result<()> {
        self.write(&protocol::CLEAR_SEQUENCE)?;
        self.write(&protocol::CLEAR_TAIL)
    }

    /// Envia um comando e, se `reply`, espera um envelope 55 AA completo.
    fn command(&mut self, op: u8, payload: &[u8], reply: bool, timeout: Duration) -> Option<Vec<u8>> {
        if reply {
            self.drain_input();
        }
        self.write(&protocol::control_frame(op, payload)).ok()?;
        if !reply {
            return None;
        }
        let deadline = Instant::now() + timeout;
        let mut buf = Vec::new();
        let mut chunk = [0u8; 4096];
        while Instant::now() < deadline {
            if let Ok(n) = self.port.read(&mut chunk) {
                if n > 0 {
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(start) = buf.windows(2).position(|w| w == protocol::MAGIC) {
                        if buf.len() - start >= 4 {
                            let need = u16::from_le_bytes([buf[start + 2], buf[start + 3]]) as usize;
                            if buf.len() - start >= need {
                                break;
                            }
                        }
                    }
                }
            }
        }
        if buf.is_empty() {
            None
        } else {
            Some(buf)
        }
    }

    pub fn connect(&mut self, probe: Duration) -> std::io::Result<PanelInfo> {
        self.clear()?;
        std::thread::sleep(Duration::from_millis(300));
        let reply = self.command(opcode::GET_DEVICE_INFO, &[], true, probe);
        if let Some(raw) = reply {
            if let Some(payload) = protocol::reply_payload(&raw) {
                if let Ok(v) = serde_json::from_slice::<serde_json::Value>(payload) {
                    let data = v.get("data").cloned().unwrap_or(v);
                    self.info = PanelInfo::from_json(&data);
                }
            }
        }
        Ok(self.info.clone())
    }

    pub fn set_brightness(&mut self, level: u8) {
        let _ = self.command(opcode::SET_BRIGHTNESS, &[level.min(100)], false, Duration::ZERO);
    }

    pub fn keepalive(&mut self, force: bool) {
        if !force && self.last_keepalive.elapsed() < Duration::from_millis(1000) {
            return;
        }
        let _ = self.write(&protocol::control_frame(opcode::KEEPALIVE, &[]));
        self.last_keepalive = Instant::now();
    }

    /// Destrava a tela sem desconectar (aborta quadro pela metade + limpa + keepalive).
    pub fn recover(&mut self) {
        let _ = self.port.clear(serialport::ClearBuffer::Output);
        std::thread::sleep(Duration::from_millis(150));
        let _ = self.clear();
        std::thread::sleep(Duration::from_millis(200));
        self.drain_input();
        self.keepalive(true);
    }

    pub fn send_frame(&mut self, jpeg: &[u8]) -> std::io::Result<()> {
        if self.info.is_spi() || !self.info.uses_length_header() {
            self.write(jpeg)
        } else {
            self.write(&protocol::image_frame(jpeg))
        }
    }

    pub fn close(&mut self, release: bool) {
        if release {
            let _ = self.clear();
            if self.info.version_number() >= 3.1 {
                let _ = self.command(opcode::CLOSE, &[], false, Duration::ZERO);
            }
        }
    }
}
