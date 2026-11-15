//! Desenho do painel em CPU (tiny-skia) + texto (fontdue) + JPEG 4:2:0 (exigência da tela).
//! Framework de Canvas + temas portados do app Python. 480x480.

use crate::lyrics::{Lyric, LyricState};
use crate::sensors::{AudioSnapshot, MediaSnapshot, Metrics};
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
    mono: Font,
    sym: Font,
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
        // mono p/ o tema Terminal (alinhamento de colunas); cai pra reg se faltar.
        let mono = f(r"C:\Windows\Fonts\consola.ttf").unwrap_or_else(|| reg.clone());
        // símbolos (nota musical ♫) p/ a música; cai pra reg se faltar.
        let sym = f(r"C:\Windows\Fonts\seguisym.ttf").unwrap_or_else(|| reg.clone());
        Fonts { reg, bold, mono, sym }
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

    /// Polígono fechado com preenchimento e/ou contorno (painéis inclinados do HUD).
    pub fn polygon(&mut self, pts: &[(f32, f32)], fill: Option<Color>, outline: Option<(Color, f32)>) {
        if pts.len() < 3 {
            return;
        }
        if let Some(col) = fill {
            let mut pb = PathBuilder::new();
            pb.move_to(pts[0].0, pts[0].1);
            for p in &pts[1..] {
                pb.line_to(p.0, p.1);
            }
            pb.close();
            self.fill_path(pb, col);
        }
        if let Some((col, width)) = outline {
            let mut pb = PathBuilder::new();
            pb.move_to(pts[0].0, pts[0].1);
            for p in &pts[1..] {
                pb.line_to(p.0, p.1);
            }
            pb.close();
            self.stroke(pb, col, width, false);
        }
    }

    /// Círculo com preenchimento e/ou contorno (bulbo do termômetro, mostradores do cronógrafo).
    pub fn circle(&mut self, cx: f32, cy: f32, r: f32, fill: Option<Color>, outline: Option<(Color, f32)>) {
        if let Some(col) = fill {
            self.disc(cx, cy, r, col);
        }
        if let Some((col, width)) = outline {
            let mut pb = PathBuilder::new();
            pb.push_circle(cx, cy, r);
            self.stroke(pb, col, width, false);
        }
    }

    /// Fatia de pizza preenchida (rastro do feixe do radar).
    pub fn wedge(&mut self, cx: f32, cy: f32, r: f32, a0: f32, a1: f32, col: Color) {
        if a1 <= a0 {
            return;
        }
        let mut pb = PathBuilder::new();
        pb.move_to(cx, cy);
        let steps = ((a1 - a0).abs() / 3.0).ceil().max(2.0) as usize;
        for i in 0..=steps {
            let a = (a0 + (a1 - a0) * i as f32 / steps as f32).to_radians();
            pb.line_to(cx + r * a.cos(), cy + r * a.sin());
        }
        pb.close();
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
        blit_text(&mut self.pm, font, s, x, y, size, col, anchor);
    }

    /// Texto em fonte monoespaçada (tema Terminal: colunas alinhadas).
    pub fn text_mono(&mut self, s: &str, x: f32, y: f32, size: f32, col: Color, anchor: Anchor) {
        blit_text(&mut self.pm, &self.f.mono, s, x, y, size, col, anchor);
    }

    /// Texto em fonte de símbolos (nota musical ♫ do tema Música).
    pub fn text_sym(&mut self, s: &str, x: f32, y: f32, size: f32, col: Color, anchor: Anchor) {
        blit_text(&mut self.pm, &self.f.sym, s, x, y, size, col, anchor);
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

/// Rasteriza texto sobre o pixmap na fonte dada (y = linha de base; âncora horizontal).
/// Função livre p/ permitir escolher a fonte (reg/bold/mono) sem conflito de empréstimo.
fn blit_text(pm: &mut Pixmap, font: &Font, s: &str, x: f32, y: f32, size: f32, col: Color, anchor: Anchor) {
    let total: f32 = s.chars().map(|c| font.metrics(c, size).advance_width).sum();
    let mut pen = match anchor {
        Anchor::Left => x,
        Anchor::Center => x - total / 2.0,
        Anchor::Right => x - total,
    };
    let (pw, ph) = (pm.width() as i32, pm.height() as i32);
    let (cr, cg, cb) = (col.red(), col.green(), col.blue());
    let data = pm.data_mut();
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

/// Preenche um retângulo cru no pixmap (sem AA — look de pixel), misturando por alfa.
fn px_rect(pm: &mut Pixmap, x: i32, y: i32, w: i32, h: i32, col: Color) {
    let (pw, ph) = (pm.width() as i32, pm.height() as i32);
    let (r, g, b, a) = (col.red() * 255.0, col.green() * 255.0, col.blue() * 255.0, col.alpha());
    let data = pm.data_mut();
    for yy in y..y + h {
        for xx in x..x + w {
            if xx < 0 || yy < 0 || xx >= pw || yy >= ph {
                continue;
            }
            let i = ((yy * pw + xx) * 4) as usize;
            for (k, cc) in [r, g, b].iter().enumerate() {
                let bg = data[i + k] as f32;
                data[i + k] = (bg + (cc - bg) * a) as u8;
            }
            let bga = data[i + 3] as f32;
            data[i + 3] = (bga + (255.0 - bga) * a) as u8;
        }
    }
}

/// Copia um pixmap pequeno ampliado por `scale` (vizinho mais próximo: pixels grandes), misturando por alfa.
fn blit_scaled(dst: &mut Pixmap, src: &Pixmap, scale: u32) {
    let (sw, sh) = (src.width(), src.height());
    let (dw, dh) = (dst.width(), dst.height());
    let sdata = src.data().to_vec();
    let ddata = dst.data_mut();
    for y in 0..dh {
        for x in 0..dw {
            let sx = (x / scale).min(sw - 1);
            let sy = (y / scale).min(sh - 1);
            let si = ((sy * sw + sx) * 4) as usize;
            let a = sdata[si + 3] as f32 / 255.0;
            if a <= 0.0 {
                continue;
            }
            let di = ((y * dw + x) * 4) as usize;
            for k in 0..3 {
                let bg = ddata[di + k] as f32;
                ddata[di + k] = (bg + (sdata[si + k] as f32 - bg) * a) as u8;
            }
            ddata[di + 3] = 255;
        }
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

/// Valor bruto de uma métrica por nome (NaN p/ as que ainda não coletamos — lote de sensores).
fn metric_val(m: &Metrics, key: &str) -> f32 {
    match key {
        "cpu_temp" => m.cpu_temp,
        "gpu_temp" => m.gpu_temp,
        "cpu_load" => m.cpu_load,
        "gpu_load" => m.gpu_load,
        "ram_load" => m.ram_load,
        "cpu_clock" => m.cpu_clock,
        "cpu_power" => m.cpu_power,
        "gpu_hotspot" => m.gpu_hotspot,
        "gpu_clock" => m.gpu_clock,
        "gpu_power" => m.gpu_power,
        "gpu_vram" => m.gpu_vram,
        "gpu_fan" => m.gpu_fan,
        "disk_temp" => m.disk_temp,
        "net_down" => m.net_down,
        "net_up" => m.net_up,
        "pump_rpm" => m.pump_rpm,
        "fan_rpm" => m.fan_rpm,
        _ => f32::NAN,
    }
}

/// Unidade de exibição por métrica.
fn metric_unit(key: &str) -> &'static str {
    match key {
        k if k.ends_with("temp") => "°C",
        "gpu_hotspot" => "°C",
        "cpu_load" | "gpu_load" | "ram_load" => "%",
        "cpu_clock" | "gpu_clock" => "GHz",
        "cpu_power" | "gpu_power" => "W",
        "gpu_vram" => "GB",
        "net_down" | "net_up" => "MB/s",
        "pump_rpm" | "fan_rpm" | "gpu_fan" => "RPM",
        _ => "",
    }
}

/// Valor formatado (só o número) ou "--" quando ausente, já na escala da unidade:
/// clock MHz→GHz, VRAM MB→GB, rede B/s→MB/s; o resto (°C, %, W, RPM) é inteiro.
fn metric_value_str(m: &Metrics, key: &str) -> String {
    let v = metric_val(m, key);
    if v.is_nan() {
        return "--".into();
    }
    match key {
        k if k.ends_with("clock") => format!("{:.1}", v / 1000.0),
        "gpu_vram" => format!("{:.1}", v / 1024.0),
        "net_down" | "net_up" => format!("{:.1}", v / 1_048_576.0),
        _ => format!("{:.0}", v),
    }
}

/// Fração 0..1 da métrica p/ barras/anéis (NaN = sem barra): temperatura 20..95 °C; carga/RAM = v/100;
/// VRAM = usada/total; rede em escala log (1 B/s … 1 GB/s); clock/potência/RPM não têm barra.
fn metric_frac(m: &Metrics, key: &str) -> f32 {
    let v = metric_val(m, key);
    if v.is_nan() {
        return f32::NAN;
    }
    match key {
        k if k.ends_with("temp") || k == "gpu_hotspot" => temp_frac(v),
        "cpu_load" | "gpu_load" | "ram_load" => (v / 100.0).clamp(0.0, 1.0),
        "gpu_vram" => {
            let t = m.gpu_vram_total;
            if t.is_nan() || t <= 0.0 {
                f32::NAN
            } else {
                (v / t).clamp(0.0, 1.0)
            }
        }
        "net_down" | "net_up" => (v.max(1.0).log10() / 9.0).clamp(0.0, 1.0),
        _ => f32::NAN,
    }
}

/// Fração com NaN tratado como 0 e presa a 0..1 (equivale ao `metric_pct(...) or 0` do Python).
fn frac0(x: f32) -> f32 {
    if x.is_nan() {
        0.0
    } else {
        x.clamp(0.0, 1.0)
    }
}

/// Histórico de uma métrica por nome (vazio p/ as séries que ainda não guardamos).
fn hist_of<'h>(h: &'h History, key: &str) -> &'h [f32] {
    match key {
        "cpu_temp" => &h.cpu_temp,
        "cpu_load" => &h.cpu_load,
        "gpu_temp" => &h.gpu_temp,
        "gpu_load" => &h.gpu_load,
        "ram_load" => &h.ram_load,
        "net_down" => &h.net_down,
        _ => &[],
    }
}

/// Arco com degradê de c1 → c2 (pontas arredondadas via caps do stroke).
fn grad_arc(c: &mut Canvas, cx: f32, cy: f32, r: f32, width: f32, start: f32, end: f32, c1: Color, c2: Color) {
    if end <= start {
        return;
    }
    let span = end - start;
    let n = (48.0 * span / 360.0).ceil().max(2.0) as usize;
    for i in 0..n {
        let a0 = start + span * i as f32 / n as f32;
        let a1 = start + span * (i + 1) as f32 / n as f32 + 0.6;
        c.arc(cx, cy, r, width, a0, end.min(a1), mix(c1, c2, i as f32 / (n - 1).max(1) as f32));
    }
}

/// Ponteiro: linha do centro p/ fora com um rabinho atrás (cronógrafo, mostradores).
fn needle(c: &mut Canvas, cx: f32, cy: f32, ang_deg: f32, length: f32, width: f32, col: Color, tail: f32) {
    let a = ang_deg.to_radians();
    c.line(cx - tail * a.cos(), cy - tail * a.sin(), cx + length * a.cos(), cy + length * a.sin(), col, width);
}

/// Histórico por métrica para os gráficos (sparklines). O laço principal empurra um ponto por quadro.
/// A ampliação dos sensores (clocks, potência, VRAM, ventoinhas, rede, disco) e mais séries vêm no lote 2.
pub struct History {
    pub cpu_load: Vec<f32>,
    pub gpu_load: Vec<f32>,
    pub cpu_temp: Vec<f32>,
    pub gpu_temp: Vec<f32>,
    pub ram_load: Vec<f32>,
    pub net_down: Vec<f32>,
}
impl Default for History {
    fn default() -> Self {
        History {
            cpu_load: Vec::new(),
            gpu_load: Vec::new(),
            cpu_temp: Vec::new(),
            gpu_temp: Vec::new(),
            ram_load: Vec::new(),
            net_down: Vec::new(),
        }
    }
}
impl History {
    const CAP: usize = 240;
    pub fn push(&mut self, m: &Metrics) {
        push_cap(&mut self.cpu_load, m.cpu_load);
        push_cap(&mut self.gpu_load, m.gpu_load);
        push_cap(&mut self.cpu_temp, m.cpu_temp);
        push_cap(&mut self.gpu_temp, m.gpu_temp);
        push_cap(&mut self.ram_load, m.ram_load);
        push_cap(&mut self.net_down, m.net_down);
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
    c.text(&format!("RAM {}   ·   REDE {}", pct(m.ram_load), fmt_rate(m.net_down)), W / 2.0, 452.0, 22.0, pal.muted, false, Anchor::Center);
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

// --- tema HUD: painel sextavado (preenchimento translúcido + contorno) ---
fn hud_panel(c: &mut Canvas, x0: f32, y0: f32, x1: f32, y1: f32, col: Color) {
    let pts = [
        (x0 + 18.0, y0),
        (x1, y0),
        (x1, y1 - 18.0),
        (x1 - 18.0, y1),
        (x0, y1),
        (x0, y0 + 18.0),
    ];
    c.polygon(&pts, Some(rgba(col, 22)), Some((rgba(col, 230), 2.0)));
}

/// Terminal retrô: texto verde de monitor CRT, com linhas de varredura.
fn theme_terminal(c: &mut Canvas, m: &Metrics) {
    c.fill(rgb(2, 10, 4));
    let clk = now();
    let green = rgb(64, 255, 120);
    let dim = rgb(30, 120, 60);
    let blink = (clk.ms / 500) % 2 == 1; // cursor piscando ~2x/s
    let lines = [
        format!("> DIGOO  {:02}:{:02}", clk.h, clk.m),
        String::new(),
        format!("CPU   {:>4}°  {:>5}", temp_str(m.cpu_temp), pct(m.cpu_load)),
        format!("GPU   {:>4}°  {:>5}", temp_str(m.gpu_temp), pct(m.gpu_load)),
        format!("RAM   {:>11}", pct(m.ram_load)),
        format!("VRAM  {:>8} GB", metric_value_str(m, "gpu_vram")),
        format!("CLK   {:>8} GHz", metric_value_str(m, "cpu_clock")),
        format!("PWR   {:>9} W", metric_value_str(m, "cpu_power")),
        String::new(),
        if blink { "> _".to_string() } else { ">".to_string() },
    ];
    for (i, ln) in lines.iter().enumerate() {
        c.text_mono(ln, 26.0, 52.0 + i as f32 * 40.0, 26.0, green, Anchor::Left);
    }
    // barras em blocos ASCII, alinhadas às linhas CPU/GPU/RAM
    for (i, &v) in [m.cpu_load, m.gpu_load, m.ram_load].iter().enumerate() {
        let frac = if v.is_nan() { 0.0 } else { (v / 100.0).clamp(0.0, 1.0) };
        let n = (frac * 14.0) as usize;
        let ascii: String = "#".repeat(n) + &".".repeat(14 - n);
        let y_top = if i < 2 { 110.0 + i as f32 * 40.0 } else { 190.0 };
        c.text_mono(&ascii, 300.0, y_top + 22.0, 20.0, dim, Anchor::Left);
    }
    // varredura CRT
    let mut y = 0.0;
    while y < H {
        c.line(0.0, y, W, y, Color::from_rgba8(0, 0, 0, 70), 1.0);
        y += 3.0;
    }
}

/// HUD cyberpunk: painéis inclinados, amarelo e ciano.
fn theme_hud(c: &mut Canvas, m: &Metrics) {
    let pal = c.pal;
    let yel = rgb(255, 214, 0);
    let cya = pal.acc;
    c.fill(rgb(8, 8, 14));
    let mut y = 0.0;
    while y < H {
        c.line(0.0, y, W, y, Color::from_rgba8(255, 255, 255, 10), 1.0);
        y += 24.0;
    }
    let clk = now();
    c.polygon(&[(0.0, 0.0), (W, 0.0), (W, 50.0), (W - 30.0, 64.0), (0.0, 64.0)], Some(yel), None);
    c.text("NET//RUN", 20.0, 42.0, 28.0, rgb(10, 10, 14), true, Anchor::Left);
    c.text(&format!("{:02}:{:02}", clk.h, clk.m), W - 44.0, 42.0, 26.0, rgb(10, 10, 14), true, Anchor::Right);
    let items = [("CPU", m.cpu_temp, m.cpu_load, cya), ("GPU", m.gpu_temp, m.gpu_load, yel)];
    for (i, &(label, temp, load, col)) in items.iter().enumerate() {
        let x0 = 16.0 + i as f32 * 232.0;
        hud_panel(c, x0, 86.0, x0 + 216.0, 290.0, col);
        c.text(label, x0 + 18.0, 118.0, 24.0, col, true, Anchor::Left);
        c.value_unit(x0 + 18.0, 210.0, &temp_str(temp), "°C", 78.0, rgb(255, 255, 255), Anchor::Left);
        let frac = if load.is_nan() { 0.0 } else { (load / 100.0).clamp(0.0, 1.0) };
        for s in 0..10 {
            let on = (s as f32) < (frac * 10.0).round();
            let seg = if on { col } else { rgb(60, 60, 70) };
            c.rrect(x0 + 18.0 + s as f32 * 18.0, 240.0, 12.0, 22.0, seg);
        }
        c.text(&pct(load), x0 + 198.0, 118.0, 22.0, rgb(220, 220, 230), true, Anchor::Right);
    }
    hud_panel(c, 16.0, 306.0, W - 16.0, 464.0, cya);
    let rows = [
        ("RAM", format!("{} %", num(m.ram_load)), metric_frac(m, "ram_load")),
        ("VRAM", format!("{} GB", metric_value_str(m, "gpu_vram")), metric_frac(m, "gpu_vram")),
        ("REDE", format!("{} MB/s", metric_value_str(m, "net_down")), metric_frac(m, "net_down")),
    ];
    for (i, (label, val, frac)) in rows.iter().enumerate() {
        let y = 340.0 + i as f32 * 42.0;
        c.text(label, 36.0, y, 22.0, cya, true, Anchor::Left);
        c.text(val, W - 36.0, y, 22.0, rgb(255, 255, 255), true, Anchor::Right);
        if !frac.is_nan() {
            c.bar(130.0, y - 4.0, 200.0, 8.0, frac.clamp(0.0, 1.0), yel);
        }
    }
}

/// Barras verticais grandes: CPU, GPU, RAM e VRAM lado a lado.
fn theme_barras(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    c.header("DIGOO");
    let pal = c.pal;
    let items: [(&str, f32, f32, bool, Color); 4] = [
        ("CPU", m.cpu_load, m.cpu_temp, true, pal.acc),
        ("GPU", m.gpu_load, m.gpu_temp, true, pal.acc2),
        ("RAM", m.ram_load, f32::NAN, false, rgb(120, 230, 140)),
        ("VRAM", metric_frac(m, "gpu_vram") * 100.0, f32::NAN, false, rgb(255, 190, 60)),
    ];
    let (bw, gap, top, bottom) = (84.0f32, 28.0f32, 120.0f32, 400.0f32);
    let x0 = (W - (4.0 * bw + 3.0 * gap)) / 2.0;
    for (i, &(label, load, temp, has_temp, col)) in items.iter().enumerate() {
        let x = x0 + i as f32 * (bw + gap);
        let frac = if load.is_nan() { 0.0 } else { (load / 100.0).clamp(0.0, 1.0) };
        c.round_rect(x, top, bw, bottom - top, 14.0, Some(rgba(pal.muted, 35)), None);
        let h = (bottom - top) * frac.max(0.02);
        let steps = 18;
        for s in 0..steps {
            let y1 = bottom - h * s as f32 / steps as f32;
            let y0 = bottom - h * (s + 1) as f32 / steps as f32;
            let k = s as f32 / steps as f32;
            let cc = mix(col, rgb(255, 255, 255), k * 0.35); // degradê de baixo pra cima
            c.rrect(x, y0, bw, (y1 - y0) + 1.0, rgba(cc, 235));
        }
        c.text(&format!("{:.0}%", frac * 100.0), x + bw / 2.0, top - 22.0, 26.0, pal.txt, true, Anchor::Center);
        c.text(label, x + bw / 2.0, bottom + 26.0, 22.0, col, true, Anchor::Center);
        if has_temp {
            c.text(&format!("{}°", temp_str(temp)), x + bw / 2.0, bottom + 56.0, 26.0, temp_color(temp, &pal), true, Anchor::Center);
        }
    }
}

/// Dígitos de LED: números enormes com brilho, estilo painel digital.
fn theme_led(c: &mut Canvas, m: &Metrics) {
    c.fill(rgb(6, 4, 4));
    let items = [("CPU", m.cpu_temp, m.cpu_load, rgb(255, 60, 40)), ("GPU", m.gpu_temp, m.gpu_load, rgb(60, 220, 255))];
    for (i, &(label, temp, load, col)) in items.iter().enumerate() {
        let y = 150.0 + i as f32 * 200.0;
        c.glow_text(&temp_str(temp), W - 110.0, y, 130.0, col, Anchor::Right);
        c.text("°C", W - 96.0, y - 44.0, 30.0, col, true, Anchor::Left);
        c.text(label, 30.0, y - 60.0, 28.0, col, true, Anchor::Left);
        c.text(&pct(load), 30.0, y - 26.0, 26.0, rgb(200, 200, 200), true, Anchor::Left);
    }
    let clk = now();
    c.text(&format!("{:02}:{:02}", clk.h, clk.m), W / 2.0, 452.0, 30.0, rgb(255, 170, 60), true, Anchor::Center);
}

/// Anel único: um valor gigante no meio de um anel grosso, alternando CPU/GPU a cada 6 s.
fn theme_anel_unico(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    let pal = c.pal;
    let clk = now();
    let secs = clk.m as u32 * 60 + clk.s as u32;
    let gpu = (secs / 6) % 2 == 1;
    let (label, tk, lk, c1, c2) = if gpu {
        ("GPU", "gpu_temp", "gpu_load", pal.acc2, pal.acc)
    } else {
        ("CPU", "cpu_temp", "cpu_load", pal.acc, pal.acc2)
    };
    let frac = frac0(metric_frac(m, tk));
    c.arc(240.0, 240.0, 206.0, 28.0, 0.0, 360.0, rgba(pal.muted, 38));
    if frac > 0.0 {
        grad_arc(c, 240.0, 240.0, 206.0, 28.0, -90.0, -90.0 + 360.0 * frac.max(0.02), c1, c2);
    }
    c.text(label, 240.0, 142.0, 34.0, c1, true, Anchor::Center);
    c.value_unit(250.0, 300.0, &metric_value_str(m, tk), "°C", 150.0, pal.txt, Anchor::Center);
    c.text(&format!("USO {}", pct(metric_val(m, lk))), 240.0, 350.0, 28.0, pal.muted, false, Anchor::Center);
    for i in 0..2usize {
        let x = 228.0 + i as f32 * 24.0;
        let on = i == gpu as usize;
        let col = if on { rgba(c1, 255) } else { rgba(pal.muted, 90) };
        c.disc(x, 395.0, 5.0, col);
    }
}

const LIST_ROWS: &[(&str, &str)] = &[
    ("cpu_temp", "temp"), ("cpu_load", "uso"), ("cpu_clock", "clock"), ("cpu_power", "consumo"),
    ("gpu_temp", "temp"), ("gpu_hotspot", "memória"), ("gpu_load", "uso"), ("gpu_clock", "clock"),
    ("gpu_power", "consumo"), ("gpu_vram", "VRAM"), ("ram_load", "RAM"), ("disk_temp", "SSD"),
    ("net_down", "download"), ("net_up", "upload"),
];

/// Lista de sensores: tudo de uma vez em linhas, com barrinhas (estilo painel de sensores de PC).
fn theme_lista(c: &mut Canvas, m: &Metrics) {
    let pal = c.pal;
    c.fill(rgb(8, 10, 14));
    let green = rgb(120, 230, 140);
    let clk = now();
    c.text_mono("SENSORES", 18.0, 30.0, 19.0, pal.acc, Anchor::Left);
    c.text_mono(&format!("{:02}:{:02}", clk.h, clk.m), W - 18.0, 30.0, 19.0, pal.txt, Anchor::Right);
    c.line(14.0, 44.0, W - 14.0, 44.0, rgba(pal.acc, 120), 2.0);
    let (mut y, rh) = (62.0f32, 29.0f32);
    for (i, (key, name)) in LIST_ROWS.iter().enumerate() {
        let (grp, col) = if key.starts_with("cpu") {
            ("CPU", pal.acc)
        } else if key.starts_with("gpu") && *key != "gpu_vram" {
            ("GPU", pal.acc2)
        } else {
            ("", green)
        };
        if i % 2 == 0 {
            c.rrect(12.0, y - rh / 2.0, W - 24.0, rh, rgba(rgb(255, 255, 255), 9));
        }
        let title = format!("{} {}", grp, name).trim().to_uppercase();
        c.text_mono(&title, 22.0, y + 6.0, 19.0, col, Anchor::Left);
        let unit = metric_unit(key);
        let vs = metric_value_str(m, key);
        let right = if unit.is_empty() { vs } else { format!("{} {}", vs, unit) };
        c.text_mono(&right, W - 22.0, y + 6.0, 19.0, pal.txt, Anchor::Right);
        let frac = metric_frac(m, key);
        if !frac.is_nan() {
            c.bar(210.0, y - 4.0, 130.0, 8.0, frac.clamp(0.0, 1.0), col);
        }
        y += rh;
    }
}

/// Cartões com gráfico: 6 cartões, cada um com o valor e a curva dos últimos minutos.
fn theme_cartoes(c: &mut Canvas, m: &Metrics, hist: &History) {
    c.gradient_bg();
    c.header("DIGOO");
    let pal = c.pal;
    let green = rgb(120, 230, 140);
    let cards: [(&str, &str, Color, f32, f32); 6] = [
        ("CPU TEMP", "cpu_temp", pal.acc, 20.0, 100.0),
        ("CPU USO", "cpu_load", pal.acc, 0.0, 100.0),
        ("GPU TEMP", "gpu_temp", pal.acc2, 20.0, 100.0),
        ("GPU USO", "gpu_load", pal.acc2, 0.0, 100.0),
        ("RAM", "ram_load", green, 0.0, 100.0),
        ("DOWNLOAD", "net_down", rgb(255, 190, 60), 0.0, f32::NAN),
    ];
    for (i, &(label, key, col, vmin, vmax)) in cards.iter().enumerate() {
        let x0 = 14.0 + (i % 2) as f32 * 230.0;
        let y0 = 58.0 + (i / 2) as f32 * 140.0;
        c.round_rect(x0, y0, 222.0, 132.0, 16.0, Some(rgba(rgb(255, 255, 255), 14)), Some((rgba(col, 70), 2.0)));
        let vals = tail(hist_of(hist, key), 90);
        let vmax = if vmax.is_nan() {
            let hi = vals.iter().cloned().filter(|v| !v.is_nan()).fold(0.0f32, f32::max);
            if hi > 0.0 { hi * 1.15 } else { 1.0 }
        } else {
            vmax
        };
        c.sparkline(x0 + 10.0, y0 + 76.0, x0 + 212.0, y0 + 122.0, vals, col, vmin, vmax, 45, 2.0);
        c.text(label, x0 + 14.0, y0 + 26.0, 18.0, col, true, Anchor::Left);
        let color = if key.ends_with("temp") { temp_color(metric_val(m, key), &pal) } else { pal.txt };
        c.value_unit(x0 + 14.0, y0 + 74.0, &metric_value_str(m, key), metric_unit(key), 42.0, color, Anchor::Left);
    }
}

/// Termômetros: dois tubos de vidro que enchem com a temperatura.
fn theme_termometro(c: &mut Canvas, m: &Metrics) {
    c.gradient_bg();
    c.header("DIGOO");
    let pal = c.pal;
    let (top, bot) = (84.0f32, 340.0f32);
    let glass = Color::from_rgba8(255, 255, 255, 22);
    let glass_ol = Color::from_rgba8(255, 255, 255, 90);
    let tubes = [(150.0f32, "CPU", "cpu_temp", "cpu_load", pal.acc), (330.0, "GPU", "gpu_temp", "gpu_load", pal.acc2)];
    for (x, label, tk, lk, col) in tubes {
        let t = metric_val(m, tk);
        let frac = frac0(metric_frac(m, tk));
        let tc = temp_color(t, &pal);
        c.round_rect(x - 22.0, top, 44.0, bot + 10.0 - top, 22.0, Some(glass), Some((glass_ol, 2.0)));
        c.circle(x, bot + 38.0, 44.0, Some(glass), Some((glass_ol, 2.0)));
        let lvl = bot - (bot - top - 16.0) * frac;
        c.round_rect(x - 13.0, lvl, 26.0, bot + 20.0 - lvl, 13.0, Some(rgba(tc, 255)), None);
        c.circle(x, bot + 38.0, 35.0, Some(rgba(tc, 255)), None);
        c.line(x - 8.0, top + 18.0, x - 8.0, bot - 10.0, Color::from_rgba8(255, 255, 255, 70), 4.0);
        for k in 0..8 {
            let yy = bot - (bot - top - 16.0) * (k as f32 * 10.0) / 75.0;
            let x2 = if k % 2 == 0 { x + 40.0 } else { x + 34.0 };
            c.line(x + 28.0, yy, x2, yy, Color::from_rgba8(255, 255, 255, 140), 2.0);
            if k % 2 == 0 {
                c.text(&format!("{}", 20 + k * 10), x + 46.0, yy + 5.0, 16.0, pal.muted, false, Anchor::Left);
            }
        }
        c.text(&format!("{}°", temp_str(t)), x, bot + 48.0, 30.0, rgb(255, 255, 255), true, Anchor::Center);
        c.text(label, x - 60.0, 72.0, 26.0, col, true, Anchor::Left);
        c.text(&format!("USO {}", pct(metric_val(m, lk))), x, 450.0, 22.0, pal.muted, false, Anchor::Center);
    }
}

/// Relógio de ponteiros estilo cronógrafo, com mostradores pequenos de CPU, GPU e RAM.
fn theme_cronografo(c: &mut Canvas, m: &Metrics) {
    let pal = c.pal;
    let green = rgb(120, 230, 140);
    let (cx, cy) = (240.0f32, 240.0f32);
    c.circle(cx, cy, 222.0, Some(Color::from_rgba8(14, 16, 22, 235)), Some((rgba(pal.muted, 120), 3.0)));
    let clk = now();
    for i in 0..60 {
        let a = (-90.0 + i as f32 * 6.0).to_radians();
        let r1 = if i % 5 == 0 { 196.0 } else { 206.0 };
        let col = rgba(if i % 5 == 0 { pal.txt } else { pal.muted }, 230);
        let w = if i % 5 == 0 { 6.0 } else { 2.0 };
        c.line(cx + r1 * a.cos(), cy + r1 * a.sin(), cx + 214.0 * a.cos(), cy + 214.0 * a.sin(), col, w);
    }
    let subs = [
        (150.0f32, 250.0f32, "CPU", "cpu_temp", pal.acc),
        (330.0, 250.0, "GPU", "gpu_temp", pal.acc2),
        (240.0, 350.0, "RAM", "ram_load", green),
    ];
    for (sx, sy, _label, key, col) in subs {
        c.arc(sx, sy, 52.0, 4.0, 135.0, 405.0, rgba(pal.muted, 90));
        let frac = frac0(metric_frac(m, key));
        c.arc(sx, sy, 52.0, 4.0, 135.0, 135.0 + 270.0 * frac, rgba(col, 255));
        needle(c, sx, sy, 135.0 + 270.0 * frac, 44.0, 3.0, col, 8.0);
    }
    for (num, x, y) in [("12", 240.0f32, 78.0f32), ("3", 410.0, 250.0), ("9", 70.0, 250.0)] {
        c.text(num, x, y, 30.0, pal.txt, true, Anchor::Center);
    }
    for (sx, sy, label, key, col) in subs {
        c.text(label, sx, sy - 14.0, 15.0, col, true, Anchor::Center);
        c.text(&format!("{}{}", metric_value_str(m, key), metric_unit(key)), sx, sy + 30.0, 17.0, pal.txt, true, Anchor::Center);
    }
    let wd = WEEK3[clk.wday as usize];
    c.text(&format!("{} {:02}", wd, clk.day), 240.0, 156.0, 18.0, pal.acc, true, Anchor::Center);
    let mins = clk.m as f32 + clk.s as f32 / 60.0;
    let hrs = (clk.h % 12) as f32 + mins / 60.0;
    needle(c, cx, cy, -90.0 + hrs * 30.0, 112.0, 10.0, pal.txt, 16.0);
    needle(c, cx, cy, -90.0 + mins * 6.0, 170.0, 6.0, pal.txt, 20.0);
    needle(c, cx, cy, -90.0 + clk.s as f32 * 6.0, 196.0, 2.0, pal.acc, 30.0);
    c.disc(cx, cy, 9.0, rgba(pal.acc, 255));
    c.disc(cx, cy, 3.0, Color::from_rgba8(20, 20, 24, 255));
}

/// Radar: o feixe gira e cada sensor é um ponto — quanto mais longe do centro, mais alto o valor.
fn theme_radar(c: &mut Canvas, m: &Metrics) {
    let pal = c.pal;
    let green = pal.acc;
    let (cx, cy, rr) = (240.0f32, 254.0f32, 196.0f32);
    c.disc(cx, cy, rr, Color::from_rgba8(0, 18, 14, 220));
    for r in [rr / 3.0, 2.0 * rr / 3.0, rr] {
        c.circle(cx, cy, r, None, Some((rgba(green, 90), 2.0)));
    }
    c.line(cx - rr, cy, cx + rr, cy, rgba(green, 60), 1.0);
    c.line(cx, cy - rr, cx, cy + rr, rgba(green, 60), 1.0);
    let clk = now();
    let t = clk.s as f32 + clk.ms as f32 / 1000.0;
    let sweep = (t * 90.0) % 360.0;
    for k in 0..18 {
        let a1 = sweep - k as f32 * 3.0;
        let alpha = (110.0 * (1.0 - k as f32 / 18.0).powi(2)) as u8;
        c.wedge(cx, cy, rr, a1 - 3.0, a1, rgba(green, alpha));
    }
    let a = sweep.to_radians();
    c.line(cx, cy, cx + rr * a.cos(), cy + rr * a.sin(), rgba(green, 255), 3.0);
    let pts = [
        ("CPU", "cpu_load", 200.0f32),
        ("GPU", "gpu_load", 300.0),
        ("RAM", "ram_load", 60.0),
        ("VRAM", "gpu_vram", 130.0),
        ("CPU", "cpu_temp", 250.0),
        ("GPU", "gpu_temp", 20.0),
    ];
    for (label, key, ang) in pts {
        let frac = frac0(metric_frac(m, key)).max(0.05);
        let radius = rr * frac * 0.92;
        let px = cx + radius * ang.to_radians().cos();
        let py = cy + radius * ang.to_radians().sin();
        let since = ((sweep - ang) % 360.0 + 360.0) % 360.0;
        let glow = (1.0 - since / 300.0).max(0.3);
        let base = if since < 20.0 { rgb(255, 255, 255) } else { green };
        let col = mix(rgb(0, 0, 0), base, glow);
        c.disc(px, py, 6.0, rgba(col, 255));
        let tcol = mix(green, rgb(255, 255, 255), if since < 20.0 { 0.6 } else { 0.0 });
        let tcol = mix(rgb(0, 0, 0), tcol, glow.max(0.65));
        c.text(&format!("{} {}{}", label, metric_value_str(m, key), metric_unit(key)), px + 10.0, py - 6.0, 16.0, tcol, true, Anchor::Left);
    }
    c.header("DIGOO");
}

/// Mancha radial suave (aproxima o blur do Python): discos concêntricos de alfa baixo.
fn soft_blob(c: &mut Canvas, cx: f32, cy: f32, r: f32, col: Color) {
    let steps = 26;
    for i in 0..steps {
        let rr = r * (steps - i) as f32 / steps as f32;
        c.disc(cx, cy, rr, rgba(col, 9));
    }
}

/// Vidro fosco: fundo suave colorido + cartões translúcidos (lindo por cima de vídeo; aqui sobre o fundo).
fn theme_vidro(c: &mut Canvas, m: &Metrics) {
    let pal = c.pal;
    c.fill(rgb(10, 10, 18));
    soft_blob(c, 90.0, 110.0, 230.0, pal.acc);
    soft_blob(c, 400.0, 360.0, 230.0, pal.acc2);
    soft_blob(c, 300.0, 30.0, 180.0, mix(pal.acc, pal.acc2, 0.5));
    c.pal.muted = rgb(225, 225, 235); // unidades legíveis sobre o vidro claro
    let white = rgb(255, 255, 255);
    let boxes = [
        (16.0f32, 60.0f32, 232.0f32, 290.0f32),
        (248.0, 60.0, 464.0, 290.0),
        (16.0, 306.0, 464.0, 464.0),
    ];
    for &(x0, y0, x1, y1) in &boxes {
        c.round_rect(x0, y0, x1 - x0, y1 - y0, 22.0, Some(Color::from_rgba8(255, 255, 255, 34)), Some((Color::from_rgba8(255, 255, 255, 90), 2.0)));
    }
    c.header("DIGOO");
    let tops = [(boxes[0], "CPU", "cpu_temp", "cpu_load"), (boxes[1], "GPU", "gpu_temp", "gpu_load")];
    for ((x0, y0, x1, _y1), label, tk, lk) in tops {
        c.text(label, x0 + 20.0, y0 + 36.0, 26.0, white, true, Anchor::Left);
        c.value_unit((x0 + x1) / 2.0, y0 + 150.0, &metric_value_str(m, tk), metric_unit(tk), 84.0, white, Anchor::Center);
        c.text(&format!("USO {}", pct(metric_val(m, lk))), (x0 + x1) / 2.0, y0 + 182.0, 22.0, rgb(235, 235, 245), false, Anchor::Center);
        let frac = frac0(metric_frac(m, lk));
        c.bar(x0 + 24.0, y0 + 200.0, x1 - x0 - 48.0, 8.0, frac, white);
    }
    let (x0, y0, x1, _y1) = boxes[2];
    let cols = [("RAM", "ram_load"), ("VRAM", "gpu_vram"), ("REDE", "net_down")];
    for (i, (label, key)) in cols.iter().enumerate() {
        let cxx = x0 + (x1 - x0) * (i as f32 + 0.5) / 3.0;
        c.text(label, cxx, y0 + 46.0, 20.0, rgb(235, 235, 245), true, Anchor::Center);
        c.value_unit(cxx, y0 + 108.0, &metric_value_str(m, key), metric_unit(key), 40.0, white, Anchor::Center);
        let frac = metric_frac(m, key);
        if !frac.is_nan() {
            c.bar(cxx - 52.0, y0 + 124.0, 104.0, 6.0, frac.clamp(0.0, 1.0), white);
        }
    }
}

/// Janela de RPG 8-bit: os sensores viram barras de vida, desenhado em pixels grandes.
fn theme_rpg(c: &mut Canvas, m: &Metrics) {
    const S: i32 = 160;
    let mut small = Pixmap::new(S as u32, S as u32).unwrap();
    let blue = rgb(24, 32, 120);
    let white = rgb(248, 248, 248);
    let frame = rgb(150, 150, 200);
    px_rect(&mut small, 3, 3, S - 6, S - 6, blue);
    px_rect(&mut small, 3, 3, S - 6, 2, white); // molduras brancas (2 px)
    px_rect(&mut small, 3, S - 5, S - 6, 2, white);
    px_rect(&mut small, 3, 3, 2, S - 6, white);
    px_rect(&mut small, S - 5, 3, 2, S - 6, white);
    px_rect(&mut small, 6, 6, S - 12, 1, frame); // moldura interna
    px_rect(&mut small, 6, S - 7, S - 12, 1, frame);
    px_rect(&mut small, 6, 6, 1, S - 12, frame);
    px_rect(&mut small, S - 7, 6, 1, S - 12, frame);
    let clk = now();
    blit_text(&mut small, &c.f.bold, "STATUS", 12.0, 22.0, 13.0, rgb(255, 220, 80), Anchor::Left);
    blit_text(&mut small, &c.f.reg, &format!("{:02}:{:02}", clk.h, clk.m), (S - 12) as f32, 22.0, 11.0, white, Anchor::Right);
    let rows = [
        ("CPU", "cpu_temp", "cpu_load"),
        ("GPU", "gpu_temp", "gpu_load"),
        ("RAM", "", "ram_load"),
        ("VRAM", "", "gpu_vram"),
    ];
    let mut y = 34i32;
    for (label, tk, lk) in rows {
        let frac = frac0(metric_frac(m, lk));
        let hp = 1.0 - frac;
        let col = if hp > 0.5 { rgb(80, 220, 80) } else if hp > 0.2 { rgb(240, 200, 40) } else { rgb(230, 50, 40) };
        blit_text(&mut small, &c.f.reg, &label[..label.len().min(4)], 12.0, (y + 11) as f32, 11.0, white, Anchor::Left);
        let tpart = if tk.is_empty() { String::new() } else { format!("{}° ", metric_value_str(m, tk)) };
        let right = format!("{}{:.0}/100", tpart, frac * 100.0);
        blit_text(&mut small, &c.f.reg, &right, (S - 12) as f32, (y + 11) as f32, 11.0, white, Anchor::Right);
        px_rect(&mut small, 12, y + 14, S - 24, 6, rgb(10, 10, 40)); // fundo da barra
        px_rect(&mut small, 12, y + 14, S - 24, 1, white); // borda branca
        px_rect(&mut small, 12, y + 19, S - 24, 1, white);
        px_rect(&mut small, 12, y + 14, 1, 6, white);
        px_rect(&mut small, S - 13, y + 14, 1, 6, white);
        if hp > 0.0 {
            let w = ((S - 27) as f32 * hp) as i32;
            px_rect(&mut small, 13, y + 15, w, 4, col);
        }
        y += 29;
    }
    blit_scaled(&mut c.pm, &small, 3);
}

/// Temas disponíveis (nome -> função). Vai crescendo conforme o port.
pub const THEMES: &[&str] = &[
    "aneis", "neon", "grade", "velocimetro", "graficos", "minimal", "relogio", "compacto", "terminal", "hud", "barras",
    "led", "anel_unico", "lista", "cartoes", "termometro", "cronografo", "radar", "vidro", "rpg",
];

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
        "terminal" => theme_terminal(&mut c, m),
        "hud" => theme_hud(&mut c, m),
        "barras" => theme_barras(&mut c, m),
        "led" => theme_led(&mut c, m),
        "anel_unico" => theme_anel_unico(&mut c, m),
        "lista" => theme_lista(&mut c, m),
        "cartoes" => theme_cartoes(&mut c, m, hist),
        "termometro" => theme_termometro(&mut c, m),
        "cronografo" => theme_cronografo(&mut c, m),
        "radar" => theme_radar(&mut c, m),
        "vidro" => theme_vidro(&mut c, m),
        "rpg" => theme_rpg(&mut c, m),
        _ => theme_aneis(&mut c, m),
    }
    c.pm
}

// ---------------------------------------------------------------- música
/// Espectro suavizado: as 48 bandas que chegam do helper (~30x/s) sobem rápido e descem devagar,
/// com "tampinhas" (caps) que caem sozinhas. Persiste entre quadros (como o History), mora no main.
pub struct AudioViz {
    pub bands: Vec<f32>, // 48, 0..1
    pub caps: Vec<f32>,
    pub bass: f32,
    pub level: f32,
    pub active: bool,
}
impl AudioViz {
    pub fn new() -> AudioViz {
        AudioViz { bands: vec![0.0; 48], caps: vec![0.0; 48], bass: 0.0, level: 0.0, active: false }
    }
    pub fn update(&mut self, raw: Option<&AudioSnapshot>, dt: f32) {
        let dt = dt.min(0.2);
        let fresh = raw.map_or(false, |r| r.ts.elapsed().as_secs_f32() < 0.4);
        let mut target = [0.0f32; 48];
        if fresh {
            let b = &raw.unwrap().bands;
            for (i, t) in target.iter_mut().enumerate() {
                *t = b.get(i).copied().unwrap_or(0.0) / 100.0;
            }
        }
        let up = 1.0 - (-dt / 0.03).exp();
        let down = 1.0 - (-dt / 0.18).exp();
        for i in 0..48 {
            let t = target[i];
            let b = self.bands[i];
            let nb = b + (t - b) * (if t > b { up } else { down });
            self.bands[i] = nb;
            self.caps[i] = if nb > self.caps[i] { nb } else { (self.caps[i] - dt * 0.45).max(0.0) };
        }
        let bass_t = if fresh { target[..6].iter().sum::<f32>() / 6.0 } else { 0.0 };
        self.bass += (bass_t - self.bass) * (if bass_t > self.bass { up } else { down });
        let lv = if fresh { raw.unwrap().level / 100.0 } else { 0.0 };
        self.level += (lv - self.level) * (if lv > self.level { up } else { down });
        self.active = fresh || self.bands.iter().cloned().fold(0.0f32, f32::max) > 0.02;
    }
}

/// Nome amigável do player a partir do id do app (igual ao app_name do Python).
fn app_name(app_id: &str) -> String {
    let a = app_id.to_lowercase();
    for (k, v) in [
        ("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"),
        ("opera", "Opera"), ("brave", "Brave"), ("vlc", "VLC"), ("zunemusic", "Media Player"),
        ("deezer", "Deezer"), ("tidal", "TIDAL"), ("youtube", "YouTube Music"), ("discord", "Discord"),
    ] {
        if a.contains(k) {
            return v.to_string();
        }
    }
    if app_id.is_empty() {
        String::new()
    } else {
        app_id.rsplit(['!', '\\']).next().unwrap_or(app_id).replace(".exe", "")
    }
}

fn fmt_time(s: f64) -> String {
    let s = s.max(0.0) as i64;
    format!("{}:{:02}", s / 60, s % 60)
}

/// Encolhe o texto com "…" até caber em max_w.
fn ellipsize(c: &Canvas, text: &str, size: f32, bold: bool, max_w: f32) -> String {
    if c.text_width(text, size, bold) <= max_w {
        return text.to_string();
    }
    let mut t = text.to_string();
    while !t.is_empty() && c.text_width(&format!("{}…", t), size, bold) > max_w {
        t.pop();
    }
    format!("{}…", t.trim_end())
}

/// Quebra o texto em até `max_lines` linhas que cabem em max_w (última ganha "…" se sobrar).
fn wrap_words(c: &Canvas, text: &str, size: f32, bold: bool, max_w: f32, max_lines: usize) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let mut cur = String::new();
    for wd in text.split_whitespace() {
        let trial = if cur.is_empty() { wd.to_string() } else { format!("{} {}", cur, wd) };
        if c.text_width(&trial, size, bold) <= max_w || cur.is_empty() {
            cur = trial;
        } else {
            lines.push(std::mem::take(&mut cur));
            cur = wd.to_string();
        }
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    if lines.len() > max_lines {
        let rest = lines[max_lines - 1..].join(" ");
        lines.truncate(max_lines - 1);
        lines.push(ellipsize(c, &rest, size, bold, max_w));
    }
    lines
}

/// Espectro em círculo, espelhado (graves embaixo nos dois lados).
fn draw_ring(c: &mut Canvas, viz: &AudioViz, cx: f32, cy: f32, r: f32, length: f32, width: f32) {
    let pal = c.pal;
    let n = 48usize;
    for i in 0..(n * 2) {
        let k = if i < n { i } else { 2 * n - 1 - i };
        let ang = std::f32::consts::FRAC_PI_2 + (i as f32 + 0.5) * (2.0 * std::f32::consts::PI / (2.0 * n as f32));
        let r1 = r + 2.0;
        let r2 = r + 4.0 + length * viz.bands[k];
        let col = rgba(mix(pal.acc, pal.acc2, k as f32 / (n as f32 - 1.0)), 235);
        c.line(cx + r1 * ang.cos(), cy + r1 * ang.sin(), cx + r2 * ang.cos(), cy + r2 * ang.sin(), col, width);
    }
}

/// Espectro numa faixa horizontal: style = barras | onda | espelho.
fn draw_bars(c: &mut Canvas, viz: &AudioViz, top: f32, bottom: f32, style: &str, x0: f32, x1: f32, glow: bool) {
    let pal = c.pal;
    let n = 48usize;
    if style == "onda" {
        let pts: Vec<(f32, f32)> = (0..n)
            .map(|i| (x0 + (x1 - x0) * i as f32 / (n as f32 - 1.0), bottom - (bottom - top) * viz.bands[i]))
            .collect();
        let mut area = PathBuilder::new();
        area.move_to(pts[0].0, pts[0].1);
        for p in &pts[1..] {
            area.line_to(p.0, p.1);
        }
        area.line_to(x1, bottom);
        area.line_to(x0, bottom);
        area.close();
        c.fill_path(area, rgba(pal.acc, 70));
        let mut ln = PathBuilder::new();
        ln.move_to(pts[0].0, pts[0].1);
        for p in &pts[1..] {
            ln.line_to(p.0, p.1);
        }
        let (lc, lw) = if glow { (rgb(255, 255, 255), 3.0) } else { (pal.acc, 4.0) };
        c.stroke(ln, rgba(lc, 235), lw, true);
        return;
    }
    let gap = (3.0f32).min((x1 - x0) / n as f32 * 0.3);
    let bw = (x1 - x0 - gap * (n as f32 - 1.0)) / n as f32;
    let espelho = style == "espelho";
    let mid = if espelho { (top + bottom) / 2.0 } else { bottom };
    let span = (bottom - top) / (if espelho { 2.0 } else { 1.0 });
    for i in 0..n {
        let bx = x0 + i as f32 * (bw + gap);
        let hgt = (span * viz.bands[i]).max(2.0);
        let col = rgba(mix(pal.acc, pal.acc2, i as f32 / (n as f32 - 1.0)), 235);
        let rad = (3.0f32).min(bw / 2.0);
        if espelho {
            c.round_rect(bx, mid - hgt, bw, 2.0 * hgt, rad, Some(col), None);
        } else {
            c.round_rect(bx, bottom - hgt, bw, hgt, rad, Some(col), None);
            let cap_y = bottom - span * viz.caps[i] - 4.0;
            c.rrect(bx, cap_y, bw, 2.0, Color::from_rgba8(255, 255, 255, 200));
        }
    }
}

/// Desenha o espectro no estilo `wave`: círculo (ring) ou faixa (band = top,bottom,x0,x1).
fn draw_spectrum(c: &mut Canvas, viz: &AudioViz, wave: &str, band: (f32, f32, f32, f32), ring: Option<(f32, f32, f32, f32)>) {
    if wave == "off" {
        return;
    }
    if wave == "circulo" {
        let (cx, cy, r, len) = ring.unwrap_or((240.0, 240.0, 150.0, 70.0));
        draw_ring(c, viz, cx, cy, r, len, 6.0);
        return;
    }
    let (top, bottom, x0, x1) = band;
    draw_bars(c, viz, top, bottom, wave, x0, x1, false);
}

/// O que os modelos de música usam (espectro, mídia tocando, estilo de onda).
pub struct MusicCtx<'a> {
    pub viz: &'a AudioViz,
    pub media: Option<&'a MediaSnapshot>,
    pub wave: &'a str,
    pub anim: f32, // segundos contínuos (p/ carretéis, ondas, giros)
    pub lyric: Option<&'a Lyric>,
}
impl<'a> MusicCtx<'a> {
    fn has(&self) -> bool {
        self.media.map_or(false, |m| !m.title.is_empty())
    }
    fn title(&self) -> String {
        if self.has() {
            self.media.unwrap().title.clone()
        } else {
            "Nada tocando".into()
        }
    }
    fn artist(&self) -> String {
        if self.has() {
            let m = self.media.unwrap();
            if !m.artist.is_empty() { m.artist.clone() } else { m.album.clone() }
        } else {
            "Spotify, YouTube e outros players aparecem aqui".into()
        }
    }
    fn t(&self, c: &mut Canvas, x: f32, y: f32, s: &str, size: f32, col: Color, bold: bool, anchor: Anchor, max_w: f32) {
        let s = ellipsize(c, s, size, bold, max_w);
        c.text(&s, x, y, size, col, bold, anchor);
    }
    fn progress(&self, c: &mut Canvas, x0: f32, x1: f32, y: f32, times: bool) {
        if !self.has() {
            return;
        }
        let m = self.media.unwrap();
        if m.duration <= 0.0 {
            return;
        }
        let (acc, muted) = (c.pal.acc, c.pal.muted);
        c.bar(x0, y, x1 - x0, 6.0, (m.position / m.duration) as f32, acc);
        if times {
            c.text(&fmt_time(m.position), x0, y + 30.0, 18.0, muted, false, Anchor::Left);
            c.text(&fmt_time(m.duration), x1, y + 30.0, 18.0, muted, false, Anchor::Right);
        }
    }
    fn app_line(&self, c: &mut Canvas, x: f32, y: f32, anchor: Anchor, size: f32) {
        if !self.has() {
            return;
        }
        let m = self.media.unwrap();
        let app = app_name(&m.app);
        let status = if m.status == "Playing" { "tocando" } else { "pausado" };
        let s = if app.is_empty() { status.to_string() } else { format!("{} · {}", app, status) };
        let acc = c.pal.acc;
        c.text(&s, x, y, size, acc, true, anchor);
    }
    /// Sem capa: nota musical dentro de um círculo (fallback do Python quando não há arte).
    fn note(&self, c: &mut Canvas, cx: f32, cy: f32, r: f32) {
        let pal = c.pal;
        c.circle(cx, cy, r, Some(Color::from_rgba8(255, 255, 255, 18)), Some((rgba(pal.acc, 160), 3.0)));
        c.text_sym("\u{266b}", cx, cy + r * 0.35, r * 1.1, rgba(pal.acc, 255), Anchor::Center);
    }
}

