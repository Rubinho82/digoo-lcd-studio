# Digoo LCD — núcleo nativo em Rust (experimental)

Reescrita da **parte quente** do Digoo LCD Studio em Rust, focada em **pouca memória e velocidade**
(a mesma ideia que a Discord usou: reescrever o que roda o tempo todo, não o app inteiro de uma vez).

## Estado: fase 1 (base funcionando)

Já funciona de ponta a ponta:
- acha a tela pelo USB (`33c3:7791/7788/7792`) e fala o protocolo HONGTAI (bytes idênticos ao Python — há testes);
- lê os sensores reaproveitando o **mesmo `sensorhelper.exe` (C#/LibreHardwareMonitor)** do app Python
  (refazer em Rust puro pioraria a cobertura de sensores) + RAM via Windows;
- desenha um painel (tiny-skia + fontdue) e envia em **JPEG 4:2:0** (exigência da tela), com keepalive e recuperação.

### Medições (nesta máquina)
| | app Python (bandeja) | núcleo Rust |
|---|---|---|
| RAM (working set) | ~53 MB | **~3,7 MB** |
| executável | 4 MB + ~264 MB de libs | **784 KB** |
| quadros | — | ~2 q/s, 24 KB cada |

O `sensorhelper.exe` (C#) é o mesmo nos dois (~37 MB), custo compartilhado.

## Falta (roadmap)
1. Portar os **20 temas** e os **14 modelos de música** (hoje só há um painel simples).
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
