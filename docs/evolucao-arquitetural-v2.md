# Anotaí v2 — auditoria e plano incremental

> Diagnóstico do código desta cópia, antes da implementação. Evidência de testes: `AUDIO_UPLOAD_DIR=/tmp/opencode/anotai-audio pytest -q -p no:cacheprovider` → **80 aprovados**. Sem esse override, 2 testes de upload falham por permissão em `/tmp/anotai-audio`. Esta cópia não contém `.git`; não é possível criar commits aqui. Não houve inspeção de banco de produção nem integração real com serviços externos.

> **Estado atual (superior ao relato histórico abaixo).** Este documento é um
> log de evolução: as seções 1–17 e os marcos F1–F6 descrevem o estado do
> código **na época da auditoria/plano** e foram mantidas como histórico.
> Mudanças posteriores tornaram os trechos indicados como *(histórico)*
> obsoletos. Hoje o sistema é:
>
> - **Gravação apenas intencional** — `POST /api/v1/audio/process` recusa
>   qualquer captura que não seja intencional; a captura contínua (always-on)
>   foi removida.
> - **STT exclusivamente Soniox** em produção (`STT_PROVIDER=soniox|fake`;
>   `fake` só em `ENV=dev|test`). Não existe STT via OpenAI/Whisper.
> - **Seleção de template por LLM** — as regras determinísticas de roteamento
>   (`app/models/template_routing_rule.py` e o endpoint
>   `/templates/{id}/routing-rules`) foram removidas; o roteador OpenAI escolhe
>   um dos modelos do usuário ou explicitamente nenhum, com limiar
>   `ROUTER_CONFIDENCE_THRESHOLD` (padrão 0.6).
> - **Sem modelo adequado** a LLM gera uma nota contextual estruturada; a nota
>   fica `processed` com `template_id` null. Os estados `NoteStatus.SUGGESTED`,
>   `NoteStatus.NO_MATCH` / `status=no_match` e a pendência `note_suggestion`
>   não fazem parte do fluxo (o arquivo `app/api/v1/inbox.py` foi removido).
> - **Caixa de triagem removida** — os endpoints `GET /api/v1/inbox/suggested`,
>   `POST /api/v1/inbox/{id}/accept` e `POST /api/v1/inbox/{id}/discard` não
>   existem mais; a revisão acontece na **Action Inbox** `GET /api/v1/pendencies`
>   (rota web `/inbox`, nav "Revisar"; kinds `task`/`reminder`/`follow_up`/
>   `action`/`approval`).
> - **Quadro nativo de Tarefas** — `GET /api/v1/kanban` +
>   `PATCH /api/v1/kanban/{id}/move`, com notas enviadas ao quadro via
>   `POST /api/v1/notes/{id}/to-kanban` e `POST /api/v1/notes/{id}/tasks-to-kanban`;
>   `PATCH /api/v1/notes/{id}` rejeita `kanban_status` (422) — o estado de Kanban
>   é das tarefas, não das notas.
>
> O pipeline de gravação hoje: áudio → Soniox → roteador LLM → documento
> Markdown em blocos (`OPENAI_TRANSCRIPT_CHUNK_CHARS`, padrão 24000) seguindo
> `example_output` + `output_schema` → nota salva → automações do template via
> ledger durável/idempotente/lease (`SEND_EMAIL`, `CREATE_TASK`,
> `SEND_TELEGRAM`, `SEND_WEBHOOK`, `CALL_MCP_TOOL`). `ExecutionLog` continua
> sendo o log de erros de pipeline/geração da nota, exposto como `NoteRead.logs`.

## 1. Arquitetura atual

*(histórico — descreve o estado na época da auditoria; ver "Estado atual" acima.)*

FastAPI/SQLModel + Alembic + PostgreSQL, Redis/ARQ e OpenAI; web React e mobile Expo compartilham API `/api/v1`. Upload de áudio → nota pendente → worker STT → roteamento de templates → extração JSON validada por JSON Schema ou nota geral → persistência → dispatcher de ações. Captura contínua bifurca em sugestões, sem template. Redis/ARQ enfileira áudio e faz cron de lembretes. `app/api/v1/audio.py:35-102`, `app/worker.py:170-416`, `app/services/router.py:49-115`, `app/services/extractor.py:19-64`, `docker-compose.yml:3-75`.

