//! Desenho do painel em CPU (tiny-skia) + texto (fontdue) + JPEG 4:2:0 (exigência da tela).
//! Framework de Canvas + temas portados do app Python. 480x480.

use crate::sensors::Metrics;
use fontdue::Font;
use jpeg_encoder::{ColorType, Encoder, SamplingFactor};
use tiny_skia::{Color, FillRule, LineCap, Paint, PathBuilder, Pixmap, Rect, Stroke, Transform};

pub const W: f32 = 480.0;
pub const H: f32 = 480.0;

/// Cores do tema (padrão escuro: ciano/rosa como no Python).
#[derive(Clone, Copy)]
pub struct Palette {
    pub acc: Color,
    pub acc2: Color,
    pub txt: Color,
    pub muted: Color,
    pub bg: Color,
    pub bg2: Color,
}
impl Default for Palette {
    fn default() -> Self {
        Palette {
            acc: rgb(0, 229, 255),
            acc2: rgb(255, 46, 136),
            txt: rgb(230, 237, 243),
            muted: rgb(120, 130, 140),
            bg: rgb(13, 17, 23),
            bg2: rgb(20, 26, 34),
        }
    }
}

pub fn rgb(r: u8, g: u8, b: u8) -> Color {
    Color::from_rgba8(r, g, b, 255)
}
fn rgba(c: Color, a: u8) -> Color {
    Color::from_rgba8((c.red() * 255.0) as u8, (c.green() * 255.0) as u8, (c.blue() * 255.0) as u8, a)
}
fn mix(a: Color, b: Color, t: f32) -> Color {
    let t = t.clamp(0.0, 1.0);
    rgb(
        ((a.red() + (b.red() - a.red()) * t) * 255.0) as u8,
        ((a.green() + (b.green() - a.green()) * t) * 255.0) as u8,
        ((a.blue() + (b.blue() - a.blue()) * t) * 255.0) as u8,
    )
}

#[derive(Clone, Copy, PartialEq)]
pub enum Anchor {
    Left,
    Center,
    Right,
}

/// Hora local (sem dependência externa) via GetLocalTime do Windows.
pub struct Clock {
    pub h: u16,
    pub m: u16,
    pub s: u16,
    pub ms: u16,
    pub wday: u16,
    pub day: u16,
    pub mon: u16,
}
pub fn now() -> Clock {
    #[repr(C)]
    #[derive(Default)]
    struct SystemTime {
        year: u16,
        month: u16,
        dow: u16,
        day: u16,
        hour: u16,
        min: u16,
        sec: u16,
        ms: u16,
    }
    extern "system" {
        fn GetLocalTime(t: *mut SystemTime);
    }
    let mut t = SystemTime::default();
    unsafe { GetLocalTime(&mut t) };
    Clock { h: t.hour, m: t.min, s: t.sec, ms: t.ms, wday: t.dow, day: t.day, mon: t.month }
}
const WEEK3: [&str; 7] = ["DOM", "SEG", "TER", "QUA", "QUI", "SEX", "SÁB"];
const MONTHS: [&str; 12] = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"];
/// Dias da semana por extenso, índice SEGUNDA..DOMINGO (como o `weekday()` do Python).
const WEEKDAYS_FULL: [&str; 7] = ["SEGUNDA", "TERÇA", "QUARTA", "QUINTA", "SEXTA", "SÁBADO", "DOMINGO"];

pub struct Fonts {
    reg: Font,
    bold: Font,
}
impl Fonts {
    pub fn load() -> Fonts {
        let f = |p: &str| {
            std::fs::read(p)
                .ok()
                .and_then(|b| Font::from_bytes(b, fontdue::FontSettings::default()).ok())
        };
        let reg = f(r"C:\Windows\Fonts\segoeui.ttf").expect("segoeui.ttf");
        let bold = f(r"C:\Windows\Fonts\segoeuib.ttf").unwrap_or_else(|| reg.clone());
        Fonts { reg, bold }
    }
}

/// Tela de desenho: um Pixmap + paleta + fontes.
pub struct Canvas<'a> {
    pub pm: Pixmap,
    pub pal: Palette,
    f: &'a Fonts,
}

