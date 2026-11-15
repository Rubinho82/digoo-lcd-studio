//! Fontes de mídia no núcleo (caminho quente): toca `.djf` (vídeo/GIF já convertidos), mostra imagem
//! estática e espelha a tela. A conversão pesada (ffmpeg → .djf) continua no app Python; aqui só se *toca*.
//! Porte fiel de sources.py (`_Frames`/`_Still`/`ScreenMirror`) e do formato de videocache.py.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use tiny_skia::Pixmap;

const W: usize = 480;
const H: usize = 480;

// ----------------------------------------------------------------------
// .djf — quadros prontos (JPEGs já girados e 4:2:0): b"DJF1" + u16 w,h,rot,fps, depois
// [u32 pts_ms][u32 tam][jpeg] por quadro. Tocar = achar o quadro pela hora e mandar os bytes como estão.
// ----------------------------------------------------------------------
pub struct FrameFile {
    f: File,
    pub rot: u16,
    pub fps: u16,
    pts: Vec<u32>,
    off: Vec<u64>,
    size: Vec<u32>,
    pub duration: f32,
}

impl FrameFile {
    pub fn open(path: &str) -> std::io::Result<FrameFile> {
        let err = |m: &str| std::io::Error::new(std::io::ErrorKind::InvalidData, m.to_string());
        let mut f = File::open(path)?;
        let mut head = [0u8; 12];
        f.read_exact(&mut head)?;
        if &head[..4] != b"DJF1" {
            return Err(err("arquivo de quadros inválido"));
        }
        let fps = u16::from_le_bytes([head[10], head[11]]);
        let rot = u16::from_le_bytes([head[8], head[9]]);
        let (mut pts, mut off, mut size) = (Vec::new(), Vec::new(), Vec::new());
        let mut pos: u64 = 12;
        loop {
            let mut hdr = [0u8; 8];
            if f.read_exact(&mut hdr).is_err() {
                break; // fim do arquivo (ou cabeçalho parcial): acabou
            }
            let p = u32::from_le_bytes([hdr[0], hdr[1], hdr[2], hdr[3]]);
            let s = u32::from_le_bytes([hdr[4], hdr[5], hdr[6], hdr[7]]);
            pos += 8;
            pts.push(p);
            off.push(pos);
            size.push(s);
            pos += s as u64;
            if f.seek(SeekFrom::Start(pos)).is_err() {
                break;
            }
        }
        if pts.is_empty() {
            return Err(err("arquivo de quadros vazio"));
        }
        let duration = (pts[pts.len() - 1] as f32 + 1000.0 / fps.max(1) as f32) / 1000.0;
        Ok(FrameFile { f, rot, fps, pts, off, size, duration })
    }

    /// Índice do quadro a mostrar na hora `t` (s): maior pts <= t (bisect_right - 1).
    pub fn index_at(&self, t: f32) -> usize {
        let key = (t * 1000.0).max(0.0) as u32;
        self.pts.partition_point(|&p| p <= key).saturating_sub(1)
    }

    pub fn read(&mut self, i: usize) -> std::io::Result<Vec<u8>> {
        self.f.seek(SeekFrom::Start(self.off[i]))?;
        let mut buf = vec![0u8; self.size[i] as usize];
        self.f.read_exact(&mut buf)?;
        Ok(buf)
    }

    pub fn len(&self) -> usize {
        self.pts.len()
    }
}

/// Caminho do .djf que o app Python gera p/ este vídeo/GIF (mesmo sha1 do videocache.py:
/// `resolve|tamanho|mtime_ns|fit|rot|92|v3`). O arquivo pode não existir ainda (a GUI é quem converte).
pub fn djf_cache_path(src: &str, fit: &str, rotation: u32) -> Option<std::path::PathBuf> {
    let meta = std::fs::metadata(src).ok()?;
    let mtime_ns = meta.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?.as_nanos();
    let raw = format!("{}|{}|{}|{}|{}|92|v3", src, meta.len(), mtime_ns, fit, rotation % 360);
    let dir = crate::lyrics::cache_dir().parent()?.join("video");
    Some(dir.join(format!("{}.djf", &crate::lyrics::sha1_hex(&raw)[..24])))
}

