//! Digoo LCD — núcleo nativo (Rust). Fase 1: acha a tela, lê os sensores pelo helper C#,
//! desenha um painel e envia os quadros, mantendo a tela viva. Foco em pouca memória e velocidade.

mod config;
mod cover;
mod lyrics;
mod media;
mod panel;
mod protocol;
mod render;
mod sensors;
mod translate;

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

    // modos de mídia (não usam sensores): video <.djf> | img <arquivo> [cover|contain] | espelho [cover|contain]
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        Some("video") => {
            run_video(&mut pan, args.get(2).map(|s| s.as_str()).unwrap_or(""), info.max_fps());
            return;
        }
        Some("img") | Some("imagem") => {
            let fit = args.get(3).map(|s| s.as_str()).unwrap_or("cover");
            run_image(&mut pan, args.get(2).map(|s| s.as_str()).unwrap_or(""), fit, rotation, max_kb);
            return;
        }
        Some("espelho") | Some("mirror") => {
            let fit = args.get(2).map(|s| s.as_str()).unwrap_or("cover");
            run_mirror(&mut pan, fit, rotation, max_kb, info.max_fps());
            return;
        }
        // sem argumento (ou `config`/`auto`): dirigido pelo config.json que a GUI escreve, ao vivo
        None | Some("config") | Some("auto") => {
            run_config(&mut pan, &info);
            return;
        }
        _ => {}
    }

    // tema: argumento (ex. `digoo-lcd neon`, ou `digoo-lcd musica-vinil` p/ o tema Música)
    let arg = std::env::args().nth(1).unwrap_or_else(|| "aneis".into());
    let is_music = arg == "musica" || arg.starts_with("musica-");
    let music_layout = {
        let l = arg.strip_prefix("musica-").unwrap_or("espectro");
        if render::MUSIC_LAYOUTS.contains(&l) { l.to_string() } else { "espectro".to_string() }
    };
    let theme = if render::THEMES.contains(&arg.as_str()) { arg.clone() } else { "aneis".into() };
    println!("tema: {}", if is_music { format!("musica/{music_layout}") } else { theme.clone() });

    let sensors = match find_helper() {
        Some(h) => {
            println!("sensores: {h}{}", if is_music { " (+áudio)" } else { "" });
            sensors::Sensors::start(&h, 500, is_music).ok()
        }
        None => {
            println!("sensorhelper.exe não achado — mostro só o que der (RAM).");
            None
        }
    };
    let fonts = render::Fonts::load();
    let pal = render::Palette::default();
    pan.keepalive(true);

    let mut hist = render::History::default();
    let mut net = sensors::NetCounter::new();
    let mut viz = render::AudioViz::new();
    let lyrics_cache = lyrics::LyricsCache::new();
    let covers = cover::Covers::new();
    let translator = translate::Translator::new(translate::Config::from_env());
    if is_music && translator.enabled() {
        println!("tradução de letras: ligada");
    }
    let period = Duration::from_millis(if is_music { 40 } else { 500 }); // música anima ~25 q/s
    let mut frames = 0u64;
    let mut last = Instant::now();
    let started = Instant::now();
    loop {
        let mut m = match &sensors {
            Some(s) => s.snapshot(),
            None => {
                let mut d = sensors::Metrics::default();
                d.ram_load = sensors::ram_load();
                d
            }
        };
        // rede não vem do helper C# (igual ao psutil no Python): mede aqui a cada quadro.
        let (down, up) = net.sample();
        m.net_down = down;
        m.net_up = up;
        hist.push(&m);
        let pm = if is_music {
            let dt = last.elapsed().as_secs_f32();
            last = Instant::now();
            let audio = sensors.as_ref().and_then(|s| s.audio_snapshot());
            viz.update(audio.as_ref(), dt);
            let media = sensors.as_ref().and_then(|s| s.media_snapshot());
            let lyric = media.as_ref().filter(|m| !m.title.is_empty()).map(|m| lyrics_cache.get(m));
            let tr = translator.for_lyrics(lyric.as_ref(), media.as_ref());
            render::render_music(&fonts, pal, &music_layout, &viz, media.as_ref(), "circulo",
                started.elapsed().as_secs_f32(), lyric.as_ref(), Some(&covers),
                tr.as_ref().map(|t| t.lines.as_slice()), translator.only())
        } else {
            render::render_theme(&fonts, pal, &theme, &m, &hist)
        };
        let jpeg = encode_fit(&pm, rotation, max_kb);
        pump(&mut pan, &jpeg, &mut frames, started);
        std::thread::sleep(period); // 500 ms nos painéis; ~40 ms (25 q/s) na música
    }
}

