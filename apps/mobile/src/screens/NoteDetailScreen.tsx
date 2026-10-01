import type { NativeStackScreenProps } from "@react-navigation/native-stack";
import React, { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import StatusBadge from "../components/StatusBadge";
import TemplatePickerModal from "../components/TemplatePickerModal";
import type { NotesStackParamList } from "../navigation/RootNavigator";
import {
  apiErrorMessage,
  deleteNote,
  getNote,
  getTemplate,
  listTemplates,
  reclassifyNote,
  redispatchNote,
  sendNoteToKanban,
  splitTasksToKanban,
  updateNote,
} from "../services/api";
import type { Note, Template } from "../types";

type Props = NativeStackScreenProps<NotesStackParamList, "NoteDetail">;

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.length > 0 ? v.map(String).join(", ") : "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** Texto inicial do editor: primitivas puras, resto em JSON. */
function toEditableText(v: unknown): string {
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (v === null || v === undefined) return "";
  return JSON.stringify(v);
}

/** Converte o texto editado de volta: tenta JSON, senão mantém string. */
function fromEditableText(text: string): unknown {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return text;
  }
}

type GeneralTask = { title: string; due_date?: string | null };

function asTasks(v: unknown): GeneralTask[] {
  if (!Array.isArray(v)) return [];
  const out: GeneralTask[] = [];
  for (const item of v) {
    if (typeof item === "string") {
      if (item.trim()) out.push({ title: item.trim() });
    } else if (item && typeof item === "object") {
      const t = (item as Record<string, unknown>).title;
      if (typeof t === "string" && t.trim()) out.push({ title: t.trim() });
    }
  }
  return out;
}

