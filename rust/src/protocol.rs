//! Protocolo serial da telinha HONGTAI / LOVINGCOOL (USB 33c3:7791).
//! Portado fiel do protocol.py do app Python (mesmos bytes no fio).
//!
//!   Comando:  55 AA | len_le16(payload+7) | opcode | payload | ck_le16(soma dos bytes)
//!   Imagem (fw > 2.8):  len_le32 | jpeg | ck_le16(soma)
//!   Imagem (fw <= 2.8 / SPI): bytes puros
//!   keepalive 0x11 a cada ~1 s; FF D9 FF D9 + 00000000 aborta um quadro pela metade.

pub const USB_VID: u16 = 0x33C3;
pub const USB_PIDS: [u16; 3] = [0x7791, 0x7788, 0x7792];
pub const BAUD: u32 = 2_000_000;

pub const MAGIC: [u8; 2] = [0x55, 0xAA];
pub const CLEAR_SEQUENCE: [u8; 4] = [0xFF, 0xD9, 0xFF, 0xD9];
pub const CLEAR_TAIL: [u8; 4] = [0x00, 0x00, 0x00, 0x00];

#[allow(dead_code)]
pub mod opcode {
    pub const RESTART: u8 = 0x01;
    pub const SET_BRIGHTNESS: u8 = 0x03;
    pub const GET_DEVICE_INFO: u8 = 0x06;
    pub const KEEPALIVE: u8 = 0x11;
    pub const CLOSE: u8 = 0x21;
    pub const SET_MOTOR: u8 = 0x25;
}

/// soma de todos os bytes & 0xFFFF, em little-endian (2 bytes).
fn checksum(data: &[u8]) -> [u8; 2] {
    let sum: u32 = data.iter().map(|&b| b as u32).sum();
    ((sum & 0xFFFF) as u16).to_le_bytes()
}

/// Quadro de comando: 55 AA | len | opcode | payload | checksum.
pub fn control_frame(opcode: u8, payload: &[u8]) -> Vec<u8> {
    let mut body = Vec::with_capacity(5 + payload.len() + 2);
    body.extend_from_slice(&MAGIC);
    body.extend_from_slice(&((payload.len() as u16) + 7).to_le_bytes());
    body.push(opcode);
    body.extend_from_slice(payload);
    let ck = checksum(&body);
    body.extend_from_slice(&ck);
    body
}

/// Quadro de imagem (fw > 2.8): len_le32 | jpeg | checksum.
pub fn image_frame(jpeg: &[u8]) -> Vec<u8> {
    let mut body = Vec::with_capacity(4 + jpeg.len() + 2);
    body.extend_from_slice(&(jpeg.len() as u32).to_le_bytes());
    body.extend_from_slice(jpeg);
    let ck = checksum(&body);
    body.extend_from_slice(&ck);
    body
}

/// Acha o começo do envelope 55 AA numa resposta e devolve o payload (JSON ou status).
pub fn reply_payload(raw: &[u8]) -> Option<&[u8]> {
    let start = raw.windows(2).position(|w| w == MAGIC)?;
    let r = &raw[start..];
    if r.len() < 8 {
        return None;
    }
    let length = u16::from_le_bytes([r[2], r[3]]) as usize;
    let r = if (7..=r.len()).contains(&length) { &r[..length] } else { r };
    Some(&r[5..r.len() - 2])
}

/// Informações da tela (resposta ao GET_DEVICE_INFO). Campos e regras iguais ao PanelInfo do Python.
#[derive(Debug, Clone)]
pub struct PanelInfo {
    pub width: u32,
    pub height: u32,
    pub angle: i32,
    pub version: String,
    pub uid: String,
    pub model: String,
    pub brightness: i32,
}

impl Default for PanelInfo {
    fn default() -> Self {
        PanelInfo {
            width: 480,
            height: 480,
            angle: 0,
            version: String::new(),
            uid: String::new(),
            model: String::new(),
            brightness: 100,
        }
    }
}