/// Comprime o Pixmap em JPEG girando `rotation`, baixando a qualidade até caber no limite da tela.
fn encode_fit(pm: &tiny_skia::Pixmap, rotation: u32, max_kb: u32) -> Vec<u8> {
    let mut quality = 92u8;
    loop {
        let j = render::to_jpeg(pm, rotation, quality);
        if j.len() <= (max_kb as usize) * 1024 || quality <= 60 {
            break j;
        }
        quality -= 8;
    }
}

/// Envia um quadro (destrava se falhar), faz o keepalive e devolve memória de tempos em tempos.
fn pump(pan: &mut panel::Panel, jpeg: &[u8], frames: &mut u64, started: Instant) {
    if let Err(e) = pan.send_frame(jpeg) {
        eprintln!("falha ao enviar quadro: {e} — tentando destravar");
        pan.recover();
    }
    pan.keepalive(false);
    *frames += 1;
    if *frames % 10 == 0 {
        let secs = started.elapsed().as_secs_f32().max(0.001);
        eprintln!("{} quadros, {:.1} q/s, último {} KB", frames, *frames as f32 / secs, jpeg.len() / 1024);
    }
    if *frames % 30 == 0 {
        trim_memory();
    }
}

/// Toca um .djf (vídeo/GIF já convertido): os quadros vêm girados e 4:2:0, manda os bytes como estão.
fn run_video(pan: &mut panel::Panel, path: &str, max_fps: u32) {
    let mut ff = match media::FrameFile::open(path) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("não abri o vídeo {path}: {e}");
            std::process::exit(2);
        }
    };
    println!("vídeo: {} quadros, {} fps, rot {}°, {:.1}s (loop)", ff.len(), ff.fps, ff.rot, ff.duration);
    let fps = ff.fps.max(1).min(max_fps.max(1) as u16) as u64;
    let period = Duration::from_millis((1000 / fps).max(1));
    pan.keepalive(true);
    let (mut frames, started) = (0u64, Instant::now());
    let mut idx = usize::MAX;
    loop {
        let t = started.elapsed().as_secs_f32() % ff.duration.max(0.001);
        let i = ff.index_at(t);
        if i != idx {
            idx = i;
            match ff.read(i) {
                Ok(jpeg) => pump(pan, &jpeg, &mut frames, started),
                Err(e) => eprintln!("falha ao ler quadro {i}: {e}"),
            }
        } else {
            pan.keepalive(false); // mesmo quadro: só mantém a tela viva
        }
        std::thread::sleep(period);
    }
}

/// Mostra uma imagem estática (JPEG/PNG) ajustada a 480×480, reenviando de tempos em tempos.
fn run_image(pan: &mut panel::Panel, path: &str, fit: &str, rotation: u32, max_kb: u32) {
    let pm = match media::image_pixmap(path, fit) {
        Some(p) => p,
        None => {
            eprintln!("não abri a imagem {path} (uso JPEG/PNG)");
            std::process::exit(2);
        }
    };
    println!("imagem: {path} ({fit})");
    let jpeg = encode_fit(&pm, rotation, max_kb);
    pan.keepalive(true);
    let (mut frames, started) = (0u64, Instant::now());
    loop {
        pump(pan, &jpeg, &mut frames, started); // reenvia (barato) p/ sobreviver a glitches da tela
        std::thread::sleep(Duration::from_millis(1000));
    }
}

/// Espelha a tela principal (captura GDI → 480×480) a ~12 q/s.
fn run_mirror(pan: &mut panel::Panel, fit: &str, rotation: u32, max_kb: u32, max_fps: u32) {
    println!("espelho da tela ({fit})");
    let fps = max_fps.clamp(1, 12) as u64;
    let period = Duration::from_millis((1000 / fps).max(1));
    pan.keepalive(true);
    let (mut frames, started) = (0u64, Instant::now());
    loop {
        match media::mirror_pixmap(fit) {
            Some(pm) => {
                let jpeg = encode_fit(&pm, rotation, max_kb);
                pump(pan, &jpeg, &mut frames, started);
            }
            None => pan.keepalive(false),
        }
        std::thread::sleep(period);
    }
}