fn layout_capa(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    let (cx, cy, r) = (240.0f32, 172.0f32, 98.0f32);
    draw_spectrum(c, x.viz, x.wave, (300.0, 330.0, 8.0, W - 8.0), Some((cx, cy, r + 8.0, 52.0)));
    x.note(c, cx, cy, r);
    x.t(c, 240.0, 362.0, &x.title(), 34.0, pal.txt, true, Anchor::Center, W - 40.0);
    x.t(c, 240.0, 396.0, &x.artist(), if x.has() { 24.0 } else { 18.0 }, pal.muted, false, Anchor::Center, W - 40.0);
    x.progress(c, 40.0, W - 40.0, 426.0, true);
    x.app_line(c, 240.0, 450.0, Anchor::Center, 18.0);
}

fn layout_vinil(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    let (cx, cy, r) = (240.0f32, 190.0f32, 128.0f32);
    draw_spectrum(c, x.viz, x.wave, (330.0, 360.0, 8.0, W - 8.0), Some((cx, cy, r + 6.0, 44.0)));
    c.disc(cx, cy, r, Color::from_rgba8(12, 12, 14, 255));
    let mut rr = r - 6.0; // sulcos do disco
    while rr > 56.0 {
        c.circle(cx, cy, rr, None, Some((Color::from_rgba8(40, 40, 46, 255), 2.0)));
        rr -= 7.0;
    }
    c.disc(cx, cy, 54.0, rgba(pal.acc, 255)); // etiqueta (sem capa → cor de acento)
    c.disc(cx, cy, 6.0, Color::from_rgba8(20, 20, 24, 255));
    c.line(430.0, 40.0, 410.0, 64.0, rgb(200, 200, 210), 6.0); // braço da agulha
    c.line(410.0, 64.0, 345.0, 205.0, rgb(200, 200, 210), 6.0);
    c.disc(430.0, 40.0, 12.0, Color::from_rgba8(170, 170, 180, 255));
    x.t(c, 240.0, 362.0, &x.title(), 32.0, pal.txt, true, Anchor::Center, W - 40.0);
    x.t(c, 240.0, 394.0, &x.artist(), if x.has() { 22.0 } else { 18.0 }, pal.muted, false, Anchor::Center, W - 40.0);
    x.progress(c, 40.0, W - 40.0, 422.0, true);
    x.app_line(c, 240.0, 448.0, Anchor::Center, 18.0);
}

