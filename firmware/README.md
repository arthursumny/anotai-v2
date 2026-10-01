# AnotAI Lapela — Firmware (Seeed Studio XIAO BLE Sense, nRF52840)

Servidor GATT BLE que captura o microfone PDM de bordo (16 kHz, PCM16 mono)
e transmite chunks para o app mobile via notificações BLE.

## Hardware

- Placa: Seeed Studio XIAO BLE Sense (nRF52840 + mic PDM + IMU)
- Botão externo (NÃO há botão na placa): ligue entre **D2 e GND** (ativo em LOW,
  pull-up interno; troque `BUTTON_PIN` em `src/main.cpp` se usar outro pino)
- LED embutido (`LED_BUILTIN`): blink lento = IDLE, aceso = RECORDING,
  blink rápido = SENDING

## Protocolo BLE (espelhado em `apps/mobile/src/ble/constants.ts`)

| Item | UUID | Propriedades |
|---|---|---|
| Serviço `ANOTAI_AUDIO_SERVICE_UUID` | `4fafc201-1fb5-459e-8fcc-c5c9c331914b` | — |
| `AUDIO_DATA_CHAR_UUID` | `4fafc202-…` | NOTIFY |
| `STATE_CHAR_UUID` (`0`=IDLE, `1`=RECORDING, `2`=SENDING) | `4fafc203-…` | READ, NOTIFY |

Pacote `AUDIO_DATA` (little-endian): `[seq u16][flags u8][reservado u8][PCM…]`
- `flags & 0x01 (LAST)` = último pacote da gravação
- Payload de até 200 bytes de PCM16LE mono 16 kHz
- O app reordena por `seq`, preenche falhas com silêncio e monta o WAV

> **MTU:** os pacotes têm ~204 bytes; a central deve negociar MTU ≥ 247
> (`react-native-ble-plx` faz isso no `connect` + `discoverAllServices`).
> Se a central só suportar 23 bytes, os pacotes serão truncados — o `seq`
> permite ao app detectar a perda.

## Compilar e gravar (PlatformIO)

```bash
# 1. Instale o VS Code + extensão PlatformIO (ou: pip install platformio)
# 2. Conecte a XIAO via USB-C (porta de dados!) e selecione a porta:
pio run -t upload
# ou com porta explícita:
pio run -t upload --upload-port /dev/ttyACM0
```

Monitor serial (logs + debug):

```bash
pio device monitor -b 115200
```

> Build validado com `pio run`: `firmware.elf`/`firmware.hex` gerados para
> `seeed_xiao_ble_sense` (RAM ~30%, Flash ~39%). O primeiro build baixa a
> plataforma, o toolchain ARM e a ArduinoBLE (~5–10 min).

## Alternativa: Arduino IDE

1. Instale o pacote **"Seeed nRF52 Boards"** no Gerenciador de Placas
   (URL: `https://files.seeedstudio.com/arduino/package_seeedgeek_index.json`)
2. Instale a lib **ArduinoBLE** no Gerenciador de Bibliotecas
3. Placa: *"Seeed XIAO BLE Sense (nRF52840)"*, porta USB correta
4. Abra `src/main.cpp` como sketch (renomeie para `main.ino` se preferir) e faça Upload

> Primeiro upload numa XIAO nova às vezes exige **duplo-reset** (entra em modo
> bootloader com o LED pulsando) para expor a porta USB.

## Uso

1. Energize a lapela → ela anuncia `AnotAI-Lapela`
2. No app: aba Gravar → "Conectar lapela" (o app filtra pelo UUID do serviço)
3. **1 clique** no botão → grava (LED aceso). **Novo clique** → encerra,
   envia o pacote END e o app faz upload + polling automaticamente
4. Desconexão súbita: a placa aborta a captura e volta a IDLE/anunciar;
   o app tenta reconectar sozinho (backoff) e descarta o buffer parcial
