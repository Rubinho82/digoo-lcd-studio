//! Lê os sensores do mesmo sensorhelper.exe (C# / LibreHardwareMonitor) que o app Python usa:
//! uma linha JSON por ciclo no stdout. A RAM vem do Windows (o helper não lê memória, igual no Python).

use serde::Deserialize;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[derive(Clone, Copy)]
pub struct Metrics {
    pub cpu_temp: f32,
    pub gpu_temp: f32,
    pub cpu_load: f32,
    pub gpu_load: f32,
    pub ram_load: f32,
    pub cpu_clock: f32,      // MHz
    pub cpu_power: f32,      // W
    pub gpu_hotspot: f32,    // °C (junção de memória / hot spot)
    pub gpu_clock: f32,      // MHz
    pub gpu_power: f32,      // W
    pub gpu_vram: f32,       // MB usados
    pub gpu_vram_total: f32, // MB totais
    pub gpu_fan: f32,        // RPM
    pub disk_temp: f32,      // °C (disco mais quente)
    pub net_down: f32,       // B/s
    pub net_up: f32,         // B/s
    pub pump_rpm: f32,       // RPM (bomba do water cooler)
    pub fan_rpm: f32,        // RPM (ventoinha da CPU)
}

impl Default for Metrics {
    fn default() -> Self {
        Metrics {
            cpu_temp: f32::NAN, gpu_temp: f32::NAN, cpu_load: f32::NAN, gpu_load: f32::NAN, ram_load: f32::NAN,
            cpu_clock: f32::NAN, cpu_power: f32::NAN, gpu_hotspot: f32::NAN, gpu_clock: f32::NAN, gpu_power: f32::NAN,
            gpu_vram: f32::NAN, gpu_vram_total: f32::NAN, gpu_fan: f32::NAN, disk_temp: f32::NAN,
            net_down: f32::NAN, net_up: f32::NAN, pump_rpm: f32::NAN, fan_rpm: f32::NAN,
        }
    }
}

#[derive(Deserialize)]
struct Row {
    #[serde(rename = "hwType")]
    hw_type: String,
    #[serde(rename = "type")]
    kind: String,
    name: String,
    value: f64,
}

/// Uma linha do helper: pode trazer sensores, áudio (espectro) ou a mídia tocando.
#[derive(Deserialize)]
struct Line {
    sensors: Option<Vec<Row>>,
    audio: Option<AudioMsg>,
    media: Option<MediaMsg>,
}
#[derive(Deserialize)]
struct AudioMsg {
    b: Option<Vec<f32>>, // 48 bandas 0..100
    lv: Option<f32>,     // nível 0..100
}
#[derive(Deserialize)]
struct MediaMsg {
    title: Option<String>,
    artist: Option<String>,
    album: Option<String>,
    status: Option<String>,
    app: Option<String>,
    position: Option<f64>,
    duration: Option<f64>,
    cover: Option<String>, // caminho do arquivo da miniatura que o helper salvou
}

/// Última leitura do espectro de áudio (loopback do PC) vinda do helper com `--audio`.
#[derive(Clone)]
pub struct AudioSnapshot {
    pub bands: Vec<f32>, // 48, 0..100
    pub level: f32,      // 0..100
    pub ts: Instant,     // quando chegou (p/ saber se está fresco)
}

/// "Tocando agora" (Spotify/YouTube…) via GSMTC, repassado pelo helper.
#[derive(Clone, Default)]
pub struct MediaSnapshot {
    pub title: String,
    pub artist: String,
    pub album: String,
    pub status: String, // "Playing" / "Paused"
    pub app: String,
    pub position: f64,  // s
    pub duration: f64,  // s
    pub cover: String,  // caminho do arquivo da capa (vazio = sem arte)
}
impl From<MediaMsg> for MediaSnapshot {
    fn from(m: MediaMsg) -> Self {
        MediaSnapshot {
            title: m.title.unwrap_or_default(),
            artist: m.artist.unwrap_or_default(),
            album: m.album.unwrap_or_default(),
            status: m.status.unwrap_or_default(),
            app: m.app.unwrap_or_default(),
            position: m.position.unwrap_or(0.0),
            duration: m.duration.unwrap_or(0.0),
            cover: m.cover.unwrap_or_default(),
        }
    }
}

