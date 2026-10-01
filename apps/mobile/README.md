# AnotAI Mobile

Cliente React Native (Expo SDK 51+, TypeScript) do backend AnotAI: gravação de áudio, feed de notas com reclassificação manual do fallback `no_match` e marketplace de templates.

## Pré-requisitos

- Node 18+ e o app **Expo Go** no celular (mesmo Wi-Fi do PC).
- Backend rodando e acessível na rede local (`docker compose up` na raiz).

## Configurar a URL do backend

O app aponta para `extra.apiUrl` em `apps/mobile/app.json`. Descubra o IP LAN da sua máquina e ajuste:

```json
{
  "expo": {
    "extra": { "apiUrl": "http://192.168.1.50:8000/api/v1" }
  }
}
```

> No emulador Android use `http://10.0.2.2:8000/api/v1`. Em dispositivo físico, use o IP LAN (não `localhost`).

## Rodar

```bash
cd apps/mobile
npm install
npx expo start
```

Escaneie o QR Code com o Expo Go. Abas:

- **Gravar** — botão central (`expo-av`, `.m4a`), upload automático para `POST /api/v1/audio/process` e polling a cada 2s até `processed`/`no_match`, com card de transição Enviando → Transcrevendo → Concluído e botão "Ver nota".
- **Notas** — lista com pull-to-refresh e badges por status; detalhe mostra transcrição, dados estruturados chave-valor e logs das ações.
- **Fallback `no_match`** — banner + botão *"Associar template manualmente"* → `POST /api/v1/notes/{id}/reclassify` com o template escolhido.
- **Human-in-the-loop** — notas `processed` permitem editar os campos do JSON (`PATCH /api/v1/notes/{id}`) e forçar o reenvio (`POST /api/v1/notes/{id}/dispatch`, botão "Re-disparar ações").
- **Marketplace** — templates públicos com badges de integração (Telegram/Webhook/MCP) e botão *"Clonar template"*.
- **Configurações** — gestão de segredos (`GET/POST/DELETE /api/v1/secrets`, valores sempre mascarados) e botão Deslogar.

## Lapela BLE (hardware)

A aba Gravar integra a lapela física (Seeed XIAO BLE Sense, firmware em `firmware/`):

- Indicador **"Lapela Conectada 🟢 / Desconectada ⚪"** no topo (toque para conectar);
  reconexão automática com backoff em queda de Bluetooth
- Banner vermelho animado enquanto o botão físico grava; ao encerrar, o app
  remonta o WAV (16 kHz PCM mono), faz upload e acompanha via polling
- Código em `src/ble/`: `constants.ts` (UUIDs espelhando o firmware),
  `BleManager.ts` (scan/conexão/pareamento persistente), `AudioReceiver.ts`
  (reordenação por `seq`, WAV via `expo-file-system`, upload),
  `useLapel.ts` (hook de estado)

> **BLE exige dev build — não funciona no Expo Go.**
> `react-native-ble-plx` precisa de código nativo: rode com
> `npx expo run:android` (gera o dev client via `expo-dev-client`) em vez de
> escanear o QR no Expo Go. As demais telas continuam funcionando no Expo Go.

## Checagem de tipos

```bash
npm run typecheck
```

## Permissões

O `app.json` já declara `RECORD_AUDIO` (Android) e `NSMicrophoneUsageDescription` (iOS). Na primeira gravação o app pede a permissão e explica o motivo.
