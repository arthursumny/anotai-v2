# AnotAI Web

Cliente web (Vite + React 18 + TypeScript) do backend AnotAI — para usar no **computador**, com as mesmas funções do app mobile mais Kanban e MCP.

## Rodar

```bash
cd apps/web
npm install
npm run dev
```

Abra http://localhost:5173. O Vite faz proxy de `/api/*` para `http://localhost:8000`
(backend via `docker compose up`), então **funciona sem configurar nada** no PC.

Para apontar para outro host (ex: celular ou outra máquina na rede):

```bash
echo 'VITE_API_URL=http://192.168.1.11:8000/api/v1' > .env
npm run dev
```

Login demo: `demo@anotai.com` / `demo12345` (crie no backend se necessário).

## Telas

- **Landing (`/` deslogado)** — hero com mock de celular animado, marquee, "como funciona", recursos, exemplos e CTA
- **Login (`/login`)** — split-screen com painel de marca, mostrar/ocultar senha, validação e `?modo=cadastro`
- **Gravar** — botão pulsante com anéis de energia, waveform viva em canvas (WebAudio), timer, nível de voz, barra de progresso e card de resultado
- **Notas** — documentos Markdown (`body`) editáveis, título e resumo; campos estruturados e transcrição original são secundários. Sem template compatível, a nota geral também é gerada e salva. Falhas de geração permanecem pendentes com logs e "Tentar gerar novamente" (`POST /notes/{id}/retry`, usa a transcrição salva). A reclassificação manual corrige o modelo.
- **Automações** — Email, Criar tarefa, Telegram, Webhook e configuração MCP real. `enabled` e `require_confirmation` são explícitos. Email usa `subject: "{title}"`, `body: "{summary}"` e `to` opcional (email da conta); tarefa usa `title: "{title}"`, `summary: "{summary}"` e `due_at` opcional. Ações desconhecidas ficam visíveis e bloqueiam o save até remoção explícita.
- **Revisar** — feed de pendências das integrações e aprovações de ações duráveis.
- **Fluxos** — livro de execuções `ActionExecution`; "Preparar ações" (`POST /notes/{id}/dispatch`) é idempotente, não reenvia ações existentes.
- **Kanban** — tarefas `TaskRead` nas colunas `todo`/`in_progress`/`done`, movidas por ID da tarefa e ligadas à nota por `source_note_id`; prazos usam `due_at` e lembretes `remind_at`.
- **Marketplace** — clonar templates públicos
- **MCP** — cadastrar/testar/remover servidores (auth livre por fluxo)
- **Segredos** — upsert com máscara + apagar

## Checagem e build

```bash
npm run typecheck
npm run build   # gera dist/
```