// ----------------------------------------------------------------------
// imagem estática e espelho → Pixmap 480×480 (o main gira 90° e comprime como nos temas)
// ----------------------------------------------------------------------
/// Imagem estática (JPEG/PNG) ajustada a 480×480 (`cover` corta as bordas; `contain` deixa tarja preta).
pub fn image_pixmap(path: &str, fit: &str) -> Option<Pixmap> {
    let (sw, sh, px) = crate::cover::load_rgb(path)?;
    rgb480_to_pixmap(&fit_rgb(&px, sw, sh, fit))
}

/// Espelho da tela principal ajustado a 480×480 (ou None se a captura falhar).
pub fn mirror_pixmap(fit: &str) -> Option<Pixmap> {
    let (sw, sh, px) = capture_screen()?;
    rgb480_to_pixmap(&fit_rgb(&px, sw, sh, fit))
}

fn rgb480_to_pixmap(rgb: &[u8]) -> Option<Pixmap> {
    let mut pm = Pixmap::new(W as u32, H as u32)?;
    let data = pm.data_mut();
    for i in 0..W * H {
        data[i * 4] = rgb[i * 3];
        data[i * 4 + 1] = rgb[i * 3 + 1];
        data[i * 4 + 2] = rgb[i * 3 + 2];
        data[i * 4 + 3] = 255;
    }
    Some(pm)
}

/// Ajusta um RGB (sw×sh) para 480×480: `contain` = cabe inteiro com tarja preta; senão cobre e corta o meio.
pub fn fit_rgb(src: &[u8], sw: usize, sh: usize, fit: &str) -> Vec<u8> {
    let mut out = vec![0u8; W * H * 3];
    if sw == 0 || sh == 0 {
        return out;
    }
    let (fw, fh) = (W as f32, H as f32);
    let k = if fit == "contain" {
        (fw / sw as f32).min(fh / sh as f32)
    } else {
        (fw / sw as f32).max(fh / sh as f32)
    };
    let nw = ((sw as f32 * k).round() as usize).max(2);
    let nh = ((sh as f32 * k).round() as usize).max(2);
    let scaled = resize_area(src, sw, sh, nw, nh);
    if fit == "contain" {
        let (ox, oy) = ((W.saturating_sub(nw)) / 2, (H.saturating_sub(nh)) / 2);
        let (cw, ch) = (nw.min(W), nh.min(H));
        for y in 0..ch {
            for x in 0..cw {
                let s = (y * nw + x) * 3;
                let d = ((oy + y) * W + ox + x) * 3;
                out[d..d + 3].copy_from_slice(&scaled[s..s + 3]);
            }
        }
    } else {
        let (x0, y0) = ((nw - W) / 2, (nh - H) / 2);
        for y in 0..H {
            for x in 0..W {
                let s = ((y0 + y) * nw + x0 + x) * 3;
                let d = (y * W + x) * 3;
                out[d..d + 3].copy_from_slice(&scaled[s..s + 3]);
            }
        }
    }
    out
}

/// Redimensiona RGB (sw×sh) → (dw×dh) por média de área (nítido ao reduzir; vizinho ao ampliar).
fn resize_area(src: &[u8], sw: usize, sh: usize, dw: usize, dh: usize) -> Vec<u8> {
    let mut out = vec![0u8; dw * dh * 3];
    for dy in 0..dh {
        let sy0 = dy * sh / dh;
        let sy1 = ((dy + 1) * sh / dh).max(sy0 + 1).min(sh);
        for dx in 0..dw {
            let sx0 = dx * sw / dw;
            let sx1 = ((dx + 1) * sw / dw).max(sx0 + 1).min(sw);
            let (mut r, mut g, mut b, mut n) = (0u32, 0u32, 0u32, 0u32);
            for yy in sy0..sy1 {
                for xx in sx0..sx1 {
                    let i = (yy * sw + xx) * 3;
                    r += src[i] as u32;
                    g += src[i + 1] as u32;
                    b += src[i + 2] as u32;
                    n += 1;
                }
            }
            let n = n.max(1);
            let d = (dy * dw + dx) * 3;
            out[d] = (r / n) as u8;
            out[d + 1] = (g / n) as u8;
            out[d + 2] = (b / n) as u8;
        }
    }
    out
}