## 2. Funcionalidades já implementadas

*(histórico — a captura contínua, o inbox de sugestões e o STT OpenAI foram removidos; ver "Estado atual" acima.)*

Áudio intencional/contínuo, STT OpenAI/fake, nota geral resiliente, roteamento LLM com heurística/threshold, extração estruturada, edição/reclassificação/redisparo de notas, Kanban, lembretes associados à nota, inbox para sugestões de captura contínua, autenticação JWT, Telegram e webhook, templates privados/públicos e marketplace de clonagem. `app/worker.py`, `app/services/general_note.py`, `app/api/v1/{notes,inbox,kanban,templates,marketplace}.py`, `app/services/dispatcher.py`. Não existem ainda os conceitos independentes de Task/Reminder/FollowUp/NormalizedEvent/GeneratedDocument/Workflow ou isolamento real por workspace.

## 3. Templates atuais

`templates`: `id`, `user_id` (nulo = oficial), `title`, `purpose`, `semantic_trigger`, `example_output`, `output_schema` JSON, `actions` JSON, `is_public`, `created_at` (`app/models/template.py:14-31`). Router já retorna `(template_id, confidence, reason)` e valida ID no catálogo do usuário; seleção de regra determinística por workspace/contexto e metadados de categoria/versionamento ainda faltam (`app/services/router.py:18-115`). Fallback atual produz nota geral com `template_id=NULL`, não um template geral versionado. Conservar esse contrato legado; introduzir identificador de fallback explícito em documentos novos sem retroativamente fingir que notas antigas tinham versão.

## 4. Marketplace atual

`GET /marketplace` lista públicos e `POST /marketplace/{id}/clone` cria cópia privada; web/mobile exibem catálogo e clone; edição/publicação via CRUD de template. Não existe instalação rastreada, origem, versionamento, categorização formal, revisão editorial nem monetização (`app/api/v1/marketplace.py:18-56`, `apps/web/src/pages/Marketplace.tsx:18-124`, `apps/web/src/pages/Templates.tsx:100-301`). Preservar endpoints/JSON Schema/actions e adicionar campos optativos/migration aditiva; testar ida/volta no builder `apps/web/src/templateBuilder.ts:55-218`.

## 5. MCP atual

Cliente HTTP/JSON-RPC real (`app/services/mcp_client.py:66-246`), servidores/catálogo/OAuth/callback na API (`app/api/v1/mcp.py:22-161`, `app/services/mcp_oauth.py`), segredos OAuth Fernet, tela de conexão (`apps/web/src/pages/Mcp.tsx`), ações MCP estáticas/dinâmicas no dispatcher (`app/services/dispatcher.py:201-345`). Tokens MCP são persistidos cifrados. O LLM pode selecionar tool dinâmica por filtro textual, sem uma política granular de confirmação: não utilizá-la como caminho de novas automações.

## 6. Retirada e reaproveitamento do MCP

Remover **da navegação e do construtor voltados ao usuário** o cadastro/OAuth/MCP como integração principal, substituindo pela UI de integrações de produto via Nango/adapters. Descontinuar criação de novas ações MCP e seleção dinâmica pelo LLM; não apagar imediatamente rotas, modelos ou ações legadas: templates privados/públicos existentes podem referenciá-las. Preservar `ExecutionLog`, arquitetura de despacho de Telegram/webhook, proteção de secrets e compatibilidade legada isolada até haver inventário de dados reais e plano de desativação. Tokens MCP antigos não viram conexões Nango automaticamente: requerem novo consentimento.

## 7. Schema atual

Oito migrations lineares de `0001_initial` a `7b4a5ca13f21_mcp_oauth_connections`. Tabelas: `users`, `templates`, `notes`, `execution_logs`, `user_secrets`, `user_mcp_servers`, `user_mcp_oauth_credentials`, `mcp_oauth_flows`. `users` tem autenticação, sem workspaces/roles (`app/models/user.py:13-20`); `notes` reúne raw_transcription, structured_data, status, Kanban, prazo e reminder (`app/models/note.py:36-60`); `execution_logs` pertencem a nota, não têm workspace (`app/models/execution_log.py:20-33`). `user_secrets.value` é texto; não tratar como cofre de OAuth. `UserSecret` entra no metadata transitivamente via `app/models/__init__.py:9` ao importar submódulos (`alembic/env.py:15-20`); registro explícito seria menos frágil, mas não é tabela ausente hoje. A cadeia versionada não comprova estado do banco implantado.

