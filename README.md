# AnotAI — Wearable Audio Ingestion & Autonomous Automation Platform

Esqueleto funcional do backend: FastAPI + SQLModel + Postgres + Redis/ARQ + Soniox (STT) + OpenAI (roteador e geração de documentos) + JWT (PyJWT + bcrypt).

## Subir tudo

```bash
cp .env.example .env   # preencha SONIOX_API_KEY, OPENAI_API_KEY e JWT_SECRET_KEY
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
2. `POST /api/v1/audio/process` (multipart `file`; só gravações intencionais) → `202 {note_id, job_id}`
3. Worker: STT Soniox → roteador OpenAI escolhe **um** dos seus modelos (ou
   explicitamente nenhum, abaixo de `ROUTER_CONFIDENCE_THRESHOLD`) → documento
   Markdown seguindo `example_output` + `output_schema` do modelo, processando a
   transcrição em blocos (`OPENAI_TRANSCRIPT_CHUNK_CHARS`) → nota salva →
   automações do modelo via ledger durável de ações (email/tarefa/telegram/
   webhook/mcp)
4. Poll: `GET /api/v1/notes/{id}` (nota + `logs`, o `ExecutionLog` da nota)
5. Sem modelo adequado → a LLM gera uma nota contextual estruturada; a nota
   fica `processed` com `template_id` null (sem ações do modelo)
6. Reclassificação manual: `POST /api/v1/notes/{id}/reclassify` com `{ "template_id": "uuid" }`
   (aceita template próprio; regenera o documento e volta `processed`)

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
  `{objeto.subcampo}` do `structured_data` (+ `note_id`, `created_at`); **variável
  ausente nunca vira `N/A`** — a ação é gravada como `failed` com
  `last_error=missing_template_variable` e as ações irmãs daquele modelo seguem
  sendo preparadas. Segredo ausente falha apenas aquela ação, na execução.

## Testes

```bash
pip install -r requirements-dev.txt
pytest            # sqlite em memória, sem docker; API, pipeline e ações
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

## Gravação intencional, pipeline e Tarefas

- Ingestão: `POST /api/v1/audio/process` aceita **apenas gravações intencionais**
  (qualquer outro modo de captura → 400). Não existe captura contínua
  nem triagem de sugestões.
- Pipeline do worker: **Soniox** transcreve (único STT de produção;
  `STT_PROVIDER=soniox`, `fake` só em dev/test) → o roteador OpenAI escolhe
  **um** dos seus modelos ou nenhum (confidence < `ROUTER_CONFIDENCE_THRESHOLD`,
  padrão 0.6) → o gerador produz um documento Markdown legível seguindo
  `example_output` + `output_schema` do modelo, processando a transcrição
  **inteira** em blocos de `OPENAI_TRANSCRIPT_CHUNK_CHARS` (padrão 24000) para
  nada se perder → nota salva (`processed`) → as automações habilitadas do
  modelo rodam pelo ledger durável de ações (idempotente, com lease):
  `SEND_EMAIL`, `CREATE_TASK`, `SEND_TELEGRAM`, `SEND_WEBHOOK`, `CALL_MCP_TOOL`.
- Sem modelo adequado: a LLM gera uma **nota contextual estruturada** (nunca a
  transcrição bruta, nunca mock); a nota fica `processed` com `template_id`
  null e não dispara ações de modelo.
- Revisão: `GET /api/v1/pendencies` (rota web `/inbox`, nav "Revisar") lista
  pendências `task` / `reminder` / `follow_up` / `action` / `approval`, com
  aceitar/ignorar/adar/editar por tipo.
- Tarefas/quadro: `GET /api/v1/kanban` (colunas `todo|in_progress|done`),
  `PATCH /api/v1/kanban/{id}/move`; notas criam tarefas via
  `POST /api/v1/notes/{id}/to-kanban` e `POST /api/v1/notes/{id}/tasks-to-kanban`.
  O quadro é das **tarefas**: `PATCH /api/v1/notes/{id}` não aceita
  `kanban_status` — o campo saiu de `NoteUpdate` (que é `extra="forbid"`), então
  enviá-lo volta 422 de validação de schema. Notas não guardam estado de quadro.
- Lembretes: cron ARQ `check_scheduled_reminders` a cada 2 min dispara Telegram
  (`Lembrete Anotaí…`) quando `reminder_at` chega, sem reenvio.

## Dev local sem Docker (opcional)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export STT_PROVIDER=fake  # STT fake exclusivo de dev/test (evita chamada ao Soniox)
uvicorn app.main:app --reload
```

## Variáveis-chave

`DATABASE_URL`, `SYNC_DATABASE_URL`, `REDIS_URL`, `OPENAI_API_KEY`, `SONIOX_API_KEY`,
`SONIOX_ENABLE_DIARIZATION=true|false`, `SONIOX_LANGUAGE_HINTS=pt,en`, `JWT_SECRET_KEY`,
`STT_PROVIDER=soniox|fake` (fake só em `ENV=dev|test`), `OPENAI_ROUTER_MODEL=gpt-4o-mini`,
`OPENAI_EXTRACTOR_MODEL=gpt-4o`, `OPENAI_TRANSCRIPT_CHUNK_CHARS=24000`,
`ROUTER_CONFIDENCE_THRESHOLD=0.6`, `MAX_AUDIO_MB`, `AUDIO_UPLOAD_DIR`,
`USER_SECRET_ENCRYPTION_KEY` (obrigatória — sem ela a migration de segredos aborta e a API não sobe).
Integrações do usuário via Nango: `NANGO_ENV_KEY`, `NANGO_SIGNING_KEY`, `NANGO_BASE_URL`,
`NANGO_ENVIRONMENT` e uma `NANGO_<APP>_PROVIDER_CONFIG_KEY` por app conectável. **Não existe SMTP**:
o email das automações sai da conta Gmail que o próprio usuário conectou, e sem ela a ação
`SEND_EMAIL` falha de forma explícita. Ver `docs/operacao-integracoes.md`.
`ffmpeg` já vem no Dockerfile para Opus/WAV do BLE.

## App mobile

Cliente Expo em `apps/mobile/` (SDK 51+, TypeScript). Ver `apps/mobile/README.md`:
ajuste `extra.apiUrl` com o IP LAN do backend e rode `npm install && npx expo start`.

## Cliente web (computador)

SPA em `apps/web/` (Vite + React + TS): gravação pelo microfone do PC,
notas, pendências (Revisar), tarefas/quadro, marketplace, MCP/conexões e
segredos. Ver `apps/web/README.md`:

```bash
cd apps/web && npm install && npm run dev   # http://localhost:5173
```

## Lapela BLE (firmware)

Firmware da lapela em `firmware/` (Seeed XIAO BLE Sense, nRF52840 — ver
`firmware/README.md`): GATT server de áudio, botão push-to-talk alternado e
stream PCM 16 kHz via NOTIFY. O app (`apps/mobile/src/ble/`) escaneia,
reconecta sozinho e monta o WAV automaticamente (exige dev build, não Expo Go).
