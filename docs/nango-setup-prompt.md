# Prompt para configurar o Nango — máxima cobertura de integrações nos fluxos de nota

> **Como usar:** abra uma conversa nova e cole o **PROMPT** abaixo. Ele diz ao
> agente exatamente o que configurar no Nango e o que implementar no AnotAI.
> A seção **REFERÊNCIA** depois dele é para você consultar enquanto configura o
> painel — não precisa colar.
>
> Estado do código quando escrevi: 692 testes passando, head Alembic
> `e5f607182b93`. Confirme o estado atual antes de começar.

---

## PROMPT (cole isto na conversa nova)

```
No repositório AnotAI (backend FastAPI em `app/`, web em `apps/web/`, worker ARQ),
quero maximizar a cobertura de integrações do Nango nos fluxos de automação das
notas. Hoje uma nota gerada pela IA pode disparar ações; quero que o usuário
consiga ligar a nota em qualquer ferramenta dele.

## Como o sistema funciona hoje (leia antes de agir)

- Cada usuário conecta a PRÓPRIA conta via Nango. Nenhuma credencial de terceiro
  mora no banco: só `IntegrationConnection` (provider, provider_config_key,
  nango_connection_id, status).
- Toda ação externa passa por `NangoClient.proxy_request` em
  `app/services/nango_client.py`, que é um proxy GENÉRICO: sem allowlist de paths
  por provedor. O proxy roteia qualquer endpoint da API do app com a credencial
  do usuário. A barreira de segurança subiu para o catálogo declarativo
  `app/services/tool_registry.py`: cada tool tem nome, provider, JSON Schema de
  argumentos e endpoint FIXO em código. A LLM só escolhe o NOME da tool e
  preenche argumentos — nunca URL, host ou escopo. Adicionar integração = uma
  entrada nova no catálogo (+ conexão ativa do usuário), sem regex no transporte.
- A regra de identidade é rígida: exige EXATAMENTE UMA conexão ativa por
  (workspace, usuário, provider). Zero ou várias → erro explícito. Não relaxe.
- O destino (database_id do Notion, canal do Slack, planilha, etc.) vem da
  configuração do TEMPLATE escolhido pelo usuário — nunca do conteúdo da nota nem
  do LLM. Isso é propriedade de segurança: impede que uma nota gerada escreva num
  lugar arbitrário. Tem teste de guarda para cada action nova.
- Registro único de provedores: `app/services/provider_registry.py`. Status
  `active` = tem execução real; `config_required` = caminho existe, falta a chave;
  `mapping_only` = só declarado. NUNCA marque `active` sem código de execução.
  Execução "real" hoje inclui o tool calling dinâmico: uma tool em
  `tool_registry.py` habilitada para um template com conexão ativa do usuário.
- A UI de integrações lê um array fixo em `apps/web/src/pages/Integrations.tsx`.
  Provedor sem chave de config aparece como "Em configuração", sem botão de
  conectar.

## O que já funciona (não refazer)

Ações implementadas e testadas: SEND_EMAIL (Gmail do usuário), SEND_SLACK_MESSAGE,
CREATE_NOTION_PAGE, CREATE_CALENDAR_EVENT, CREATE_TASK/CREATE_REMINDER (quadro
interno), SEND_TELEGRAM, SEND_WEBHOOK, CALL_MCP_TOOL.

Provedores ativos no registry: google_gmail, google_calendar, google_sheets,
google_tasks, google_drive, notion, slack (os quatro últimos executam via tool
calling dinâmico).
Provedores só mapeados: nenhum hoje.

## Tarefa 1 — página de setup no painel do Nango

Escreva um runbook em `docs/` (ou atualize `docs/operacao-integracoes.md`) com,
para CADA provedor que vamos suportar:
  - o slug exato do Nango (ex.: `notion`, `slack`, `google-tasks`);
  - o nome da integração a criar no painel, que deve coincidir com a chave
    `NANGO_<APP>_PROVIDER_CONFIG_KEY` no `.env`;
  - os escopos OAuth que a integração precisa pedir;
  - um passo de verificação (criar a integração, conectar uma conta de teste,
    rodar a ação, confirmar o resultado).

Marque explicitamente o que está VERIFICADO (eu testei contra a doc oficial do
Nango ou contra conta real) e o que está NÃO VERIFICADO. Não invente escopo.
Onde não conseguir confirmar, escreva "verificar no painel" e siga.

## Tarefa 2 — actions novas, em lotes, cada uma com teste

Implemente em lotes pequenos, um provedor por vez, cada lote com suíte verde
antes do próximo. Para cada action nova são 17 pontos de toque — cf.
`app/schemas/workflow_action.py` (enum + modelo + união),
`app/services/action_registry.py`, `app/services/integration_service.py`
(dispatch + `_xxx`), `app/schemas/template.py` (ActionType + config + registry),
`app/services/template_actions.py` (`_ACTION_TYPES` + `_canonical_action`),
`app/services/provider_registry.py` (status → active + capability),
`app/schemas/integration.py` (Connectable + IntegrationProvider),
`app/api/v1/integrations.py` (`_provider_configs`), teste novo em `tests/`, e
`app/models/integration_connection.py` + migration se o provider for novo no CHECK.

### Como decidir o que dá para implementar

**O proxy do Nango é genérico**: `POST https://api.nango.dev/proxy/<endpoint>`
repassa para a API do app com a credencial do usuário. A doc de cada provedor
lista endpoints que o Nango conhecidamente roteia — **qualquer endpoint daquele
provedor que apareça na doc já prova que o path base roteia**, e ações no mesmo
path base funcionam.

