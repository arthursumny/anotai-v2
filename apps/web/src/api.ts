/** Cliente HTTP do AnotAI Web (axios + Bearer + baseURL configurável). */

import axios from "axios";
import type {
  ActionExecution,
  ActionExecutionDetail,
  AuditPage,
  AutomationSettings,
  IntegrationConnection,
  IntegrationProvider,
  KanbanBoard,
  LocalizationSettings,
  MCPProvider,
  MCPServer,
  Note,
  PendencyFeed,
  PendencyKind,
  PrivacyDeleteResult,
  PrivacySource,
  PrivacySummary,
  RoutingRule,
  Secret,
  SourcePolicy,
  Template,
  TemplateDraft,
  User,
  WhatsAppConversationMeta,
  WhatsAppStatus,
  Workflow,
} from "./types";

const fromEnv = (import.meta.env.VITE_API_URL as string | undefined)?.trim();
export const API_BASE_URL = fromEnv && fromEnv.length > 0 ? fromEnv : "/api/v1";

let authToken: string | null = localStorage.getItem("anotai_token");

export function setAuthToken(token: string | null): void {
  authToken = token;
  if (token) localStorage.setItem("anotai_token", token);
  else localStorage.removeItem("anotai_token");
}

export function getAuthToken(): string | null {
  return authToken;
}

export const api = axios.create({ baseURL: API_BASE_URL, timeout: 30000 });

api.interceptors.request.use((config) => {
  if (authToken) config.headers.setAuthorization(`Bearer ${authToken}`);
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error) && error.response?.status === 401 && authToken) {
      const requestUrl = String(error.config?.url ?? "");
      const isAuthenticationAttempt = /\/auth\/(login|register)(?:\?|$)/.test(requestUrl);
      if (!isAuthenticationAttempt) window.dispatchEvent(new Event("anotai:unauthorized"));
    }
    return Promise.reject(error);
  },
);

export function apiError(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = (err.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string") return detail;
    if (err.response) return `Erro ${err.response.status}`;
    return "Falha de rede — confira se o backend está no ar.";
  }
  return err instanceof Error ? err.message : "Erro inesperado";
}

/** Item de fila que já mudou de estado no backend (404/409): mensagenzinha amigável. */
export function isStaleItemError(err: unknown): boolean {
  return axios.isAxiosError(err) && (err.response?.status === 404 || err.response?.status === 409);
}

// --- auth ---
export async function login(email: string, password: string): Promise<string> {
  const form = new FormData();
  form.append("username", email.trim().toLowerCase());
  form.append("password", password);
  const { data } = await api.post<{ access_token: string }>("/auth/login", form);
  return data.access_token;
}

export async function register(name: string, email: string, password: string): Promise<void> {
  await api.post("/auth/register", { name: name.trim(), email: email.trim().toLowerCase(), password });
}

export async function fetchMe(): Promise<User> {
  return (await api.get<User>("/auth/me")).data;
}

// --- áudio ---
export async function uploadAudio(blob: Blob, filename: string): Promise<{ note_id: string; job_id: string }> {
  const form = new FormData();
  form.append("file", blob, filename);
  const { data } = await api.post("/audio/process", form, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 120000,
  });
  return data;
}

// --- notas ---
export const listNotes = async (): Promise<Note[]> =>
  (await api.get<Note[]>("/notes", { params: { limit: 200 } })).data;

export const getNote = async (id: string): Promise<Note> =>
  (await api.get<Note>(`/notes/${id}`)).data;

export async function patchNote(id: string, patch: Record<string, unknown>): Promise<Note> {
  return (await api.patch<Note>(`/notes/${id}`, patch)).data;
}

export async function reclassifyNote(id: string, templateId: string): Promise<Note> {
  return (await api.post<Note>(`/notes/${id}/reclassify`, { template_id: templateId })).data;
}

export async function redispatchNote(id: string): Promise<Note> {
  return (await api.post<Note>(`/notes/${id}/dispatch`)).data;
}

export async function sendNoteToKanban(id: string): Promise<Note> {
  return (await api.post<Note>(`/notes/${id}/to-kanban`)).data;
}

export async function splitTasksToKanban(id: string): Promise<Note[]> {
  return (await api.post<Note[]>(`/notes/${id}/tasks-to-kanban`)).data;
}

