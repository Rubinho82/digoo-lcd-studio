//! Tradução das letras sincronizadas. Porte fiel de translate.py (MyMemory sem chave, DeepL com chave):
//! a letra inteira vai junta (o tradutor entende o contexto), cada linha repetida só uma vez, numa thread,
//! e o resultado fica em cache no disco — nos MESMOS arquivos `tr_<sha1>.json` do app Python.

use crate::lyrics::{cache_dir, sha1_hex, Lyric, LyricState};
use crate::sensors::MediaSnapshot;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const UA: &str = "DigooLCDStudio (https://github.com/Rubinho82/digoo-lcd-studio)";
const LANGS: &[&str] = &["pt", "en", "es", "fr", "it", "de", "ja", "ko"];

fn mymemory_lang(t: &str) -> &'static str {
    match t {
        "pt" => "pt-BR", "en" => "en-US", "es" => "es-ES", "fr" => "fr-FR",
        "it" => "it-IT", "de" => "de-DE", "ja" => "ja-JP", "ko" => "ko-KR", _ => "en-US",
    }
}
fn deepl_lang(t: &str) -> &'static str {
    match t {
        "pt" => "PT-BR", "en" => "EN-US", "es" => "ES", "fr" => "FR",
        "it" => "IT", "de" => "DE", "ja" => "JA", "ko" => "KO", _ => "EN-US",
    }
}

/// Erro com janela de espera (segundos) até tentar de novo.
struct TrError {
    retry: f32,
}
impl TrError {
    fn new(retry: f32) -> TrError {
        TrError { retry }
    }
}

fn same_lang(src: &Option<String>, target: &str) -> bool {
    src.as_deref().map_or(false, |s| s.to_lowercase().split('-').next().unwrap_or("") == target)
}

/// Desfaz as entidades HTML que o MyMemory devolve (&amp; &#39; &quot; …).
fn html_unescape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(amp) = rest.find('&') {
        out.push_str(&rest[..amp]);
        let tail = &rest[amp..];
        let semi = match tail.find(';') {
            Some(p) if p <= 10 => p,
            _ => {
                out.push('&');
                rest = &tail[1..];
                continue;
            }
        };
        let ent = &tail[1..semi];
        let ch = match ent {
            "amp" => Some('&'),
            "lt" => Some('<'),
            "gt" => Some('>'),
            "quot" => Some('"'),
            "apos" | "#39" => Some('\''),
            "#160" => Some('\u{a0}'),
            _ if ent.starts_with("#x") || ent.starts_with("#X") => {
                u32::from_str_radix(&ent[2..], 16).ok().and_then(char::from_u32)
            }
            _ if ent.starts_with('#') => ent[1..].parse::<u32>().ok().and_then(char::from_u32),
            _ => None,
        };
        match ch {
            Some(c) => out.push(c),
            None => out.push_str(&tail[..=semi]), // entidade desconhecida: deixa como está
        }
        rest = &tail[semi + 1..];
    }
    out.push_str(rest);
    out
}

// ----------------------------------------------------------------------
// MyMemory (grátis, sem cadastro)
// ----------------------------------------------------------------------
fn mymemory_one(text: &str, target: &str) -> Result<(Option<String>, Option<String>), TrError> {
    let resp = ureq::get("https://api.mymemory.translated.net/get")
        .timeout(Duration::from_secs(20))
        .set("User-Agent", UA)
        .query("q", text)
        .query("langpair", &format!("autodetect|{}", mymemory_lang(target)))
        .call();
    let body = match resp {
        Ok(r) => r.into_string().map_err(|_| TrError::new(60.0))?,
        Err(ureq::Error::Status(429, _)) => return Err(TrError::new(3600.0)), // limite diário
        Err(ureq::Error::Status(_, _)) => return Err(TrError::new(60.0)),
        Err(_) => return Err(TrError::new(60.0)),
    };
    let d: serde_json::Value = serde_json::from_str(&body).map_err(|_| TrError::new(60.0))?;
    if d.get("quotaFinished").and_then(|v| v.as_bool()).unwrap_or(false) {
        return Err(TrError::new(3600.0));
    }
    let status = match d.get("responseStatus") {
        Some(serde_json::Value::Number(n)) => n.as_i64().unwrap_or(200),
        Some(serde_json::Value::String(s)) => s.parse().unwrap_or(200),
        _ => 200,
    };
    if status == 429 {
        return Err(TrError::new(10.0));
    }
    if status != 200 {
        return Err(TrError::new(1e9)); // langpair inválido / recusa: não insiste
    }
    let rd = d.get("responseData");
    let out = rd
        .and_then(|r| r.get("translatedText"))
        .and_then(|t| t.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| html_unescape(s).replace('\r', ""));
    let src = rd.and_then(|r| r.get("detectedLanguage")).and_then(|t| t.as_str()).map(String::from);
    Ok((out, src))
}