## 8. Novo schema proposto (aditivo)

Todos os dados privados novos levam `workspace_id`, índices e autorização de membership; manter `user_id` legada durante transição. Para contas existentes, criar workspace pessoal + membership `OWNER` por usuário. Um único produto; não criar schemas por nicho.

| Entidade | Campos/constraints principais | Papel |
|---|---|---|
| `Workspace`, `WorkspaceMember` | id, owner/name, timezone/locale, `(workspace_id,user_id)` único, role | Escopo e membros; filtros obrigatórios em queries/jobs. |
| `AutomationSettings`, `SourceMonitoringPolicy` | workspace/user, mode, thresholds, timezone/locale, allow/deny, retenção | Defaults seguros; políticas por source/action; rejeição **antes** de persistir corpo/LLM. |
| `NormalizedEvent` | workspace/user, source, external_id, type, conversation_id, content, occurred_at UTC, metadata mínima, status, expiração | Ingestão idempotente (`workspace_id,source,external_id` quando conhecido), eventos filtrados não entram. |
| `GeneratedDocument` | workspace, source_event_id, transcript/note_id opcional, template_id nullable, template_version/snapshot, structured_data, rendered_content, status, timestamps | Snapshot de geração; convive com `Note`/API antiga até migração. |
| `Task`, `Reminder`, `FollowUp` | workspace/user, origem + chave única de inferência, status, due_at/remind_at UTC, timezone, timestamps | Entidades distintas; `FollowUp` depende de pergunta sem resposta e pode ser resolvido/cancelado. |
| `ConversationState`, `PendingConversationItem` | workspace/source/conversation_id único, incoming/outgoing, última mensagem, perguntas aguardando resposta, estado | Processar mensagens autorizadas `isFromMe`; não chamar LLM periodicamente para todo chat. |
| `ActionSuggestion`, `CalendarEventSuggestion`, `Memory` | origem, structured intent + confidence, status/retention/expiry | Sugestão ≠ execução; Memory opt-in, exclusão e expurgo. |
| `Workflow`, `WorkflowStep`, `WorkflowExecution`, `ActionExecution` | trigger, conditions, action_type, input validado, permission snapshot, idempotency_key único, status/tentativas | Motor determinístico, histórico separado de `ExecutionLog` legado. |
| `IntegrationConnection`, `GmailWatchState`, `NotificationPreference`, `AuditEvent` | workspace/user/provider + provider_config_key/nango_connection_id, cursor/expiration, canais, ator/resultado | Referências sem OAuth token; ciclo watch e trilha sem conteúdo sensível. |

Não colocar dados bancários/segredos em `metadata` ou `execution_logs`; campos JSON devem ter schemas/limites e retenção. Guardar UTC + timezone IANA e contexto de interpretação temporal. Na ausência de ID estável externo, gerar chave canônica específica por fonte e deduplicar também efeitos derivados.

## 9. Migrations necessárias

1. `workspaces`, memberships, workspace pessoal por usuário existente; colunas nullable em recursos existentes e backfill por `user_id` sem alterar ainda contratos da API; validar nulidade/ownership antes de NOT NULL.
2. Campos opcionais de template (routing description, category/use-cases/version/origin); configurações e regras por contexto; nunca substituir `purpose`/`semantic_trigger` abruptamente.
3. Eventos/documentos/resultados/tarefas/lembretes/follow-ups com índices por workspace/tempo e unique idempotente; backfill apenas dos registros com mapeamento inequívoco. Notas antigas e novos objetos coexistem; nenhuma duplicação automática de ações no backfill.
4. Workflows/executions/connections/cursor Gmail/conversation policies e estado; expansão sem remoção de MCP. Contract futuro em migration separada após telemetria e inventário dos dados existentes.

Exigir upgrade em banco descartável e teste de backfill repetível; validar FKs/únicos e isolamento em API e worker. Não autogerar mudanças destrutivas. Sem git nesta cópia, manter lista explícita de arquivos alterados/backups antes de migrations destrutivas.

## 10. Módulos a criar