fn layout_cartao(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    let s = 190.0f32;
    x.note(c, 24.0 + s / 2.0, 36.0 + s / 2.0, s / 2.0);
    let tx = 24.0 + s + 18.0;
    let tw = W - tx - 20.0;
    let lines = wrap_words(c, &x.title(), 34.0, true, tw, 2);
    let mut y = 66.0;
    for ln in &lines {
        x.t(c, tx, y, ln, 34.0, pal.txt, true, Anchor::Left, tw);
        y += 38.0;
    }
    x.t(c, tx, y + 6.0, &x.artist(), if x.has() { 22.0 } else { 16.0 }, pal.muted, false, Anchor::Left, tw);
    x.app_line(c, tx, y + 44.0, Anchor::Left, 17.0);
    x.progress(c, 24.0, W - 24.0, 252.0, true);
    draw_spectrum(c, x.viz, x.wave, (300.0, 470.0, 8.0, W - 8.0), Some((240.0, 385.0, 52.0, 34.0)));
}

fn layout_minimal(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    let lines = wrap_words(c, &x.title(), 46.0, true, W - 50.0, 3);
    let y0 = 190.0 - (lines.len() as f32 - 1.0) * 26.0;
    for (i, ln) in lines.iter().enumerate() {
        x.t(c, 240.0, y0 + i as f32 * 52.0, ln, 46.0, pal.txt, true, Anchor::Center, W - 40.0);
    }
    let ay = y0 + lines.len() as f32 * 52.0 + 6.0;
    x.t(c, 240.0, ay, &x.artist(), if x.has() { 24.0 } else { 18.0 }, pal.acc, false, Anchor::Center, W - 40.0);
    x.progress(c, 60.0, W - 60.0, 330.0, false);
    let wave = if x.wave == "circulo" { "onda" } else { x.wave };
    draw_spectrum(c, x.viz, wave, (370.0, 466.0, 8.0, W - 8.0), None);
}

