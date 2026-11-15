//! Desenho do painel em CPU (tiny-skia) + texto (fontdue) + JPEG 4:2:0 (exigência da tela).
//! Fase 1: um painel simples (CPU/GPU temperatura e uso, RAM) para provar o pipeline nativo.

use crate::sensors::Metrics;
use fontdue::Font;
use jpeg_encoder::{ColorType, Encoder, SamplingFactor};
use tiny_skia::{
    Color, FillRule, LineCap, Paint, PathBuilder, Pixmap, Stroke, Transform,
};

pub const W: u32 = 480;
pub const H: u32 = 480;

pub struct Renderer {
    font: Font,
    font_bold: Font,
}

impl Renderer {
    pub fn new() -> Renderer {
        // fontes do Windows (sempre presentes); depois dá para embutir as do app.
        let load = |p: &str| {
            std::fs::read(p)
                .ok()
                .and_then(|b| Font::from_bytes(b, fontdue::FontSettings::default()).ok())
        };
        let font = load(r"C:\Windows\Fonts\segoeui.ttf").expect("segoeui.ttf");
        let font_bold = load(r"C:\Windows\Fonts\segoeuib.ttf").unwrap_or_else(|| font.clone());
        Renderer { font, font_bold }
    }

    pub fn render(&self, m: &Metrics) -> Pixmap {
        let mut pm = Pixmap::new(W, H).unwrap();
        pm.fill(Color::from_rgba8(13, 17, 23, 255));

        let acc = Color::from_rgba8(0, 229, 255, 255);
        let acc2 = Color::from_rgba8(255, 46, 136, 255);
        let muted = Color::from_rgba8(80, 90, 100, 255);
        let white = Color::from_rgba8(230, 237, 243, 255);

        // dois anéis: CPU (esq) e GPU (dir)
        self.ring(&mut pm, 124.0, 180.0, m.cpu_temp, muted, acc);
        self.ring(&mut pm, 356.0, 180.0, m.gpu_temp, muted, acc2);

        self.text(&mut pm, "CPU", 124.0, 118.0, 26.0, acc, true, true);
        self.text(&mut pm, "GPU", 356.0, 118.0, 26.0, acc2, true, true);
        self.text(&mut pm, &temp_str(m.cpu_temp), 124.0, 192.0, 60.0, white, true, true);
        self.text(&mut pm, &temp_str(m.gpu_temp), 356.0, 192.0, 60.0, white, true, true);
        self.text(&mut pm, &format!("USO {}", pct_str(m.cpu_load)), 124.0, 238.0, 20.0, muted, false, true);
        self.text(&mut pm, &format!("USO {}", pct_str(m.gpu_load)), 356.0, 238.0, 20.0, muted, false, true);

        // barra de RAM
        self.bar(&mut pm, 40.0, 330.0, 400.0, 16.0, m.ram_load / 100.0, muted, acc);
        self.text(&mut pm, "RAM", 40.0, 316.0, 20.0, white, true, false);
        self.text(&mut pm, &pct_str(m.ram_load), 440.0, 316.0, 20.0, white, true, false_right());

        pm
    }

    /// Anel: fundo cinza + arco colorido proporcional à temperatura (20–95 °C).
    fn ring(&self, pm: &mut Pixmap, cx: f32, cy: f32, temp: f32, bg: Color, col: Color) {
        self.arc(pm, cx, cy, 90.0, 14.0, 135.0, 405.0, bg);
        let frac = ((temp - 20.0) / 75.0).clamp(0.02, 1.0);
        self.arc(pm, cx, cy, 90.0, 14.0, 135.0, 135.0 + 270.0 * frac, col);
    }

    fn arc(&self, pm: &mut Pixmap, cx: f32, cy: f32, r: f32, width: f32, a0: f32, a1: f32, col: Color) {
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
        if let Some(path) = pb.finish() {
            let mut paint = Paint::default();
            paint.set_color(col);
            paint.anti_alias = true;
            let stroke = Stroke { width, line_cap: LineCap::Round, ..Default::default() };
            pm.stroke_path(&path, &paint, &stroke, Transform::identity(), None);
        }
    }

