# Digoo LCD — núcleo nativo em Rust (experimental)

Reescrita da **parte quente** do Digoo LCD Studio em Rust, focada em **pouca memória e velocidade**
(a mesma ideia que a Discord usou: reescrever o que roda o tempo todo, não o app inteiro de uma vez).

## Estado: fase 6 (dirigido pelo config.json da GUI — temas, música, mídia, espelho)

Já funciona de ponta a ponta:
- acha a tela pelo USB (`33c3:7791/7788/7792`) e fala o protocolo HONGTAI (bytes idênticos ao Python — há testes);
- lê os sensores reaproveitando o **mesmo `sensorhelper.exe` (C#/LibreHardwareMonitor)** do app Python —
  CPU/GPU temperatura·uso·clock·potência, VRAM, hot-spot e disco, escolhendo NVIDIA vs iGPU AMD como o app faz;
  RAM via `GlobalMemoryStatusEx` e **rede via `GetIfTable`** (o helper C# não emite rede, igual ao psutil no Python);
- desenha os **20 temas** (tiny-skia + fontdue, escolhidos por argumento) e envia em **JPEG 4:2:0** (exigência da
  tela), com keepalive e recuperação;
- **tema Música** (`digoo-lcd musica-<modelo>`): espectro do som do PC (48 bandas via `--audio` do helper) +
  "tocando agora", em **14 modelos** (capa, vinil, cartão, minimal, espectro, neon, cassete, aparelho, pixel,
  ondas, rádio, polaroide, letra, letra sobre a capa) — anima a ~25 q/s, com a **capa do álbum** real que o
  player fornece (decodifica JPEG/PNG; recorte quadrado + fundo borrado como no app Python);
- **letras sincronizadas** (karaokê): busca na **LRCLIB** (`ureq`, sem chave) numa thread, guarda no disco
  (`%APPDATA%\DigooLCD\cache\lyrics`, os mesmos arquivos do app Python) e pinta a linha atual conforme é cantada;
- **tradução das letras** (opcional): MyMemory (sem chave) ou DeepL (com chave), numa thread e em cache no disco;
  ligue com `DIGOO_TRADUZIR=pt` (ou en/es/fr/it/de/ja/ko) — `DIGOO_TRADUTOR=deepl` + `DIGOO_DEEPL_KEY=…` usa o DeepL;
- **fontes de mídia**: toca **vídeo/GIF** já convertido (`digoo-lcd video <arquivo.djf>` — o Python prepara o `.djf`
  com ffmpeg, o núcleo só lê e manda os quadros prontos, já girados e 4:2:0), mostra **imagem**
  (`digoo-lcd img <arquivo> [cover|contain]`, JPEG/PNG) e **espelha a tela** (`digoo-lcd espelho [cover|contain]`,
  captura por GDI a ~12 q/s);
- **dirigido pelo `config.json`** (sem argumento, só `digoo-lcd`): lê o que a GUI escreve em
  `%APPDATA%\DigooLCD\config.json` e **segue ao vivo** — modo (painel/mídia/espelho), tema ou música, cores,
  giro, FPS, brilho (com agenda noturna) e tradução; quando a GUI salva, o núcleo relê e troca na hora. (Em
  mídia toca imagens — vídeo cai no tema/música configurado, use `video <.djf>` p/ tocar vídeo; espelho = monitor
  principal; a chave do DeepL no config fica cifrada por DPAPI, então para o DeepL use `DIGOO_DEEPL_KEY` por ora.)

### Medições (nesta máquina)
| | app Python (bandeja) | núcleo Rust |
|---|---|---|
| RAM (working set) | ~53 MB | **~3,7 MB** |
| executável | 4 MB + ~264 MB de libs | **~3 MB** |
| quadros | — | ~2 q/s, 24 KB cada |

O executável embute a pilha TLS (`ureq`/rustls, p/ letras e tradução) e os decodificadores de capa (JPEG/PNG);
ainda assim é uma fração das ~264 MB de bibliotecas do app Python, e a **RAM em uso** (o que importa) continua
minúscula. O `sensorhelper.exe` (C#) é o mesmo nos dois (~37 MB), custo compartilhado.

## Falta (roadmap)
1. **Tema Música completo** (feito): os 14 modelos, a capa de álbum real, as letras sincronizadas com cache em
   disco e a tradução (MyMemory/DeepL). Falta só a tradução pela IA (Claude) e as "cores automáticas" da capa.
2. **Vídeo/GIF, imagem e espelho de tela** (feito): o núcleo lê o `.djf` pronto, mostra imagens e espelha a tela.
   Falta só a **biblioteca online** (GIPHY/Tenor/anime/Wikimedia/Workshop) — mas isso é da GUI, que baixa os
   arquivos que o núcleo então toca.
3. **A interface Python fala com o núcleo** (feito): ele lê o `config.json` e segue ao vivo. Falta: decifrar as
   chaves de API (DPAPI) do config, tocar vídeo direto do config (resolver o `.djf` do cache) e espelhar outros
   monitores além do principal. Uma GUI própria em egui continua opcional.
4. Resolução/giro adaptáveis a outras telas.

O app Python continua sendo o completo e estável; este núcleo é a fundação da versão enxuta.

## Compilar / rodar
```
cargo test            # testes do protocolo
cargo build --release # gera target/release/digoo-lcd.exe (~3 MB)
target/release/digoo-lcd.exe
```
Precisa de Rust (edition 2021) e, para os sensores, do `sensorhelper.exe` ao lado do exe
(`bin/sensorhelper/`) ou da instalação do app Python.