fn layout_espectro(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    let tx = 20.0f32;
    x.t(c, tx, 50.0, &x.title(), 28.0, pal.txt, true, Anchor::Left, W - tx - 20.0);
    x.t(c, tx, 82.0, &x.artist(), if x.has() { 20.0 } else { 15.0 }, pal.muted, false, Anchor::Left, W - tx - 20.0);
    draw_spectrum(c, x.viz, x.wave, (130.0, 440.0, 8.0, W - 8.0), Some((240.0, 290.0, 70.0, 90.0)));
    x.progress(c, 20.0, W - 20.0, 452.0, false);
}

fn layout_neon(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    if x.has() {
        let s = ellipsize(c, &x.title(), 40.0, true, W - 40.0);
        c.glow_text(&s, 240.0, 76.0, 40.0, pal.acc, Anchor::Center);
    } else {
        x.t(c, 240.0, 76.0, &x.title(), 36.0, pal.acc, true, Anchor::Center, W - 40.0);
    }
    x.t(c, 240.0, 118.0, &x.artist(), if x.has() { 22.0 } else { 16.0 }, pal.txt, false, Anchor::Center, W - 40.0);
    if x.wave != "off" {
        if x.wave == "circulo" {
            draw_ring(c, x.viz, 240.0, 260.0, 70.0, 70.0, 6.0);
        } else {
            draw_bars(c, x.viz, 170.0, 350.0, x.wave, 8.0, W - 8.0, true);
        }
    }
    x.progress(c, 40.0, W - 40.0, 430.0, true);
}

