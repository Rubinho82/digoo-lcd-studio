//! Digoo LCD — núcleo nativo (Rust). Fase 1: acha a tela, lê os sensores pelo helper C#,
//! desenha um painel e envia os quadros, mantendo a tela viva. Foco em pouca memória e velocidade.

mod panel;
mod protocol;
mod render;
mod sensors;

use std::time::{Duration, Instant};

/// Devolve ao Windows a memória ociosa (igual ao EmptyWorkingSet que o app Python chama).
fn trim_memory() {
    extern "system" {
        fn GetCurrentProcess() -> isize;
        fn EmptyWorkingSet(h: isize) -> i32;
    }
    unsafe {
        EmptyWorkingSet(GetCurrentProcess());
    }
}

fn find_helper() -> Option<String> {
    // 1) ao lado do exe:  <exe>/bin/sensorhelper/sensorhelper.exe
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join("bin/sensorhelper/sensorhelper.exe");
            if p.exists() {
                return Some(p.to_string_lossy().into_owned());
            }
        }
    }
    // 2) a instalação do app Python (reaproveita o mesmo helper)
    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        let p = std::path::Path::new(&local)
            .join(r"Programs\Digoo LCD Studio\_internal\bin\sensorhelper\sensorhelper.exe");
        if p.exists() {
            return Some(p.to_string_lossy().into_owned());
        }
    }
    None
}

fn main() {
    println!("Digoo LCD (núcleo Rust) — procurando a tela…");
    let port = match panel::find_panel() {
        Some(p) => p,
        None => {
            eprintln!("Tela não encontrada (USB 33c3:7791). Ela está conectada e livre?");
            std::process::exit(2);
        }
    };
    println!("tela na porta {port}");
    let mut pan = match panel::Panel::open(&port) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("não consegui abrir {port}: {e}");
            std::process::exit(2);
        }
    };
    let info = pan.connect(Duration::from_secs(3)).unwrap_or_default();
    println!(
        "modelo: {} | {}x{} | fw {} | limite {} KB / {} fps",
        if info.model.is_empty() { "?" } else { &info.model },
        info.width,
        info.height,
        info.version,
        info.max_frame_kb(),
        info.max_fps()
    );

    // rotação: a tela TXW818 deste PC precisa de 90° (igual ao app Python). Depois vira configurável.
    let rotation = 90u32;
    let max_kb = info.max_frame_kb();

    let sensors = match find_helper() {
        Some(h) => {
            println!("sensores: {h}");
            sensors::Sensors::start(&h, 500).ok()
        }
        None => {
            println!("sensorhelper.exe não achado — mostro só o que der (RAM).");
            None
        }
    };

    // tema: argumento (ex. `digoo-lcd neon`) ou "aneis"
    let theme = std::env::args().nth(1).unwrap_or_else(|| "aneis".into());
    let theme = if render::THEMES.contains(&theme.as_str()) { theme } else { "aneis".into() };
    println!("tema: {theme}");
    let fonts = render::Fonts::load();
    let pal = render::Palette::default();
    pan.keepalive(true);

    let mut frames = 0u64;
    let started = Instant::now();
    loop {
        let m = match &sensors {
            Some(s) => s.snapshot(),
            None => {
                let mut d = sensors::Metrics::default();
                d.ram_load = sensors::ram_load();
                d
            }
        };
        let pm = render::render_theme(&fonts, pal, &theme, &m);
        let mut quality = 92u8;
        let jpeg = loop {
            let j = render::to_jpeg(&pm, rotation, quality);
            if j.len() <= (max_kb as usize) * 1024 || quality <= 60 {
                break j;
            }
            quality -= 8;
        };
        if let Err(e) = pan.send_frame(&jpeg) {
            eprintln!("falha ao enviar quadro: {e} — tentando destravar");
            pan.recover();
        }
        pan.keepalive(false);

        frames += 1;
        if frames % 10 == 0 {
            let secs = started.elapsed().as_secs_f32();
            eprintln!("{frames} quadros, {:.1} q/s, último {} KB", frames as f32 / secs, jpeg.len() / 1024);
        }
        if frames % 30 == 0 {
            trim_memory(); // devolve memória ociosa de tempos em tempos
        }
        std::thread::sleep(Duration::from_millis(500)); // painel muda devagar: ~2 q/s já basta
    }
}
