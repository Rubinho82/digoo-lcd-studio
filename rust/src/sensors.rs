//! Lê os sensores do mesmo sensorhelper.exe (C# / LibreHardwareMonitor) que o app Python usa:
//! uma linha JSON por ciclo no stdout. A RAM vem do Windows (o helper não lê memória, igual no Python).

use serde::Deserialize;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

#[derive(Clone, Copy)]
pub struct Metrics {
    pub cpu_temp: f32,
    pub gpu_temp: f32,
    pub cpu_load: f32,
    pub gpu_load: f32,
    pub ram_load: f32,
}

impl Default for Metrics {
    fn default() -> Self {
        Metrics {
            cpu_temp: f32::NAN,
            gpu_temp: f32::NAN,
            cpu_load: f32::NAN,
            gpu_load: f32::NAN,
            ram_load: f32::NAN,
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

#[derive(Deserialize)]
struct Packet {
    sensors: Vec<Row>,
}

pub struct Sensors {
    pub metrics: Arc<Mutex<Metrics>>,
    child: Child,
}

impl Sensors {
    /// Sobe o helper C# e começa a ler os sensores numa thread.
    pub fn start(helper: &str, interval_ms: u32) -> std::io::Result<Sensors> {
        let mut child = Command::new(helper)
            .arg(interval_ms.to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let metrics = Arc::new(Mutex::new(Metrics::default()));
        let out = child.stdout.take().unwrap();
        let m = metrics.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(out);
            for line in reader.lines().map_while(Result::ok) {
                if !line.starts_with('{') || !line.contains("\"sensors\"") {
                    continue;
                }
                if let Ok(pkt) = serde_json::from_str::<Packet>(&line) {
                    let mut cur = m.lock().unwrap();
                    apply(&mut cur, &pkt.sensors);
                    cur.ram_load = ram_load();
                }
            }
        });
        Ok(Sensors { metrics, child })
    }

    pub fn snapshot(&self) -> Metrics {
        *self.metrics.lock().unwrap()
    }
}

impl Drop for Sensors {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

/// Extrai CPU/GPU temperatura e uso das linhas do LibreHardwareMonitor (heurística — fase 1).
fn apply(m: &mut Metrics, rows: &[Row]) {
    let is_gpu = |t: &str| t.contains("Gpu");
    let mut cpu_core_max = f32::NAN;
    for r in rows {
        let v = r.value as f32;
        let t = r.hw_type.as_str();
        match (t, r.kind.as_str()) {
            (ct, "Temperature") if ct.contains("Cpu") => {
                if r.name.contains("Package") || r.name.contains("Tctl") || r.name.contains("Tdie") {
                    m.cpu_temp = v;
                } else if r.name.contains("Core") {
                    cpu_core_max = if cpu_core_max.is_nan() { v } else { cpu_core_max.max(v) };
                }
            }
            (gt, "Temperature") if is_gpu(gt) && r.name.contains("GPU Core") => m.gpu_temp = v,
            (ct, "Load") if ct.contains("Cpu") && r.name.contains("CPU Total") => m.cpu_load = v,
            (gt, "Load") if is_gpu(gt) && r.name.contains("GPU Core") => m.gpu_load = v,
            _ => {}
        }
    }
    if m.cpu_temp.is_nan() && !cpu_core_max.is_nan() {
        m.cpu_temp = cpu_core_max;
    }
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