impl<'a> Canvas<'a> {
    pub fn new(f: &'a Fonts, pal: Palette) -> Canvas<'a> {
        let mut pm = Pixmap::new(W as u32, H as u32).unwrap();
        pm.fill(pal.bg);
        Canvas { pm, pal, f }
    }

    pub fn fill(&mut self, c: Color) {
        self.pm.fill(c);
    }

    /// Fundo em degradê vertical suave (aproxima o radial do Python).
    pub fn gradient_bg(&mut self) {
        let (a, b) = (self.pal.bg2, self.pal.bg);
        let h = H as usize;
        let data = self.pm.data_mut();
        for y in 0..h {
            let t = (y as f32 / h as f32 * 1.3).min(1.0);
            let c = mix(a, b, t);
            let (r, g, bl) = ((c.red() * 255.0) as u8, (c.green() * 255.0) as u8, (c.blue() * 255.0) as u8);
            for x in 0..W as usize {
                let i = (y * W as usize + x) * 4;
                data[i] = r;
                data[i + 1] = g;
                data[i + 2] = bl;
                data[i + 3] = 255;
            }
        }
    }

    pub fn fill_path(&mut self, pb: PathBuilder, col: Color) {
        if let Some(p) = pb.finish() {
            let mut paint = Paint::default();
            paint.set_color(col);
            paint.anti_alias = true;
            self.pm.fill_path(&p, &paint, FillRule::Winding, Transform::identity(), None);
        }
    }

    pub fn rrect(&mut self, x: f32, y: f32, w: f32, h: f32, col: Color) {
        if let Some(r) = Rect::from_xywh(x, y, w.max(1.0), h.max(1.0)) {
            let mut pb = PathBuilder::new();
            pb.push_rect(r);
            self.fill_path(pb, col);
        }
    }

    pub fn stroke(&mut self, pb: PathBuilder, col: Color, width: f32, round: bool) {
        if let Some(p) = pb.finish() {
            let mut paint = Paint::default();
            paint.set_color(col);
            paint.anti_alias = true;
            let stroke = Stroke {
                width,
                line_cap: if round { LineCap::Round } else { LineCap::Butt },
                ..Default::default()
            };
            self.pm.stroke_path(&p, &paint, &stroke, Transform::identity(), None);
        }
    }

    pub fn line(&mut self, x0: f32, y0: f32, x1: f32, y1: f32, col: Color, width: f32) {
        let mut pb = PathBuilder::new();
        pb.move_to(x0, y0);
        pb.line_to(x1, y1);
        self.stroke(pb, col, width, false);
    }

    /// Arco (graus, sentido horário a partir do leste) com pontas arredondadas.
    pub fn arc(&mut self, cx: f32, cy: f32, r: f32, width: f32, a0: f32, a1: f32, col: Color) {
        if a1 <= a0 {
            return;
        }
        let mut pb = PathBuilder::new();
        let steps = ((a1 - a0).abs() / 3.0).ceil().max(2.0) as usize;
        for i in 0..=steps {
            let a = (a0 + (a1 - a0) * i as f32 / steps as f32).to_radians();
            let (x, y) = (cx + r * a.cos(), cy + r * a.sin());
            if i == 0 {
                pb.move_to(x, y);
            } else {
                pb.line_to(x, y);
            }
        }
        self.stroke(pb, col, width, true);
    }

    pub fn bar(&mut self, x: f32, y: f32, w: f32, h: f32, frac: f32, col: Color) {
        self.rrect(x, y, w, h, rgba(self.pal.muted, 45));
        let fw = (w * frac.clamp(0.0, 1.0)).max(h);
        self.rrect(x, y, fw, h, col);
    }

    /// Retângulo arredondado com preenchimento e/ou contorno (cantos por curva quadrática).
    pub fn round_rect(&mut self, x: f32, y: f32, w: f32, h: f32, radius: f32, fill: Option<Color>, outline: Option<(Color, f32)>) {
        let r = radius.min(w / 2.0).min(h / 2.0).max(0.0);
        let mut pb = PathBuilder::new();
        pb.move_to(x + r, y);
        pb.line_to(x + w - r, y);
        pb.quad_to(x + w, y, x + w, y + r);
        pb.line_to(x + w, y + h - r);
        pb.quad_to(x + w, y + h, x + w - r, y + h);
        pb.line_to(x + r, y + h);
        pb.quad_to(x, y + h, x, y + h - r);
        pb.line_to(x, y + r);
        pb.quad_to(x, y, x + r, y);
        pb.close();
        let path = match pb.finish() {
            Some(p) => p,
            None => return,
        };
        if let Some(col) = fill {
            let mut paint = Paint::default();
            paint.set_color(col);
            paint.anti_alias = true;
            self.pm.fill_path(&path, &paint, FillRule::Winding, Transform::identity(), None);
        }
        if let Some((col, width)) = outline {
            let mut paint = Paint::default();
            paint.set_color(col);
            paint.anti_alias = true;
            let stroke = Stroke { width, ..Default::default() };
            self.pm.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
        }
    }

    /// Triângulo preenchido (ponteiro do velocímetro).
    pub fn fill_tri(&mut self, p0: (f32, f32), p1: (f32, f32), p2: (f32, f32), col: Color) {
        let mut pb = PathBuilder::new();
        pb.move_to(p0.0, p0.1);
        pb.line_to(p1.0, p1.1);
        pb.line_to(p2.0, p2.1);
        pb.close();
        self.fill_path(pb, col);
    }

    /// Disco (círculo preenchido) — eixo central do ponteiro.
    pub fn disc(&mut self, cx: f32, cy: f32, r: f32, col: Color) {
        let mut pb = PathBuilder::new();
        pb.push_circle(cx, cy, r);
        self.fill_path(pb, col);
    }

    /// Largura em px de um texto (para caixas que abraçam o texto, como o relógio do tema compacto).
    pub fn text_width(&self, s: &str, size: f32, bold: bool) -> f32 {
        let font = if bold { &self.f.bold } else { &self.f.reg };
        s.chars().map(|ch| font.metrics(ch, size).advance_width).sum()
    }

    /// Mini-gráfico (área preenchida + linha) de uma série dentro da caixa (x0,y0)-(x1,y1).
    pub fn sparkline(&mut self, x0: f32, y0: f32, x1: f32, y1: f32, values: &[f32], col: Color, vmin: f32, vmax: f32, fill_alpha: u8, width: f32) {
        let n = values.len();
        if n < 2 {
            return;
        }
        let span = if (vmax - vmin).abs() < 1e-6 { 1.0 } else { vmax - vmin };
        let mut pts: Vec<(f32, f32)> = Vec::with_capacity(n);
        for (i, &v) in values.iter().enumerate() {
            if v.is_nan() {
                continue;
            }
            let x = x0 + (x1 - x0) * i as f32 / (n - 1).max(1) as f32;
            let t = ((v - vmin) / span).clamp(0.0, 1.0);
            pts.push((x, y1 - (y1 - y0) * t));
        }
        if pts.len() < 2 {
            return;
        }
        if fill_alpha > 0 {
            let mut pb = PathBuilder::new();
            pb.move_to(pts[0].0, pts[0].1);
            for p in &pts[1..] {
                pb.line_to(p.0, p.1);
            }
            pb.line_to(pts[pts.len() - 1].0, y1);
            pb.line_to(pts[0].0, y1);
            pb.close();
            self.fill_path(pb, rgba(col, fill_alpha));
        }
        let mut pb = PathBuilder::new();
        pb.move_to(pts[0].0, pts[0].1);
        for p in &pts[1..] {
            pb.line_to(p.0, p.1);
        }
        self.stroke(pb, col, width, true);
    }

    pub fn text(&mut self, s: &str, x: f32, y: f32, size: f32, col: Color, bold: bool, anchor: Anchor) {
        let font = if bold { &self.f.bold } else { &self.f.reg };
        let total: f32 = s.chars().map(|c| font.metrics(c, size).advance_width).sum();
        let mut pen = match anchor {
            Anchor::Left => x,
            Anchor::Center => x - total / 2.0,
            Anchor::Right => x - total,
        };
        let (pw, ph) = (W as i32, H as i32);
        let (cr, cg, cb) = (col.red(), col.green(), col.blue());
        let data = self.pm.data_mut();
        for ch in s.chars() {
            let (mt, bmp) = font.rasterize(ch, size);
            let gx = pen.round() as i32 + mt.xmin;
            let gy = y.round() as i32 - (mt.height as i32 + mt.ymin);
            for ry in 0..mt.height as i32 {
                for rx in 0..mt.width as i32 {
                    let a = bmp[(ry * mt.width as i32 + rx) as usize] as f32 / 255.0;
                    if a <= 0.0 {
                        continue;
                    }
                    let (px, py) = (gx + rx, gy + ry);
                    if px < 0 || py < 0 || px >= pw || py >= ph {
                        continue;
                    }
                    let idx = ((py * pw + px) * 4) as usize;
                    for (k, cc) in [cr, cg, cb].iter().enumerate() {
                        let bgv = data[idx + k] as f32;
                        data[idx + k] = (bgv + (cc * 255.0 - bgv) * a) as u8;
                    }
                    data[idx + 3] = 255;
                }
            }
            pen += mt.advance_width;
        }
    }

    /// Número grande + unidade pequena, alinhados pela base.
    pub fn value_unit(&mut self, x: f32, y: f32, val: &str, unit: &str, size: f32, col: Color, anchor: Anchor) {
        let fv = &self.f.bold;
        let wv: f32 = val.chars().map(|c| fv.metrics(c, size).advance_width).sum();
        let usz = (size / 3.0).max(12.0);
        let wu: f32 = unit.chars().map(|c| fv.metrics(c, usz).advance_width).sum();
        let total = wv + 4.0 + wu;
        let x0 = match anchor {
            Anchor::Left => x,
            Anchor::Center => x - total / 2.0,
            Anchor::Right => x - total,
        };
        self.text(val, x0, y, size, col, true, Anchor::Left);
        self.text(unit, x0 + wv + 4.0, y, usz, self.pal.muted, true, Anchor::Left);
    }

    /// Texto com um "brilho" barato (uma cópia dim atrás + o texto por cima).
    pub fn glow_text(&mut self, s: &str, x: f32, y: f32, size: f32, col: Color, anchor: Anchor) {
        let halo = rgba(col, 70);
        for (dx, dy) in [(-2.0, 0.0), (2.0, 0.0), (0.0, -2.0), (0.0, 2.0)] {
            self.text(s, x + dx, y + dy, size, halo, true, anchor);
        }
        self.text(s, x, y, size, col, true, anchor);
    }

    pub fn header(&mut self, title: &str) {
        let c = now();
        self.text(title, 28.0, 36.0, 24.0, self.pal.acc, true, Anchor::Left);
        self.text(&format!("{:02}:{:02}", c.h, c.m), W - 28.0, 36.0, 24.0, self.pal.txt, false, Anchor::Right);
    }

    pub fn into_jpeg(self, rotation: u32, quality: u8) -> Vec<u8> {
        to_jpeg(&self.pm, rotation, quality)
    }
}

// ---------------------------------------------------------------- temas
fn temp_str(v: f32) -> String {
    if v.is_nan() { "--".into() } else { format!("{:.0}", v) }
}
fn pct(v: f32) -> String {
    if v.is_nan() { "--".into() } else { format!("{:.0}%", v) }
}
fn temp_frac(t: f32) -> f32 {
    ((t - 20.0) / 75.0).clamp(0.0, 1.0)
}
/// Cor da temperatura: acento até ~50 °C, amarelo em 70, vermelho em 88.
fn temp_color(v: f32, pal: &Palette) -> Color {
    if v.is_nan() {
        return pal.muted;
    }
    if v <= 50.0 {
        pal.acc
    } else if v <= 70.0 {
        mix(pal.acc, rgb(255, 196, 0), (v - 50.0) / 20.0)
    } else {
        mix(rgb(255, 196, 0), rgb(255, 59, 59), (v - 70.0) / 18.0)
    }
}

/// Número puro (sem unidade): "61" ou "--".
fn num(v: f32) -> String {
    if v.is_nan() {
        "--".into()
    } else {
        format!("{:.0}", v)
    }
}

/// Taxa de rede legível ("12.3 MB/s"); "--" quando não há leitura.
fn fmt_rate(bps: f32) -> String {
    if bps.is_nan() {
        return "--".into();
    }
    for (unit, div) in [("GB/s", 1073741824.0f32), ("MB/s", 1048576.0), ("KB/s", 1024.0)] {
        if bps >= div {
            return format!("{:.1} {}", bps / div, unit);
        }
    }
    format!("{:.0} B/s", bps)
}

/// Fatia com os últimos `n` elementos de um histórico.
fn tail(v: &[f32], n: usize) -> &[f32] {
    &v[v.len().saturating_sub(n)..]
}

/// Histórico por métrica para os gráficos (sparklines). O laço principal empurra um ponto por quadro.
/// A ampliação dos sensores (clocks, potência, VRAM, ventoinhas, rede, disco) e mais séries vêm no lote 2.
pub struct History {
    pub cpu_load: Vec<f32>,
    pub gpu_load: Vec<f32>,
    pub cpu_temp: Vec<f32>,
    pub gpu_temp: Vec<f32>,
}
impl Default for History {
    fn default() -> Self {
        History { cpu_load: Vec::new(), gpu_load: Vec::new(), cpu_temp: Vec::new(), gpu_temp: Vec::new() }
    }
}
impl History {
    const CAP: usize = 240;
    pub fn push(&mut self, m: &Metrics) {
        push_cap(&mut self.cpu_load, m.cpu_load);
        push_cap(&mut self.gpu_load, m.gpu_load);
        push_cap(&mut self.cpu_temp, m.cpu_temp);
        push_cap(&mut self.gpu_temp, m.gpu_temp);
    }
}
fn push_cap(buf: &mut Vec<f32>, v: f32) {
    buf.push(v);
    if buf.len() > History::CAP {
        let drop = buf.len() - History::CAP;
        buf.drain(0..drop);
    }
}

fn theme_aneis(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    c.header("DIGOO");
    let pal = c.pal;
    let rings = [(124.0, 196.0, "CPU", m.cpu_temp, m.cpu_load, pal.acc), (356.0, 196.0, "GPU", m.gpu_temp, m.gpu_load, pal.acc2)];
    for (cx, cy, _l, temp, load, col) in rings {
        c.arc(cx, cy, 104.0, 15.0, 135.0, 405.0, rgba(pal.muted, 55));
        if !temp.is_nan() {
            c.arc(cx, cy, 104.0, 15.0, 135.0, 135.0 + 270.0 * temp_frac(temp).max(0.02), temp_color(temp, &pal));
        }
        c.arc(cx, cy, 80.0, 6.0, 135.0, 405.0, rgba(pal.muted, 45));
        if !load.is_nan() {
            c.arc(cx, cy, 80.0, 6.0, 135.0, 135.0 + 270.0 * (load / 100.0).max(0.01), col);
        }
    }
    for (cx, cy, label, temp, load, col) in rings {
        c.text(label, cx, cy - 44.0, 26.0, col, true, Anchor::Center);
        c.value_unit(cx + 6.0, cy + 30.0, &temp_str(temp), "°C", 66.0, pal.txt, Anchor::Center);
        c.text(&format!("USO {}", pct(load)), cx, cy + 58.0, 20.0, pal.muted, false, Anchor::Center);
    }
    // rodapé: RAM
    c.text("RAM", 40.0, 348.0, 22.0, pal.acc, true, Anchor::Left);
    c.text(&pct(m.ram_load), W - 40.0, 348.0, 22.0, pal.txt, true, Anchor::Right);
    let frac = if m.ram_load.is_nan() { 0.0 } else { m.ram_load / 100.0 };
    c.bar(40.0, 360.0, 400.0, 10.0, frac, pal.acc);
}

fn theme_neon(c: &mut Canvas, m: &Metrics) {
    let pal = c.pal;
    c.fill(pal.bg);
    for (y0, label, temp, load, col) in
        [(40.0f32, "CPU", m.cpu_temp, m.cpu_load, pal.acc), (260.0, "GPU", m.gpu_temp, m.gpu_load, pal.acc2)]
    {
        c.text(label, 30.0, y0 + 30.0, 28.0, col, true, Anchor::Left);
        c.glow_text(&format!("{}°", temp_str(temp)), 30.0, y0 + 150.0, 120.0, col, Anchor::Left);
        c.text(&pct(load), W - 30.0, y0 + 120.0, 70.0, pal.txt, true, Anchor::Right);
        c.text("USO", W - 30.0, y0 + 150.0, 22.0, pal.muted, false, Anchor::Right);
    }
    c.line(20.0, 240.0, W - 20.0, 240.0, rgba(pal.muted, 60), 2.0);
}

fn theme_grade(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    c.header("DIGOO");
    let pal = c.pal;
    let clk = now();
    let cells: [(&str, String, Color); 6] = [
        ("CPU TEMP", format!("{}°", temp_str(m.cpu_temp)), pal.acc),
        ("GPU TEMP", format!("{}°", temp_str(m.gpu_temp)), pal.acc2),
        ("CPU USO", pct(m.cpu_load), pal.acc),
        ("GPU USO", pct(m.gpu_load), pal.acc2),
        ("RAM", pct(m.ram_load), rgb(120, 230, 140)),
        ("HORA", format!("{:02}:{:02}", clk.h, clk.m), pal.txt),
    ];
    let (gw, gh, gap) = (216.0, 116.0, 16.0);
    let x0 = (W - (2.0 * gw + gap)) / 2.0;
    for (i, (label, val, col)) in cells.iter().enumerate() {
        let x = x0 + (i % 2) as f32 * (gw + gap);
        let y = 64.0 + (i / 2) as f32 * (gh + gap);
        c.rrect(x, y, gw, gh, rgba(rgb(255, 255, 255), 14));
        c.text(label, x + 18.0, y + 32.0, 20.0, *col, true, Anchor::Left);
        c.text(val, x + 18.0, y + 86.0, 48.0, pal.txt, true, Anchor::Left);
    }
}

fn theme_minimal(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    let pal = c.pal;
    let clk = now();
    c.arc(240.0, 240.0, 206.0, 10.0, 0.0, 360.0, rgba(pal.muted, 40));
    if !m.cpu_temp.is_nan() {
        c.arc(240.0, 240.0, 206.0, 10.0, -90.0, -90.0 + 360.0 * temp_frac(m.cpu_temp).max(0.01), temp_color(m.cpu_temp, &pal));
    }
    c.text(&format!("{:02}:{:02}", clk.h, clk.m), 240.0, 110.0, 40.0, pal.muted, false, Anchor::Center);
    c.text("CPU", 240.0, 168.0, 30.0, pal.acc, true, Anchor::Center);
    c.value_unit(250.0, 300.0, &temp_str(m.cpu_temp), "°C", 140.0, pal.txt, Anchor::Center);
    c.text(&format!("GPU {}°", temp_str(m.gpu_temp)), 240.0, 372.0, 34.0, pal.acc2, true, Anchor::Center);
    c.text(&format!("USO {} · {}", pct(m.cpu_load), pct(m.gpu_load)), 240.0, 410.0, 22.0, pal.muted, false, Anchor::Center);
}

fn theme_velocimetro(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    c.header("DIGOO");
    let pal = c.pal;
    let gauges = [
        (124.0f32, 210.0f32, "CPU", m.cpu_temp, m.cpu_load, pal.acc),
        (356.0, 210.0, "GPU", m.gpu_temp, m.gpu_load, pal.acc2),
    ];
    for (cx, cy, _label, _temp, load, color) in gauges {
        let r = 100.0f32;
        c.arc(cx, cy, r, 4.0, 150.0, 390.0, rgba(pal.muted, 90)); // mostrador (240°)
        c.arc(cx, cy, r, 4.0, 150.0 + 240.0 * 0.75, 390.0, rgba(rgb(255, 59, 59), 200)); // zona quente
        for i in 0..=10 {
            let ang = (150.0 + 24.0 * i as f32).to_radians();
            let r1 = r - 8.0;
            let r2 = r - if i % 5 == 0 { 22.0 } else { 15.0 };
            c.line(cx + r1 * ang.cos(), cy + r1 * ang.sin(), cx + r2 * ang.cos(), cy + r2 * ang.sin(), rgba(pal.txt, 200), if i % 5 == 0 { 3.0 } else { 2.0 });
        }
        let frac = if load.is_nan() { 0.0 } else { (load / 100.0).clamp(0.0, 1.0) };
        c.arc(cx, cy, r + 12.0, 8.0, 150.0, 150.0 + 240.0 * frac.max(0.01), rgba(color, 240));
        let ang = (150.0 + 240.0 * frac).to_radians(); // ponteiro
        let tip = (cx + (r - 26.0) * ang.cos(), cy + (r - 26.0) * ang.sin());
        let perp = ang + std::f32::consts::FRAC_PI_2;
        let b0 = (cx + 6.0 * perp.cos(), cy + 6.0 * perp.sin());
        let b1 = (cx - 6.0 * perp.cos(), cy - 6.0 * perp.sin());
        c.fill_tri(b0, tip, b1, color);
        c.disc(cx, cy, 11.0, pal.txt);
    }
    for (cx, cy, label, temp, load, color) in gauges {
        c.text(label, cx, cy - 38.0, 24.0, color, true, Anchor::Center);
        c.text(&pct(load), cx, cy + 58.0, 34.0, pal.txt, true, Anchor::Center);
        c.value_unit(cx, cy + 128.0, &temp_str(temp), "°C", 52.0, temp_color(temp, &pal), Anchor::Center);
    }
    c.text(&format!("RAM {}   ·   REDE {}", pct(m.ram_load), fmt_rate(f32::NAN)), W / 2.0, 452.0, 22.0, pal.muted, false, Anchor::Center);
}

fn theme_graficos(c: &mut Canvas, m: &Metrics, hist: &History) {
    c.gradient_bg();
    c.header("DIGOO");
    let pal = c.pal;
    let panels = [
        (64.0f32, "CPU", m.cpu_temp, m.cpu_load, &hist.cpu_temp, &hist.cpu_load, pal.acc),
        (268.0, "GPU", m.gpu_temp, m.gpu_load, &hist.gpu_temp, &hist.gpu_load, pal.acc2),
    ];
    for (y0, _label, _temp, _load, th, lh, color) in panels {
        let (bx0, by0, bx1, by1) = (24.0f32, y0 + 60.0, W - 24.0, y0 + 190.0);
        c.round_rect(16.0, y0, W - 32.0, 196.0, 18.0, Some(rgba(rgb(255, 255, 255), 12)), Some((rgba(color, 60), 2.0)));
        for gy in 1..4 {
            let yy = by0 + (by1 - by0) * gy as f32 / 4.0;
            c.line(bx0, yy, bx1, yy, rgba(pal.muted, 30), 1.0);
        }
        c.sparkline(bx0, by0, bx1, by1, tail(lh, 120), color, 0.0, 100.0, 55, 3.0); // uso (área)
        c.sparkline(bx0, by0, bx1, by1, tail(th, 120), pal.txt, 20.0, 100.0, 0, 2.0); // temperatura (linha)
    }
    for (y0, label, temp, load, _th, _lh, color) in panels {
        c.text(label, 32.0, y0 + 30.0, 28.0, color, true, Anchor::Left);
        c.value_unit(250.0, y0 + 44.0, &temp_str(temp), "°C", 42.0, temp_color(temp, &pal), Anchor::Right);
        c.value_unit(W - 32.0, y0 + 44.0, &num(load), "%", 42.0, pal.txt, Anchor::Right);
    }
    c.text("linha branca = temperatura · área = uso", W / 2.0, 470.0, 16.0, pal.muted, false, Anchor::Center);
}

fn theme_relogio(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    let pal = c.pal;
    let clk = now();
    let sec = clk.s as f32 + clk.ms as f32 / 1000.0;
    c.arc(240.0, 240.0, 222.0, 6.0, 0.0, 360.0, rgba(pal.muted, 40));
    c.arc(240.0, 240.0, 222.0, 6.0, -90.0, -90.0 + 360.0 * (sec / 60.0), pal.acc);
    for i in 0..60 {
        let ang = (-90.0 + i as f32 * 6.0).to_radians();
        let r1 = if i % 5 == 0 { 206.0 } else { 210.0 };
        c.line(240.0 + r1 * ang.cos(), 240.0 + r1 * ang.sin(), 240.0 + 214.0 * ang.cos(), 240.0 + 214.0 * ang.sin(), rgba(if i % 5 == 0 { pal.txt } else { pal.muted }, 200), 2.0);
    }
    c.glow_text(&format!("{:02}:{:02}", clk.h, clk.m), 240.0, 222.0, 128.0, pal.txt, Anchor::Center);
    let wd = WEEKDAYS_FULL[((clk.wday + 6) % 7) as usize];
    c.text(&format!("{}, {:02} {}", wd, clk.day, MONTHS[(clk.mon - 1) as usize]), 240.0, 300.0, 30.0, pal.acc, true, Anchor::Center);
    c.text("CPU", 176.0, 364.0, 20.0, pal.muted, true, Anchor::Center);
    c.text(&format!("{}°C", temp_str(m.cpu_temp)), 176.0, 392.0, 32.0, temp_color(m.cpu_temp, &pal), true, Anchor::Center);
    c.text("GPU", 304.0, 364.0, 20.0, pal.muted, true, Anchor::Center);
    c.text(&format!("{}°C", temp_str(m.gpu_temp)), 304.0, 392.0, 32.0, temp_color(m.gpu_temp, &pal), true, Anchor::Center);
}

fn theme_compacto(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    let pal = c.pal;
    let clk = now();
    let hhmm = format!("{:02}:{:02}", clk.h, clk.m);
    let tw = c.text_width(&hhmm, 30.0, true);
    c.round_rect(W - 34.0 - tw, 14.0, 20.0 + tw, 48.0, 16.0, Some(rgba(rgb(0, 0, 0), 140)), None);
    c.text(&hhmm, W - 24.0, 39.0, 30.0, pal.txt, true, Anchor::Right);
    c.round_rect(14.0, H - 132.0, W - 28.0, 118.0, 22.0, Some(rgba(rgb(0, 0, 0), 150)), None);
    let half = (W - 28.0) / 2.0;
    let cols = [("CPU", m.cpu_temp, m.cpu_load, pal.acc), ("GPU", m.gpu_temp, m.gpu_load, pal.acc2)];
    for (i, &(label, temp, load, color)) in cols.iter().enumerate() {
        let x0 = 14.0 + i as f32 * half;
        c.text(label, x0 + 22.0, H - 108.0, 22.0, color, true, Anchor::Left);
        c.value_unit(x0 + 22.0, H - 46.0, &temp_str(temp), "°C", 56.0, temp_color(temp, &pal), Anchor::Left);
        c.text(&pct(load), x0 + half - 22.0, H - 64.0, 30.0, pal.txt, true, Anchor::Right);
        let frac = if load.is_nan() { 0.0 } else { load / 100.0 };
        c.bar(x0 + 22.0, H - 36.0, half - 44.0, 8.0, frac, color);
    }
    c.line(W / 2.0, H - 118.0, W / 2.0, H - 28.0, rgba(pal.muted, 80), 2.0);
}

/// Temas disponíveis (nome -> função). Vai crescendo conforme o port.
pub const THEMES: &[&str] = &["aneis", "neon", "grade", "velocimetro", "graficos", "minimal", "relogio", "compacto"];

pub fn render_theme(fonts: &Fonts, pal: Palette, name: &str, m: &Metrics, hist: &History) -> Pixmap {
    let mut c = Canvas::new(fonts, pal);
    match name {
        "neon" => theme_neon(&mut c, m),
        "grade" => theme_grade(&mut c, m),
        "velocimetro" => theme_velocimetro(&mut c, m),
        "graficos" => theme_graficos(&mut c, m, hist),
        "minimal" => theme_minimal(&mut c, m),
        "relogio" => theme_relogio(&mut c, m),
        "compacto" => theme_compacto(&mut c, m),
        _ => theme_aneis(&mut c, m),
    }
    c.pm
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sensors::Metrics;

    #[test]
    fn dump_sheet() {
        let fonts = Fonts::load();
        let pal = Palette::default();
        let m = Metrics { cpu_temp: 61.0, gpu_temp: 54.0, cpu_load: 34.0, gpu_load: 72.0, ram_load: 48.0 };
        // histórico sintético p/ enxergar as sparklines do tema "graficos"
        let mut hist = History::default();
        for i in 0..120 {
            let t = i as f32;
            hist.cpu_load.push(45.0 + 25.0 * (t * 0.15).sin());
            hist.gpu_load.push(60.0 + 30.0 * (t * 0.10 + 1.0).sin());
            hist.cpu_temp.push(58.0 + 8.0 * (t * 0.12).sin());
            hist.gpu_temp.push(52.0 + 6.0 * (t * 0.09 + 2.0).sin());
        }
        // grade cols x rows com todos os temas
        let cols = 3u32;
        let rows = (THEMES.len() as u32 + cols - 1) / cols;
        let (sheet_w, sheet_h) = (cols * 480, rows * 480);
        let mut sheet = Pixmap::new(sheet_w, sheet_h).unwrap();
        sheet.fill(rgb(40, 40, 40));
        for (i, name) in THEMES.iter().enumerate() {
            let pm = render_theme(&fonts, pal, name, &m, &hist);
            let (ox, oy) = ((i as u32 % cols) * 480, (i as u32 / cols) * 480);
            let (pw, sdata, ddata) = (pm.width(), pm.data(), sheet.data_mut());
            for y in 0..480u32 {
                for x in 0..480u32 {
                    let s = ((y * pw + x) * 4) as usize;
                    let d = (((oy + y) * sheet_w + ox + x) * 4) as usize;
                    ddata[d..d + 4].copy_from_slice(&sdata[s..s + 4]);
                }
            }
        }
        let jpeg = to_jpeg(&sheet, 0, 90);
        let out = std::env::temp_dir().join("digoo_rust_sheet.jpg");
        std::fs::write(&out, &jpeg).unwrap();
        println!("folha {}x{}: {} bytes -> {}", sheet_w, sheet_h, jpeg.len(), out.display());
    }
}

/// Converte o pixmap para JPEG 4:2:0 (a tela só aceita 4:2:0), girando `rotation` graus (0/90/180/270).
pub fn to_jpeg(pm: &Pixmap, rotation: u32, quality: u8) -> Vec<u8> {
    let (w, h) = (pm.width() as usize, pm.height() as usize);
    let src = pm.data();
    let (ow, oh) = if rotation == 90 || rotation == 270 { (h, w) } else { (w, h) };
    let mut rgb = vec![0u8; ow * oh * 3];
    for y in 0..h {
        for x in 0..w {
            let s = (y * w + x) * 4;
            let (ox, oy) = match rotation {
                90 => (h - 1 - y, x),
                180 => (w - 1 - x, h - 1 - y),
                270 => (y, w - 1 - x),
                _ => (x, y),
            };
            let d = (oy * ow + ox) * 3;
            rgb[d] = src[s];
            rgb[d + 1] = src[s + 1];
            rgb[d + 2] = src[s + 2];
        }
    }
    let mut out = Vec::new();
    let mut enc = Encoder::new(&mut out, quality);
    enc.set_sampling_factor(SamplingFactor::R_4_2_0);
    enc.encode(&rgb, ow as u16, oh as u16, ColorType::Rgb).unwrap();
    out
}
