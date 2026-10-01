# WhatsApp pessoal via Evolution GO — segurança e operação

> Integração de gateway baseada no [repositório oficial Evolution GO](https://github.com/evolution-foundation/evolution-go/tree/main/docs/wiki), referência da release 0.7.2. O produto não usa o protocolo MCP nem envia tokens do gateway ao navegador. WhatsApp pessoal via `whatsmeow` é uma integração **não oficial**; disponibilidade da conta/número e políticas da plataforma não são garantidas.

## Preparação da infraestrutura

1. Implante o GO em ambiente controlado, com TLS e rede privada quando possível. Não publique sua `GLOBAL_API_KEY` nem a API administrativa. Configure `WHATSAPP_EVOLUTION_BASE_URL`, `WHATSAPP_EVOLUTION_GLOBAL_API_KEY`, `WHATSAPP_TOKEN_ENCRYPTION_KEY` (chave Fernet **exclusiva**, não reutilize a chave MCP) e `WHATSAPP_WEBHOOK_BASE_URL` no backend. Defaults vazios deixam a integração desabilitada.
2. O GO recebe `apikey` no header para operações de administração/instância; **não** usa `Authorization: Bearer`. O Anotaí cifra o token por instância, mantém somente o hash do segredo de webhook e apresenta o QR apenas ao dono, sem armazenamento ou cache do QR.
3. Os docs GO **não especificam assinatura nem autenticação dos webhooks de saída**. O Anotaí usa um segredo aleatório separado por instância na URL do callback e o compara antes de analisar o corpo. Esse segredo pode aparecer em logs de URL do GO, proxy ou servidor: o Compose desabilita access logs do Uvicorn, mas **seu reverse proxy e o próprio GO também precisam omitir/redigir o path**. Não publique a integração até verificar isso no deploy real; rede privada é defesa adicional. Rotacionar o segredo pode exigir recriar a instância e escanear novo QR.
4. Conectar uma sessão **não** autoriza monitoramento. O usuário deve escolher conversas e direção (recebidas/enviadas), indivíduos/grupos. Conversas excluídas e mídia sem texto não são persistidas nem vão para o LLM. Se mensagens enviadas (`fromMe`) não forem permitidas, não se pode inferir com segurança uma pergunta sem resposta.
5. A wiki GO mostra `data.key.remoteJid/fromMe/id` e `message.conversation`, mas nomes de eventos e shape alternativo do Swagger diferem. Até capturar um webhook **real da build 0.7.2**, o parser só aceita os campos documentados sem adivinhar variantes. O conteúdo de mensagens e o segredo de callback nunca pertencem a logs técnicos. Não há backfill histórico automático.

## Gate de produção (pendente de operador)

- Confirmar no Swagger da **instância GO realmente instalada** os métodos de criação, QR, status, logout e o formato exato de webhook. A documentação wiki e OpenAPI não são inteiramente consistentes.
- Usar número de teste: conectar → escanear QR → habilitar uma conversa → enviar mensagem recebida e resposta `fromMe` → confirmar que resposta compatível resolve pergunta pendente e cancela follow-up; enviar mensagem de conversa bloqueada → confirmar **zero** conteúdo/evento/LLM; testar duplicação/retry de webhook. Redigir conteúdo pessoal nas evidências.
- Verificar a higiene de access logs do proxy e do GO para o path com segredo; revisar retenção/exclusão de mensagens armazenadas antes de processar conversas pessoais reais.
- A API de envio de mensagens GO apresenta caminhos divergentes entre README e wiki. **Não** habilitar resposta/envio automático pelo WhatsApp sem validar endpoint da build real; Telegram/in-app permanecem canais alternativos.

## Verificação local

O backend é validado com `AUDIO_UPLOAD_DIR=/tmp/opencode/anotai-audio pytest -q -p no:cacheprovider`. Migrações podem ser testadas em PostgreSQL descartável; não execute testes destrutivos no volume do projeto. Esta cópia não contém `.git`.