fn mymemory(lines: &[String], target: &str) -> Result<(Vec<String>, Option<String>, bool), TrError> {
    // o MyMemory aceita ~500 bytes por pedido: agrupa as linhas em pedaços de até 450 bytes
    let mut chunks: Vec<Vec<String>> = Vec::new();
    let mut cur: Vec<String> = Vec::new();
    for s in lines {
        if !cur.is_empty() {
            let mut joined = cur.join("\n");
            joined.push('\n');
            joined.push_str(s);
            if joined.len() > 450 {
                chunks.push(std::mem::take(&mut cur));
            }
        }
        cur.push(s.clone());
    }
    if !cur.is_empty() {
        chunks.push(cur);
    }
    if chunks.is_empty() {
        return Ok((Vec::new(), None, false));
    }

    let (first, src) = mymemory_one(&chunks[0].join("\n"), target)?;
    if same_lang(&src, target) {
        return Ok((lines.to_vec(), src, false)); // já está no idioma: fica o original
    }

    let mut degraded = false;
    let mut out: Vec<String> = Vec::new();
    for (ci, ch) in chunks.iter().enumerate() {
        let tr = if ci == 0 { first.clone() } else { mymemory_one(&ch.join("\n"), target)?.0 };
        let parts: Vec<String> = tr.as_deref().map(|t| t.split('\n').map(|p| p.trim().to_string()).collect()).unwrap_or_default();
        if parts.len() == ch.len() {
            out.extend(parts);
        } else {
            for s in ch {
                // perdeu o alinhamento: traduz linha por linha (sem tradução → fica o original)
                match mymemory_one(s, target)?.0 {
                    Some(t) => out.push(t.trim().to_string()),
                    None => {
                        degraded = true;
                        out.push(s.clone());
                    }
                }
            }
        }
    }
    Ok((out, src, degraded))
}

// ----------------------------------------------------------------------
// DeepL (chave grátis, tradução muito boa)
// ----------------------------------------------------------------------
fn deepl(lines: &[String], target: &str, key: &str) -> Result<(Vec<String>, Option<String>, bool), TrError> {
    if key.is_empty() {
        return Err(TrError::new(1e9));
    }
    let host = if key.trim().ends_with(":fx") { "api-free.deepl.com" } else { "api.deepl.com" };
    let url = format!("https://{host}/v2/translate");
    let context: String = lines.join("\n").chars().take(4000).collect();
    let mut out: Vec<String> = Vec::new();
    let mut src: Option<String> = None;
    for part in lines.chunks(50) {
        let payload = serde_json::json!({
            "text": part, "target_lang": deepl_lang(target), "context": context,
        });
        let body = serde_json::to_string(&payload).unwrap_or_default();
        let resp = ureq::post(&url)
            .timeout(Duration::from_secs(30))
            .set("Authorization", &format!("DeepL-Auth-Key {}", key.trim()))
            .set("Content-Type", "application/json")
            .set("User-Agent", UA)
            .send_string(&body);
        let body = match resp {
            Ok(r) => r.into_string().map_err(|_| TrError::new(60.0))?,
            Err(ureq::Error::Status(403, _)) => return Err(TrError::new(1e9)), // chave inválida
            Err(ureq::Error::Status(456, _)) => return Err(TrError::new(3600.0)), // cota do mês
            Err(ureq::Error::Status(_, _)) => return Err(TrError::new(60.0)),
            Err(_) => return Err(TrError::new(60.0)),
        };
        let d: serde_json::Value = serde_json::from_str(&body).map_err(|_| TrError::new(60.0))?;
        let tr = d.get("translations").and_then(|t| t.as_array()).cloned().unwrap_or_default();
        if tr.len() != part.len() {
            return Err(TrError::new(60.0));
        }
        if src.is_none() {
            src = tr.first().and_then(|t| t.get("detected_source_language")).and_then(|s| s.as_str()).map(String::from);
        }
        for t in tr {
            out.push(t.get("text").and_then(|s| s.as_str()).unwrap_or("").to_string());
        }
    }
    Ok((out, src, false))
}

