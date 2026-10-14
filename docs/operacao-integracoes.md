# Integrações Google — configuração e verificação

As credenciais OAuth de terceiros ficam no Nango, **não** no banco do Anotaí. Esta cópia não traz chaves nem um projeto Google configurado; testes locais usam mocks. Configure um ambiente de teste antes de considerar Gmail/Agenda operacionais.

## Nango

1. Crie uma integração por app no ambiente Nango escolhido. Os nomes das integrações devem coincidir com a respectiva chave de config no backend:

   | App | Chave de config | Escopo OAuth que a integração precisa pedir |
   |---|---|---|
   | Gmail | `NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY` | `https://www.googleapis.com/auth/gmail.readonly` **+ um escopo que permita enviar**: `https://www.googleapis.com/auth/gmail.send` (ou `gmail.modify`) |
   | Google Agenda | `NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY` | `https://www.googleapis.com/auth/calendar.events` |
   | Google Planilhas | `NANGO_GOOGLE_SHEETS_PROVIDER_CONFIG_KEY` | `https://www.googleapis.com/auth/spreadsheets` (+ `https://www.googleapis.com/auth/drive.file` se o Anotaí criar arquivos) — **ainda sem execução** |
   | Notion | `NANGO_NOTION_PROVIDER_CONFIG_KEY` | OAuth próprio do Notion (não é escopo Google); conceda acesso às páginas/databases — **ainda sem execução** |

   > **Gmail: sem o escopo de envio a ação `SEND_EMAIL` falha em runtime.** `gmail.readonly` sozinho lê, não envia. Se você já criou a integração Gmail só com leitura, precisa **recriá-la ou editar seus escopos no painel do Nango** e reconectar a conta do usuário. A listagem canônica de apps, capacidades e status é `app/services/provider_registry.py`.

   Configure no painel do Nango as credenciais OAuth do aplicativoGoogle e os consentimentos/redirects que ele indicar.
2. Configure no backend `NANGO_ENV_KEY` (chave **de ambiente**, com acesso a Connect Sessions/proxy/connections), `NANGO_BASE_URL` (por exemplo `https://api.nango.dev`), `NANGO_ENVIRONMENT` correspondente ao ambiente dos webhooks e `NANGO_SIGNING_KEY` (chave **de assinatura do webhook**, distinta da API key). Nunca publique essas chaves no frontend.
3. No painel Nango, habilite o webhook de auth em `https://<api-publica>/api/v1/integrations/webhooks/nango` e o envio de eventos de criação. A API verifica `X-Nango-Hmac-Sha256` sobre os bytes originais, reconsulta a conexão no Nango e só a vincula após conferir as tags de usuário/workspace e a membership. Tokens OAuth não são copiados para o Anotaí.

## Gmail Push / Google Cloud