export default function NoteDetailScreen({ route, navigation }: Props): JSX.Element {
  const { noteId } = route.params;
  const [note, setNote] = useState<Note | null>(null);
  const [templateName, setTemplateName] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [pickerOpen, setPickerOpen] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [busyTemplateId, setBusyTemplateId] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [draftBody, setDraftBody] = useState("");
  const [saving, setSaving] = useState(false);
  const [dispatching, setDispatching] = useState(false);
  const [kanbanBusy, setKanbanBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const fresh = await getNote(noteId);
      setNote(fresh);
      if (fresh.template_id) {
        try {
          setTemplateName((await getTemplate(fresh.template_id)).title);
        } catch {
          setTemplateName(null);
        }
      } else {
        setTemplateName(null);
      }
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [noteId]);

  useEffect(() => {
    void load();
  }, [load]);

  const openPicker = async (): Promise<void> => {
    setPickerOpen(true);
    setTemplatesLoading(true);
    try {
      setTemplates(await listTemplates());
    } catch (e) {
      setError(apiErrorMessage(e));
      setPickerOpen(false);
    } finally {
      setTemplatesLoading(false);
    }
  };

  const handleSelect = async (templateId: string): Promise<void> => {
    setBusyTemplateId(templateId);
    try {
      const updated = await reclassifyNote(noteId, templateId);
      setNote(updated);
      setEditing(false);
      setPickerOpen(false);
      if (updated.template_id) {
        try {
          setTemplateName((await getTemplate(updated.template_id)).title);
        } catch {
          setTemplateName(null);
        }
      } else {
        setTemplateName(null);
      }
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusyTemplateId(null);
    }
  };

  const startEditing = (): void => {
    if (!note?.structured_data) return;
    const init: Record<string, string> = {};
    for (const [k, v] of Object.entries(note.structured_data)) init[k] = toEditableText(v);
    setDraft(init);
    const sd = note.structured_data as Record<string, unknown>;
    setDraftBody(typeof sd.nota === "string" ? sd.nota : "");
    setEditing(true);
  };

  const handleSaveEdit = async (): Promise<void> => {
    if (!note) return;
    const sd = (note.structured_data ?? {}) as Record<string, unknown>;
    const isGeneral = note.template_id === null && typeof sd.nota === "string";
    setSaving(true);
    setError(null);
    try {
      if (isGeneral) {
        setNote(await updateNote(noteId, {
          structured_data: { ...sd, tipo: "nota_geral", nota: draftBody },
        }));
      } else {
        const structured: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(draft)) structured[k] = fromEditableText(v);
        setNote(await updateNote(noteId, { structured_data: structured }));
      }
      setEditing(false);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const handleToKanban = async (): Promise<void> => {
    setKanbanBusy(true);
    setError(null);
    try {
      setNote(await sendNoteToKanban(noteId));
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setKanbanBusy(false);
    }
  };

  const handleTasksToKanban = async (count: number): Promise<void> => {
    setKanbanBusy(true);
    setError(null);
    try {
      await splitTasksToKanban(noteId);
      Alert.alert("Kanban", `${count} tarefa(s) criada(s) em A fazer.`);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setKanbanBusy(false);
    }
  };

  const handleRedispatch = async (): Promise<void> => {
    setDispatching(true);
    setError(null);
    try {
      setNote(await redispatchNote(noteId));
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setDispatching(false);
    }
  };

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" />
      </View>
    );
  }

  if (error && !note) {
    return (
      <View style={styles.center}>
        <Text style={styles.error}>{error}</Text>
        <Pressable style={styles.retry} onPress={() => void load()}>
          <Text style={styles.retryText}>Tentar de novo</Text>
        </Pressable>
      </View>
    );
  }

  if (!note) return <View style={styles.center} />;

  const entries = note.structured_data ? Object.entries(note.structured_data) : [];
  const sd = (note.structured_data ?? {}) as Record<string, unknown>;
  const isGeneral =
    note.template_id === null &&
    (note.status === "processed" || note.status === "no_match") &&
    (typeof sd.nota === "string" || Array.isArray(sd.tarefas));
  const generalBody = typeof sd.nota === "string" ? sd.nota : "";
  const tasks = asTasks(sd.tarefas);

  return (
    <ScrollView style={styles.container}>
      {error && <Text style={styles.error}>{error}</Text>}
      <StatusBadge status={note.status} templateId={note.template_id} />
      {note.title ? <Text style={styles.noteTitle}>{note.title}</Text> : null}
      <Text style={styles.date}>{new Date(note.created_at).toLocaleString()}</Text>

      <Text style={styles.section}>Transcrição</Text>
      <Text style={styles.body}>{note.raw_transcription || "Ainda transcrevendo…"}</Text>

      {isGeneral && (
        <>
          <Text style={styles.section}>Nota geral (IA)</Text>
          {!editing ? (
            <>
              <Text style={styles.body}>{generalBody || "—"}</Text>
              {tasks.length > 0 && (
                <>
                  <Text style={styles.section}>Tarefas ({tasks.length})</Text>
                  {tasks.map((t, i) => (
                    <View key={i} style={styles.kv}>
                      <Text style={styles.k}>•</Text>
                      <Text style={styles.v}>{t.title}</Text>
                    </View>
                  ))}
                </>
              )}
              <View style={styles.sectionRow}>
                <Text style={styles.section}>Kanban</Text>
              </View>
              <Text style={styles.body}>
                {note.kanban_status ? `No quadro: ${note.kanban_status}` : "Fora do quadro"}
              </Text>
              {!note.kanban_status && (
                <Pressable style={[styles.secondary, styles.dispatchBtn]} onPress={() => void handleToKanban()} disabled={kanbanBusy}>
                  {kanbanBusy ? <ActivityIndicator /> : <Text style={styles.secondaryText}>Enviar para o Kanban</Text>}
                </Pressable>
              )}
              {tasks.length > 0 && (
                <Pressable style={[styles.secondary, styles.dispatchBtn]} onPress={() => void handleTasksToKanban(tasks.length)} disabled={kanbanBusy}>
                  {kanbanBusy ? <ActivityIndicator /> : <Text style={styles.secondaryText}>Criar cards das tarefas</Text>}
                </Pressable>
              )}
              <View style={styles.sectionRow}>
                <Text style={styles.section}>Nota</Text>
                <Pressable onPress={startEditing}>
                  <Text style={styles.link}>Editar</Text>
                </Pressable>
              </View>
            </>
          ) : (
            <>
              <Text style={styles.k}>Texto da nota</Text>
              <TextInput
                style={styles.editInput}
                value={draftBody}
                onChangeText={setDraftBody}
                multiline
                editable={!saving}
              />
              <Pressable style={[styles.primary, styles.saveBtn]} onPress={() => void handleSaveEdit()} disabled={saving}>
                {saving ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryText}>Salvar alterações</Text>}
              </Pressable>
              <Pressable onPress={() => setEditing(false)} disabled={saving}>
                <Text style={styles.link}>Cancelar</Text>
              </Pressable>
            </>
          )}
          <Pressable style={styles.secondary} onPress={() => void openPicker()}>
            <Text style={styles.secondaryText}>Classificar com um template</Text>
          </Pressable>
        </>
      )}

      {note.status === "processed" && !isGeneral && (
        <>
          <Text style={styles.section}>Template</Text>
          <Text style={styles.body}>{templateName ?? note.template_id ?? "—"}</Text>

          <View style={styles.sectionRow}>
            <Text style={styles.section}>Dados estruturados</Text>
            {!editing ? (
              <Pressable onPress={startEditing}>
                <Text style={styles.link}>Editar</Text>
              </Pressable>
            ) : (
              <Pressable onPress={() => setEditing(false)} disabled={saving}>
                <Text style={styles.link}>Cancelar</Text>
              </Pressable>
            )}
          </View>
          {entries.length === 0 ? (
            <Text style={styles.body}>—</Text>
          ) : !editing ? (
            entries.map(([k, v]) => (
              <View key={k} style={styles.kv}>
                <Text style={styles.k}>{k}</Text>
                <Text style={styles.v}>{formatValue(v)}</Text>
              </View>
            ))
          ) : (
            <>
              {entries.map(([k]) => (
                <View key={k} style={styles.editRow}>
                  <Text style={styles.k}>{k}</Text>
                  <TextInput
                    style={styles.editInput}
                    value={draft[k] ?? ""}
                    onChangeText={(t) => setDraft((d) => ({ ...d, [k]: t }))}
                    multiline
                    editable={!saving}
                  />
                </View>
              ))}
              <Pressable style={[styles.primary, styles.saveBtn]} onPress={() => void handleSaveEdit()} disabled={saving}>
                {saving ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryText}>Salvar alterações</Text>}
              </Pressable>
            </>
          )}

          <Text style={styles.section}>Ações disparadas</Text>
          {note.logs.length === 0 ? (
            <Text style={styles.body}>Nenhuma ação registrada.</Text>
          ) : (
            note.logs.map((lg) => (
              <View key={lg.id} style={styles.log}>
                <Text style={styles.logTitle}>
                  {lg.action_type} — {lg.status === "success" ? "ok" : "falhou"}
                </Text>
                {lg.error_message && <Text style={styles.logErr}>{lg.error_message}</Text>}
              </View>
            ))
          )}

          <Pressable
            style={[styles.secondary, styles.dispatchBtn]}
            onPress={() => void handleRedispatch()}
            disabled={dispatching}
          >
            {dispatching ? (
              <ActivityIndicator />
            ) : (
              <Text style={styles.secondaryText}>Re-disparar ações</Text>
            )}
          </Pressable>

          <Pressable style={styles.secondary} onPress={() => void openPicker()}>
            <Text style={styles.secondaryText}>Reclassificar com outro template</Text>
          </Pressable>

          <Pressable
            style={[styles.secondary, styles.deleteBtn]}
            onPress={() =>
              Alert.alert("Apagar nota?", "A nota e os logs serão removidos.", [
                { text: "Cancelar", style: "cancel" },
                {
                  text: "Apagar",
                  style: "destructive",
                  onPress: () =>
                    void (async () => {
                      try {
                        await deleteNote(noteId);
                        navigation.goBack();
                      } catch (e) {
                        setError(apiErrorMessage(e));
                      }
                    })(),
                },
              ])
            }
          >
            <Text style={styles.deleteText}>Apagar nota</Text>
          </Pressable>
        </>
      )}

      {note.status === "pending_review" && (
        <>
          <Text style={styles.pending}>Extração pendente de revisão.</Text>
          {note.logs.length > 0 && (
            <>
              <Text style={styles.section}>Histórico</Text>
              {note.logs.map((lg) => (
                <View key={lg.id} style={styles.log}>
                  <Text style={styles.logTitle}>
                    {lg.action_type} — {lg.status === "success" ? "ok" : "falhou"}
                  </Text>
                  {lg.error_message && <Text style={styles.logErr}>{lg.error_message}</Text>}
                </View>
              ))}
            </>
          )}
          <Pressable style={styles.secondary} onPress={() => void openPicker()}>
            <Text style={styles.secondaryText}>Classificar com um template</Text>
          </Pressable>
        </>
      )}

      <TemplatePickerModal
        visible={pickerOpen}
        templates={templates}
        loading={templatesLoading}
        busyId={busyTemplateId}
        onSelect={(id) => void handleSelect(id)}
        onClose={() => setPickerOpen(false)}
      />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#fff", padding: 16 },
  center: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24 },
  error: { color: "#b91c1c", marginBottom: 12, textAlign: "center" },
  retry: { backgroundColor: "#111827", borderRadius: 10, paddingHorizontal: 20, paddingVertical: 12, marginTop: 8 },
  retryText: { color: "#fff", fontWeight: "700" },
  date: { color: "#6b7280", fontSize: 12, marginTop: 6 },
  noteTitle: { fontSize: 20, fontWeight: "800", marginTop: 8 },
  section: { fontSize: 15, fontWeight: "800", marginTop: 20, marginBottom: 6 },
  sectionRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginTop: 20, marginBottom: 6 },
  link: { color: "#2563eb", fontWeight: "700" },
  saveBtn: { marginTop: 12 },
  body: { fontSize: 14, color: "#111827", lineHeight: 20 },
  banner: { backgroundColor: "#fffbeb", borderWidth: 1, borderColor: "#fcd34d", borderRadius: 12, padding: 14, marginTop: 16 },
  bannerText: { color: "#92400e", marginBottom: 12 },
  primary: { backgroundColor: "#111827", borderRadius: 10, padding: 12, alignItems: "center" },
  primaryText: { color: "#fff", fontWeight: "700" },
  secondary: { borderWidth: 1, borderColor: "#d1d5db", borderRadius: 10, padding: 12, alignItems: "center", marginTop: 20, marginBottom: 32 },
  secondaryText: { fontWeight: "700", color: "#111827" },
  pending: { marginTop: 16, color: "#1e40af" },
  kv: { flexDirection: "row", justifyContent: "space-between", paddingVertical: 8, borderBottomWidth: 1, borderColor: "#f3f4f6" },
  k: { fontWeight: "700", color: "#374151", flex: 1 },
  v: { flex: 2, textAlign: "right", color: "#111827" },
  log: { backgroundColor: "#f9fafb", borderRadius: 8, padding: 10, marginBottom: 8 },
  logTitle: { fontWeight: "700", fontSize: 13 },
  logErr: { color: "#b91c1c", fontSize: 12, marginTop: 4 },
  dispatchBtn: { marginTop: 20, marginBottom: 0 },
  deleteBtn: { borderColor: "#fca5a5", marginTop: 12, marginBottom: 32 },
  deleteText: { fontWeight: "700", color: "#b91c1c" },
  editRow: { paddingVertical: 8, borderBottomWidth: 1, borderColor: "#f3f4f6" },
  editInput: {
    borderWidth: 1,
    borderColor: "#d1d5db",
    borderRadius: 8,
    padding: 8,
    marginTop: 4,
    fontSize: 14,
    color: "#111827",
    backgroundColor: "#f9fafb",
  },
});
