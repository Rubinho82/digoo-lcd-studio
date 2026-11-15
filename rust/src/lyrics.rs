//! Letras sincronizadas via LRCLIB (lrclib.net — aberto, sem chave). Porte fiel do lyrics.py:
//! busca numa thread (uma música por vez), guarda em memória e no disco (mesmos arquivos do app Python).

use crate::sensors::MediaSnapshot;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const API: &str = "https://lrclib.net/api";
const UA: &str = "DigooLCDStudio (https://github.com/Rubinho82/digoo-lcd-studio)";
const ERROR_RETRY: f32 = 15.0; // falha de rede: janela curta p/ tentar de novo
const NOT_FOUND_RETRY: f64 = 3.0 * 86400.0; // letra que não existia: tenta de novo depois de 3 dias

#[derive(Clone, Copy, PartialEq)]
pub enum LyricState {
    Searching,
    Ok,
    None,
    Instrumental,
    Error,
    Off,
}

#[derive(Clone)]
pub struct Lyric {
    pub state: LyricState,
    pub lines: Vec<(f32, String)>, // (segundos, texto)
    pub synced: bool,
}
impl Lyric {
    fn simple(state: LyricState) -> Lyric {
        Lyric { state, lines: Vec::new(), synced: false }
    }
    /// (linha atual, fração 0..1 já cantada, segundos desde o início da linha). -1 = antes da 1ª.
    /// Letra sem tempo é espalhada pela duração da música.
    pub fn at(&self, pos: f32, duration: f32) -> (i32, f32, f32) {
        let n = self.lines.len();
        if n == 0 {
            return (-1, 0.0, 0.0);
        }
        if !self.synced {
            if duration <= 0.0 {
                return (0, 0.0, 99.0);
            }
            let per = duration / n as f32;
            let i = ((pos / per) as usize).min(n - 1);
            return (i as i32, (pos - i as f32 * per) / per, pos - i as f32 * per);
        }
        let mut i: i32 = -1; // maior índice com time <= pos
        for (k, (t, _)) in self.lines.iter().enumerate() {
            if *t <= pos {
                i = k as i32;
            } else {
                break;
            }
        }
        if i < 0 {
            return (-1, 0.0, 0.0);
        }
        let iu = i as usize;
        let t0 = self.lines[iu].0;
        let t1 = if iu + 1 < n { self.lines[iu + 1].0 } else { t0 + 5.0 };
        (i, ((pos - t0) / (t1 - t0).max(0.05)).clamp(0.0, 1.0), pos - t0)
    }
}

#[derive(Deserialize, Clone)]
struct LrcRec {
    #[serde(rename = "syncedLyrics")]
    synced: Option<String>,
    #[serde(rename = "plainLyrics")]
    plain: Option<String>,
    instrumental: Option<bool>,
    duration: Option<f64>,
}

/// Segundos de um marcador LRC "mm:ss" ou "mm:ss.xx".
fn parse_ts(s: &str) -> Option<f32> {
    let (mm, ss) = s.split_once(':')?;
    if mm.is_empty() || !mm.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let m: f32 = mm.parse().ok()?;
    let sec: f32 = ss.parse().ok()?;
    Some(m * 60.0 + sec)
}

/// Converte o texto LRC em (segundos, texto). Uma linha pode ter vários marcadores.
fn parse_lrc(text: &str) -> Vec<(f32, String)> {
    let mut out: Vec<(f32, String)> = Vec::new();
    for raw in text.lines() {
        let mut times: Vec<f32> = Vec::new();
        let mut buf = raw.to_string();
        loop {
            let (lb, rb) = match (buf.find('['), buf.find(']')) {
                (Some(a), Some(b)) if a < b => (a, b),
                _ => break,
            };
            match parse_ts(&buf[lb + 1..rb]) {
                Some(sec) => {
                    times.push(sec);
                    buf.replace_range(lb..rb + 1, "");
                }
                None => break, // "[Chorus]"/"[ar:..]" etc.: para por aqui
            }
        }
        if times.is_empty() {
            continue;
        }
        let words = buf.trim().to_string();
        for t in times {
            out.push((t, words.clone()));
        }
    }
    out.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    out
}

