# Digoo LCD Studio

Software livre para a **telinha LCD de watercoolers** que usam o painel **HONGTAI / LOVINGCOOL "MONITOR"**
(USB `33c3:7791`, também `7788`/`7792`) — vendido com várias marcas (Digoo, LovingCool, Delta…). Feito porque o
cooler vem sem programa (ou com um programa fraco). Windows 10/11.

> *Free software for the LCD screen on AIO coolers using the HONGTAI/LOVINGCOOL panel (USB 33c3:7791).
> Sensor dashboards, GIFs/videos, Wallpaper Engine Workshop, music visualizer. UI in Portuguese.*

## O que faz

| | |
|---|---|
| **Painel de sensores** | 12 temas: Anéis, Neon, Grade 6, Velocímetro, Gráficos, Minimalista, Relógio, Compacto, Terminal retrô, HUD cyberpunk, Barras verticais, Dígitos de LED |
| **Personalizar** | cores (ou paletas prontas), 6 fontes, °C/°F, relógio (hora, segundos, data, dia da semana), título opcional, imagem de fundo |
| **Fotos, GIFs e vídeos** | playlist com velocidade ajustável; vídeos/GIFs convertidos uma vez em quadros prontos (tocar ≈ 1% de CPU) |
| **Biblioteca** | **Workshop do Wallpaper Engine** (navega, assina e baixa pela sua Steam, sem senha), GIPHY/Tenor (chave grátis), anime, Wikimedia; prévia animada antes de baixar; 8 efeitos animados gerados ao vivo |
| **Tema por cima da mídia** | qualquer tema por cima do vídeo/foto, com transparência e escurecimento |
| **Camadas** | mover e redimensionar o tema e a música (arrastar na prévia, rodinha = tamanho) ou modos prontos: encaixe automático, música em pílula |
| **Música** | "tocando agora" do Windows (Spotify, YouTube no navegador…) com capa, título e tempo + ondas do som do PC (sem microfone); 8 modelos: Capa central, Disco de vinil, Cartão, Minimalista, Espectro gigante, Onda neon, Polaroide, Rádio antigo; cores automáticas da capa |
| **Espelhar tela** | um monitor inteiro ou uma região |
| **Sensores** | CPU, GPU (NVIDIA/AMD/Intel), RAM, VRAM, SSDs, rede, ventoinhas — via LibreHardwareMonitor; ventoinha da GPU controlável (volta ao automático ao sair, 100% se passar de 83 °C) |
| **Sistema** | bandeja, início com o Windows sem UAC (tarefa agendada), modo noturno, brilho por software, reconexão automática |

A temperatura da CPU e as ventoinhas da placa-mãe precisam do driver gratuito
[PawnIO](https://github.com/namazso/PawnIO.Setup) (botão na aba Sensores).

## Instalar / compilar

Precisa de **Python 3.10+**, **.NET SDK 8** e as libs do `requirements.txt`:

```bash
build.bat
```

Gera `dist\DigooLCD\DigooLCD.exe`. Para rodar do código: `python DigooLCD.pyw`.
Diagnóstico: `DigooLCD.exe --diagnostico` (grava `%APPDATA%\DigooLCD\diagnostico.txt`).

## Estrutura

| Arquivo | Função |
|---|---|
| `digoo_lcd/protocol.py` | protocolo serial da tela |
| `digoo_lcd/engine.py` | motor: conecta, desenha, camadas, envia, mantém a tela no modo ao vivo |
| `digoo_lcd/render.py` / `themes_extra.py` | temas do painel (e camadas transparentes) |
| `digoo_lcd/music.py` | espectro, "tocando agora" e modelos do tema Música |
| `digoo_lcd/sources.py` / `videocache.py` | playlist, espelho, quadros prontos em cache |
| `digoo_lcd/library.py` / `steam_ws.py` | biblioteca online e Workshop do Wallpaper Engine |
| `digoo_lcd/gui.py` / `gui_library.py` | interface (PySide6) |
| `sensorhelper/` | leitor .NET: sensores (LibreHardwareMonitor), som do PC (NAudio) e mídia do Windows |

## Protocolo (resumo)

- Comando: `55 AA | len(2, LE) | opcode | payload | soma(2, LE)`, `len = payload + 7`.
  `0x06` info (JSON), `0x11` keepalive (a cada ~1,5 s), `0x03` brilho, `0x21` fechar.
- Quadro (fw > 2.8): `len_jpeg(4, LE) | jpeg | soma(2, LE)`.
- **Só JPEG 4:2:0** — o decodificador da tela mostra 4:4:4 pixelado.
- O painel TXW818-ST7701S 4" (fw ≤ 3.1) engasga com quadros grandes: até 50 KB e 25 fps.
- Neste modelo a luz de fundo ignora o `0x03`; o brilho é feito escurecendo a imagem.
- `FF D9 FF D9 00 00 00 00` aborta um quadro pela metade.

## Créditos

Protocolo baseado na engenharia reversa de [GOG1071/hongtai-panel](https://github.com/GOG1071/hongtai-panel).
Sensores: [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) (MPL-2.0).
Áudio: [NAudio](https://github.com/naudio/NAudio). Fontes Orbitron, Rajdhani, Exo 2 e Share Tech Mono (SIL OFL).
O Workshop usa a `steam_api64.dll` da sua instalação do Wallpaper Engine (não é distribuída aqui).
Projeto independente, sem ligação com Digoo, HONGTAI, Valve ou Wallpaper Engine.

Licença: MIT.