export async function deleteNote(id: string): Promise<void> {
  await api.delete(`/notes/${id}`);
}

// --- templates / marketplace ---
export const listTemplates = async (): Promise<Template[]> =>
  (await api.get<Template[]>("/templates", { params: { limit: 100 } })).data;

export const listMarketplace = async (): Promise<Template[]> =>
  (await api.get<Template[]>("/marketplace", { params: { limit: 200 } })).data;

export async function cloneTemplate(id: string): Promise<Template> {
  return (await api.post<Template>(`/marketplace/${id}/clone`)).data;
}

export async function createTemplate(body: Record<string, unknown>): Promise<Template> {
  return (await api.post<Template>("/templates", body)).data;
}

export async function updateTemplate(id: string, body: Record<string, unknown>): Promise<Template> {
  return (await api.patch<Template>(`/templates/${id}`, body)).data;
}

export async function deleteTemplate(id: string): Promise<void> {
  await api.delete(`/templates/${id}`);
}

/** "Construir com IA": rascunho a partir de descrição livre (nada é salvo). */
export async function draftTemplate(description: string): Promise<TemplateDraft> {
  return (await api.post<TemplateDraft>("/templates/draft", { description }, { timeout: 120000 })).data;
}

/** Deriva gatilho+campos a partir do exemplo (fonte da verdade). */
export async function deriveTemplate(
  example_output: string,
): Promise<{ semantic_trigger: string; fields: TemplateDraft["fields"]; suggested_message: string }> {
  return (await api.post("/templates/derive", { example_output }, { timeout: 120000 })).data;
}

// --- regras fortes de roteamento do template ---
export const listRoutingRules = async (templateId: string): Promise<RoutingRule[]> =>
  (await api.get<RoutingRule[]>(`/templates/${templateId}/routing-rules`)).data;

export async function createRoutingRule(
  templateId: string,
  body: Record<string, unknown>,
): Promise<RoutingRule> {
  return (await api.post<RoutingRule>(`/templates/${templateId}/routing-rules`, body)).data;
}

export async function updateRoutingRule(
  templateId: string,
  ruleId: string,
  body: Record<string, unknown>,
): Promise<RoutingRule> {
  return (await api.patch<RoutingRule>(`/templates/${templateId}/routing-rules/${ruleId}`, body)).data;
}

export async function deleteRoutingRule(templateId: string, ruleId: string): Promise<void> {
  await api.delete(`/templates/${templateId}/routing-rules/${ruleId}`);
}

// --- fluxos declarativos (workflows) ---
export const listWorkflows = async (): Promise<Workflow[]> =>
  (await api.get<Workflow[]>("/workflows")).data;

export async function createWorkflow(body: Record<string, unknown>): Promise<Workflow> {
  return (await api.post<Workflow>("/workflows", body)).data;
}

export async function updateWorkflow(id: string, body: Record<string, unknown>): Promise<Workflow> {
  return (await api.patch<Workflow>(`/workflows/${id}`, body)).data;
}

export async function deleteWorkflow(id: string): Promise<void> {
  await api.delete(`/workflows/${id}`);
}

// Preferências de automação do fluxo (modo geral + decisão por tipo de ação).
export async function getWorkflowSettings(): Promise<AutomationSettings> {
  return (await api.get<AutomationSettings>("/workflows/settings")).data;
}

export async function putWorkflowSettings(body: {
  automation_mode: string;
  action_thresholds: Record<string, number>;
  action_config: Record<string, string>;
  notifications_enabled?: boolean;
  quiet_start_minute?: number | null;
  quiet_end_minute?: number | null;
}): Promise<AutomationSettings> {
  return (await api.put<AutomationSettings>("/workflows/settings", body)).data;
}

// --- privacidade (resumo + apagar dados de uma fonte, com confirmação digitada) ---
export async function getPrivacySummary(source: PrivacySource): Promise<PrivacySummary> {
  return (await api.get<PrivacySummary>("/privacy/summary", { params: { source } })).data;
}

export async function deletePrivacyData(source: PrivacySource): Promise<PrivacyDeleteResult> {
  return (await api.post<PrivacyDeleteResult>("/privacy/delete-data", { source, confirm: "APAGAR" })).data;
}