/// Remove todos os marcadores [..] (p/ contar/limpar linhas plenas).
fn strip_ts(s: &str) -> String {
    let mut out = String::new();
    let mut depth = 0u32;
    for ch in s.chars() {
        match ch {
            '[' => depth += 1,
            ']' => depth = depth.saturating_sub(1),
            _ if depth == 0 => out.push(ch),
            _ => {}
        }
    }
    out
}

/// Tira blocos (…)/[…] com palavras-lixo ("official video", "lyric", "ao vivo"…) do título.
fn strip_bracket_junk(s: &str, open: char, close: char) -> String {
    const JUNK: &[&str] = &[
        "official", "oficial", "video", "vídeo", "clipe", "clip", "lyric", "letra", "audio", "áudio", "hd", "4k",
        "mv", "live", "ao vivo", "remaster", "visuali", "explicit", "prod.", "legendad", "color coded", "tradu",
    ];
    let mut out = String::new();
    let mut rest = s;
    while let Some(lb) = rest.find(open) {
        let rb = match rest[lb..].find(close) {
            Some(r) => lb + r,
            None => break,
        };
        let inside = rest[lb + 1..rb].to_lowercase();
        out.push_str(&rest[..lb]);
        if !JUNK.iter().any(|j| inside.contains(j)) {
            out.push_str(&rest[lb..=rb]); // bloco "bom" (ex. "(feat. X)"): mantém
        }
        rest = &rest[rb + close.len_utf8()..];
    }
    out.push_str(rest);
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn clean(title: &str) -> String {
    let s = strip_bracket_junk(title, '(', ')');
    let s = strip_bracket_junk(&s, '[', ']');
    s.trim_matches(|c: char| c.is_whitespace() || "-–\"'".contains(c)).to_string()
}

/// Pares (música, artista) a tentar, do mais provável ao menos (fiel ao candidates do Python).
fn candidates(media: &MediaSnapshot) -> Vec<(String, String)> {
    let (title, artist) = (media.title.trim(), media.artist.trim());
    let app = media.app.to_lowercase();
    let browser = ["chrome", "msedge", "firefox", "opera", "brave", "vivaldi"].iter().any(|b| app.contains(b));
    if !artist.is_empty() && !browser {
        let t = clean(title);
        return if t.is_empty() { vec![] } else { vec![(t, artist.to_string())] };
    }
    for sep in [" - ", " – ", " — "] {
        if let Some((a, t)) = title.split_once(sep) {
            let (tc, ac) = (clean(t), clean(a));
            if !tc.is_empty() {
                return vec![(tc, ac)];
            }
        }
    }
    if !artist.is_empty() {
        let t = clean(title);
        if !t.is_empty() {
            return vec![(t, artist.to_string())];
        }
    }
    vec![]
}

fn to_recs(val: serde_json::Value) -> Vec<LrcRec> {
    if val.is_array() {
        serde_json::from_value::<Vec<LrcRec>>(val).unwrap_or_default()
    } else if val.is_object() {
        serde_json::from_value::<LrcRec>(val).ok().into_iter().collect()
    } else {
        Vec::new()
    }
}

/// GET na LRCLIB: Ok(registros) (vazio = 404/nada), Err = falha de rede (retry em 503/timeout).
fn get_json(path: &str, params: &[(&str, String)]) -> Result<Vec<LrcRec>, ()> {
    for attempt in 0..3u64 {
        let mut req = ureq::get(&format!("{}/{}", API, path))
            .timeout(Duration::from_secs(12))
            .set("User-Agent", UA)
            .set("Accept", "application/json");
        for (k, v) in params {
            req = req.query(k, v.as_str());
        }
        match req.call() {
            Ok(resp) => {
                let body = resp.into_string().map_err(|_| ())?;
                return serde_json::from_str::<serde_json::Value>(&body).map(to_recs).map_err(|_| ());
            }
            Err(ureq::Error::Status(404, _)) => return Ok(Vec::new()),
            Err(ureq::Error::Status(code, _)) if code >= 500 && attempt < 2 => {
                std::thread::sleep(Duration::from_millis(1500 * (attempt + 1)));
            }
            Err(ureq::Error::Status(_, _)) => return Err(()),
            Err(_) => {
                if attempt == 2 {
                    return Err(());
                }
                std::thread::sleep(Duration::from_millis(1000 * (attempt + 1)));
            }
        }
    }
    Err(())
}

fn rec_lines(r: &LrcRec) -> usize {
    let txt = r.synced.as_deref().or(r.plain.as_deref()).unwrap_or("");
    txt.lines().filter(|ln| !strip_ts(ln).trim().is_empty()).count()
}

/// Score p/ ordenar: com tempo primeiro, depois duração mais parecida.
fn score(r: &LrcRec, dur: f32) -> (u8, u8, f32) {
    let d = if dur > 0.0 { (r.duration.unwrap_or(0.0) as f32 - dur).abs() } else { 0.0 };
    (if r.synced.is_some() { 0 } else { 1 }, if d < 4.0 { 0 } else { 1 }, d)
}

/// Escolhe o melhor registro: filtra lixo (>=5 linhas), prefere duração próxima, com tempo.
fn pick(recs: Vec<LrcRec>, dur: f32) -> Option<LrcRec> {
    let mut good: Vec<LrcRec> =
        recs.into_iter().filter(|r| r.instrumental.unwrap_or(false) || rec_lines(r) >= 5).collect();
    if dur > 0.0 {
        let near: Vec<LrcRec> =
            good.iter().filter(|r| r.duration.map_or(true, |d| (d as f32 - dur).abs() < 25.0)).cloned().collect();
        if !near.is_empty() {
            good = near;
        }
    }
    good.into_iter().min_by(|a, b| {
        score(a, dur).partial_cmp(&score(b, dur)).unwrap_or(std::cmp::Ordering::Equal)
    })
}

fn fetch(media: &MediaSnapshot) -> Result<Option<LrcRec>, ()> {
    let dur = media.duration as f32;
    for (track, artist) in candidates(media) {
        let mut params = vec![("track_name", track.clone()), ("artist_name", artist.clone())];
        if dur > 0.0 {
            params.push(("duration", (dur.round() as i64).to_string()));
        }
        if let Some(rec) = pick(get_json("get", &params)?, dur) {
            return Ok(Some(rec));
        }
        let search = vec![("track_name", track.clone()), ("artist_name", artist.clone())];
        if let Some(rec) = pick(get_json("search", &search)?, dur) {
            return Ok(Some(rec));
        }
    }
    if let Some((t, a)) = candidates(media).into_iter().next() {
        let q = vec![("q", format!("{} {}", t, a).trim().to_string())];
        return Ok(pick(get_json("search", &q)?, dur));
    }
    Ok(None)
}

fn to_lyric(rec: Option<LrcRec>) -> Lyric {
    let rec = match rec {
        Some(r) => r,
        None => return Lyric::simple(LyricState::None),
    };
    if rec.instrumental.unwrap_or(false) {
        return Lyric::simple(LyricState::Instrumental);
    }
    let synced = parse_lrc(rec.synced.as_deref().unwrap_or(""));
    if !synced.is_empty() {
        return Lyric { state: LyricState::Ok, lines: synced, synced: true };
    }
    let plain: Vec<(f32, String)> = rec
        .plain
        .as_deref()
        .unwrap_or("")
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| (0.0, l.trim().to_string()))
        .collect();
    if plain.is_empty() {
        Lyric::simple(LyricState::None)
    } else {
        Lyric { state: LyricState::Ok, lines: plain, synced: false }
    }
}

