# VoiceFlow: Wearable Audio Ingestion & Autonomous Automation Platform

## 1. Visão Geral do Projeto
O VoiceFlow é uma plataforma de hardware e software que captura áudio contínuo ou pontual (via microfone de lapela BLE baseado no ecossistema Omi, ou gravador no app mobile), transcreve o conteúdo, classifica semanticamente o contexto em "Templates" cadastrados e executa pipelines automatizados (Telegram, WhatsApp, Webhooks, Google Agenda ou chamadas a servidores MCP).

## 2. Fluxo de Dados (Pipeline)
1. **Captura:** O usuário pressiona o botão na lapela (BLE) ou no app mobile para gravar e parar.
2. **Ingestão:** O áudio compactado (Opus/WAV) é enviado para o endpoint `/api/v1/audio/process`.
3. **Transcrição (STT):** O backend executa o Whisper e produz o texto com pontuação.
4. **Roteamento Semântico (Router):** 
   - Uma LLM compara a transcrição com a lista de templates ativos do usuário.
   - Cada template possui um `matching_rule` (descrição de contexto).
   - Se houver match com confiança satisfatória: seleciona o template.
   - Se NÃO houver match: gera sempre uma **Nota Geral** interpretada pela IA
     (título + corpo em markdown com resumo/listas/tópicos + tarefas extraídas
     + prazo/lembrete quando mencionados). A nota geral nasce `processed` com
     `template_id=None`; se for acionável, já entra no Kanban (`todo`).
     Não existe mais nota "sem match" — `no_match` é só legado de leitura.
5. **Extração Estruturada:** O modelo extrai as informações exigidas pelo `json_schema` do template selecionado.
6. **Despacho de Ações (Actions & MCP):** 
   - O orquestrador executa a lista de ações vinculadas (ex: enviar mensagem estruturada no Telegram, disparar webhook, invocar ferramenta de servidor MCP configurado).
7. **Persistência:** O resultado e os logs de execução são salvos no banco de dados e sincronizados no app.

## 3. Modelo de Dados de um Template
Cada template no banco possui a estrutura:
- `id`: UUID
- `user_id`: UUID (ou null se for do sistema/marketplace oficial)
- `title`: Nome do template (ex: "Consulta Veterinária")
- `description`: Finalidade do template
- `semantic_trigger`: Descrição do contexto em linguagem natural para o roteador LLM.
- `output_schema`: JSON Schema que a LLM deve preencher com os dados extraídos.
- `actions`: Lista de ações configuradas:
  - `type`: "telegram" | "whatsapp" | "webhook" | "mcp_tool" | "email"
  - `config`: Objeto com tokens, URLs, IDs de chat ou nome da ferramenta MCP a ser chamada.
- `is_public`: Boolean (disponível no Marketplace).

## 4. Stack Tecnológica
- **Backend:** Python 3.11+, FastAPI, SQLAlchemy (ou SQLModel), Pydantic v2.
- **Filas e Cache:** Redis + ARQ ou Celery para processamento assíncrono de áudios longos.
- **Banco de Dados:** PostgreSQL (com suporte a pgvector opcional para busca de templates/memória).
- **LLM & STT:** OpenAI API / Groq / Anthropic (suporte a Structured Outputs) + Faster-Whisper / OpenAI Whisper.
- **Integração Externa:** Python-telegram-bot, httpx (para webhooks e chamadas HTTP MCP).
- **Mobile (Cliente):** React Native (Expo) com biblioteca react-native-ble-plx.