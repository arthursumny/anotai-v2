# Privacidade — retenção, exclusão e chave de segredos

## Antes do upgrade F6

1. Faça backup testado do PostgreSQL. Provisione `USER_SECRET_ENCRYPTION_KEY` com uma chave Fernet exclusiva no gerenciador de segredos do ambiente; forneça a **mesma chave** ao processo Alembic, à API e ao worker. Não use a chave MCP ou WhatsApp, nem imprima o valor em logs ou neste repositório.
2. Execute `alembic upgrade head` **somente após** a chave estar disponível. A migration `9c2e7f4a1b63` verifica a chave antes de alterar `user_secrets.value`, muda a coluna para TEXT e cifra os valores legados. Sem chave válida, ela falha fechado: não contorne com plaintext fallback.
3. Confirme em ambiente descartável que o CRUD mascara segredos e que Telegram/dispatcher continuam funcionando. Perder a chave impede a leitura dos segredos existentes. Rotação requer procedimento offline de descriptografar com chave antiga e recifrar com a nova; simplesmente trocar a variável quebra o acesso.
4. **Downgrade dessa migration restaura plaintext** em `user_secrets.value` e exige a chave vigente. Não faça rollback em produção sem autorizar explicitamente esse risco e planejar nova criptografia.

## Retenção e exclusão

- Eventos normalizados de áudio, Gmail e WhatsApp têm retenção padrão de **30 dias a partir da captura**, configurável por fonte monitorada de 1 a 365 dias. A policy ausente/revogada volta ao padrão global. A limpeza diária é limitada e repetível; não envia conteúdo ao LLM.
- Ao expirar um evento, documentos gerados e sugestões derivadas **não aceitas** são removidos antes da raiz. Tarefas/lembretes/acompanhamentos já aceitos permanecem como registros do usuário, desligados da origem. A transcrição em `Note` continua acessível: notas são a superfície legada de produto/Kanban, **não** são apagadas automaticamente junto com a cópia em `NormalizedEvent`.
- A tela Privacidade mostra uma prévia por fonte e exige digitar **APAGAR** para excluir dados de captura em qualquer idade. Lembretes/acompanhamentos vinculados à origem são removidos; notas são preservadas e podem ser excluídas em **Notas**. Caso alguma linha falhe, a resposta indica exclusão parcial e permite tentar novamente; nunca descreva uma operação parcial como completa.
- `AuditEvent` armazena apenas ator, ação, escopo, resultado e contagens permitidas, **não** conteúdo de mensagem, assunto, telefone, e-mail ou token. O histórico é acessível apenas no workspace autorizado e também tem prazo de retenção. Payloads de ações bem-sucedidas são expurgados após o prazo sem apagar o ledger de idempotência.
- A classificação MEMORY é descartada enquanto não houver produto de memória opt-in com leitura, controle e expurgo próprios. **Nenhuma memória pessoal é armazenada automaticamente nesta fase.**

## Horários e canais

- O usuário define fuso IANA do workspace. Horários silenciosos seguem esse fuso; apenas notificações Telegram são adiadas sem consumir tentativas. Criar tarefa/Calendar não é silenciado. Horários absolutos já agendados não são reescritos ao alterar o fuso.
- WhatsApp pessoal usa o [runbook próprio](operacao-whatsapp-evolution-go.md); Gmail e Agenda usam o [runbook Google/Nango](operacao-integracoes.md). Sem as credenciais, topologia e smoke com contas de teste dessas integrações, a suíte local **não** atesta operação real.
- Dependências web têm advisories conhecidos. `npm audit fix --force` migraria versões major de React Router/Vite; avalie num ciclo de upgrade com testes, não durante uma operação de privacidade.