**"Pre-built actions/syncs" NÃO é bloqueio.** São *Functions* — uma conveniência
que o Nango oferece, não o mecanismo. A doc de Auth é explícita: "Combine with
Proxy or Functions" — são alternativas. Um provedor descrito como "no pre-built
actions yet" é implementável normalmente via proxy.

**O que realmente bloqueia** é só: (a) o slug não existir no catálogo
(`https://nango.dev/docs/api-catalog.txt`, ~1050 provedores), ou (b) o path conter
algo que o proxy rejeite. Na dúvida, teste com um GET documentado do provedor.

### Configuração no painel — 1x por app, NÃO por usuário

Criar a integração é passo único por app (painel → *Configure New Integration* →
pick the API, ou `POST /integrations`). O cliente **não** passa por isso: ele só
autoriza no Connect UI. Para APIs populares o Nango já traz um OAuth app
compartilhado ("test connections with zero setup") e sugere os escopos
automaticamente — registrar app própria é recomendado para produção
(whitelabel, escopos, callback URL), não é pré-requisito para funcionar.

Nosso único trabalho por app: o `unique_key` no Nango tem de bater com
`NANGO_<APP>_PROVIDER_CONFIG_KEY` no `.env`.

Ordem sugerida por valor para este produto (nota de áudio → ação):

  Lote A (Google, mesmo padrão de proxy do Calendar que já funciona):
    - google_sheets  → APPEND_SHEET_ROW (acrescentar linha numa planilha)
    - google-drive   → SAVE_DRIVE_FILE (salvar o documento gerado)
  Lote B (tarefas — o usuário citou "criar no meu tasks"):
    - google-tasks   → tool `create_google_task` (tool calling dinâmico). A
      criação usa `POST /tasks/v1/lists/@default/tasks` — a lista @default é a
      "Minhas tarefas" do usuário e evita a variação `/users/@me`. Verifique com
      um teste real antes de considerar pronto, como qualquer ação externa nova.
  Lote C (docs/CRM):
    - confluence     → CREATE_CONFLUENCE_PAGE
    - jira / linear  → CREATE_ISSUE (com project/team vindo do template)
    - asana, todoist, clickup, monday → CREATE_TASK (um de cada vez)
    - airtable       → CREATE_AIRTABLE_RECORD
    - hubspot        → CREATE_HUBSPOT_CONTACT
    - trello         → CREATE_TRELLO_CARD — ATENÇÃO: é OAUTH1, não OAUTH2;
      fluxo de auth diferente, confirme antes de incluir.

  Microsoft (se quiser cobrir esse público): microsoft-teams → SEND_TEAMS_MESSAGE,
  outlook → SEND_EMAIL (variante Outlook), one-drive → SAVE_DRIVE_FILE.

  NÃO faça: Obsidian (não está no catálogo do Nango), e não implemente provider
  sem action real só para inflar a lista.

## Tarefa 3 — UI do template builder

Para cada action nova, o construtor de templates (`apps/web/src/templateBuilder.ts`
e `apps/web/src/pages/Templates.tsx`) precisa de: option no select, botão de
adicionar, formulário de config, e branch em `actionsToDefs`/`defsToActions`.
O destino (database, canal, planilha, projeto) é campo de config do template,
com placeholder claro — nunca campo preenchível pela IA.

## Tarefa 4 — entradas (nota a partir de outra ferramenta)

Hoje só Gmail (leitura) e WhatsApp geram eventos. Avalie e implemente, se fizer
sentido: Slack como fonte (ler mensagens de canais), e Notion como fonte
(ler uma database e transformar em evento). Mesma política de opt-in e retenção
do Gmail: nada é lido sem o usuário ativar explicitamente.

## Regras que não pode quebrar

- Nunca enfraquecer o catálogo `app/services/tool_registry.py`: endpoint e
  provider_config_key de cada tool ficam fixos em código; argumentos entram só
  via JSON Schema e valores que vão para URL são validados antes (ex.:
  spreadsheet_id/sheet_name da aba do Sheets).
- Nunca deixar ação sem conexão ativa "funcionar por fallback" — falha explícito.
- Nunca mandar email/mensagem real em teste; stub do `NangoClient`.
- Nunca marcar provider `active` no registry sem execução real.
- Cada lote termina com `python -m pytest -q -p no:cacheprovider` em 0 failed,
  `python -c "import app.main"` OK, `alembic heads` com um único head, e
  `cd apps/web && npm run build` + `npx tsc --noEmit` limpos.