1. Habilite Gmail API e Pub/Sub no projeto Google. Crie um tópico e configure `GMAIL_PUBSUB_TOPIC=projects/<projeto>/topics/<topico>`. Conceda a `gmail-api-push@system.gserviceaccount.com` permissão de publicação no tópico, conforme [guia oficial Gmail Push](https://developers.google.com/gmail/api/guides/push).
2. Crie uma assinatura push autenticada com URL HTTPS pública `https://<api-publica>/api/v1/gmail/push`, uma service account de envio OIDC e audience exata correspondente à URL configurada. Defina `GMAIL_PUSH_AUDIENCE` e `GMAIL_PUSH_SA_EMAIL` no backend. Valide permissões de emissão de token no console do seu projeto; não use endpoint público sem autenticação OIDC.
3. Ao conectar Gmail, o acompanhamento **continua desligado**. Na tela Integrações, ative-o explicitamente e escolha remetentes/labels; o padrão é `INBOX`, com SPAM/TRASH ignorados. O worker registra/renova o watch com esse tópico. A notificação Pub/Sub só enfileira trabalho após verificar assinatura, audience, service account, conexão, watch e opt-in; o histórico do Gmail é a fonte de verdade.
4. O watch expira: renovar pelo menos a cada sete dias (a implementação verifica diariamente); o worker também reconcilia histórico a cada 15 minutos em lotes para cobrir notificações perdidas. `expiration` vem em **epoch milissegundos**. Duplicatas são toleradas pelo ID externo por workspace/conexão.
5. Se `history.list` retornar 404, a conexão exibe **lacuna de histórico** e pausa o processamento. Não fazemos leitura integral automática da caixa. O usuário pode aceitar explicitamente um novo baseline em Integrações; emails da lacuna **não são recuperados** por essa operação.

## E-mail: sai da conta do usuário, não existe SMTP

A ação `SEND_EMAIL` **não usa SMTP da plataforma**. Ela resolve a conexão Gmail ativa do próprio usuário (exigindo exatamente uma) e envia via proxy Nango em `gmail/v1/users/me/messages/send`, com a mensagem MIME completa em base64url. O remetente é a conta conectada — o Anotaí não falsifica um `From`.

- Sem conta Gmail conectada: a ação falha com `Conecte seu Gmail para enviar email` e nada é enviado. **Não existe fallback** para outro transporte.
- Sem `NANGO_ENV_KEY`/`NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY`: falha com `Gmail não está configurado`.
- Duas conexões ativas para o mesmo usuário: falha por ambiguidade de identidade (a constraint `uq_integration_connection_owner_provider` mantém uma conexão por provedor por usuário justamente para isso).
- O destinatário é o que a ação configurar; o `user.email` da conta Anotaí **não** é usado como remetente.
- O `Message-ID` deriva da chave de idempotência da execução: uma tentativa repetida não gera outro e-mail.

## Como adicionar um novo provedor

1. Adicione uma linha em `PROVIDERS` em `app/services/provider_registry.py` (provider, `config_key_setting`, label, descrição, capacidades, nota de escopo e status).
2. Amplie o CHECK `ck_integration_connection_provider` no modelo `app/models/integration_connection.py` **e** crie uma migration que dropa e recria a constraint pelo mesmo nome — isso só amplia o conjunto permitido, sem tocar em dados.
3. Adicione a chave `NANGO_<APP>_PROVIDER_CONFIG_KEY` em `app/core/config.py` (default vazio = app desativado).
4. Só então exponha conexão na tela de Integrações e implemente a execução. Enquanto a chave estiver vazia, o app aparece como indisponível em vez de oferecer um botão que quebraria no Nango.
5. Atualize a tabela de escopos deste runbook.

Regra: `ConnectableIntegrationProvider` em `app/schemas/integration.py` é o que a tela pode conectar **hoje**. Um provedor só entra nessa lista quando existe código de execução real — declarar no registro não é o mesmo que funcionar.

## Agenda e limites operacionais

- A ação Calendar usa somente a conexão Agenda pertencente ao mesmo workspace/usuário, cria evento no calendário `primary` e não convida participantes. A entrada da ação não escolhe conexão, token ou calendarId. O modo sugestivo pede confirmação; ação automática requer preferência e autorização explícitas.
- Envio Telegram permanece *at-least-once*: falha entre aceite pelo provedor e confirmação no banco pode repetir a mensagem. O registro de execução limita tentativas e não promete entrega exatamente uma vez.
- A integração MCP antiga mantém compatibilidade de backend/template, mas não é mostrada nas novas telas; credenciais MCP antigas não são transferidas para o Nango sem novo consentimento.
- Este documento não comprova configuração real do Nango/Google. Faça smoke em conta de teste: conectar Gmail → ativar policy → confirmar watch ativo sem aguardar o cron do dia seguinte → receber email permitido → verificar evento e sugestão únicos; bloquear remetente e verificar ausência de corpo/evento; **desconectar e reconectar Gmail com um novo ID Nango** → conferir vínculo à mesma conta do Anotaí e necessidade de novo opt-in; conectar Agenda e confirmar evento teste antes de autorizar automação. Não use comunicações reais sensíveis neste smoke.

## Evidência local

Backend: `AUDIO_UPLOAD_DIR=/tmp/opencode/anotai-audio pytest -q -p no:cacheprovider`. Web: `npm run build` em `apps/web`. A cópia atual não contém `.git`; não há commits por fase nesta cópia. Ver [plano e riscos](evolucao-arquitetural-v2.md).