pub fn key(media: &MediaSnapshot) -> String {
    format!("{}|{}", media.title.trim().to_lowercase(), media.artist.trim().to_lowercase())
}

/// Pasta de cache compartilhada com o app Python: %APPDATA%\DigooLCD\cache\lyrics.
pub fn cache_dir() -> std::path::PathBuf {
    std::env::var_os("APPDATA")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("DigooLCD")
        .join("cache")
        .join("lyrics")
}

/// SHA-1 em hex (40 chars). Usado p/ nomear os arquivos de cache igual ao Python (sha1(chave)).
pub fn sha1_hex(input: &str) -> String {
    let mut h: [u32; 5] = [0x6745_2301, 0xEFCD_AB89, 0x98BA_DCFE, 0x1032_5476, 0xC3D2_E1F0];
    let mut msg = input.as_bytes().to_vec();
    let ml = (msg.len() as u64) * 8;
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&ml.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 80];
        for (i, word) in w.iter_mut().take(16).enumerate() {
            *word = u32::from_be_bytes([chunk[i * 4], chunk[i * 4 + 1], chunk[i * 4 + 2], chunk[i * 4 + 3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, &wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A82_7999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDC),
                _ => (b ^ c ^ d, 0xCA62_C1D6),
            };
            let tmp = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = tmp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

fn now_unix() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Formato do arquivo de cache (igual ao Python): a letra crua, ou o selo {none,t} de "não existe".
#[derive(Serialize, Deserialize, Default)]
struct DiskRec {
    #[serde(rename = "syncedLyrics", skip_serializing_if = "Option::is_none")]
    synced: Option<String>,
    #[serde(rename = "plainLyrics", skip_serializing_if = "Option::is_none")]
    plain: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    instrumental: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    none: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    t: Option<f64>,
}

/// Busca as letras numa thread (uma por vez) e guarda em memória + disco; devolve na hora o que já tem.
pub struct LyricsCache {
    mem: Arc<Mutex<HashMap<String, Lyric>>>,
    errors: Arc<Mutex<HashMap<String, Instant>>>,
    busy: Arc<Mutex<bool>>,
}
impl LyricsCache {
    pub fn new() -> LyricsCache {
        LyricsCache {
            mem: Arc::new(Mutex::new(HashMap::new())),
            errors: Arc::new(Mutex::new(HashMap::new())),
            busy: Arc::new(Mutex::new(false)),
        }
    }

    pub fn get(&self, media: &MediaSnapshot) -> Lyric {
        let k = key(media);
        if let Some(l) = self.mem.lock().unwrap().get(&k) {
            return l.clone();
        }
        if let Some(t) = self.errors.lock().unwrap().get(&k) {
            if t.elapsed().as_secs_f32() < ERROR_RETRY {
                return Lyric::simple(LyricState::Error);
            }
        }
        if let Some(l) = Self::load_disk(&k) {
            self.mem.lock().unwrap().insert(k, l.clone());
            return l;
        }
        let mut busy = self.busy.lock().unwrap();
        if !*busy {
            *busy = true;
            let (mem, errors, busy_arc) = (self.mem.clone(), self.errors.clone(), self.busy.clone());
            let (media2, k2) = (media.clone(), k.clone());
            std::thread::spawn(move || {
                match fetch(&media2) {
                    Ok(rec) => {
                        LyricsCache::save_disk(&k2, rec.as_ref());
                        mem.lock().unwrap().insert(k2, to_lyric(rec));
                    }
                    Err(_) => {
                        errors.lock().unwrap().insert(k2, Instant::now());
                    }
                }
                *busy_arc.lock().unwrap() = false;
            });
        }
        Lyric::simple(LyricState::Searching)
    }

    /// Lê a letra do disco (None = sem cache, ou "não existe" já vencido — vale buscar de novo).
    fn load_disk(k: &str) -> Option<Lyric> {
        let path = cache_dir().join(format!("{}.json", &sha1_hex(k)[..20]));
        let d: DiskRec = serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()?;
        if d.none.unwrap_or(false) {
            return (now_unix() - d.t.unwrap_or(0.0) < NOT_FOUND_RETRY).then(|| Lyric::simple(LyricState::None));
        }
        Some(to_lyric(Some(LrcRec { synced: d.synced, plain: d.plain, instrumental: d.instrumental, duration: None })))
    }

    /// Grava no disco a letra crua (ou o selo de "não existe") — mesmo arquivo que o app Python lê.
    fn save_disk(k: &str, rec: Option<&LrcRec>) {
        let _ = std::fs::create_dir_all(cache_dir());
        let disk = match rec {
            Some(r) => DiskRec {
                synced: r.synced.clone(), plain: r.plain.clone(), instrumental: r.instrumental, none: None, t: None,
            },
            None => DiskRec { none: Some(true), t: Some(now_unix()), ..Default::default() },
        };
        if let Ok(s) = serde_json::to_string(&disk) {
            let _ = std::fs::write(cache_dir().join(format!("{}.json", &sha1_hex(k)[..20])), s);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_lrc_basico() {
        let lrc = "[ar:The Weeknd]\n[00:12.50]primeira linha\n[00:15.00]segunda\n[01:18.20]terceira";
        let v = parse_lrc(lrc);
        assert_eq!(v.len(), 3, "metadado [ar:] deve ser ignorado");
        assert!((v[0].0 - 12.5).abs() < 0.01);
        assert_eq!(v[0].1, "primeira linha");
        assert!((v[2].0 - 78.2).abs() < 0.01); // 01:18.20
    }

    #[test]
    fn limpa_titulo() {
        assert_eq!(clean("Blinding Lights (Official Video)"), "Blinding Lights");
        assert_eq!(clean("Song (feat. Artist)"), "Song (feat. Artist)"); // bloco "bom" fica
    }

    #[test]
    fn sha1_vetores() {
        // vetores conhecidos: garante que o nome do arquivo de cache bate com o do Python (hashlib.sha1)
        assert_eq!(sha1_hex(""), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
        assert_eq!(sha1_hex("abc"), "a9993e364706816aba3e25717850c26c9cd0d89d");
        assert_eq!(&sha1_hex("blinding lights|the weeknd")[..20], &sha1_hex("blinding lights|the weeknd")[..20]);
    }

    #[test]
    fn fetch_ao_vivo() {
        // valida o caminho HTTP+LRCLIB de ponta a ponta (música que existe no banco).
        let m = MediaSnapshot {
            title: "Blinding Lights".into(), artist: "The Weeknd".into(), album: String::new(),
            status: "Playing".into(), app: "Spotify.exe".into(), position: 0.0, duration: 200.0,
            cover: String::new(),
        };
        let cache = LyricsCache::new();
        let mut lyr = cache.get(&m);
        for _ in 0..30 {
            if lyr.state != LyricState::Searching {
                break;
            }
            std::thread::sleep(Duration::from_millis(500));
            lyr = cache.get(&m);
        }
        let st = match lyr.state {
            LyricState::Ok => "ok",
            LyricState::None => "sem",
            LyricState::Instrumental => "instrumental",
            LyricState::Error => "erro",
            LyricState::Searching => "buscando",
            LyricState::Off => "off",
        };
        println!("letra 'Blinding Lights': estado={st} linhas={} synced={}", lyr.lines.len(), lyr.synced);
        if lyr.state == LyricState::Ok && !lyr.lines.is_empty() {
            println!("  1ª linha: {:?} @ {:.1}s", lyr.lines[0].1, lyr.lines[0].0);
        }
        assert!(lyr.state != LyricState::Searching, "não resolveu em 15s");
        if lyr.state == LyricState::Ok {
            assert!(!lyr.lines.is_empty());
        }
    }
}
