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
- **Notas** — busca instantânea, lista + detalhe com transcrição, JSON editável, logs,
  reclassificação manual e "Re-disparar ações"
- **Inbox** — triagem das sugestões do modo contínuo (aceitar com prazo/lembrete → Kanban, descartar)
- **Kanban** — colunas `todo`/`in_progress`/`done` com botões ◀ ▶ e prazos
- **Marketplace** — clonar templates públicos
- **MCP** — cadastrar/testar/remover servidores (auth livre por fluxo)
- **Segredos** — upsert com máscara + apagar

## Checagem e build

```bash
npm run typecheck
npm run build   # gera dist/
```