// ----------------------------------------------------------------------
/// Tradução pronta, alinhada linha a linha com a letra (lines[k] = tradução de lyr.lines[k]).
pub struct Translation {
    pub lines: Vec<String>,
}

/// Configuração da tradução (por variáveis de ambiente, até a GUI escrever um config.json lido aqui).
#[derive(Clone)]
pub struct Config {
    pub enabled: bool,
    pub provider: String,
    pub target: String,
    pub key: String,
    pub only: bool, // mostrar só a tradução (em vez de original + tradução embaixo)
}
impl Config {
    /// DIGOO_TRADUZIR=<idioma> liga (pt/en/es/fr/it/de/ja/ko); DIGOO_TRADUTOR=mymemory|deepl;
    /// DIGOO_DEEPL_KEY=<chave>; DIGOO_LETRA_MOSTRAR=ambos|traducao.
    pub fn from_env() -> Config {
        Config::from_values(
            true,
            &std::env::var("DIGOO_TRADUTOR").unwrap_or_default(),
            &std::env::var("DIGOO_TRADUZIR").unwrap_or_default(),
            &std::env::var("DIGOO_DEEPL_KEY").unwrap_or_default(),
            &std::env::var("DIGOO_LETRA_MOSTRAR").unwrap_or_default(),
        )
    }

    /// Constrói a config a partir do config.json (lyrics_translate / provider / lang / key / show).
    pub fn from_values(translate: bool, provider: &str, lang: &str, key: &str, show: &str) -> Config {
        let target = lang.trim().to_lowercase();
        let enabled = translate && LANGS.contains(&target.as_str());
        let mut provider = provider.trim().to_lowercase();
        if provider != "mymemory" && provider != "deepl" {
            provider = "mymemory".into();
        }
        Config {
            enabled,
            provider,
            target: if enabled { target } else { "pt".into() },
            key: key.to_string(),
            only: show.trim() == "traducao",
        }
    }
}

type MapSrc = (HashMap<String, String>, Option<String>);

/// Traduz a letra numa thread (uma por vez) e guarda em memória + disco; devolve na hora o que já tem.
pub struct Translator {
    cfg: Config,
    mem: Arc<Mutex<HashMap<String, MapSrc>>>,
    errors: Arc<Mutex<HashMap<String, (Instant, f32)>>>,
    busy: Arc<Mutex<bool>>,
}
impl Translator {
    pub fn new(cfg: Config) -> Translator {
        Translator {
            cfg,
            mem: Arc::new(Mutex::new(HashMap::new())),
            errors: Arc::new(Mutex::new(HashMap::new())),
            busy: Arc::new(Mutex::new(false)),
        }
    }
    pub fn enabled(&self) -> bool {
        self.cfg.enabled
    }
    pub fn only(&self) -> bool {
        self.cfg.only
    }
    /// Troca a configuração (ao reler o config.json) sem perder o cache de traduções.
    pub fn set_config(&mut self, cfg: Config) {
        self.cfg = cfg;
    }