fn is_image(path: &str) -> bool {
    let p = path.to_lowercase();
    [".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"].iter().any(|e| p.ends_with(e))
}

fn is_video(path: &str) -> bool {
    let p = path.to_lowercase();
    [".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".ogv"].iter().any(|e| p.ends_with(e))
}

/// Em modo mídia: se a 1ª fonte for um vídeo já convertido (.djf no cache do app Python), devolve o quadro
/// do momento (JPEG pronto, já girado). Mantém o FrameFile aberto entre voltas; reabre se a fonte mudar.
fn play_config_video(state: &mut Option<(String, media::FrameFile, Instant)>, cfg: &config::Config) -> Option<Vec<u8>> {
    let src = cfg.media_paths.iter().find(|p| is_video(p))?;
    if state.as_ref().map_or(true, |(s, _, _)| s != src) {
        let djf = media::djf_cache_path(src, &cfg.media_fit, cfg.rotation.rem_euclid(360) as u32)?;
        if !djf.exists() {
            *state = None;
            return None; // a GUI ainda não converteu este vídeo
        }
        match media::FrameFile::open(djf.to_str()?) {
            Ok(ff) => *state = Some((src.clone(), ff, Instant::now())),
            Err(_) => {
                *state = None;
                return None;
            }
        }
    }
    let (_, ff, start) = state.as_mut()?;
    let t = start.elapsed().as_secs_f32() % ff.duration.max(0.001);
    let i = ff.index_at(t);
    ff.read(i).ok()
}

/// Lê os sensores + a rede (igual ao laço de temas) num Metrics.
fn read_metrics(sensors: &Option<sensors::Sensors>, net: &mut sensors::NetCounter) -> sensors::Metrics {
    let mut m = match sensors {
        Some(s) => s.snapshot(),
        None => {
            let mut d = sensors::Metrics::default();
            d.ram_load = sensors::ram_load();
            d
        }
    };
    let (down, up) = net.sample();
    m.net_down = down;
    m.net_up = up;
    m
}

fn describe(cfg: &config::Config) -> String {
    match cfg.mode.as_str() {
        "espelho" => format!("espelho ({})", cfg.mirror_fit),
        "midia" => format!("mídia ({}, {} itens)", cfg.media_fit, cfg.media_paths.len()),
        _ if cfg.theme == "musica" => format!("música/{} (onda {})", cfg.music_layout, cfg.music_wave),
        _ => format!("tema {}", cfg.theme),
    }
}

/// Modo dirigido pelo config.json (o que a GUI escreve): relê o arquivo quando muda e segue tema/música/
/// mídia/espelho, cores, giro, brilho (agenda noturna) e tradução — tudo ao vivo.
fn run_config(pan: &mut panel::Panel, info: &protocol::PanelInfo) {
    let fonts = render::Fonts::load();
    let covers = cover::Covers::new();
    let lyrics_cache = lyrics::LyricsCache::new();
    let sensors = match find_helper() {
        Some(h) => {
            println!("sensores: {h} (+áudio)");
            sensors::Sensors::start(&h, 500, true).ok()
        }
        None => {
            println!("sensorhelper.exe não achado — sensores básicos (RAM).");
            None
        }
    };
    let mut cfg = config::load();
    let mut cfg_mtime = config::mtime();
    let mut pal = config::palette(&cfg);
    let mut translator = translate::Translator::new(translate::Config::from_values(
        cfg.lyrics_translate, &cfg.lyrics_provider, &cfg.lyrics_lang, &cfg.deepl_key, &cfg.lyrics_show,
    ));
    let mut hist = render::History::default();
    let mut net = sensors::NetCounter::new();
    let mut viz = render::AudioViz::new();
    let mut image_cache: Option<(String, String, tiny_skia::Pixmap)> = None;
    let mut video: Option<(String, media::FrameFile, Instant)> = None;
    let mut bright: i32 = -1;
    println!("modo config.json ({}): {}", config::config_path().display(), describe(&cfg));
    pan.keepalive(true);
    let (mut frames, started) = (0u64, Instant::now());
    let mut last = Instant::now();
    loop {
        let dt = last.elapsed().as_secs_f32();
        last = Instant::now();
        // relê o config.json quando a GUI o altera
        let mt = config::mtime();
        if mt != cfg_mtime {
            cfg_mtime = mt;
            cfg = config::load();
            pal = config::palette(&cfg);
            translator.set_config(translate::Config::from_values(
                cfg.lyrics_translate, &cfg.lyrics_provider, &cfg.lyrics_lang, &cfg.deepl_key, &cfg.lyrics_show,
            ));
            image_cache = None;
            bright = -1; // reforça o brilho na próxima volta
            println!("config recarregado: {}", describe(&cfg));
        }
        let target = config::target_brightness(&cfg) as i32;
        if target != bright {
            bright = target;
            pan.set_brightness(target as u8);
        }
        let rotation = match cfg.rotation {
            0 | 90 | 180 | 270 => cfg.rotation as u32,
            _ => 90,
        };
        let max_kb = if cfg.max_kb > 0 { cfg.max_kb as u32 } else { info.max_frame_kb() };
        let period = Duration::from_millis((1000 / cfg.fps.clamp(1, 30) as u64).max(1));

        // vídeo do config já convertido (.djf no cache do app Python): tem prioridade, vai com os bytes prontos
        if cfg.mode == "midia" {
            if let Some(jpeg) = play_config_video(&mut video, &cfg) {
                pump(pan, &jpeg, &mut frames, started);
                std::thread::sleep(period);
                continue;
            }
        } else {
            video = None;
        }

        // em modo mídia: 1ª imagem tocável. Vídeo ainda não convertido pela GUI → cai no tema/música do config
        // (o que a sobreposição pretende mostrar); vídeo pronto já foi tratado acima.
        let media_img = if cfg.mode == "midia" {
            cfg.media_paths.iter().find(|p| is_image(p) && std::path::Path::new(p.as_str()).exists()).cloned()
        } else {
            None
        };
        let frame: Option<tiny_skia::Pixmap> = if cfg.mode == "espelho" {
            media::mirror_pixmap(&cfg.mirror_fit)
        } else if let Some(p) = &media_img {
            if image_cache.as_ref().map_or(true, |(ip, ifit, _)| ip != p || ifit != &cfg.media_fit) {
                image_cache = media::image_pixmap(p, &cfg.media_fit).map(|pm| (p.clone(), cfg.media_fit.clone(), pm));
            }
            image_cache.as_ref().map(|(_, _, pm)| pm.clone()).or_else(|| {
                let m = read_metrics(&sensors, &mut net);
                Some(render::render_theme(&fonts, pal, "relogio", &m, &hist))
            })
        } else if cfg.theme == "musica" {
            let audio = sensors.as_ref().and_then(|s| s.audio_snapshot());
            viz.update(audio.as_ref(), dt);
            let media = sensors.as_ref().and_then(|s| s.media_snapshot());
            let lyric = if cfg.music_lyrics {
                media.as_ref().filter(|m| !m.title.is_empty()).map(|m| lyrics_cache.get(m))
            } else {
                None
            };
            let tr = translator.for_lyrics(lyric.as_ref(), media.as_ref());
            Some(render::render_music(&fonts, pal, &cfg.music_layout, &viz, media.as_ref(), &cfg.music_wave,
                started.elapsed().as_secs_f32(), lyric.as_ref(), Some(&covers),
                tr.as_ref().map(|t| t.lines.as_slice()), translator.only()))
        } else {
            let m = read_metrics(&sensors, &mut net);
            hist.push(&m);
            let theme = if render::THEMES.contains(&cfg.theme.as_str()) { cfg.theme.as_str() } else { "aneis" };
            Some(render::render_theme(&fonts, pal, theme, &m, &hist))
        };

        match frame {
            Some(pm) => {
                let jpeg = encode_fit(&pm, rotation, max_kb);
                pump(pan, &jpeg, &mut frames, started);
            }
            None => pan.keepalive(false),
        }
        std::thread::sleep(period);
    }
}