- Um commit por lote, com mensagem descrevendo o provedor e a action.
```

---

## REFERÊNCIA — o que configurar no painel do Nango

### Provedores já ativos (só criar a integração e preencher a chave)

| App | Slug Nango | Chave no `.env` | Escopo OAuth | Estado |
|---|---|---|---|---|
| Gmail | `google-mail` | `NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY` | `gmail.readonly` **+ `gmail.send`** (ou `gmail.modify`) | verificado |
| Google Agenda | `google-calendar` | `NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY` | `calendar.events` | verificado |
| Notion | `notion` | `NANGO_NOTION_PROVIDER_CONFIG_KEY` | OAuth do Notion; conceder acesso às pages/databases | verificado |
| Slack | `slack` | `NANGO_SLACK_PROVIDER_CONFIG_KEY` | `chat:write` + ser membro do canal | avaliar no painel |

> **Gmail:** se a integração foi criada só com `gmail.readonly`, o `SEND_EMAIL`
> conecta normalmente e **falha na hora de enviar**. Precisa recriar ou editar os
> escopos e reconectar a conta.

> **Slack:** `chat:write` é o escopo documentado para `chat.postMessage`, mas eu
> não o verifiquei contra os docs do Slack nesta sessão. Confirme no painel.

### Provedores a criar (Lote A e B)

| App | Slug | Chave a adicionar | Escopo | Estado |
|---|---|---|---|---|
| Google Planilhas | `google-sheets` | `NANGO_GOOGLE_SHEETS_PROVIDER_CONFIG_KEY` | `spreadsheets` (+ `drive.file` se criar arquivo) | implementado (tool `append_google_sheet_row`) — confirmar conta real |
| Google Drive | `google-drive` | `NANGO_GOOGLE_DRIVE_PROVIDER_CONFIG_KEY` | `drive.file` | implementado (tool `create_google_drive_file`) — confirmar conta real |
| Google Tasks | `google-tasks` | `NANGO_GOOGLE_TASKS_PROVIDER_CONFIG_KEY` | escopo `tasks` | implementado (tool `create_google_task`) — confirmar conta real |

### Provedores a criar (Lote C)

| App | Slug | Chave | Auth |
|---|---|---|---|
| Confluence | `confluence` | `NANGO_CONFLUENCE_PROVIDER_CONFIG_KEY` | OAUTH2 |
| Jira | `jira` | `NANGO_JIRA_PROVIDER_CONFIG_KEY` | OAUTH2 |
| Linear | `linear` | `NANGO_LINEAR_PROVIDER_CONFIG_KEY` | OAUTH2 |
| Asana | `asana` | `NANGO_ASANA_PROVIDER_CONFIG_KEY` | OAUTH2 |
| Todoist | `todoist` | `NANGO_TODOIST_PROVIDER_CONFIG_KEY` | OAUTH2 |
| ClickUp | `clickup` | `NANGO_CLICKUP_PROVIDER_CONFIG_KEY` | OAUTH2 |
| Monday | `monday` | `NANGO_MONDAY_PROVIDER_CONFIG_KEY` | OAUTH2 |
| Trello | `trello` | `NANGO_TRELLO_PROVIDER_CONFIG_KEY` | **OAUTH1** |
| Airtable | `airtable` | `NANGO_AIRTABLE_PROVIDER_CONFIG_KEY` | OAUTH2 |
| HubSpot | `hubspot` | `NANGO_HUBSPOT_PROVIDER_CONFIG_KEY` | OAUTH2 |

### Microsoft (opcional)

| App | Slug | Chave | Auth |
|---|---|---|---|
| Teams | `microsoft-teams` | `NANGO_MICROSOFT_TEAMS_PROVIDER_CONFIG_KEY` | OAUTH2 |
| Outlook | `outlook` | `NANGO_OUTLOOK_PROVIDER_CONFIG_KEY` | OAUTH2 |
| OneDrive | `one-drive` | `NANGO_ONE_DRIVE_PROVIDER_CONFIG_KEY` | OAUTH2 |

### Chaves globais do Nango (obrigatórias, painel → Settings → API Keys)

```
NANGO_ENV_KEY=            # chave de ambiente (Connect Sessions / proxy / connections)
NANGO_SIGNING_KEY=        # chave de assinatura do webhook (diferente da API key)
NANGO_BASE_URL=https://api.nango.dev
NANGO_ENVIRONMENT=        # mesmo ambiente dos webhooks
```

Webhook de auth: `https://<api-publica>/api/v1/integrations/webhooks/nango`.

### Ordem prática no painel

1. Preencher as 4 chaves globais.
2. Criar as integrações dos provedores **já ativos** (Gmail, Agenda, Notion,
   Slack) — os nomes devem bater com as chaves `NANGO_*_PROVIDER_CONFIG_KEY`.
3. Verificar cada uma: conectar conta de teste, rodar a ação, confirmar resultado.
4. Só então criar as integrações dos lotes seguintes, conforme a ação for
   implementada. Integração sem action não faz nada.
