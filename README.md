# AnotAI — Wearable Audio Ingestion & Autonomous Automation Platform

Esqueleto funcional do backend: FastAPI + SQLModel + Postgres + Redis/ARQ + OpenAI (Whisper + GPT Structured Outputs) + JWT (PyJWT + bcrypt).

## Subir tudo

```bash
cp .env.example .env   # preencha OPENAI_API_KEY e JWT_SECRET_KEY
docker compose up --build
# API: http://localhost:8000/docs  | health: GET /health
```

O serviço `api` roda `alembic upgrade head` antes do uvicorn. O serviço `worker` consome a fila ARQ.

## Seed do marketplace

```bash
docker compose exec api python -m app.seed_marketplace
```

Importa `templates/*.json` como templates públicos (`user_id=NULL`).

## Fluxo

1. `POST /api/v1/auth/register` → `POST /api/v1/auth/login` (Bearer JWT)
2. `POST /api/v1/audio/process` (multipart `file`) → `202 {note_id, job_id}`
3. Worker: STT → router (match/threshold) → extractor (JSON Schema) → dispatcher (telegram/webhook/mcp_tool)
4. Poll: `GET /api/v1/notes/{id}` (nota + `logs`)
5. Sem match → `status=no_match` (caixa de entrada, sem ações externas)
6. Reclassificação manual: `POST /api/v1/notes/{id}/reclassify` com `{ "template_id": "uuid" }`
   (aceita template próprio ou público; roda extração + dispatcher e volta `processed`)

## Templates (incl. "Construir com IA")

- CRUD em `/api/v1/templates` + `POST /api/v1/templates/draft` com
  `{ "description": "texto livre" }` → rascunho (título, gatilho, campos,
  mensagem) para revisar no construtor visual. Nada é salvo automaticamente.
- Modelo mental: o template é **UM exemplo de como a nota deve sair**
  (`example_output`). `POST /api/v1/templates/derive` extrai gatilho+campos
  do exemplo; o roteador compara usando o exemplo e o extrator segue o schema
  derivado.
- Cada template tem **`purpose` obrigatório** (para que serve) — o roteador lê
  título + descrição + purpose + exemplo para casar.
- Sem match, a IA gera uma **nota geral adaptativa** (título + markdown) em vez
  de deixar só a transcrição bruta.

## Ciclo human-in-the-loop

- `PATCH /api/v1/notes/{id}` — corrige `raw_transcription`/`structured_data` à mão  (com `template_id`, o JSON é validado contra o schema; 422 se inválido).
- `POST /api/v1/notes/{id}/dispatch` — re-dispara as ações do template vinculado
  após correção manual.
- Segredos (`GET|POST /api/v1/secrets`, `DELETE /api/v1/secrets/{key}`) — tokens
  por usuário (`TELEGRAM_BOT_TOKEN`, `WEBHOOK_AUTH_TOKEN`…) usados pelo dispatcher
  via `${CHAVE}` (listagem sempre mascarada). Mensagens suportam `{campo}` e
  `{objeto.subcampo}` do `structured_data` (+ `note_id`, `created_at`); variável
  ausente vira `N/A`, segredo ausente falha só aquela ação.

## Testes

```bash
pip install -r requirements-dev.txt
pytest            # sqlite em memória, sem docker; 15 testes (API + dispatcher)
```

## Ferramentas externas via MCP

Não há padrão de auth no MCP — cada servidor define o seu. Por isso o cadastro
é livre: `auth_header_key` é qualquer header (`Authorization`, `X-API-Key`,
...) e o segredo guarda o **valor completo** do header (`Bearer abc123` ou só
a chave, conforme o servidor exigir); sem header = sem auth. A `server_url`
também aceita `${SECRETO}` (ex: `https://mcp.exemplo.com/${TENANT}/mcp`).

- Servidores por usuário: `GET|POST /api/v1/mcp`, `POST /api/v1/mcp/{id}/test`
  (lista as tools — 502 se o servidor falhar), `PATCH|DELETE /api/v1/mcp/{id}`.
  Auth via `UserSecret`: `auth_header_key` + `auth_secret_name`.
- Ação `mcp_tool` no template, dois modos:
  ```json
  { "type": "mcp_tool", "config": {
      "server_id": "uuid-do-servidor",
      "tool_name": "create_event",
      "arguments_mapping": {"title": "{animal_nome} - Retorno", "date": "{retorno_em}"} } }
  { "type": "mcp_tool", "config": { "server_id": "uuid-do-servidor", "dynamic_llm_call": true } }
  ```
  Estático interpola `{campo}` do `structured_data`; dinâmico passa as tools do
  servidor ao OpenAI Tool Calling e executa a escolha da LLM. Timeouts, erros
  JSON-RPC/`isError` e segredo ausente viram log `failed` sem quebrar as demais ações.

## Gravação contínua, inbox e Kanban

- Ingestão: `POST /api/v1/audio/process` aceita `capture_mode=intentional|continuous`.
  Contínuo roda o extrator Always-On e cria uma `Note` (`suggested`) por item
  acionável; ruído não gera nada.
- Triagem: `GET /api/v1/inbox/suggested`, `POST /api/v1/inbox/{id}/accept`
  (vira `processed` + entra no Kanban, `reminder_at` agenda alerta) e
  `POST /api/v1/inbox/{id}/discard`.
- Kanban: `GET /api/v1/kanban` (colunas `todo|in_progress|done`),
  `PATCH /api/v1/kanban/{id}/move`, `PATCH /api/v1/notes/{id}` (corrige JSON,
  prazos e coluna).
- Lembretes: cron ARQ `check_scheduled_reminders` a cada 2 min dispara Telegram
  (`⏰ Lembrete AnotAI…`) quando `reminder_at` chega, sem reenvio.

## Dev local sem Docker (opcional)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export STT_PROVIDER=fake  # evita custo OpenAI no STT
uvicorn app.main:app --reload
```

## Variáveis-chave

`DATABASE_URL`, `SYNC_DATABASE_URL`, `REDIS_URL`, `OPENAI_API_KEY`, `JWT_SECRET_KEY`,
`STT_PROVIDER=openai|fake`, `ROUTER_CONFIDENCE_THRESHOLD=0.6`, `MAX_AUDIO_MB`, `AUDIO_UPLOAD_DIR`.
`ffmpeg` já vem no Dockerfile para Opus/WAV do BLE.

## App mobile

Cliente Expo em `apps/mobile/` (SDK 51+, TypeScript). Ver `apps/mobile/README.md`:
ajuste `extra.apiUrl` com o IP LAN do backend e rode `npm install && npx expo start`.

## Cliente web (computador)

SPA em `apps/web/` (Vite + React + TS): gravação pelo microfone do PC,
notas, inbox, Kanban, marketplace, MCP e segredos. Ver `apps/web/README.md`:

```bash
cd apps/web && npm install && npm run dev   # http://localhost:5173
```

## Lapela BLE (firmware)

Firmware da lapela em `firmware/` (Seeed XIAO BLE Sense, nRF52840 — ver
`firmware/README.md`): GATT server de áudio, botão push-to-talk alternado e
stream PCM 16 kHz via NOTIFY. O app (`apps/mobile/src/ble/`) escaneia,
reconecta sozinho e monta o WAV automaticamente (exige dev build, não Expo Go).