/// Linha poligonal (AA) — usada pelas ondas.
fn poly_line(c: &mut Canvas, pts: &[(f32, f32)], col: Color, width: f32) {
    if pts.len() < 2 {
        return;
    }
    let mut pb = PathBuilder::new();
    pb.move_to(pts[0].0, pts[0].1);
    for p in &pts[1..] {
        pb.line_to(p.0, p.1);
    }
    c.stroke(pb, col, width, true);
}

/// Máximo de uma fatia das bandas (agrupa 48 bandas em menos colunas, p/ aparelho/pixel).
fn band_group(viz: &AudioViz, i: usize, per: usize, caps: bool) -> f32 {
    let src = if caps { &viz.caps } else { &viz.bands };
    (0..per).map(|k| src[i * per + k]).fold(0.0f32, f32::max)
}

/// Fita cassete: os carretéis giram e a fita passa de um lado p/ o outro com o tempo da música.
fn layout_cassete(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    c.round_rect(28.0, 60.0, W - 56.0, 270.0, 24.0, Some(Color::from_rgba8(36, 36, 42, 255)), Some((Color::from_rgba8(78, 78, 90, 255), 3.0)));
    for (sx, sy) in [(44.0, 76.0), (W - 44.0, 76.0), (44.0, 314.0), (W - 44.0, 314.0)] {
        c.disc(sx, sy, 6.0, Color::from_rgba8(90, 90, 100, 255)); // parafusos
    }
    c.round_rect(56.0, 84.0, W - 112.0, 122.0, 10.0, Some(Color::from_rgba8(242, 234, 214, 255)), None);
    c.rrect(56.0, 170.0, W - 112.0, 10.0, rgba(pal.acc, 255));
    c.rrect(56.0, 182.0, W - 112.0, 8.0, rgba(pal.acc2, 255));
    let title = ellipsize(c, &x.title(), 26.0, true, W - 140.0);
    c.text(&title, 240.0, 122.0, 26.0, rgb(40, 36, 44), true, Anchor::Center);
    let artist = ellipsize(c, &x.artist(), 18.0, false, W - 140.0);
    c.text(&artist, 240.0, 152.0, 18.0, rgb(90, 84, 90), false, Anchor::Center);
    c.text("A", 70.0, 106.0, 20.0, rgb(40, 36, 44), true, Anchor::Center);
    c.round_rect(118.0, 214.0, W - 236.0, 76.0, 36.0, Some(Color::from_rgba8(16, 16, 20, 255)), Some((Color::from_rgba8(70, 70, 80, 255), 2.0)));
    let dur = x.media.map_or(0.0, |m| m.duration);
    let p = if dur > 0.0 { (x.media.unwrap().position / dur) as f32 } else { 0.3 };
    let playing = x.media.map_or(false, |m| m.status == "Playing");
    let ang0 = if playing { (x.anim * 120.0) % 360.0 } else { 0.0 };
    for (cx, amount, sign) in [(176.0f32, 1.0 - p, 1.0f32), (304.0, p, -1.0)] {
        let cy = 252.0;
        let rt = 16.0 + 20.0 * amount; // carretel enche conforme a fita passa
        c.disc(cx, cy, rt, Color::from_rgba8(74, 48, 34, 255));
        c.disc(cx, cy, 15.0, Color::from_rgba8(235, 235, 240, 255));
        for s in 0..6 {
            let a = (ang0 * sign + s as f32 * 60.0).to_radians();
            c.line(cx + 5.0 * a.cos(), cy + 5.0 * a.sin(), cx + 13.0 * a.cos(), cy + 13.0 * a.sin(), Color::from_rgba8(60, 60, 66, 255), 3.0);
        }
    }
    c.polygon(&[(130.0, 330.0), (150.0, 300.0), (W - 150.0, 300.0), (W - 130.0, 330.0)], Some(Color::from_rgba8(52, 52, 60, 255)), None);
    draw_spectrum(c, x.viz, if x.wave == "circulo" { "barras" } else { x.wave }, (350.0, 418.0, 30.0, W - 30.0), None);
    x.progress(c, 30.0, W - 30.0, 430.0, true);
    x.app_line(c, 240.0, 466.0, Anchor::Center, 16.0);
}