    fn bar(&self, pm: &mut Pixmap, x: f32, y: f32, w: f32, h: f32, frac: f32, bg: Color, col: Color) {
        self.rrect(pm, x, y, w, h, bg_alpha(bg, 60));
        let fw = (w * frac.clamp(0.0, 1.0)).max(h);
        self.rrect(pm, x, y, fw, h, col);
    }

    fn rrect(&self, pm: &mut Pixmap, x: f32, y: f32, w: f32, h: f32, col: Color) {
        if let Some(rect) = tiny_skia::Rect::from_xywh(x, y, w, h) {
            let path = PathBuilder::from_rect(rect);
            let mut paint = Paint::default();
            paint.set_color(col);
            paint.anti_alias = true;
            pm.fill_path(&path, &paint, FillRule::Winding, Transform::identity(), None);
        }
    }

    /// Texto via fontdue; anchor: (centralizado?, baseline em y). Mistura alfa sobre o pixmap.
    fn text(&self, pm: &mut Pixmap, s: &str, x: f32, y: f32, size: f32, col: Color, bold: bool, center: bool) {
        let font = if bold { &self.font_bold } else { &self.font };
        // largura total p/ centralizar
        let total: f32 = s.chars().map(|c| font.metrics(c, size).advance_width).sum();
        let mut pen = if center { x - total / 2.0 } else { x };
        let data = pm.data_mut();
        let (pw, ph) = (W as i32, H as i32);
        let (cr, cg, cb) = (col.red(), col.green(), col.blue());
        for ch in s.chars() {
            let (mt, bitmap) = font.rasterize(ch, size);
            let gx = pen.round() as i32 + mt.xmin;
            let gy = y.round() as i32 - (mt.height as i32 + mt.ymin);
            for row in 0..mt.height as i32 {
                for colp in 0..mt.width as i32 {
                    let a = bitmap[(row * mt.width as i32 + colp) as usize] as f32 / 255.0;
                    if a <= 0.0 {
                        continue;
                    }
                    let (px, py) = (gx + colp, gy + row);
                    if px < 0 || py < 0 || px >= pw || py >= ph {
                        continue;
                    }
                    let idx = ((py * pw + px) * 4) as usize;
                    for (k, cc) in [cr, cg, cb].iter().enumerate() {
                        let bgv = data[idx + k] as f32;
                        data[idx + k] = (bgv + (cc.clamp(0.0, 1.0) * 255.0 - bgv) * a) as u8;
                    }
                    data[idx + 3] = 255;
                }
            }
            pen += mt.advance_width;
        }
    }
}

fn false_right() -> bool {
    false
}
fn temp_str(v: f32) -> String {
    if v.is_nan() { "--".into() } else { format!("{:.0}°", v) }
}
fn pct_str(v: f32) -> String {
    if v.is_nan() { "--".into() } else { format!("{:.0}%", v) }
}
fn bg_alpha(c: Color, a: u8) -> Color {
    Color::from_rgba8((c.red() * 255.0) as u8, (c.green() * 255.0) as u8, (c.blue() * 255.0) as u8, a)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sensors::Metrics;

    #[test]
    fn dump_frame() {
        let r = Renderer::new();
        let m = Metrics {
            cpu_temp: 61.0,
            gpu_temp: 54.0,
            cpu_load: 34.0,
            gpu_load: 72.0,
            ram_load: 48.0,
        };
        let pm = r.render(&m);
        let jpeg = to_jpeg(&pm, 0, 92);
        let out = std::env::temp_dir().join("digoo_rust_frame.jpg");
        std::fs::write(&out, &jpeg).unwrap();
        println!("quadro: {} bytes -> {}", jpeg.len(), out.display());
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