impl PanelInfo {
    pub fn from_json(data: &serde_json::Value) -> Self {
        let geti = |k: &str, d: i64| data.get(k).and_then(|v| v.as_i64()).unwrap_or(d);
        let gets = |k: &str| {
            data.get(k)
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .unwrap_or_default()
        };
        let mut info = PanelInfo {
            width: geti("width", 480) as u32,
            height: geti("height", 480) as u32,
            angle: geti("angle", 0) as i32,
            version: gets("version"),
            uid: gets("uid"),
            model: gets("model"),
            brightness: geti("brightness", 100) as i32,
        };
        if info.angle == 90 || info.angle == 270 {
            std::mem::swap(&mut info.width, &mut info.height);
        }
        info
    }

    pub fn version_number(&self) -> f32 {
        self.version.replace("Ver", "").trim().parse().unwrap_or(0.0)
    }

    pub fn uses_length_header(&self) -> bool {
        self.version_number() > 2.8
    }

    pub fn is_spi(&self) -> bool {
        let m = self.model.to_lowercase();
        m.contains("2.99") || m.contains("txw813-st7789-2.8inch") || m.contains("qspi")
    }

    /// TXW818-ST7701S 4" fw <= 3.1: modelo frágil (engasga com quadros grandes/rápidos).
    pub fn fragile(&self) -> bool {
        self.model.contains("TXW818-ST7701S-4.0inch") && self.version_number() <= 3.1
    }

    pub fn max_frame_kb(&self) -> u32 {
        if self.fragile() {
            return 50;
        }
        let longest = self.width.max(self.height);
        if self.model.contains("10.26") {
            return 350;
        }
        if self.model.contains("6.67") || longest >= 1024 {
            return 260;
        }
        if self.model.contains("9.16") {
            return if self.uses_length_header() { 120 } else { 90 };
        }
        if self.uses_length_header() {
            80
        } else {
            50
        }
    }

    pub fn max_fps(&self) -> u32 {
        if self.fragile() {
            return 25;
        }
        let longest = self.width.max(self.height);
        if self.model.contains("10.26") || self.model.contains("6.67") || longest >= 1024 {
            60
        } else {
            30
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keepalive_bytes_match_python() {
        // control_frame(0x11) == 55 AA 07 00 11 | soma=0x117 -> 17 01
        assert_eq!(control_frame(opcode::KEEPALIVE, &[]), vec![0x55, 0xAA, 0x07, 0x00, 0x11, 0x17, 0x01]);
    }

    #[test]
    fn brightness_bytes_match_python() {
        // control_frame(0x03, [100]) -> 55 AA 08 00 03 64 | soma = 0x55+0xAA+0x08+0x03+0x64 = 0x16E -> 6E 01
        assert_eq!(control_frame(opcode::SET_BRIGHTNESS, &[100]), vec![0x55, 0xAA, 0x08, 0x00, 0x03, 0x64, 0x6E, 0x01]);
    }

    #[test]
    fn image_frame_header_and_checksum() {
        // jpeg = [0xAA, 0xBB] -> len_le32 02 00 00 00 | AA BB | soma = 2+0xAA+0xBB = 0x167 -> 67 01
        assert_eq!(image_frame(&[0xAA, 0xBB]), vec![0x02, 0x00, 0x00, 0x00, 0xAA, 0xBB, 0x67, 0x01]);
    }

    #[test]
    fn parse_reply_json() {
        // monta um envelope com JSON {"width":480}
        let payload = br#"{"width":480}"#;
        let mut body = Vec::new();
        body.extend_from_slice(&MAGIC);
        body.extend_from_slice(&((payload.len() as u16) + 7).to_le_bytes());
        body.push(opcode::GET_DEVICE_INFO);
        body.extend_from_slice(payload);
        body.extend_from_slice(&[0, 0]); // checksum fake (parse não confere)
        let got = reply_payload(&body).unwrap();
        let v: serde_json::Value = serde_json::from_slice(got).unwrap();
        assert_eq!(v["width"], 480);
    }

    #[test]
    fn fragile_limits() {
        let mut info = PanelInfo::default();
        info.model = "TXW818-ST7701S-4.0inch".into();
        info.version = "3.1".into();
        assert!(info.fragile());
        assert_eq!(info.max_frame_kb(), 50);
        assert_eq!(info.max_fps(), 25);
    }
}