// ----------------------------------------------------------------------
// captura de tela (GDI do Windows) — monitor principal, devolve (largura, altura, RGB)
// ----------------------------------------------------------------------
#[repr(C)]
struct BitmapInfoHeader {
    size: u32,
    width: i32,
    height: i32,
    planes: u16,
    bit_count: u16,
    compression: u32,
    size_image: u32,
    x_ppm: i32,
    y_ppm: i32,
    clr_used: u32,
    clr_important: u32,
}

#[link(name = "user32")]
extern "system" {
    fn GetDC(hwnd: isize) -> isize;
    fn ReleaseDC(hwnd: isize, dc: isize) -> i32;
    fn GetSystemMetrics(index: i32) -> i32;
}
#[link(name = "gdi32")]
extern "system" {
    fn CreateCompatibleDC(dc: isize) -> isize;
    fn DeleteDC(dc: isize) -> i32;
    fn CreateCompatibleBitmap(dc: isize, w: i32, h: i32) -> isize;
    fn DeleteObject(o: isize) -> i32;
    fn SelectObject(dc: isize, o: isize) -> isize;
    fn BitBlt(dc: isize, x: i32, y: i32, w: i32, h: i32, src: isize, sx: i32, sy: i32, rop: u32) -> i32;
    fn GetDIBits(dc: isize, bmp: isize, start: u32, lines: u32, bits: *mut u8, bmi: *mut BitmapInfoHeader, usage: u32) -> i32;
}

const SM_CXSCREEN: i32 = 0;
const SM_CYSCREEN: i32 = 1;
const SRCCOPY: u32 = 0x00CC_0020;

