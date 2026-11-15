//! Capa do álbum: decodifica o arquivo que o helper salvou (`media.cover`, JPEG ou PNG), recorta o
//! quadrado do meio e guarda em cache — refeito só quando a faixa muda. Decodificadores puros e enxutos
//! (jpeg-decoder/png) + redimensionamento/desfoque próprios (sem arrastar a árvore pesada do crate `image`).
//! Porte fiel de music.py `_cover`/`_cover_bg`: recorte central (ImageOps.fit) e fundo borrado+escurecido.

use std::collections::HashMap;
use std::sync::Mutex;
use tiny_skia::Pixmap;

const W: u32 = 480;
const H: u32 = 480;

/// Cache das capas já decodificadas (por caminho+tamanho) e do fundo borrado (por caminho).
pub struct Covers {
    fit: Mutex<HashMap<(String, u32), Option<Pixmap>>>,
    bg: Mutex<HashMap<String, Option<Pixmap>>>,
}

impl Covers {
    pub fn new() -> Covers {
        Covers { fit: Mutex::new(HashMap::new()), bg: Mutex::new(HashMap::new()) }
    }

    /// Capa quadrada `size`×`size` (recorte central), ou None se não houver/der erro. Clona do cache.
    pub fn fit(&self, path: &str, size: u32) -> Option<Pixmap> {
        if path.is_empty() || size == 0 {
            return None;
        }
        let mut cache = self.fit.lock().unwrap();
        if cache.len() > 24 {
            cache.clear(); // trocou de faixa várias vezes: solta o que ficou
        }
        cache
            .entry((path.to_string(), size))
            .or_insert_with(|| decode(path).and_then(|img| rgb_to_pixmap(&fit_square(&img, size), size)))
            .clone()
    }

    /// Fundo 480×480 da capa: borrado e escurecido (para a letra sobre a capa). Clona do cache.
    pub fn bg(&self, path: &str) -> Option<Pixmap> {
        if path.is_empty() {
            return None;
        }
        let mut cache = self.bg.lock().unwrap();
        if cache.len() > 4 {
            cache.clear();
        }
        cache.entry(path.to_string()).or_insert_with(|| decode(path).and_then(|img| make_bg(&img))).clone()
    }
}

/// Imagem RGB crua (3 bytes/pixel).
struct Rgb {
    w: usize,
    h: usize,
    px: Vec<u8>,
}

/// Lê o arquivo e decodifica pelo formato detectado nos primeiros bytes (JPEG ou PNG).
fn decode(path: &str) -> Option<Rgb> {
    let bytes = std::fs::read(path).ok()?;
    if bytes.len() < 4 {
        return None;
    }
    if bytes[..3] == [0xFF, 0xD8, 0xFF] {
        decode_jpeg(&bytes)
    } else if bytes[..4] == [0x89, 0x50, 0x4E, 0x47] {
        decode_png(&bytes)
    } else {
        None
    }
}

fn decode_jpeg(bytes: &[u8]) -> Option<Rgb> {
    let mut d = jpeg_decoder::Decoder::new(std::io::Cursor::new(bytes));
    let px = d.decode().ok()?;
    let info = d.info()?;
    let (w, h) = (info.width as usize, info.height as usize);
    match info.pixel_format {
        jpeg_decoder::PixelFormat::RGB24 => Some(Rgb { w, h, px }),
        jpeg_decoder::PixelFormat::L8 => {
            let mut rgb = vec![0u8; w * h * 3];
            for (i, &v) in px.iter().enumerate().take(w * h) {
                rgb[i * 3] = v;
                rgb[i * 3 + 1] = v;
                rgb[i * 3 + 2] = v;
            }
            Some(Rgb { w, h, px: rgb })
        }
        _ => None, // L16/CMYK são raros em miniaturas: cai no fallback (nota ♫)
    }
}