    fn cache_path(key: &str) -> std::path::PathBuf {
        cache_dir().join(format!("tr_{}.json", &sha1_hex(key)[..24]))
    }
    fn load_disk(key: &str) -> Option<MapSrc> {
        let d: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(Self::cache_path(key)).ok()?).ok()?;
        let map: HashMap<String, String> = serde_json::from_value(d.get("map")?.clone()).ok()?;
        Some((map, d.get("src").and_then(|s| s.as_str()).map(String::from)))
    }
    fn save_disk(key: &str, map: &HashMap<String, String>, src: &Option<String>) {
        let _ = std::fs::create_dir_all(cache_dir());
        let v = serde_json::json!({ "map": map, "src": src });
        if let Ok(s) = serde_json::to_string(&v) {
            let _ = std::fs::write(Self::cache_path(key), s);
        }
    }

    /// Tradução da letra atual (None = desligada, sem letra, traduzindo ainda, ou já no idioma).
    pub fn for_lyrics(&self, lyr: Option<&Lyric>, _media: Option<&MediaSnapshot>) -> Option<Translation> {
        if !self.cfg.enabled {
            return None;
        }
        let lyr = lyr?;
        if lyr.state != LyricState::Ok {
            return None;
        }
        // linhas únicas (na ordem), sem repetir nem vazias — o tradutor recebe cada uma só uma vez
        let mut uniq: Vec<String> = Vec::new();
        for (_, s) in &lyr.lines {
            let t = s.trim();
            if !t.is_empty() && !uniq.iter().any(|u| u == t) {
                uniq.push(t.to_string());
            }
        }
        if uniq.is_empty() {
            return None;
        }
        let key = format!("{}|{}|{}", self.cfg.provider, self.cfg.target, uniq.join("\n"));

        let mut mem = self.mem.lock().unwrap();
        let hit = match mem.get(&key) {
            Some(v) => Some(v.clone()),
            None => {
                let d = Self::load_disk(&key);
                if let Some(ref v) = d {
                    mem.insert(key.clone(), v.clone());
                }
                d
            }
        };
        drop(mem);
        if let Some((map, src)) = hit {
            if same_lang(&src, &self.cfg.target) {
                return None; // já está no idioma: mostra só o original
            }
            let lines = lyr.lines.iter().map(|(_, s)| map.get(s.trim()).cloned().unwrap_or_default()).collect();
            return Some(Translation { lines });
        }

        if let Some((t, retry)) = self.errors.lock().unwrap().get(&key) {
            if t.elapsed().as_secs_f32() < *retry {
                return None;
            }
        }
        let mut busy = self.busy.lock().unwrap();
        if !*busy {
            *busy = true;
            let (mem, errors, busy_arc) = (self.mem.clone(), self.errors.clone(), self.busy.clone());
            let (cfg, uniq2, key2) = (self.cfg.clone(), uniq.clone(), key.clone());
            std::thread::spawn(move || {
                let res = if cfg.provider == "deepl" {
                    deepl(&uniq2, &cfg.target, &cfg.key)
                } else {
                    mymemory(&uniq2, &cfg.target)
                };
                match res {
                    Ok((out, src, partial)) => {
                        let map: HashMap<String, String> = uniq2.into_iter().zip(out).collect();
                        if !partial {
                            Self::save_disk(&key2, &map, &src); // tradução degradada fica só na memória
                        }
                        mem.lock().unwrap().insert(key2, (map, src));
                    }
                    Err(e) => {
                        errors.lock().unwrap().insert(key2, (Instant::now(), e.retry));
                    }
                }
                *busy_arc.lock().unwrap() = false;
            });
        }
        None // ainda traduzindo: mostra o original
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn desfaz_html() {
        assert_eq!(html_unescape("you &amp; me &#39;cause"), "you & me 'cause");
        assert_eq!(html_unescape("a &lt;b&gt; &quot;c&quot;"), "a <b> \"c\"");
        assert_eq!(html_unescape("sem entidade & fim"), "sem entidade & fim");
    }

    #[test]
    fn idioma_de_origem() {
        assert!(same_lang(&Some("en-US".into()), "en"));
        assert!(same_lang(&Some("PT".into()), "pt"));
        assert!(!same_lang(&Some("en".into()), "pt"));
        assert!(!same_lang(&None, "pt"));
    }
}
