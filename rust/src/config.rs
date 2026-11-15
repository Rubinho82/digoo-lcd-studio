//! Lê o config.json que a GUI escreve (%APPDATA%\DigooLCD\config.json) e dirige o núcleo por ele.
//! Porte do essencial de config.py: só o subconjunto de campos que o núcleo usa, com os mesmos padrões.
//! Assim a interface Python continua mandando — o núcleo relê o arquivo quando ele muda (ao vivo).

use crate::render::{self, Palette};
use serde::Deserialize;
use std::path::PathBuf;
use std::time::SystemTime;
use tiny_skia::Color;

const MODES: &[&str] = &["painel", "midia", "espelho"];

#[derive(Deserialize, Clone)]
#[serde(default)]
pub struct Colors {
    pub accent: String,
    pub accent2: String,
    pub text: String,
    pub muted: String,
    pub background: String,
    pub background2: String,
}
impl Default for Colors {
    fn default() -> Self {
        Colors {
            accent: "#00E5FF".into(),
            accent2: "#FF2E88".into(),
            text: "#FFFFFF".into(),
            muted: "#8A93A6".into(),
            background: "#05070D".into(),
            background2: "#141A2E".into(),
        }
    }
}

/// Subconjunto do config.py que o núcleo honra (campos ausentes caem no padrão; desconhecidos são ignorados).
#[derive(Deserialize, Clone)]
#[serde(default)]
pub struct Config {
    pub mode: String,  // painel | midia | espelho
    pub theme: String, // tema de sensor, ou "musica"
    pub rotation: i64,
    pub fps: i64,
    pub max_kb: i64,
    pub jpeg_quality: i64,
    pub colors: Colors,
    pub music_layout: String,
    pub music_wave: String,
    pub music_lyrics: bool,
    pub lyrics_translate: bool,
    pub lyrics_lang: String,
    pub lyrics_provider: String,
    pub lyrics_show: String,
    pub deepl_key: String,
    pub media_paths: Vec<String>,
    pub media_fit: String,
    pub mirror_fit: String,
    pub brightness: i64,
    pub night_enabled: bool,
    pub night_start: String,
    pub night_end: String,
    pub night_brightness: i64,
}
impl Default for Config {
    fn default() -> Self {
        Config {
            mode: "painel".into(),
            theme: "aneis".into(),
            rotation: 90,
            fps: 20,
            max_kb: 0,
            jpeg_quality: 95,
            colors: Colors::default(),
            music_layout: "capa".into(),
            music_wave: "circulo".into(),
            music_lyrics: true,
            lyrics_translate: false,
            lyrics_lang: "pt".into(),
            lyrics_provider: "mymemory".into(),
            lyrics_show: "ambos".into(),
            deepl_key: String::new(),
            media_paths: Vec::new(),
            media_fit: "cover".into(),
            mirror_fit: "contain".into(),
            brightness: 80,
            night_enabled: false,
            night_start: "23:00".into(),
            night_end: "07:00".into(),
            night_brightness: 15,
        }
    }
}

pub fn config_path() -> PathBuf {
    std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("DigooLCD")
        .join("config.json")
}

/// Hora da última alteração do config.json (p/ saber quando a GUI mexeu).
pub fn mtime() -> Option<SystemTime> {
    std::fs::metadata(config_path()).and_then(|m| m.modified()).ok()
}