/// Captura o monitor principal em RGB (converte do BGRA do GDI). None se algo falhar.
pub fn capture_screen() -> Option<(usize, usize, Vec<u8>)> {
    unsafe {
        let (w, h) = (GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN));
        if w <= 0 || h <= 0 {
            return None;
        }
        let (wu, hu) = (w as usize, h as usize);
        let screen = GetDC(0);
        if screen == 0 {
            return None;
        }
        let mem = CreateCompatibleDC(screen);
        let bmp = CreateCompatibleBitmap(screen, w, h);
        let mut rgb: Option<Vec<u8>> = None;
        if mem != 0 && bmp != 0 {
            let old = SelectObject(mem, bmp);
            if BitBlt(mem, 0, 0, w, h, screen, 0, 0, SRCCOPY) != 0 {
                let mut bmi = BitmapInfoHeader {
                    size: 40,
                    width: w,
                    height: -h, // negativo = de cima p/ baixo (ordem natural das linhas)
                    planes: 1,
                    bit_count: 32,
                    compression: 0, // BI_RGB
                    size_image: 0,
                    x_ppm: 0,
                    y_ppm: 0,
                    clr_used: 0,
                    clr_important: 0,
                };
                let mut bgra = vec![0u8; wu * hu * 4];
                if GetDIBits(mem, bmp, 0, h as u32, bgra.as_mut_ptr(), &mut bmi, 0) != 0 {
                    let mut out = vec![0u8; wu * hu * 3];
                    for i in 0..wu * hu {
                        out[i * 3] = bgra[i * 4 + 2]; // R
                        out[i * 3 + 1] = bgra[i * 4 + 1]; // G
                        out[i * 3 + 2] = bgra[i * 4]; // B
                    }
                    rgb = Some(out);
                }
            }
            SelectObject(mem, old);
        }
        if bmp != 0 {
            DeleteObject(bmp);
        }
        if mem != 0 {
            DeleteDC(mem);
        }
        ReleaseDC(0, screen);
        rgb.map(|v| (wu, hu, v))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jpeg_encoder::{ColorType, Encoder, SamplingFactor};

    fn solid_jpeg(r: u8, g: u8, b: u8) -> Vec<u8> {
        let px: Vec<u8> = (0..16 * 16).flat_map(|_| [r, g, b]).collect();
        let mut buf = Vec::new();
        let mut enc = Encoder::new(&mut buf, 90);
        enc.set_sampling_factor(SamplingFactor::R_4_2_0);
        enc.encode(&px, 16, 16, ColorType::Rgb).unwrap();
        buf
    }

    #[test]
    fn djf_round_trip() {
        // monta um .djf com 3 quadros (0, 100, 200 ms) e confere o índice por tempo + a leitura
        let frames = [solid_jpeg(200, 20, 20), solid_jpeg(20, 200, 20), solid_jpeg(20, 20, 200)];
        let mut data = Vec::new();
        data.extend_from_slice(b"DJF1");
        data.extend_from_slice(&480u16.to_le_bytes());
        data.extend_from_slice(&480u16.to_le_bytes());
        data.extend_from_slice(&90u16.to_le_bytes()); // rot
        data.extend_from_slice(&10u16.to_le_bytes()); // fps
        for (i, fr) in frames.iter().enumerate() {
            data.extend_from_slice(&((i as u32) * 100).to_le_bytes()); // pts ms
            data.extend_from_slice(&(fr.len() as u32).to_le_bytes());
            data.extend_from_slice(fr);
        }
        let path = std::env::temp_dir().join("digoo_test.djf");
        std::fs::write(&path, &data).unwrap();

        let mut ff = FrameFile::open(path.to_str().unwrap()).unwrap();
        assert_eq!(ff.len(), 3);
        assert_eq!(ff.rot, 90);
        assert_eq!(ff.fps, 10);
        assert_eq!(ff.index_at(0.0), 0);
        assert_eq!(ff.index_at(0.05), 0);
        assert_eq!(ff.index_at(0.15), 1);
        assert_eq!(ff.index_at(0.25), 2);
        assert_eq!(ff.index_at(99.0), 2); // passou do fim → último
        assert_eq!(ff.read(1).unwrap(), frames[1]); // bytes exatos do 2º quadro
        assert!((ff.duration - 0.3).abs() < 0.01); // 200 ms + 1 quadro a 10 fps
    }

    #[test]
    fn djf_cache_path_do_env() {
        // rode com DIGOO_TEST_VIDEO=<caminho> p/ conferir o hash contra o do Python (senão não faz nada)
        if let Ok(p) = std::env::var("DIGOO_TEST_VIDEO") {
            println!("djf cache p/ {p}: {:?}", djf_cache_path(&p, "cover", 90));
        }
    }

    #[test]
    fn fit_dump() {
        // imagem sintética 800×300 (degradê) → cover (corta) e contain (tarja) → folha 2×1 p/ conferir
        let (sw, sh) = (800usize, 300usize);
        let mut img = vec![0u8; sw * sh * 3];
        for y in 0..sh {
            for x in 0..sw {
                let d = (y * sw + x) * 3;
                img[d] = (x * 255 / sw) as u8;
                img[d + 1] = (y * 255 / sh) as u8;
                img[d + 2] = 180;
            }
        }
        let cover = fit_rgb(&img, sw, sh, "cover");
        let contain = fit_rgb(&img, sw, sh, "contain");
        let mut sheet = vec![0u8; 960 * 480 * 3];
        for y in 0..480 {
            for x in 0..480 {
                let s = (y * 480 + x) * 3;
                sheet[(y * 960 + x) * 3..(y * 960 + x) * 3 + 3].copy_from_slice(&cover[s..s + 3]);
                sheet[(y * 960 + 480 + x) * 3..(y * 960 + 480 + x) * 3 + 3].copy_from_slice(&contain[s..s + 3]);
            }
        }
        let mut buf = Vec::new();
        let mut enc = Encoder::new(&mut buf, 92);
        enc.set_sampling_factor(SamplingFactor::R_4_2_0);
        enc.encode(&sheet, 960, 480, ColorType::Rgb).unwrap();
        let out = std::env::temp_dir().join("digoo_media_fit.jpg");
        std::fs::write(&out, &buf).unwrap();
        println!("fit cover|contain -> {}", out.display());
    }
}
