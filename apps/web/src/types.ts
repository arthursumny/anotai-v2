/** Tipos espelhando o backend AnotAI. */

export type NoteStatus = "pending_review" | "processed" | "discarded";
export type KanbanStatus = "todo" | "in_progress" | "done";
export type ExecutionStatus = "success" | "failed";

export interface User {
  id: string;
  name: string;
  email: string;
  created_at: string;
}

export interface TemplateAction {
  type: string;
  config: Record<string, unknown>;
  enabled: boolean;
  require_confirmation?: boolean;
}

export interface Template {
  id: string;
  user_id: string | null;
  title: string;
  purpose: string;
  semantic_trigger: string;
  example_output: string;
  output_schema: Record<string, unknown>;
  actions: TemplateAction[];
  category: string | null;
  use_cases: string[];
  routing_description: string | null;
  version: number;
  is_public: boolean;
  created_at: string;
}

export interface ExecutionLog {
  id: string;
  note_id: string;
  action_type: string;
  status: ExecutionStatus;
  payload: Record<string, unknown>;
  error_message: string | null;
  created_at: string;
}

export interface Note {
  id: string;
  user_id: string;
  template_id: string | null;
  title: string;
  body: string;
  summary: string;
  raw_transcription: string;
  structured_data: Record<string, unknown> | null;
  status: NoteStatus;
  due_date: string | null;
  reminder_at: string | null;
  reminder_sent: boolean;
  created_at: string;
  logs: ExecutionLog[];
}

export interface Secret {
  id: string;
  key: string;
  value_masked: string;
  created_at: string;
}

export interface MCPServer {
  id: string;
  name: string;
  server_url: string;
  auth_header_key: string | null;
  auth_secret_name: string | null;
  provider_id?: string | null;
  is_active: boolean;
  created_at: string;
}

export interface MCPProvider {
  id: string;
  name: string;
  brand: string;
  description: string;
  scope_text: string;
  server_url: string;
  is_ready: boolean;
  setup_hint: string | null;
  is_connected: boolean;
  beta: boolean;
}