/// Aparelho de som: visor fluorescente (VFD) + equalizador de LEDs verde/amarelo/vermelho.
fn layout_aparelho(c: &mut Canvas, x: &MusicCtx) {
    let vfd = rgb(90, 255, 225);
    c.round_rect(8.0, 8.0, W - 16.0, H - 16.0, 26.0, Some(Color::from_rgba8(14, 14, 17, 255)), Some((Color::from_rgba8(60, 60, 68, 255), 3.0)));
    c.round_rect(24.0, 24.0, W - 48.0, 100.0, 10.0, Some(Color::from_rgba8(4, 16, 18, 255)), Some((Color::from_rgba8(30, 60, 60, 255), 2.0)));
    let title = ellipsize(c, &x.title().to_uppercase(), 34.0, true, W - 80.0);
    c.text_mono(&title, 240.0, 62.0, 34.0, vfd, Anchor::Center);
    let artist = ellipsize(c, &x.artist().to_uppercase(), 22.0, false, W - 80.0);
    c.text_mono(&artist, 240.0, 104.0, 22.0, mix(vfd, rgb(0, 0, 0), 0.35), Anchor::Center);
    let (cols, rows) = (16usize, 14i32);
    let (gx0, gx1, gy0, gy1) = (30.0f32, W - 30.0, 146.0f32, 402.0f32);
    let cw = (gx1 - gx0) / cols as f32;
    let rh = (gy1 - gy0) / rows as f32;
    let per = 48 / cols;
    for i in 0..cols {
        let lit = (band_group(x.viz, i, per, false) * rows as f32).round() as i32;
        let capr = ((band_group(x.viz, i, per, true) * rows as f32).round() as i32).min(rows - 1);
        for r in 0..rows {
            let col = if r < 8 { rgb(40, 230, 90) } else if r < 11 { rgb(255, 210, 40) } else { rgb(255, 60, 50) };
            let on = r < lit || (r == capr && capr > 0);
            let y1 = gy1 - r as f32 * rh;
            c.rrect(gx0 + i as f32 * cw + 3.0, y1 - rh + 3.0, cw - 6.0, rh - 4.0, rgba(col, if on { 255 } else { 34 }));
        }
    }
    let dur = x.media.map_or(0.0, |m| m.duration);
    let pos = x.media.map_or(0.0, |m| m.position);
    let tm = if x.has() && dur > 0.0 { format!("{} / {}", fmt_time(pos), fmt_time(dur)) } else { "--:--".to_string() };
    c.text_mono(&tm, 34.0, 446.0, 26.0, vfd, Anchor::Left);
    if x.has() {
        let app = app_name(&x.media.unwrap().app).to_uppercase();
        let dim = mix(vfd, rgb(0, 0, 0), 0.3);
        c.text_mono(&app, W - 34.0, 446.0, 22.0, dim, Anchor::Right);
        let ix = W - 34.0 - app.chars().count() as f32 * 22.0 * 0.55 - 26.0; // ▶ / ❚❚ (a fonte do visor não tem)
        if x.media.unwrap().status == "Playing" {
            c.fill_tri((ix, 427.0), (ix + 14.0, 436.0), (ix, 445.0), dim);
        } else {
            c.rrect(ix, 427.0, 4.0, 18.0, dim);
            c.rrect(ix + 9.0, 427.0, 4.0, 18.0, dim);
        }
    }
}