Sob `app/models/`, `app/services/`, `app/api/v1/` e `app/schemas/` existentes: escopo workspace; `source_provider`/normalização; router determinístico; intelligence com schema e confidence; conversation state; policy/permission; workflow/action registry; scheduler sobre ARQ; integration service e adapters Nango/Gmail Push/Evolution GO; APIs de integrações/políticas/pendências/histórico. Novas páginas web para Integrações e Pendências; não duplicar aplicação por nicho. Interfaces são limites de domínio e contratos testáveis, não hierarquias abstratas desnecessárias.

## 11. Módulos a alterar

*(histórico — o módulo `inbox` foi removido; a Action Inbox hoje é `/api/v1/pendencies`.)*

`app/worker.py`, `app/services/{router,extractor,dispatcher}.py`, `app/core/{config,queue,deps}.py`, `app/api/v1/{audio,notes,templates,marketplace,inbox,__init__}.py`, modelos/schemas e Alembic. Web: `App.tsx`, `api.ts`, `types.ts`, `templateBuilder.ts`, `pages/{Templates,Mcp,Marketplace,Inbox,NoteDetail}.tsx`; mobile apenas contratos relevantes em `apps/mobile/src/services/api.ts` inicialmente. Preservar captura, notas e marketplace atuais.

## 12. Integrações atuais

*(histórico — o STT hoje é Soniox (não OpenAI), o email real sai da conta Gmail do próprio usuário via Nango (sem SMTP), e há Nango/Gmail/Calendar/Evolution GO implementados; ver "Estado atual" no topo e os runbooks `operacao-integracoes.md` / `operacao-whatsapp-evolution-go.md`.)*

Telegram bot, webhook HTTP e MCP (OAuth para serviços catalogados, servidor arbitrário, tools); OpenAI para STT/LLM. Ações `email` e `whatsapp` no dispatcher são stubs (`app/services/dispatcher.py:348-401`). Não há Nango, conexão Google via Nango, Gmail source, Google Calendar action ou Evolution GO implementados.

## 13. Filas e scheduler atuais

Redis + ARQ com `process_audio_job` (`app/core/queue.py:11-31`) e cron `check_scheduled_reminders` a cada 2 minutos (`app/worker.py:370-416`). Cron faz envio+marcação sem atomicidade; reenvio em falha entre envio e commit é possível. Não há idempotência global no enqueue/dispatcher. Usar ARQ existente para jobs persistentes, recovery e varredura periódica de itens vencidos; decidir delayed job vs cron de reconciliação conforme durabilidade e concorrência, sem cron por item. Efeitos externos são no máximo efetivamente uma vez quando o provedor suporta idempotency key; caso contrário tratar como at-least-once e registrar tentativa/resultado.

## 14. Riscos

Isolamento entre usuários/workspaces hoje é apenas filtro de aplicação; novos webhooks e jobs precisam validar membership. Ações legadas de template público podem conter destinos/URLs arbitrários; clonar não é conceder permissão. Conteúdo de comunicações exige filtragem ANTES do LLM e idealmente antes de persistir. Retenção, expurgo, registro sem conteúdo sensível, SSRF em webhook/HTTP, auditoria e reprocessamento seguro são requisitos. Gmail exige endpoint HTTPS Pub/Sub OIDC, watch renovado e reconciliação de history; notificações não são a fonte de verdade. Evolution GO não documenta claramente assinatura de webhooks de saída: autenticação por gateway/segredo de implantação ou rede privada deve ser confirmada antes de expor endpoint. Integrações reais dependem de credenciais, projeto GCP e ambiente Nango externos.

## 15. Possíveis breaking changes

Troca brusca de status/shape de `Note`, API e campos de template afeta web/mobile e edição em `templateBuilder.ts`; remoção de MCP quebra templates legados e OAuth existente. `template_id=NULL` em nota geral é esperado por consumidores antigos. Campos de workspace obrigatórios antes do backfill quebram dados implantados; mudanças de fuso afetam lembretes. OAuth MCP não equivale a conexão Nango. Preservar contratos, introduzir novas rotas/versionamento se shape divergir e migrar por expansão → backfill → dual-read/write controlado → retirada futura.

## 16. Decisões arquiteturais preliminares