// --- auditoria (somente leitura, sem conteúdo cru) ---
export async function listAudit(limit = 25, offset = 0): Promise<AuditPage> {
  return (await api.get<AuditPage>("/audit", { params: { limit, offset } })).data;
}

// --- fuso do espaço e idioma ---
export async function getLocalization(): Promise<LocalizationSettings> {
  return (await api.get<LocalizationSettings>("/settings/localization")).data;
}

export async function patchLocalization(body: { timezone?: string; locale?: string }): Promise<LocalizationSettings> {
  return (await api.patch<LocalizationSettings>("/settings/localization", body)).data;
}

// --- execuções de fluxo: lista é resumo sem conteúdo; conteúdo só no detalhe ---
export const listWorkflowExecutions = async (): Promise<ActionExecution[]> =>
  (await api.get<ActionExecution[]>("/workflows/executions", { params: { limit: 50 } })).data;

export async function getWorkflowExecution(id: string): Promise<ActionExecutionDetail> {
  return (await api.get<ActionExecutionDetail>(`/workflows/executions/${id}`)).data;
}

export async function confirmWorkflowExecution(id: string): Promise<ActionExecution> {
  return (await api.post<ActionExecution>(`/workflows/executions/${id}/confirm`)).data;
}

export async function dismissWorkflowExecution(id: string): Promise<ActionExecution> {
  return (await api.post<ActionExecution>(`/workflows/executions/${id}/dismiss`)).data;
}

// --- inbox / kanban ---
export const listSuggested = async (): Promise<Note[]> =>
  (await api.get<Note[]>("/inbox/suggested", { params: { limit: 100 } })).data;

export async function acceptSuggestion(
  id: string,
  body: { kanban_status?: string; due_date?: string | null; reminder_at?: string | null },
): Promise<Note> {
  return (await api.post<Note>(`/inbox/${id}/accept`, body)).data;
}

export async function discardSuggestion(id: string): Promise<Note> {
  return (await api.post<Note>(`/inbox/${id}/discard`)).data;
}

export const getBoard = async (): Promise<KanbanBoard> =>
  (await api.get<KanbanBoard>("/kanban")).data;

export async function moveCard(id: string, kanban_status: string, due_date?: string | null): Promise<Note> {
  return (await api.patch<Note>(`/kanban/${id}/move`, { kanban_status, due_date })).data;
}

// --- segredos ---
export const listSecrets = async (): Promise<Secret[]> =>
  (await api.get<Secret[]>("/secrets")).data;

export async function upsertSecret(key: string, value: string): Promise<Secret> {
  return (await api.post<Secret>("/secrets", { key, value })).data;
}

export async function deleteSecret(key: string): Promise<void> {
  await api.delete(`/secrets/${encodeURIComponent(key)}`);
}

// --- telegram da plataforma ---
export async function getTelegramBot(): Promise<{ configured: boolean; username: string | null }> {
  return (await api.get("/telegram/bot")).data;
}

export async function testTelegram(): Promise<{ ok: boolean; chat_id: string }> {
  return (await api.post("/telegram/test")).data;
}

// --- MCP ---
export async function listMcpProviders(): Promise<MCPProvider[]> {
  return (await api.get<MCPProvider[]>("/mcp/providers")).data;
}

export async function startMcpProviderOAuth(providerId: string): Promise<{ authorization_url: string }> {
  return (await api.post<{ authorization_url: string }>(`/mcp/providers/${providerId}/oauth/start`)).data;
}

export const listServers = async (): Promise<MCPServer[]> =>
  (await api.get<MCPServer[]>("/mcp", { params: { limit: 100 } })).data;

export async function createServer(body: Record<string, unknown>): Promise<MCPServer> {
  return (await api.post<MCPServer>("/mcp", body)).data;
}

export async function testServer(id: string): Promise<{ ok: boolean; tools: { name: string }[] }> {
  return (await api.post(`/mcp/${id}/test`)).data;
}

export async function deleteServer(id: string): Promise<void> {
  await api.delete(`/mcp/${id}`);
}

// --- feed único de pendências ("Para revisar") ---
export async function listPendencies(limit = 25, offset = 0): Promise<PendencyFeed> {
  return (await api.get<PendencyFeed>("/pendencies", { params: { limit, offset } })).data;
}