/// Videogame 8-bit: desenhado em 160×160 e ampliado sem suavizar, nas 4 cores de portátil antigo.
fn layout_pixel(c: &mut Canvas, x: &MusicCtx) {
    const S: i32 = 160;
    let gb = [rgb(15, 56, 15), rgb(48, 98, 48), rgb(139, 172, 15), rgb(155, 188, 15)];
    let mut sm = Pixmap::new(S as u32, S as u32).unwrap();
    px_rect(&mut sm, 0, 0, S, S, gb[3]);
    px_rect(&mut sm, 2, 2, S - 4, 1, gb[0]);
    px_rect(&mut sm, 2, S - 3, S - 4, 1, gb[0]);
    px_rect(&mut sm, 2, 2, 1, S - 4, gb[0]);
    px_rect(&mut sm, S - 3, 2, 1, S - 4, gb[0]);
    px_rect(&mut sm, 8, 8, 40, 40, gb[1]); // "capa" (sem arte → nota)
    blit_text(&mut sm, &c.f.sym, "\u{266a}", 28.0, 36.0, 24.0, gb[3], Anchor::Center);
    let title = ellipsize(c, &x.title(), 11.0, false, (S - 62) as f32);
    blit_text(&mut sm, &c.f.mono, &title, 54.0, 20.0, 11.0, gb[0], Anchor::Left);
    let artist = ellipsize(c, &x.artist(), 9.0, false, (S - 62) as f32);
    blit_text(&mut sm, &c.f.mono, &artist, 54.0, 32.0, 9.0, gb[1], Anchor::Left);
    if x.has() {
        let st = if x.media.unwrap().status == "Playing" { "> PLAY" } else { "|| PAUSE" };
        blit_text(&mut sm, &c.f.mono, st, 54.0, 44.0, 9.0, gb[0], Anchor::Left);
    }
    let (cols, base, top) = (16usize, 128i32, 58i32);
    let per = 48 / cols;
    for i in 0..cols {
        let blocks = (band_group(x.viz, i, per, false) * (base - top) as f32 / 4.0).round() as i32;
        let x0 = 9 + i as i32 * 9;
        for k in 0..blocks {
            px_rect(&mut sm, x0, base - k * 4 - 3, 6, 2, if k < 9 { gb[0] } else { gb[1] });
        }
    }
    let dur = x.media.map_or(0.0, |m| m.duration);
    let pos = x.media.map_or(0.0, |m| m.position);
    if x.has() && dur > 0.0 {
        let n = (26.0 * pos / dur) as i32;
        for k in 0..26 {
            px_rect(&mut sm, 9 + k * 5 + 1, 136, 2, 3, if k <= n { gb[0] } else { gb[2] });
        }
        blit_text(&mut sm, &c.f.mono, &fmt_time(pos), 9.0, 150.0, 9.0, gb[0], Anchor::Left);
        blit_text(&mut sm, &c.f.mono, &fmt_time(dur), (S - 9) as f32, 150.0, 9.0, gb[0], Anchor::Right);
    }
    blit_scaled(&mut c.pm, &sm, 3);
}

/// Ondas suaves: quatro senoides que se cruzam, cada uma puxada por uma faixa do som.
fn layout_ondas(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    let b = &x.viz.bands;
    let low = b[..8].iter().sum::<f32>() / 8.0;
    let mid = b[8..24].iter().sum::<f32>() / 16.0;
    let high = b[24..].iter().sum::<f32>() / 24.0;
    let waves = [
        (low, 1.1f32, 1.6f32, pal.acc),
        (mid, 1.9, -2.2, pal.acc2),
        (high, 2.8, 2.9, mix(pal.acc, pal.acc2, 0.5)),
        ((low + mid) / 2.0, 1.5, -1.2, pal.txt),
    ];
    let cy = 250.0f32;
    for (amt, freq, speed, col) in waves {
        let amp = 14.0 + 120.0 * (amt * 1.4).min(1.0);
        let mut pts = Vec::new();
        let mut px = 0.0;
        while px <= W {
            let env = (std::f32::consts::PI * px / W).sin().powi(2);
            pts.push((px, cy + amp * env * (freq * 2.0 * std::f32::consts::PI * px / W + x.anim * speed).sin()));
            px += 6.0;
        }
        poly_line(c, &pts, rgba(col, 40), 12.0);
        poly_line(c, &pts, rgba(col, 230), 3.0);
    }
    if x.has() {
        let s = ellipsize(c, &x.title(), 38.0, true, W - 40.0);
        c.glow_text(&s, 240.0, 80.0, 38.0, pal.txt, Anchor::Center);
    } else {
        x.t(c, 240.0, 80.0, &x.title(), 34.0, pal.txt, true, Anchor::Center, W - 40.0);
    }
    x.t(c, 240.0, 120.0, &x.artist(), if x.has() { 22.0 } else { 16.0 }, pal.acc, false, Anchor::Center, W - 40.0);
    x.progress(c, 40.0, W - 40.0, 428.0, true);
}

/// Rádio antigo: dial de frequência que anda com a música, alto-falante pulsando com o grave.
fn layout_radio(c: &mut Canvas, x: &MusicCtx) {
    let wood = rgb(92, 58, 34);
    c.round_rect(20.0, 40.0, W - 40.0, H - 80.0, 30.0, Some(wood), Some((Color::from_rgba8(60, 36, 20, 255), 6.0)));
    c.round_rect(44.0, 64.0, W - 88.0, 106.0, 14.0, Some(Color::from_rgba8(236, 214, 160, 255)), None);
    for i in 0..=20 {
        let xx = 64.0 + i as f32 * (W - 128.0) / 20.0; // escala do dial
        let y1 = if i % 5 == 0 { 128.0 } else { 160.0 };
        c.line(xx, 140.0, xx, y1, Color::from_rgba8(80, 60, 40, 255), 2.0);
    }
    let dur = x.media.map_or(0.0, |m| m.duration);
    let frac = if dur > 0.0 { (x.media.unwrap().position / dur) as f32 } else { 0.5 };
    let nx = 64.0 + frac * (W - 128.0);
    c.line(nx, 74.0, nx, 162.0, Color::from_rgba8(200, 30, 30, 255), 4.0);
    x.t(c, 240.0, 106.0, &x.title(), 28.0, rgb(60, 40, 25), true, Anchor::Center, W - 120.0);
    x.t(c, 240.0, 130.0, &x.artist(), 18.0, rgb(100, 75, 50), false, Anchor::Center, W - 120.0);
    let (cx, cy) = (160.0f32, 300.0f32); // alto-falante pulsando com o grave
    let r = 92.0 + x.viz.bass * 10.0;
    c.disc(cx, cy, r, Color::from_rgba8(40, 26, 16, 255));
    for k in 0..5 {
        let rr = r - 10.0 - k as f32 * 16.0;
        if rr > 4.0 {
            c.circle(cx, cy, rr, None, Some((Color::from_rgba8(110, 80, 50, 255), 3.0)));
        }
    }
    if x.wave != "off" {
        draw_bars(c, x.viz, 380.0, 420.0, "barras", 230.0, W - 50.0, false);
    }
    x.app_line(c, 370.0, 225.0, Anchor::Center, 16.0);
}