- **Source → Intelligence → Action:** adapters recebem e filtram; NormalizedEvent é entrada do motor; inferência apenas produz intenções estruturadas com confidence; schema/permissão/política/workflow resolvem referências e executam. LLM não escolhe workspace, conexão nem payload livre para API.
- **Tenancy:** workspace pessoal por usuário existente; `user_id` permanece enquanto APIs migram. Nango dev/prod são ambientes, **não** isolamento por workspace; mapear tags/IDs e conferir associação no banco na volta do webhook.
- **Fallback geral:** manter nota geral atual como fallback explícito e não migrar notas antigas para um template falso; avaliar template geral versionado para documentos novos sem quebra da API.
- **MCP:** somente compatibilidade isolada por tempo definido; sem exposição de novos recursos MCP ao usuário nem seleção dinâmica pela LLM.
- **Política de automação:** defaults sugestivos; execução externa requer autorização por action/workflow e input validado; operações destrutivas sempre confirmação, independentemente de confidence.

### Decisões após revisão arquitetural

Revisão `@oracle` identificou riscos comprovados: clones de templates públicos hoje copiam `actions` ativas e o dispatcher as executa automaticamente (`app/api/v1/marketplace.py:50`, `app/services/dispatcher.py:348-401`); tools MCP dinâmicas são escolhidas por LLM com blocklist textual; `app/worker.py:216-280` duplica o ramo de nota geral. Corrigir a primeira superfície antes de habilitar novas sources. Templates oficiais com `user_id=NULL` **não** entram na consulta de roteamento atual (`app/worker.py:208`). O fallback geral será **virtual**, preservando `template_id=NULL` e o marcador já existente `structured_data.tipo=nota_geral` para compatibilidade; documentos futuros registrarão explicitamente que usaram fallback.

Não adotar duas sugestões da revisão porque contrariam requisitos explícitos: (1) manter Task/Reminder/FollowUp/GeneratedDocument só dentro de Note; cada um precisa de ciclo de vida/estado próprios e o usuário os exigiu separadamente; (2) Nango somente para ingestão, mantendo MCP OAuth como caminho principal de actions; o objetivo é Nango para conexões e actions suportadas, MCP apenas compatibilidade. Workspaces pessoais **com memberships** permitem escopo compartilhado futuro; dados novos terão `workspace_id`, mantendo ownership legado até backfill. Não usar `reminder_sent=true` como claim definitivo *antes* de enviar: crash nesse intervalo perde o lembrete; utilizar estado com lease/recovery + chave idempotente, reconhecendo limites de exactly-once na API de destino.

## 17. Plano de entrega incremental e gates

| Etapa | Entrega observável | Gate de evidência e risco |
|---|---|---|
| F0, auditoria | Este relatório; baseline de testes; docs oficiais verificadas | Revisão de arquitetura antes de codificar. |
| F1, segurança e escopo base | Clones públicos com ações externas inativas por padrão; workspace pessoal/membership/backfill; editor de ações; APIs e ownership legados mantidos | Clone sem saída externa, migration com dados, regressão auth/áudio e web build; revisão @oracle. O filtro cross-workspace só entra com recursos efetivamente escopados na F2. |
| F2, eventos e notas inteligentes | Source policies + NormalizedEvent/idempotência, regras determinísticas + router/fallback + GeneratedDocument; inteligência classifica Task/Reminder/FollowUp separadamente com confidence | Eventos bloqueados não persistem/chegam ao LLM, escopo workspace, schema, timezone, regressão marketplace; revisão @oracle. |
| F3, ação confiável | Registry schema/permission, workflow trigger/conditions/actions, executions, ARQ para reminder/follow-up/recovery, Telegram e interno | Idempotência/retries/restart, sem chamada LLM→provider; revisão @oracle. |
| F4, Nango e Google | Connect UI, IntegrationService, Calendar action, Gmail watch/PubSub/cursor/renovação/filtros | Webhook OIDC, ownership, história paginada/404/retry, testes mocks e smoke com credenciais externas; revisão @oracle. |
| F5, WhatsApp e pendências | Evolution GO adapter/QR/status, whitelist/blacklist antes da persistência/LLM, fromMe/conversation state/follow-up; Action Inbox web e preferências *(hoje: `GET /api/v1/pendencies`, rota web `/inbox`)* | Conversa bloqueada jamais alcança storage/LLM, pergunta resolvida cancela follow-up; revisão @oracle e designer para UI. |
| F6, hardening | Auditoria, retenção/exclusão, observabilidade sem PII, DLQ/reconciliação, rate limits e documentação operacional | Testes de segurança/migração/recovery + revisão @oracle; nenhum commit nesta cópia sem `.git`. |