fn decode_png(bytes: &[u8]) -> Option<Rgb> {
    let mut dec = png::Decoder::new(std::io::Cursor::new(bytes));
    // expande paleta→RGB e reduz 16→8 bits, p/ sair sempre em 8 bits por canal
    dec.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = dec.read_info().ok()?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).ok()?;
    if info.bit_depth != png::BitDepth::Eight {
        return None;
    }
    let (w, h) = (info.width as usize, info.height as usize);
    let data = &buf[..info.buffer_size()];
    let px = match info.color_type {
        png::ColorType::Rgb => data.to_vec(),
        png::ColorType::Rgba => {
            let mut o = vec![0u8; w * h * 3];
            for i in 0..w * h {
                o[i * 3] = data[i * 4];
                o[i * 3 + 1] = data[i * 4 + 1];
                o[i * 3 + 2] = data[i * 4 + 2];
            }
            o
        }
        png::ColorType::Grayscale => {
            let mut o = vec![0u8; w * h * 3];
            for i in 0..w * h {
                o[i * 3] = data[i];
                o[i * 3 + 1] = data[i];
                o[i * 3 + 2] = data[i];
            }
            o
        }
        png::ColorType::GrayscaleAlpha => {
            let mut o = vec![0u8; w * h * 3];
            for i in 0..w * h {
                o[i * 3] = data[i * 2];
                o[i * 3 + 1] = data[i * 2];
                o[i * 3 + 2] = data[i * 2];
            }
            o
        }
        png::ColorType::Indexed => return None, // EXPAND deveria ter resolvido; se não, fallback
    };
    Some(Rgb { w, h, px })
}

/// Recorta o quadrado central e redimensiona p/ `size` por média de área (ImageOps.fit do Pillow).
fn fit_square(img: &Rgb, size: u32) -> Vec<u8> {
    let side = img.w.min(img.h).max(1);
    let (ox0, oy0) = ((img.w - side) / 2, (img.h - side) / 2);
    let s = size as usize;
    let mut out = vec![0u8; s * s * 3];
    for oy in 0..s {
        let sy0 = oy0 + oy * side / s;
        let sy1 = (oy0 + (oy + 1) * side / s).max(sy0 + 1).min(img.h);
        for ox in 0..s {
            let sx0 = ox0 + ox * side / s;
            let sx1 = (ox0 + (ox + 1) * side / s).max(sx0 + 1).min(img.w);
            let (mut r, mut g, mut b, mut n) = (0u32, 0u32, 0u32, 0u32);
            for yy in sy0..sy1 {
                for xx in sx0..sx1 {
                    let i = (yy * img.w + xx) * 3;
                    r += img.px[i] as u32;
                    g += img.px[i + 1] as u32;
                    b += img.px[i + 2] as u32;
                    n += 1;
                }
            }
            let n = n.max(1);
            let d = (oy * s + ox) * 3;
            out[d] = (r / n) as u8;
            out[d + 1] = (g / n) as u8;
            out[d + 2] = (b / n) as u8;
        }
    }
    out
}

/// RGB (opaco) → Pixmap do tiny-skia (pré-multiplicado; com alfa 255 é idêntico ao RGB direto).
fn rgb_to_pixmap(rgb: &[u8], size: u32) -> Option<Pixmap> {
    let mut pm = Pixmap::new(size, size)?;
    let data = pm.data_mut();
    for i in 0..(size * size) as usize {
        data[i * 4] = rgb[i * 3];
        data[i * 4 + 1] = rgb[i * 3 + 1];
        data[i * 4 + 2] = rgb[i * 3 + 2];
        data[i * 4 + 3] = 255;
    }
    Some(pm)
}

/// Fundo da letra_capa: capa pequena → desfoca → amplia p/ 480 → escurece 55% (fiel ao `_cover_bg`).
fn make_bg(img: &Rgb) -> Option<Pixmap> {
    let n = 64usize;
    let small = box_blur(&fit_square(img, n as u32), n, 3, 2);
    let mut pm = Pixmap::new(W, H)?;
    let data = pm.data_mut();
    let sc = n as f32 / W as f32;
    for y in 0..H as usize {
        let fy = (y as f32 + 0.5) * sc - 0.5;
        for x in 0..W as usize {
            let fx = (x as f32 + 0.5) * sc - 0.5;
            let (r, g, b) = bilinear(&small, n, fx, fy);
            let d = (y * W as usize + x) * 4;
            data[d] = (r * 0.45) as u8; // 55% rumo ao preto
            data[d + 1] = (g * 0.45) as u8;
            data[d + 2] = (b * 0.45) as u8;
            data[d + 3] = 255;
        }
    }
    Some(pm)
}