export interface TaskRead {
  id: string;
  title: string;
  summary: string;
  status: string;
  kanban_status: KanbanStatus;
  due_at: string | null;
  remind_at: string | null;
  source_note_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface KanbanBoard {
  todo: TaskRead[];
  in_progress: TaskRead[];
  done: TaskRead[];
}

export interface DraftField {
  name: string;
  kind: "texto" | "número" | "data" | "lista" | "sim/não" | "opções";
  required: boolean;
  options: string[];
}

export interface TemplateDraft {
  title: string;
  purpose: string;
  semantic_trigger: string;
  fields: DraftField[];
  suggested_message: string;
  suggested_example: string;
}

/* ------------------------------------------------ fluxos declarativos */

export type WorkflowTrigger =
  | "MANUAL" | "DOCUMENT_GENERATED" | "TASK_CREATED" | "REMINDER_DUE" | "FOLLOW_UP_DUE";

export interface WorkflowStep {
  id: string;
  position: number;
  action_type: string;
  input_template: Record<string, unknown>;
  require_confirmation: boolean;
  enabled: boolean;
}

export interface Workflow {
  id: string;
  title: string;
  description: string;
  trigger_type: string;
  conditions: Record<string, unknown>;
  enabled: boolean;
  version: number;
  steps: WorkflowStep[];
  created_at: string;
  updated_at: string;
}

export type AutomationMode = "suggestive" | "semi_automatic" | "automatic" | "disabled";
export type ActionDisposition = "require_confirmation" | "automatic" | "disabled";

export interface AutomationSettings {
  automation_mode: AutomationMode;
  action_thresholds: Record<string, number>;
  action_config: Record<string, string>;
  /** Avisos por Telegram; horário de silêncio é par (minutos do dia, fuso do espaço). */
  notifications_enabled: boolean;
  quiet_start_minute: number | null;
  quiet_end_minute: number | null;
}

/** Linha do livro-razão de execuções: nunca carrega conteúdo nem snapshots. */
export interface ActionExecution {
  id: string;
  trigger_type: string;
  action_type: string;
  status: string; // awaiting_confirmation | pending | claimed | succeeded | failed
  attempts: number;
  created_at: string;
  /** Já vem sanitizado do backend (mensagem genérica, sem segredo). */
  last_error: string | null;
  source_id: string | null;
}

/** Detalhe sob demanda: preview vem de allowlist do backend (ou indisponível). */
export interface ActionExecutionDetail extends ActionExecution {
  preview_available: boolean;
  preview: Record<string, unknown> | null;
}

/* ------------------------------------------------ privacidade + auditoria */

export type PrivacySource = "audio" | "gmail" | "whatsapp" | "all";

export interface PrivacySummary {
  source: PrivacySource;
  events: number;
  generated_documents: number;
  notes_retained: number;
  reminders_to_cancel: number;
  follow_ups_to_cancel: number;
}

export interface PrivacyCounts {
  events: number;
  generated_documents: number;
  tasks: number;
  reminders: number;
  action_suggestions: number;
  pending_conversation_items: number;
  follow_ups: number;
  failed: number;
}

export interface PrivacyDeleteResult {
  source: PrivacySource;
  counts: PrivacyCounts;
  notes_retained: number;
  complete: boolean;
  notice: string;
}

/** Item de auditoria: sem conteúdo cru, sem segredos; target_ref é interno e não vai à tela. */
export interface AuditEventItem {
  actor: string;
  event: string;
  action: string;
  status: string;
  source: string;
  target_kind: string;
  target_ref: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface AuditPage {
  items: AuditEventItem[];
  limit: number;
  offset: number;
}

export interface LocalizationSettings {
  timezone: string; // IANA válido (ex: America/Sao_Paulo)
  locale: string; // pt-BR | en-US | es-ES
}

/* ------------------------------------------------ feed de pendências */

export type PendencyKind =
  | "task" | "reminder" | "follow_up" | "action" | "approval";

export interface PendencyItem {
  id: string;
  kind: PendencyKind;
  /** follow_up: "suggestion" (ideia aceitável) vs "active" (já aguardando resposta). */
  subkind?: "suggestion" | "active" | null;
  title: string;
  summary?: string | null;
  confidence?: number | null;
  created_at: string;
  due_at?: string | null;
  source?: string | null; // ex: "audio", "gmail"; em approvals vem o gatilho do fluxo
}

export interface PendencyFeed {
  items: PendencyItem[];
  total: number;
  offset: number;
  limit: number;
}

/* ------------------------------------------------ integrações Google */

export type IntegrationProvider = "google_gmail" | "google_calendar";
export type IntegrationStatus = "pending" | "active" | "error" | "disconnected";

export interface IntegrationConnection {
  id: string;
  provider: IntegrationProvider;
  /** Já vem mascarado do backend (ex: a***@gmail.com). */
  account_email: string | null;
  display_name: string | null;
  status: IntegrationStatus;
  monitor_enabled: boolean;
  /** Estado real da sincronização do Gmail (null = nunca configurada). Só vem ao dono. */
  gmail_watch_status: "paused" | "active" | "needs_resync" | null;
  expiration_at: string | null;
}

export type PolicyMode = "ALL" | "ALLOWLIST" | "BLOCKLIST";

export type PolicyDirection = "incoming" | "outgoing" | "both";

/** WhatsApp: status da sessão (telefone já vem mascarado do backend). */
export interface WhatsAppStatus {
  status: string; // pending_qr | connected | disconnected | error
  phone: string | null;
}

/** Metadado de conversa WhatsApp: JID + nome de exibição. Sem conteúdo de mensagem. */
export interface WhatsAppConversationMeta {
  jid: string;
  display_name: string;
  is_group: boolean;
}

export interface SourcePolicy {
  source: string;
  monitor_enabled: boolean;
  mode: PolicyMode;
  /** No Gmail: e-mails de remetentes (conforme o modo). No WhatsApp: JIDs das conversas. */
  selected_ids: string[];
  direction: string;
  allow_groups: boolean;
  allow_individuals: boolean;
  gmail_label_ids: string[];
  gmail_allowed_senders: string[];
  /** Dias de guarda do conteúdo de eventos da fonte; null = regra padrão (30 dias). */
  retention_days: number | null;
}