/// Polaroide: a "foto" numa moldura instantânea, título escrito embaixo (sem giro no núcleo Rust).
fn layout_polaroid(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    let (cw, ch) = (290.0f32, 340.0f32);
    let (cx0, cy0) = ((W - cw) / 2.0, 30.0f32);
    c.rrect(cx0 + 8.0, cy0 + 10.0, cw, ch, Color::from_rgba8(0, 0, 0, 90)); // sombra
    c.rrect(cx0, cy0, cw, ch, rgb(245, 242, 236));
    c.rrect(cx0 + 20.0, cy0 + 20.0, 250.0, 250.0, rgba(pal.acc, 255)); // "foto" (sem capa → acento)
    c.text_sym("\u{266b}", cx0 + 145.0, cy0 + 165.0, 90.0, Color::from_rgba8(255, 255, 255, 210), Anchor::Center);
    let title = ellipsize(c, &x.title(), 26.0, true, 260.0);
    c.text(&title, cx0 + 145.0, cy0 + 300.0, 26.0, rgb(40, 40, 46), true, Anchor::Center);
    x.t(c, 240.0, 416.0, &x.artist(), if x.has() { 24.0 } else { 16.0 }, pal.txt, false, Anchor::Center, W - 40.0);
    x.progress(c, 50.0, W - 50.0, 434.0, false);
    draw_spectrum(c, x.viz, if x.wave == "circulo" { "onda" } else { x.wave }, (444.0, 476.0, 8.0, W - 8.0), None);
}

/// Cabeçalho de música: título + artista no topo à esquerda (sem capa).
fn music_header(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    x.t(c, 20.0, 38.0, &x.title(), 24.0, pal.txt, true, Anchor::Left, W - 40.0);
    x.t(c, 20.0, 64.0, &x.artist(), 17.0, pal.muted, false, Anchor::Left, W - 40.0);
}

/// Texto com "varrição" de karaokê: pinta os caracteres já cantados (até `frac`) com `hi`.
fn sweep_text(c: &mut Canvas, cx: f32, y: f32, text: &str, size: f32, frac: f32, base: Color, hi: Color) {
    let total = c.text_width(text, size, true);
    let x0 = cx - total / 2.0;
    let sweep_x = x0 + total * frac.clamp(0.0, 1.0);
    let mut pen = x0;
    for ch in text.chars() {
        let w = c.f.bold.metrics(ch, size).advance_width;
        let col = if pen + w * 0.5 <= sweep_x { hi } else { base };
        c.text(&ch.to_string(), pen, y, size, col, true, Anchor::Left);
        pen += w;
    }
}

/// Mensagem quando não dá p/ mostrar a letra (buscando, sem, erro, instrumental…).
fn lyric_message(lyr: Option<&Lyric>, has: bool) -> &'static str {
    if !has {
        return "Toque uma música no Spotify ou YouTube";
    }
    match lyr.map(|l| l.state) {
        Some(LyricState::Searching) => "Procurando a letra…",
        Some(LyricState::Error) => "Sem conexão para a letra — tento de novo",
        Some(LyricState::Instrumental) => "♪  Instrumental  ♪",
        Some(LyricState::Off) => "Letras desligadas",
        _ => "Letra não encontrada",
    }
}

/// Karaokê: a linha atual grande pintando conforme é cantada; as vizinhas embaixo/em cima, mais apagadas.
fn layout_letra(c: &mut Canvas, x: &MusicCtx) {
    let pal = c.pal;
    let bg = rgb(8, 8, 12);
    music_header(c, x);
    let (cy, step) = (252.0f32, 56.0f32);
    let ok = x.lyric.map_or(false, |l| l.state == LyricState::Ok && !l.lines.is_empty());
    if ok {
        let l = x.lyric.unwrap();
        let dur = x.media.map_or(0.0, |m| m.duration) as f32;
        let pos = x.media.map_or(0.0, |m| m.position) as f32;
        let (i, frac, _since) = l.at(pos, dur);
        let cur = i.max(0) as usize;
        let line = &l.lines[cur].1;
        let fr = if l.synced { frac } else { 0.0 };
        sweep_text(c, 240.0, cy, if line.is_empty() { "♪" } else { line }, 32.0, fr, pal.txt, pal.acc);
        for d in 1..=3i32 {
            let dim = mix(pal.txt, bg, 0.25 + 0.22 * d as f32); // mais longe = mais apagada
            let up = cur as i32 - d;
            if up >= 0 {
                let s = ellipsize(c, &l.lines[up as usize].1, 24.0, false, W - 40.0);
                c.text(&s, 240.0, cy - d as f32 * step, 24.0, dim, false, Anchor::Center);
            }
            let dn = cur + d as usize;
            if dn < l.lines.len() {
                let s = ellipsize(c, &l.lines[dn].1, 24.0, false, W - 40.0);
                c.text(&s, 240.0, cy + d as f32 * step, 24.0, dim, false, Anchor::Center);
            }
        }
    } else {
        let msg = lyric_message(x.lyric, x.has());
        x.t(c, 240.0, cy, msg, 26.0, pal.muted, false, Anchor::Center, W - 40.0);
    }
    x.progress(c, 40.0, W - 40.0, 440.0, false);
    if x.wave != "off" {
        draw_bars(c, x.viz, 450.0, 476.0, if x.wave == "circulo" { "barras" } else { x.wave }, 200.0, W - 40.0, false);
    }
}

/// Temas de música (modelos de music.py + music_extra.py). Sem capa de álbum ainda → nota ♫.
pub const MUSIC_LAYOUTS: &[&str] = &[
    "capa", "vinil", "cartao", "minimal", "espectro", "neon", "cassete", "aparelho", "pixel", "ondas", "radio", "polaroid", "letra",
];

/// Renderiza o tema Música no modelo `layout` com o estilo de onda `wave`.
pub fn render_music(fonts: &Fonts, pal: Palette, layout: &str, viz: &AudioViz, media: Option<&MediaSnapshot>, wave: &str, anim: f32, lyric: Option<&Lyric>) -> Pixmap {
    let mut c = Canvas::new(fonts, pal);
    c.fill(rgb(8, 8, 12));
    let ctx = MusicCtx { viz, media, wave, anim, lyric };
    match layout {
        "letra" => layout_letra(&mut c, &ctx),
        "capa" => layout_capa(&mut c, &ctx),
        "vinil" => layout_vinil(&mut c, &ctx),
        "cartao" => layout_cartao(&mut c, &ctx),
        "minimal" => layout_minimal(&mut c, &ctx),
        "neon" => layout_neon(&mut c, &ctx),
        "cassete" => layout_cassete(&mut c, &ctx),
        "aparelho" => layout_aparelho(&mut c, &ctx),
        "pixel" => layout_pixel(&mut c, &ctx),
        "ondas" => layout_ondas(&mut c, &ctx),
        "radio" => layout_radio(&mut c, &ctx),
        "polaroid" => layout_polaroid(&mut c, &ctx),
        _ => layout_espectro(&mut c, &ctx),
    }
    c.pm
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lyrics::{Lyric, LyricState};
    use crate::sensors::{MediaSnapshot, Metrics};

    #[test]
    fn dump_music_sheet() {
        let fonts = Fonts::load();
        let pal = Palette::default();
        // espectro sintético (grave forte, agudos menores) + "tocando agora"
        let mut viz = AudioViz::new();
        for i in 0..48 {
            let t = i as f32 / 47.0;
            viz.bands[i] = (0.9 * (1.0 - t) * (1.0 - t) + 0.3 * (i as f32 * 0.7).sin().abs()).min(1.0);
            viz.caps[i] = (viz.bands[i] + 0.08).min(1.0);
        }
        viz.bass = 0.8;
        viz.level = 0.6;
        viz.active = true;
        let media = MediaSnapshot {
            title: "Blinding Lights".into(), artist: "The Weeknd".into(), album: String::new(),
            status: "Playing".into(), app: "Spotify.exe".into(), position: 83.0, duration: 200.0,
        };
        let lyric = Lyric {
            state: LyricState::Ok,
            synced: true,
            lines: vec![
                (76.0, "I'm blinded by the lights".into()),
                (80.0, "No, I can't sleep until I feel your touch".into()),
                (84.0, "I said, ooh, I'm drowning in the night".into()),
                (88.0, "Oh, when I'm like this, you're the one I trust".into()),
            ],
        };
        let waves = ["circulo", "circulo", "barras", "onda", "barras", "onda", "barras", "barras", "barras", "onda", "barras", "onda"];
        let cols = 3u32;
        let rows = (MUSIC_LAYOUTS.len() as u32 + cols - 1) / cols;
        let (sheet_w, sheet_h) = (cols * 480, rows * 480);
        let mut sheet = Pixmap::new(sheet_w, sheet_h).unwrap();
        sheet.fill(rgb(40, 40, 40));
        for (i, layout) in MUSIC_LAYOUTS.iter().enumerate() {
            let pm = render_music(&fonts, pal, layout, &viz, Some(&media), waves[i % waves.len()], 1.3, Some(&lyric));
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
        let out = std::env::temp_dir().join("digoo_music_sheet.jpg");
        std::fs::write(&out, &jpeg).unwrap();
        println!("folha música {}x{}: {} bytes -> {}", sheet_w, sheet_h, jpeg.len(), out.display());
    }

    #[test]
    fn net_counter_reads() {
        // valida o FFI do GetIfTable (offsets/stride) com interfaces reais: taxa finita e plausível.
        let mut nc = crate::sensors::NetCounter::new();
        let _ = nc.sample();
        std::thread::sleep(std::time::Duration::from_millis(200));
        let (d, u) = nc.sample();
        println!("rede medida: down={:.2} KB/s  up={:.2} KB/s", d / 1024.0, u / 1024.0);
        assert!(!d.is_infinite() && !u.is_infinite(), "taxa infinita");
        assert!(d.is_nan() || (0.0..5.0e9).contains(&d), "download absurdo: {d}");
        assert!(u.is_nan() || (0.0..5.0e9).contains(&u), "upload absurdo: {u}");
    }

    #[test]
    fn dump_sheet() {
        let fonts = Fonts::load();
        let pal = Palette::default();
        let m = Metrics {
            cpu_temp: 61.0, gpu_temp: 54.0, cpu_load: 34.0, gpu_load: 72.0, ram_load: 48.0,
            cpu_clock: 5225.0, cpu_power: 72.8, gpu_hotspot: 64.0, gpu_clock: 2722.0, gpu_power: f32::NAN,
            gpu_vram: 3720.0, gpu_vram_total: 12227.0, gpu_fan: 1600.0, disk_temp: 44.0,
            net_down: 2_400_000.0, net_up: 350_000.0, pump_rpm: 2200.0, fan_rpm: 1290.0,
        };
        // histórico sintético p/ enxergar as sparklines do tema "graficos"
        let mut hist = History::default();
        for i in 0..120 {
            let t = i as f32;
            hist.cpu_load.push(45.0 + 25.0 * (t * 0.15).sin());
            hist.gpu_load.push(60.0 + 30.0 * (t * 0.10 + 1.0).sin());
            hist.cpu_temp.push(58.0 + 8.0 * (t * 0.12).sin());
            hist.gpu_temp.push(52.0 + 6.0 * (t * 0.09 + 2.0).sin());
            hist.ram_load.push(46.0 + 12.0 * (t * 0.07 + 0.5).sin());
            hist.net_down.push((1.5e6 + 1.4e6 * (t * 0.2).sin()).max(0.0));
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
