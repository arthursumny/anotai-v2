/** AnotAI mobile — tipos espelhando o backend (Pydantic v2). */

export type NoteStatus = "pending_review" | "processed" | "discarded";
export type ExecutionStatus = "success" | "failed";
export type ActionType = "telegram" | "webhook" | "mcp_tool" | "email" | "create_task";

export interface User {
  id: string;
  name: string;
  email: string;
  created_at: string;
}

export interface AuthToken {
  access_token: string;
  token_type: string;
}

export interface TemplateAction {
  type: ActionType;
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

export interface AudioProcessAccepted {
  note_id: string;
  job_id: string;
  status: string;
  message: string;
}

export interface Secret {
  id: string;
  key: string;
  value_masked: string;
  created_at: string;
}

export interface NotePatch {
  body?: string;
  summary?: string;
  raw_transcription?: string;
  structured_data?: Record<string, unknown> | null;
  template_id?: string | null;
  title?: string;
  due_date?: string | null;
  reminder_at?: string | null;
}

export interface TaskRead {
  id: string;
  title: string;
  summary: string;
  status: string;
  kanban_status: "todo" | "in_progress" | "done";
  due_at: string | null;
  remind_at: string | null;
  source_note_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ActionExecution {
  id: string;
  trigger_type: string;
  action_type: string;
  status: string;
  attempts: number;
  created_at: string;
  last_error: string | null;
  source_id: string | null;
}