export async function acceptPendency(
  kind: PendencyKind,
  id: string,
  body: { title?: string; due_at?: string; remind_at?: string } = {},
): Promise<void> {
  await api.post(`/pendencies/${kind}/${id}/accept`, body);
}

export async function ignorePendency(kind: PendencyKind, id: string): Promise<void> {
  await api.post(`/pendencies/${kind}/${id}/ignore`);
}

export async function snoozePendency(kind: PendencyKind, id: string, untilIso: string): Promise<void> {
  await api.post(`/pendencies/${kind}/${id}/snooze`, { until: untilIso });
}

export async function editPendency(
  kind: PendencyKind,
  id: string,
  body: { title?: string; due_at?: string; remind_at?: string },
): Promise<void> {
  await api.patch(`/pendencies/${kind}/${id}/edit`, body);
}

export async function resolveFollowUp(id: string): Promise<void> {
  await api.post(`/pendencies/follow_up/${id}/resolve`);
}

// --- integrações Google (sessão de conexão + status local) ---
/** Cria a sessão de conexão. O token retornado vive SÓ em memória na página. */
export async function createIntegrationSession(
  provider: IntegrationProvider,
): Promise<{ session_token: string; expires_at: string }> {
  return (await api.post("/integrations/sessions", { provider })).data;
}

export const listIntegrationConnections = async (): Promise<IntegrationConnection[]> =>
  (await api.get<IntegrationConnection[]>("/integrations/connections")).data;

export async function deleteIntegrationConnection(id: string): Promise<void> {
  await api.delete(`/integrations/connections/${id}`);
}

/** Recomeça o acompanhamento do Gmail a partir de agora, após intervalo (opt-in explícito). */
export async function resumeGmailConnection(id: string): Promise<IntegrationConnection> {
  return (await api.post<IntegrationConnection>(`/integrations/connections/${id}/resume-gmail`, {
    accept_gap: true,
  })).data;
}

// --- política de acompanhamento do Gmail (opt-in explícito; desativar apaga) ---
export async function getGmailPolicy(): Promise<SourcePolicy> {
  return (await api.get<SourcePolicy>("/source-policies/gmail")).data;
}

export async function putGmailPolicy(body: Record<string, unknown>): Promise<SourcePolicy> {
  return (await api.put<SourcePolicy>("/source-policies/gmail", body)).data;
}

export async function deleteGmailPolicy(): Promise<SourcePolicy> {
  return (await api.delete<SourcePolicy>("/source-policies/gmail")).data;
}

// --- WhatsApp pessoal (sessão via QR transitório; nada de token exposto) ---
export async function connectWhatsApp(): Promise<{ status: string; instance_id: string }> {
  return (await api.post("/whatsapp/connect")).data;
}

export async function getWhatsAppQr(): Promise<{ qr: string }> {
  return (await api.get("/whatsapp/qr")).data;
}

export async function getWhatsAppStatus(): Promise<WhatsAppStatus> {
  return (await api.get<WhatsAppStatus>("/whatsapp/status")).data;
}

export async function disconnectWhatsApp(): Promise<{ status: string; message: string }> {
  return (await api.delete("/whatsapp/disconnect")).data;
}

/** Metadados (nome de exibição + identificador), nunca conteúdo de mensagem. */
export const listWhatsAppContacts = async (): Promise<WhatsAppConversationMeta[]> =>
  (await api.get<WhatsAppConversationMeta[]>("/whatsapp/contacts")).data;

export const listWhatsAppGroups = async (): Promise<WhatsAppConversationMeta[]> =>
  (await api.get<WhatsAppConversationMeta[]>("/whatsapp/groups")).data;

// --- política de acompanhamento do WhatsApp (opt-in explícito; desativar apaga) ---
export async function getWhatsAppPolicy(): Promise<SourcePolicy> {
  return (await api.get<SourcePolicy>("/source-policies/whatsapp")).data;
}

export async function putWhatsAppPolicy(body: Record<string, unknown>): Promise<SourcePolicy> {
  return (await api.put<SourcePolicy>("/source-policies/whatsapp", body)).data;
}

export async function deleteWhatsAppPolicy(): Promise<SourcePolicy> {
  return (await api.delete<SourcePolicy>("/source-policies/whatsapp")).data;
}