pub struct Sensors {
    pub metrics: Arc<Mutex<Metrics>>,
    audio: Arc<Mutex<Option<AudioSnapshot>>>,
    media: Arc<Mutex<Option<MediaSnapshot>>>,
    media_at: Arc<Mutex<Instant>>, // quando chegou a última mídia (p/ avançar a posição)
    child: Child,
}

impl Sensors {
    /// Sobe o helper C# e começa a ler numa thread. Com `with_audio` passa `--audio`
    /// (loopback + FFT no helper) p/ receber o espectro e a mídia tocando.
    pub fn start(helper: &str, interval_ms: u32, with_audio: bool) -> std::io::Result<Sensors> {
        let mut cmd = Command::new(helper);
        cmd.arg(interval_ms.to_string());
        if with_audio {
            cmd.arg("--audio");
        }
        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let metrics = Arc::new(Mutex::new(Metrics::default()));
        let audio: Arc<Mutex<Option<AudioSnapshot>>> = Arc::new(Mutex::new(None));
        let media: Arc<Mutex<Option<MediaSnapshot>>> = Arc::new(Mutex::new(None));
        let media_at = Arc::new(Mutex::new(Instant::now()));
        let out = child.stdout.take().unwrap();
        let (m, a, md, mt) = (metrics.clone(), audio.clone(), media.clone(), media_at.clone());
        std::thread::spawn(move || {
            let reader = BufReader::new(out);
            for line in reader.lines().map_while(Result::ok) {
                if !line.starts_with('{') {
                    continue;
                }
                let msg = match serde_json::from_str::<Line>(&line) {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                if let Some(rows) = msg.sensors {
                    let mut cur = m.lock().unwrap();
                    apply(&mut cur, &rows);
                    cur.ram_load = ram_load();
                } else if let Some(au) = msg.audio {
                    if let Some(b) = au.b {
                        *a.lock().unwrap() =
                            Some(AudioSnapshot { bands: b, level: au.lv.unwrap_or(0.0), ts: Instant::now() });
                    }
                } else if let Some(me) = msg.media {
                    *md.lock().unwrap() = Some(MediaSnapshot::from(me));
                    *mt.lock().unwrap() = Instant::now();
                }
            }
        });
        Ok(Sensors { metrics, audio, media, media_at, child })
    }

    pub fn snapshot(&self) -> Metrics {
        *self.metrics.lock().unwrap()
    }

    pub fn audio_snapshot(&self) -> Option<AudioSnapshot> {
        self.audio.lock().unwrap().clone()
    }

    pub fn media_snapshot(&self) -> Option<MediaSnapshot> {
        let mut m = self.media.lock().unwrap().clone()?;
        // o helper só reemite a mídia a cada ~5 s: avança a posição pelo tempo decorrido (como o Python)
        if m.status == "Playing" && m.duration > 0.0 {
            let elapsed = self.media_at.lock().unwrap().elapsed().as_secs_f64();
            m.position = (m.position + elapsed).min(m.duration);
        }
        Some(m)
    }
}

impl Drop for Sensors {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

/// Primeiro sensor (hwType, type) cujo nome bate (na ordem da lista); `positive` exige valor > 0.
/// Equivale ao `_pick` do Python (percorre nomes por fora, linhas por dentro).
fn pick(rows: &[Row], hw: &str, kind: &str, names: &[&str], positive: bool) -> f32 {
    for name in names {
        for r in rows {
            if r.hw_type == hw && r.kind == kind && r.name == *name {
                let v = r.value as f32;
                if !positive || v > 0.0 {
                    return v;
                }
            }
        }
    }
    f32::NAN
}

fn present(rows: &[Row], hw: &str) -> bool {
    rows.iter().any(|r| r.hw_type == hw)
}

/// Escolhe UMA GPU como o `_gpu_type` do Python: NVIDIA preferida; iGPU AMD fraca
/// (VRAM total < 2 GB) é ignorada quando há NVIDIA. Evita misturar as duas placas.
fn select_gpu(rows: &[Row]) -> Option<&'static str> {
    let has_nv = present(rows, "GpuNvidia");
    for t in ["GpuNvidia", "GpuAmd", "GpuIntel"] {
        if !present(rows, t) {
            continue;
        }
        if t == "GpuAmd" && has_nv {
            let tot = pick(rows, "GpuAmd", "SmallData", &["GPU Memory Total"], false);
            if !tot.is_nan() && tot < 2048.0 {
                continue;
            }
        }
        return Some(match t {
            "GpuNvidia" => "GpuNvidia",
            "GpuAmd" => "GpuAmd",
            _ => "GpuIntel",
        });
    }
    None
}

fn pick_gpu(rows: &[Row], gpu: Option<&str>, kind: &str, names: &[&str], positive: bool) -> f32 {
    match gpu {
        Some(g) => pick(rows, g, kind, names, positive),
        None => f32::NAN,
    }
}

/// Mapeia as linhas do LibreHardwareMonitor para o Metrics, FIEL ao `_compute` do app Python
/// (mesmas listas de nomes, mesma ordem, mesma escolha de GPU). A RAM vem do chamador.
fn apply(m: &mut Metrics, rows: &[Row]) {
    // CPU
    m.cpu_temp = pick(rows, "Cpu", "Temperature",
        &["Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)", "Tdie", "Core Average", "CPU Cores", "Core Max"], true);
    m.cpu_load = pick(rows, "Cpu", "Load", &["CPU Total"], false);
    m.cpu_clock = pick(rows, "Cpu", "Clock", &["Cores (Average)", "Cores (Average Effective)"], true);
    if m.cpu_clock.is_nan() {
        let mut mx = f32::NAN; // fallback: maior "Core #N" (sem Effective)
        for r in rows {
            if r.hw_type == "Cpu" && r.kind == "Clock" && r.name.starts_with("Core #")
                && !r.name.contains("Effective") && r.value > 0.0
            {
                let v = r.value as f32;
                mx = if mx.is_nan() { v } else { mx.max(v) };
            }
        }
        m.cpu_clock = mx;
    }
    m.cpu_power = pick(rows, "Cpu", "Power", &["Package", "CPU Package"], true);

    // GPU (só do tipo escolhido, p/ não misturar iGPU AMD + NVIDIA)
    let g = select_gpu(rows);
    m.gpu_temp = pick_gpu(rows, g, "Temperature", &["GPU Core"], true);
    m.gpu_hotspot = pick_gpu(rows, g, "Temperature", &["GPU Hot Spot", "GPU Memory Junction", "GPU Memory"], true);
    m.gpu_load = pick_gpu(rows, g, "Load", &["GPU Core", "D3D 3D"], false);
    m.gpu_clock = pick_gpu(rows, g, "Clock", &["GPU Core"], true);
    m.gpu_power = pick_gpu(rows, g, "Power", &["GPU Package", "GPU Power", "GPU Core"], true);
    m.gpu_vram = pick_gpu(rows, g, "SmallData", &["GPU Memory Used", "D3D Dedicated Memory Used"], true);
    m.gpu_vram_total = pick_gpu(rows, g, "SmallData", &["GPU Memory Total", "D3D Dedicated Memory Total"], true);
    m.gpu_fan = pick_gpu(rows, g, "Fan", &["GPU Fan", "GPU Fan 1"], false);

    // Disco: maior temperatura entre Composite/Temperature plausíveis
    let mut disk = f32::NAN;
    for r in rows {
        if r.hw_type == "Storage" && r.kind == "Temperature"
            && (r.name == "Composite Temperature" || r.name == "Temperature")
        {
            let v = r.value as f32;
            if v > 0.0 && v < 120.0 {
                disk = if disk.is_nan() { v } else { disk.max(v) };
            }
        }
    }
    m.disk_temp = disk;

    // Ventoinhas da placa-mãe (SuperIO): bomba e fan, por dica de nome
    let fan = |hints: &[&str]| -> f32 {
        for h in hints {
            for r in rows {
                if r.kind == "Fan" && !r.hw_type.starts_with("Gpu") && r.value > 0.0
                    && r.name.to_lowercase().contains(h)
                {
                    return r.value as f32;
                }
            }
        }
        f32::NAN
    };
    m.pump_rpm = fan(&["pump", "aio", "bomba"]);
    m.fan_rpm = fan(&["cpu fan", "cpu_fan", "fan #1", "cpu"]);
}

/// Uso de RAM (0..100) via GlobalMemoryStatusEx do Windows — sem dependência externa.
pub fn ram_load() -> f32 {
    #[repr(C)]
    struct MemStatusEx {
        length: u32,
        memory_load: u32,
        total_phys: u64,
        avail_phys: u64,
        total_page: u64,
        avail_page: u64,
        total_virtual: u64,
        avail_virtual: u64,
        avail_ext: u64,
    }
    extern "system" {
        fn GlobalMemoryStatusEx(buffer: *mut MemStatusEx) -> i32;
    }
    unsafe {
        let mut s: MemStatusEx = std::mem::zeroed();
        s.length = std::mem::size_of::<MemStatusEx>() as u32;
        if GlobalMemoryStatusEx(&mut s) != 0 {
            s.memory_load as f32
        } else {
            f32::NAN
        }
    }
}

/// Octetos (recebidos, enviados) somados das interfaces de rede via GetIfTable (iphlpapi) — o
/// helper C# não emite rede, então leio direto do Windows, como o psutil no app Python.
/// Lê os campos por offset na MIB_IFROW (contadores de 32 bits): dwType@516, dwInOctets@552, dwOutOctets@576,
/// linha de 860 bytes, tabela começa em +4 (após dwNumEntries).
fn read_octets() -> Option<(u64, u64)> {
    #[link(name = "iphlpapi")]
    extern "system" {
        fn GetIfTable(table: *mut u8, size: *mut u32, order: i32) -> u32;
    }
    let mut size: u32 = 0;
    unsafe { GetIfTable(std::ptr::null_mut(), &mut size, 0) }; // 1ª chamada: descobre o tamanho
    if size == 0 {
        return None;
    }
    let mut buf = vec![0u8; size as usize];
    let rc = unsafe { GetIfTable(buf.as_mut_ptr(), &mut size, 0) };
    if rc != 0 {
        return None;
    }
    const ROW: usize = 860;
    const OFF_TYPE: usize = 516;
    const OFF_IN: usize = 552;
    const OFF_OUT: usize = 576;
    let rd = |o: usize| -> Option<u32> { Some(u32::from_ne_bytes(buf.get(o..o + 4)?.try_into().ok()?)) };
    let num = rd(0)? as usize;
    let (mut rx, mut tx) = (0u64, 0u64);
    for i in 0..num {
        let o = 4 + i * ROW;
        if o + ROW > buf.len() {
            break;
        }
        if rd(o + OFF_TYPE)? == 24 {
            continue; // IF_TYPE_SOFTWARE_LOOPBACK
        }
        rx += rd(o + OFF_IN)? as u64;
        tx += rd(o + OFF_OUT)? as u64;
    }
    Some((rx, tx))
}

/// Mede a taxa de rede (download, upload) em bytes/s pela diferença entre amostras.
pub struct NetCounter {
    last: Option<(std::time::Instant, u64, u64)>,
}
impl NetCounter {
    pub fn new() -> NetCounter {
        NetCounter { last: None }
    }
    /// (download, upload) em B/s desde a última amostra; NaN se a leitura falhar, 0 na 1ª vez.
    pub fn sample(&mut self) -> (f32, f32) {
        let now = std::time::Instant::now();
        let (rx, tx) = match read_octets() {
            Some(v) => v,
            None => return (f32::NAN, f32::NAN),
        };
        let out = match self.last {
            Some((t0, r0, s0)) => {
                let dt = now.duration_since(t0).as_secs_f64().max(0.001);
                let d = rx.wrapping_sub(r0); // estouro de 32 bits → delta enorme → zera
                let u = tx.wrapping_sub(s0);
                let big = u64::MAX / 2;
                let down = if d > big { 0.0 } else { (d as f64 / dt) as f32 };
                let up = if u > big { 0.0 } else { (u as f64 / dt) as f32 };
                (down, up)
            }
            None => (0.0, 0.0),
        };
        self.last = Some((now, rx, tx));
        out
    }
}
