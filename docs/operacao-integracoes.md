# Integrações Google — configuração e verificação

As credenciais OAuth de terceiros ficam no Nango, **não** no banco do Anotaí. Esta cópia não traz chaves nem um projeto Google configurado; testes locais usam mocks. Configure um ambiente de teste antes de considerar Gmail/Agenda operacionais.

## Nango

1. Crie duas integrações no ambiente Nango escolhido: Gmail com `https://www.googleapis.com/auth/gmail.readonly`, e Agenda com `https://www.googleapis.com/auth/calendar.events`. Configure no painel do Nango as credenciais OAuth do aplicativo Google e os consentimentos/redirects que ele indicar. Os nomes das integrações devem coincidir com `NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY` e `NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY`.
2. Configure no backend `NANGO_ENV_KEY` (chave **de ambiente**, com acesso a Connect Sessions/proxy/connections), `NANGO_BASE_URL` (por exemplo `https://api.nango.dev`), `NANGO_ENVIRONMENT` correspondente ao ambiente dos webhooks e `NANGO_SIGNING_KEY` (chave **de assinatura do webhook**, distinta da API key). Nunca publique essas chaves no frontend.
3. No painel Nango, habilite o webhook de auth em `https://<api-publica>/api/v1/integrations/webhooks/nango` e o envio de eventos de criação. A API verifica `X-Nango-Hmac-Sha256` sobre os bytes originais, reconsulta a conexão no Nango e só a vincula após conferir as tags de usuário/workspace e a membership. Tokens OAuth não são copiados para o Anotaí.

## Gmail Push / Google Cloud

1. Habilite Gmail API e Pub/Sub no projeto Google. Crie um tópico e configure `GMAIL_PUBSUB_TOPIC=projects/<projeto>/topics/<topico>`. Conceda a `gmail-api-push@system.gserviceaccount.com` permissão de publicação no tópico, conforme [guia oficial Gmail Push](https://developers.google.com/gmail/api/guides/push).
2. Crie uma assinatura push autenticada com URL HTTPS pública `https://<api-publica>/api/v1/gmail/push`, uma service account de envio OIDC e audience exata correspondente à URL configurada. Defina `GMAIL_PUSH_AUDIENCE` e `GMAIL_PUSH_SA_EMAIL` no backend. Valide permissões de emissão de token no console do seu projeto; não use endpoint público sem autenticação OIDC.
3. Ao conectar Gmail, o acompanhamento **continua desligado**. Na tela Integrações, ative-o explicitamente e escolha remetentes/labels; o padrão é `INBOX`, com SPAM/TRASH ignorados. O worker registra/renova o watch com esse tópico. A notificação Pub/Sub só enfileira trabalho após verificar assinatura, audience, service account, conexão, watch e opt-in; o histórico do Gmail é a fonte de verdade.
4. O watch expira: renovar pelo menos a cada sete dias (a implementação verifica diariamente); o worker também reconcilia histórico a cada 15 minutos em lotes para cobrir notificações perdidas. `expiration` vem em **epoch milissegundos**. Duplicatas são toleradas pelo ID externo por workspace/conexão.
5. Se `history.list` retornar 404, a conexão exibe **lacuna de histórico** e pausa o processamento. Não fazemos leitura integral automática da caixa. O usuário pode aceitar explicitamente um novo baseline em Integrações; emails da lacuna **não são recuperados** por essa operação.

## Agenda e limites operacionais

- A ação Calendar usa somente a conexão Agenda pertencente ao mesmo workspace/usuário, cria evento no calendário `primary` e não convida participantes. A entrada da ação não escolhe conexão, token ou calendarId. O modo sugestivo pede confirmação; ação automática requer preferência e autorização explícitas.
- Envio Telegram permanece *at-least-once*: falha entre aceite pelo provedor e confirmação no banco pode repetir a mensagem. O registro de execução limita tentativas e não promete entrega exatamente uma vez.
- A integração MCP antiga mantém compatibilidade de backend/template, mas não é mostrada nas novas telas; credenciais MCP antigas não são transferidas para o Nango sem novo consentimento.
- Este documento não comprova configuração real do Nango/Google. Faça smoke em conta de teste: conectar Gmail → ativar policy → confirmar watch ativo sem aguardar o cron do dia seguinte → receber email permitido → verificar evento e sugestão únicos; bloquear remetente e verificar ausência de corpo/evento; **desconectar e reconectar Gmail com um novo ID Nango** → conferir vínculo à mesma conta do Anotaí e necessidade de novo opt-in; conectar Agenda e confirmar evento teste antes de autorizar automação. Não use comunicações reais sensíveis neste smoke.

## Evidência local

Backend: `AUDIO_UPLOAD_DIR=/tmp/opencode/anotai-audio pytest -q -p no:cacheprovider`. Web: `npm run build` em `apps/web`. A cópia atual não contém `.git`; não há commits por fase nesta cópia. Ver [plano e riscos](evolucao-arquitetural-v2.md).
