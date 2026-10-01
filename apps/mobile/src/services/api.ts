/** AnotAI mobile — camada de API (axios + Bearer + endpoints do backend). */

import axios, { type AxiosInstance } from "axios";
import Constants from "expo-constants";

import type { AudioProcessAccepted, Note, NotePatch, Secret, Template, User } from "../types";

const FALLBACK_API_URL = "http://192.168.0.10:8000/api/v1";

/** baseURL dinâmica: `extra.apiUrl` do app.json (troque pelo IP LAN da sua máquina). */
export function resolveBaseUrl(): string {
  const extra = Constants.expoConfig?.extra as { apiUrl?: string } | undefined;
  const url = extra?.apiUrl?.trim();
  return url && url.length > 0 ? url : FALLBACK_API_URL;
}

let authToken: string | null = null;

/** Chamado pelo AuthContext após login/logout. */
export function setAuthToken(token: string | null): void {
  authToken = token;
}

export const api: AxiosInstance = axios.create({
  baseURL: resolveBaseUrl(),
  timeout: 30000,
});

api.interceptors.request.use((config) => {
  if (authToken) {
    config.headers.setAuthorization(`Bearer ${authToken}`);
  }
  return config;
});

export function apiErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data as { detail?: unknown } | undefined;
    if (typeof detail?.detail === "string") return detail.detail;
    if (err.response) return `Erro ${err.response.status}`;
    if (err.code === "ECONNABORTED") return "Tempo esgotado. Verifique o IP do backend.";
    return "Falha de rede. Confira a URL do backend (app.json → extra.apiUrl).";
  }
  return err instanceof Error ? err.message : "Erro inesperado";
}

// --- Auth ---
export async function login(email: string, password: string): Promise<string> {
  const form = new FormData();
  form.append("username", email.trim().toLowerCase());
  form.append("password", password);
  const { data } = await api.post<{ access_token: string }>("/auth/login", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data.access_token;
}

export async function register(name: string, email: string, password: string): Promise<User> {
  const { data } = await api.post<User>("/auth/register", {
    name: name.trim(),
    email: email.trim().toLowerCase(),
    password,
  });
  return data;
}

export async function fetchMe(): Promise<User> {
  const { data } = await api.get<User>("/auth/me");
  return data;
}

// --- Áudio ---
export async function uploadAudio(uri: string, mimeType = "audio/m4a"): Promise<AudioProcessAccepted> {
  const form = new FormData();
  const ext = mimeType === "audio/wav" ? "wav" : "m4a";
  const name = uri.split("/").pop() || `recording-${Date.now()}.${ext}`;
  form.append("file", { uri, name, type: mimeType } as unknown as Blob);
  const { data } = await api.post<AudioProcessAccepted>("/audio/process", form, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 60000,
  });
  return data;
}

// --- Notas ---
export async function listNotes(): Promise<Note[]> {
  const { data } = await api.get<Note[]>("/notes", { params: { limit: 100 } });
  return data;
}

export async function getNote(noteId: string): Promise<Note> {
  const { data } = await api.get<Note>(`/notes/${noteId}`);
  return data;
}

/** Reclassificação manual: associa um template à nota e roda extração + ações. */
export async function reclassifyNote(noteId: string, templateId: string): Promise<Note> {
  const { data } = await api.post<Note>(`/notes/${noteId}/reclassify`, { template_id: templateId });
  return data;
}

/** Edição human-in-the-loop: corrige transcrição/JSON (validado contra o schema). */
export async function updateNote(noteId: string, patch: NotePatch): Promise<Note> {
  const { data } = await api.patch<Note>(`/notes/${noteId}`, patch);
  return data;
}

/** Re-dispara as ações do template vinculado (após correção manual do JSON). */
export async function redispatchNote(noteId: string): Promise<Note> {
  const { data } = await api.post<Note>(`/notes/${noteId}/dispatch`);
  return data;
}

/** Envia nota processada (inclusive nota geral) ao Kanban. */
export async function sendNoteToKanban(noteId: string): Promise<Note> {
  const { data } = await api.post<Note>(`/notes/${noteId}/to-kanban`);
  return data;
}

/** Cada tarefa da nota geral vira um card no Kanban. */
export async function splitTasksToKanban(noteId: string): Promise<Note[]> {
  const { data } = await api.post<Note[]>(`/notes/${noteId}/tasks-to-kanban`);
  return data;
}

/** Apaga a nota (logs vão junto). */
export async function deleteNote(noteId: string): Promise<void> {
  await api.delete(`/notes/${noteId}`);
}

// --- Segredos ---
export async function listSecrets(): Promise<Secret[]> {
  const { data } = await api.get<Secret[]>("/secrets");
  return data;
}

export async function upsertSecret(key: string, value: string): Promise<Secret> {
  const { data } = await api.post<Secret>("/secrets", { key, value });
  return data;
}

export async function deleteSecret(key: string): Promise<void> {
  await api.delete(`/secrets/${encodeURIComponent(key)}`);
}

// --- Templates / Marketplace ---
export async function listTemplates(): Promise<Template[]> {
  const { data } = await api.get<Template[]>("/templates", { params: { limit: 100 } });
  return data;
}

export async function getTemplate(templateId: string): Promise<Template> {
  const { data } = await api.get<Template>(`/templates/${templateId}`);
  return data;
}

export async function listMarketplace(): Promise<Template[]> {
  const { data } = await api.get<Template[]>("/marketplace", { params: { limit: 100 } });
  return data;
}

export async function cloneTemplate(templateId: string): Promise<Template> {
  const { data } = await api.post<Template>(`/marketplace/${templateId}/clone`);
  return data;
}
