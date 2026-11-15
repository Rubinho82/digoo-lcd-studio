# Digoo LCD — núcleo nativo em Rust (experimental)

Reescrita da **parte quente** do Digoo LCD Studio em Rust, focada em **pouca memória e velocidade**
(a mesma ideia que a Discord usou: reescrever o que roda o tempo todo, não o app inteiro de uma vez).

## Estado: fase 4 (20 temas + sensores + tema Música)

Já funciona de ponta a ponta:
- acha a tela pelo USB (`33c3:7791/7788/7792`) e fala o protocolo HONGTAI (bytes idênticos ao Python — há testes);
- lê os sensores reaproveitando o **mesmo `sensorhelper.exe` (C#/LibreHardwareMonitor)** do app Python —
  CPU/GPU temperatura·uso·clock·potência, VRAM, hot-spot e disco, escolhendo NVIDIA vs iGPU AMD como o app faz;
  RAM via `GlobalMemoryStatusEx` e **rede via `GetIfTable`** (o helper C# não emite rede, igual ao psutil no Python);
- desenha os **20 temas** (tiny-skia + fontdue, escolhidos por argumento) e envia em **JPEG 4:2:0** (exigência da
  tela), com keepalive e recuperação;
- **tema Música** (`digoo-lcd musica-<modelo>`): espectro do som do PC (48 bandas via `--audio` do helper) +
  "tocando agora", em **12 modelos** (capa, vinil, cartão, minimal, espectro, neon, cassete, aparelho, pixel,
  ondas, rádio, polaroide) — anima a ~25 q/s.

### Medições (nesta máquina)
| | app Python (bandeja) | núcleo Rust |
|---|---|---|
| RAM (working set) | ~53 MB | **~3,7 MB** |
| executável | 4 MB + ~264 MB de libs | **784 KB** |
| quadros | — | ~2 q/s, 24 KB cada |

O `sensorhelper.exe` (C#) é o mesmo nos dois (~37 MB), custo compartilhado.

## Falta (roadmap)
1. Já portados: os **20 temas**, os **sensores** e **12 dos 14 modelos de música**. Faltam: os **2 modelos de
   letra** (letra/letra_capa, que dependem das **letras** via LRCLIB), a **tradução** (MyMemory/DeepL) e as
   **capas de álbum** (decodificar JPEG). Letras e tradução precisam de um cliente HTTP.
2. Letras/tradução, biblioteca online, vídeo/GIF, espelho.
3. Interface de configuração (provável: egui, leve) — ou manter a interface Python falando com este núcleo.
4. Resolução/giro adaptáveis a outras telas.

O app Python continua sendo o completo e estável; este núcleo é a fundação da versão enxuta.

## Compilar / rodar
```
cargo test            # testes do protocolo
cargo build --release # gera target/release/digoo-lcd.exe (~784 KB)
target/release/digoo-lcd.exe
```
Precisa de Rust (edition 2021) e, para os sensores, do `sensorhelper.exe` ao lado do exe
(`bin/sensorhelper/`) ou da instalação do app Python.