/// Desfoque de caixa separável (horizontal+vertical), `passes` vezes, raio `r`, sobre RGB n×n.
fn box_blur(src: &[u8], n: usize, r: usize, passes: usize) -> Vec<u8> {
    let mut a = src.to_vec();
    let mut b = vec![0u8; a.len()];
    for _ in 0..passes {
        blur_axis(&a, &mut b, n, r, true);
        blur_axis(&b, &mut a, n, r, false);
    }
    a
}

fn blur_axis(src: &[u8], dst: &mut [u8], n: usize, r: usize, horizontal: bool) {
    for y in 0..n {
        for x in 0..n {
            let (mut rr, mut gg, mut bb, mut c) = (0u32, 0u32, 0u32, 0u32);
            let p = if horizontal { x } else { y };
            let lo = p.saturating_sub(r);
            let hi = (p + r).min(n - 1);
            for q in lo..=hi {
                let (xx, yy) = if horizontal { (q, y) } else { (x, q) };
                let i = (yy * n + xx) * 3;
                rr += src[i] as u32;
                gg += src[i + 1] as u32;
                bb += src[i + 2] as u32;
                c += 1;
            }
            let i = (y * n + x) * 3;
            dst[i] = (rr / c) as u8;
            dst[i + 1] = (gg / c) as u8;
            dst[i + 2] = (bb / c) as u8;
        }
    }
}

/// Amostra bilinear de um RGB n×n em (fx,fy).
fn bilinear(buf: &[u8], n: usize, fx: f32, fy: f32) -> (f32, f32, f32) {
    let x0 = fx.floor().clamp(0.0, (n - 1) as f32) as usize;
    let y0 = fy.floor().clamp(0.0, (n - 1) as f32) as usize;
    let (x1, y1) = ((x0 + 1).min(n - 1), (y0 + 1).min(n - 1));
    let tx = (fx - x0 as f32).clamp(0.0, 1.0);
    let ty = (fy - y0 as f32).clamp(0.0, 1.0);
    let s = |xx: usize, yy: usize, k: usize| buf[(yy * n + xx) * 3 + k] as f32;
    let lerp = |a: f32, b: f32, t: f32| a + (b - a) * t;
    let mut out = [0f32; 3];
    for (k, o) in out.iter_mut().enumerate() {
        let top = lerp(s(x0, y0, k), s(x1, y0, k), tx);
        let bot = lerp(s(x0, y1, k), s(x1, y1, k), tx);
        *o = lerp(top, bot, ty);
    }
    (out[0], out[1], out[2])
}

/// Aplica uma máscara de cantos arredondados (raio `radius`) a uma capa já pré-multiplicada.
/// `radius == size/2` dá um círculo (etiqueta do vinil, pílula). Suaviza a borda em ~1 px.
pub fn mask_round(pm: &mut Pixmap, radius: f32) {
    let (w, h) = (pm.width() as f32, pm.height() as f32);
    let r = radius.min(w / 2.0).min(h / 2.0).max(0.0);
    let data = pm.data_mut();
    for y in 0..h as u32 {
        for x in 0..w as u32 {
            let cov = round_cov(x as f32 + 0.5, y as f32 + 0.5, w, h, r);
            if cov < 0.999 {
                let i = ((y * w as u32 + x) * 4) as usize;
                for k in 0..4 {
                    data[i + k] = (data[i + k] as f32 * cov) as u8;
                }
            }
        }
    }
}

/// Cobertura 0..1 de um ponto dentro de um retângulo (0,0)-(w,h) com cantos de raio `r` (AA de ~1 px).
fn round_cov(px: f32, py: f32, w: f32, h: f32, r: f32) -> f32 {
    let cx = (r - px).max(px - (w - r)).max(0.0);
    let cy = (r - py).max(py - (h - r)).max(0.0);
    let d = (cx * cx + cy * cy).sqrt() - r;
    (0.5 - d).clamp(0.0, 1.0)
}