/// Lê e valida o config.json (ausente/corrompido → padrão). Chave DeepL cifrada (DPAPI) não é lida aqui —
/// use a variável de ambiente DIGOO_DEEPL_KEY para o DeepL até o núcleo decifrar o DPAPI.
pub fn load() -> Config {
    let mut cfg: Config = std::fs::read_to_string(config_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();
    if !MODES.contains(&cfg.mode.as_str()) {
        cfg.mode = "painel".into();
    }
    if cfg.deepl_key.starts_with("dpapi:") {
        cfg.deepl_key.clear(); // cifrada pela GUI; sem DPAPI ainda, não dá p/ usar
    }
    cfg
}

fn hex(s: &str, fallback: Color) -> Color {
    let s = s.trim().trim_start_matches('#');
    if s.len() == 6 {
        if let (Ok(r), Ok(g), Ok(b)) = (
            u8::from_str_radix(&s[0..2], 16),
            u8::from_str_radix(&s[2..4], 16),
            u8::from_str_radix(&s[4..6], 16),
        ) {
            return render::rgb(r, g, b);
        }
    }
    fallback
}

/// Paleta a partir das cores do config (cada cor inválida cai na cor padrão correspondente).
pub fn palette(cfg: &Config) -> Palette {
    let d = Palette::default();
    let c = &cfg.colors;
    Palette {
        acc: hex(&c.accent, d.acc),
        acc2: hex(&c.accent2, d.acc2),
        txt: hex(&c.text, d.txt),
        muted: hex(&c.muted, d.muted),
        bg: hex(&c.background, d.bg),
        bg2: hex(&c.background2, d.bg2),
    }
}

fn parse_hhmm(s: &str) -> u32 {
    let mut it = s.trim().split(':');
    let h = it.next().and_then(|x| x.trim().parse::<u32>().ok()).unwrap_or(0) % 24;
    let m = it.next().and_then(|x| x.trim().parse::<u32>().ok()).unwrap_or(0) % 60;
    h * 60 + m
}

/// Hora local atual em minutos desde a meia-noite (GetLocalTime do Windows).
fn now_minutes() -> u32 {
    #[repr(C)]
    #[derive(Default)]
    struct SystemTime {
        year: u16,
        month: u16,
        day_of_week: u16,
        day: u16,
        hour: u16,
        minute: u16,
        second: u16,
        ms: u16,
    }
    extern "system" {
        fn GetLocalTime(t: *mut SystemTime);
    }
    let mut t = SystemTime::default();
    unsafe { GetLocalTime(&mut t) };
    t.hour as u32 * 60 + t.minute as u32
}

/// Está dentro da janela noturna? (trata a virada da meia-noite, como o in_night_window do Python.)
pub fn in_night_window(cfg: &Config) -> bool {
    if !cfg.night_enabled {
        return false;
    }
    let (a, b) = (parse_hhmm(&cfg.night_start), parse_hhmm(&cfg.night_end));
    if a == b {
        return false;
    }
    let now = now_minutes();
    if a < b {
        a <= now && now < b
    } else {
        now >= a || now < b
    }
}

/// Brilho alvo (0..100) considerando a agenda noturna.
pub fn target_brightness(cfg: &Config) -> u8 {
    let v = if in_night_window(cfg) { cfg.night_brightness } else { cfg.brightness };
    v.clamp(0, 100) as u8
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn le_config_parcial() {
        // só alguns campos: o resto cai no padrão; cores parciais também
        let json = r##"{ "mode": "espelho", "theme": "neon", "rotation": 270,
                        "colors": { "accent": "#112233" }, "fps": 30, "music_layout": "vinil" }"##;
        let cfg: Config = serde_json::from_str(json).unwrap();
        assert_eq!(cfg.mode, "espelho");
        assert_eq!(cfg.theme, "neon");
        assert_eq!(cfg.rotation, 270);
        assert_eq!(cfg.fps, 30);
        assert_eq!(cfg.music_layout, "vinil");
        assert_eq!(cfg.brightness, 80); // padrão
        assert_eq!(cfg.colors.accent, "#112233");
        assert_eq!(cfg.colors.text, "#FFFFFF"); // padrão (cor ausente)
        let p = palette(&cfg);
        assert_eq!((p.acc.red() * 255.0).round() as u8, 0x11);
        assert_eq!((p.acc.blue() * 255.0).round() as u8, 0x33);
    }

    #[test]
    fn config_invalido_vira_padrao() {
        let cfg: Config = serde_json::from_str("isto não é json").unwrap_or_default();
        assert_eq!(cfg.mode, "painel");
        assert_eq!(cfg.theme, "aneis");
    }

    #[test]
    fn janela_noturna() {
        let mut cfg = Config::default();
        cfg.night_enabled = true;
        cfg.night_start = "23:00".into();
        cfg.night_end = "07:00".into();
        // a janela cruza a meia-noite: 23:30 e 02:00 dentro; 12:00 fora
        assert!(in_minutes(&cfg, 23 * 60 + 30));
        assert!(in_minutes(&cfg, 2 * 60));
        assert!(!in_minutes(&cfg, 12 * 60));
        cfg.night_enabled = false;
        assert!(!in_minutes(&cfg, 23 * 60 + 30));
    }

    // versão testável de in_night_window com a hora injetada
    fn in_minutes(cfg: &Config, now: u32) -> bool {
        if !cfg.night_enabled {
            return false;
        }
        let (a, b) = (parse_hhmm(&cfg.night_start), parse_hhmm(&cfg.night_end));
        if a == b {
            return false;
        }
        if a < b {
            a <= now && now < b
        } else {
            now >= a || now < b
        }
    }
}