Gates de fase: implementar em lanes sem conflito, validar evidência específica, revisão arquitetural, corrigir achados materiais e avançar. Presets/marketplace de workflows e provedores adicionais ficam como extensões **não-MVP**. Não prometer conexão funcional sem Nango/GCP/Evolution GO configurados.

### Documentação oficial consultada

- Nango: https://nango.dev/docs/guides/auth/auth-guide ; https://nango.dev/docs/reference/frontend/frontend-sdk ; https://nango.dev/docs/reference/backend/http-api/proxy/get
- Gmail: https://developers.google.com/gmail/api/guides/push ; https://developers.google.com/gmail/api/reference/rest/v1/users.history/list ; https://developers.google.com/gmail/api/reference/rest/v1/users.messages/get
- Pub/Sub: https://docs.cloud.google.com/pubsub/docs/push
- Evolution GO (fonte oficial no repositório, distinta da Evolution API Node): https://github.com/evolution-foundation/evolution-go

## Estado da implementação nesta cópia (pós-auditoria)

- **F1 fechada:** workspace pessoal com membership e backfill; clone de marketplace com ações externas desativadas, contrato `enabled`/consentimento e execução bloqueada para template público de outro dono.
- **F2 fechada:** eventos normalizados com opt-in por fonte e filtros antes da persistência/LLM; regras fortes + fallback geral preservado; documento versionado separado, classificação estruturada e Task/Reminder/ActionSuggestion sugestivos; áudio ligado à pipeline. Captura contínua não gera GeneratedDocument neste corte *(histórico: a captura contínua foi removida depois; ver "Estado atual" no topo)*.
- **F3 fechada:** definições de workflow/etapas, settings por action, execution ledger com claim/lease, schema de ações, confirmação com preview, actions internas/Telegram, scheduler ARQ delayed + reconciliação SQL, recovery de documentos e interface web de fluxos. Uma definição inválida não bloqueia outros workflows/tenants. Telegram externo mantém semântica at-least-once: crash entre envio e commit pode duplicar.
- **Evidência final F3:** `AUDIO_UPLOAD_DIR=/tmp/opencode/anotai-audio pytest -q -p no:cacheprovider` → **257 testes aprovados**; build web aprovado; migrations F2/F3 e concorrência de claim verificadas em PostgreSQL 16 descartável. Cópia sem `.git`, sem commits. Nenhuma conexão Google/Nango/WhatsApp real foi testada.
- **F4 fechada para desenvolvimento:** Nango Connect UI/REST e webhook HMAC, Gmail Push Pub/Sub OIDC, history/watch com opt-in e filtro pré-corpo/LLM, renovação/reconciliação, lacuna de histórico com retomada explícita, Calendar action via conexão própria e UI de Integrações sem MCP. **357 testes backend** e build web aprovados; migration testada em PostgreSQL descartável. Uso real depende de configuração externa Nango/Google e smoke de conta teste; ver [runbook](operacao-integracoes.md). MCP legado permanece no backend para compatibilidade, mas não aparece na navegação/editor.
- **F5 fechada para desenvolvimento:** Action Inbox unificada com aceite/edição/snooze, anti-duplicação determinística com Kanban legado, FollowUp separado e cron de perguntas sem resposta; WhatsApp com instância/QR, filtro de conversas e `fromMe` antes de persistência/LLM, segredos cifrados e webhook com segredo por instância, desligamento local mesmo se o GO falhar. **444 testes backend** e build web aprovados; migrations validadas em PostgreSQL descartável. A documentação do GO é inconsistente entre wiki/Swagger: fixture e smoke com GO 0.7.2 real e proteção de logs externos são bloqueios de produção, não foram comprovados por mocks; ver [runbook WhatsApp](operacao-whatsapp-evolution-go.md). Envio automático de WhatsApp não está habilitado sem contrato de envio confirmado. *(Histórico: o "Kanban legado" das notas foi removido — hoje o quadro é das Tarefas; a Action Inbox é `GET /api/v1/pendencies`, sem o estado `suggested`/`note_suggestion` das notas — ver "Estado atual" no topo.)*
- **Pendente:** F6 retenção/exclusão, auditoria, observabilidade, preferências de notificação e hardening. O risco de advisories npm e a operação real das integrações exigem validação separada; não substituem as evidências locais de testes.
