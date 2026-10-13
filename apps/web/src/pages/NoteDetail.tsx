import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  apiError,
  deleteNote,
  getNote,
  listTemplates,
  patchNote,
  reclassifyNote,
  redispatchNote,
  retryNote,
  sendNoteToKanban,
  splitTasksToKanban,
} from "../api";
import type { Note, Template } from "../types";
import { StatusBadge } from "./Notes";

function toLocalDateTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromLocalDateTime(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.length > 0 ? v.map(String).join(", ") : "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function toText(v: unknown): string {
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (v === null || v === undefined) return "";
  return JSON.stringify(v);
}

function fromText(t: string): unknown {
  const s = t.trim();
  if (s === "") return null;
  try {
    return JSON.parse(s);
  } catch {
    return t;
  }
}

type GeneralTask = { title: string; due_date?: string | null; reminder_at?: string | null };

function asTasks(v: unknown): GeneralTask[] {
  if (!Array.isArray(v)) return [];
  const out: GeneralTask[] = [];
  for (const item of v) {
    if (typeof item === "string") {
      if (item.trim()) out.push({ title: item.trim() });
    } else if (item && typeof item === "object") {
      const t = (item as Record<string, unknown>).title;
      if (typeof t === "string" && t.trim()) {
        out.push({
          title: t.trim(),
          due_date: typeof (item as Record<string, unknown>).due_date === "string"
            ? (item as Record<string, unknown>).due_date as string
            : null,
          reminder_at: typeof (item as Record<string, unknown>).reminder_at === "string"
            ? (item as Record<string, unknown>).reminder_at as string
            : null,
        });
      }
    }
  }
  return out;
}

/** Render mínimo de markdown (títulos, listas, numeradas, **negrito**). */
function Markdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const els: JSX.Element[] = [];
  let listBuf: string[] = [];
  const flushList = (key: string) => {
    if (listBuf.length === 0) return;
    els.push(
      <ul key={key} style={{ margin: "6px 0", paddingLeft: 20 }}>
        {listBuf.map((li, i) => <li key={i}>{inline(li)}</li>)}
      </ul>,
    );
    listBuf = [];
  };
  const inline = (s: string) => {
    const parts = s.split(/(\*\*[^*]+\*\*)/g);
    return parts.map((p, i) =>
      p.startsWith("**") && p.endsWith("**") && p.length > 4
        ? <b key={i}>{p.slice(2, -2)}</b>
        : <span key={i}>{p}</span>,
    );
  };
  lines.forEach((raw, idx) => {
    const line = raw.trimEnd();
    const t = line.trim();
    if (/^#{1,4}\s/.test(t)) {
      flushList(`ul-${idx}`);
      els.push(<div key={idx} style={{ fontWeight: 800, marginTop: 8 }}>{t.replace(/^#+\s*/, "")}</div>);
    } else if (/^([-*•]\s|\d+[.)]\s)/.test(t)) {
      listBuf.push(t.replace(/^([-*•]\s|\d+[.)]\s)/, ""));
      if (idx === lines.length - 1) flushList(`ul-${idx}`);
    } else if (t === "") {
      flushList(`ul-${idx}`);
    } else {
      flushList(`ul-${idx}`);
      els.push(<p key={idx} style={{ margin: "4px 0" }}>{inline(t)}</p>);
    }
  });
  return <div>{els}</div>;
}

export default function NoteDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [note, setNote] = useState<Note | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [draftTitle, setDraftTitle] = useState("");
  const [draftBody, setDraftBody] = useState("");
  const [draftSummary, setDraftSummary] = useState("");
  const [retrying, setRetrying] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dispatching, setDispatching] = useState(false);
  const [kanbanBusy, setKanbanBusy] = useState(false);
  const [tasksBusy, setTasksBusy] = useState(false);
  const [picker, setPicker] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [busyTpl, setBusyTpl] = useState<string | null>(null);
  const [scheduleDue, setScheduleDue] = useState("");
  const [scheduleReminder, setScheduleReminder] = useState("");
  const [scheduleBusy, setScheduleBusy] = useState(false);
  const [taskMessage, setTaskMessage] = useState<string | null>(null);
  const [notificationStatus, setNotificationStatus] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      setNote(await getNote(id));
    } catch (e) {
      setError(apiError(e));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    setScheduleDue(toLocalDateTime(note?.due_date));
    setScheduleReminder(toLocalDateTime(note?.reminder_at));
  }, [note?.id, note?.due_date, note?.reminder_at]);

  const openPicker = async () => {
    setPicker(true);
    try {
      setTemplates(await listTemplates());
    } catch (e) {
      setError(apiError(e));
      setPicker(false);
    }
  };

  const choose = async (tplId: string) => {
    if (!id) return;
    setBusyTpl(tplId);
    try {
      setNote(await reclassifyNote(id, tplId));
      setPicker(false);
      setEditing(false);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setBusyTpl(null);
    }
  };

  const startEdit = () => {
    if (!note) return;
    const d: Record<string, string> = {};
    for (const [k, v] of Object.entries(note.structured_data ?? {})) d[k] = toText(v);
    setDraft(d);
    setDraftTitle(note.title ?? "");
    setDraftBody(note.body);
    setDraftSummary(note.summary);
    setEditing(true);
  };

  const saveEdit = async () => {
    if (!id || !note) return;
    setSaving(true);
    setError(null);
    try {
      const structured: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(draft)) structured[k] = fromText(v);
      setNote(await patchNote(id, {
        title: draftTitle, body: draftBody, summary: draftSummary,
        ...(note.structured_data !== null ? { structured_data: structured } : {}),
      }));
      setEditing(false);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSaving(false);
    }
  };

  const redispatch = async () => {
    if (!id) return;
    setDispatching(true);
    setError(null);
    try {
      setNote(await redispatchNote(id));
      setTaskMessage("Ações preparadas. Acompanhe a execução e as aprovações em Fluxos e Revisar; ações existentes não são reenviadas.");
    } catch (e) {
      setError(apiError(e));
    } finally {
      setDispatching(false);
    }
  };

  const retryGeneration = async () => {
    if (!id) return;
    setRetrying(true);
    setError(null);
    try {
      setNote(await retryNote(id));
    } catch (e) {
      setError(apiError(e));
    } finally {
      setRetrying(false);
    }
  };

  const toKanban = async () => {
    if (!id) return;
    setKanbanBusy(true);
    setError(null);
    try {
      await sendNoteToKanban(id);
      setTaskMessage("Tarefa disponível no quadro.");
    } catch (e) {
      setError(apiError(e));
    } finally {
      setKanbanBusy(false);
    }
  };

  const tasksToKanban = async () => {
    if (!id) return;
    setTasksBusy(true);
    setError(null);
    try {
      const cards = await splitTasksToKanban(id);
      setNote(await getNote(id));
      setTaskMessage(`${cards.length} tarefa(s) disponível(is) no quadro.`);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setTasksBusy(false);
    }
  };

  const saveSchedule = async () => {
    if (!id) return;
    setScheduleBusy(true);
    setError(null);
    setTaskMessage(null);
    try {
      setNote(await patchNote(id, {
        due_date: fromLocalDateTime(scheduleDue),
        reminder_at: fromLocalDateTime(scheduleReminder),
      }));
      setTaskMessage("Prazo e lembrete salvos.");
    } catch (e) {
      setError(apiError(e));
    } finally {
      setScheduleBusy(false);
    }
  };

  const enableDeviceNotifications = async () => {
    if (!("Notification" in window)) {
      setNotificationStatus("Este navegador não oferece notificações do dispositivo.");
      return;
    }
    const permission = await Notification.requestPermission();
    setNotificationStatus(permission === "granted"
      ? "Avisos ativados neste navegador. Mantenha o Anotaí aberto para receber os alertas agendados."
      : "Notificações bloqueadas. Você pode liberá-las nas configurações do navegador.");
  };

  const remove = async () => {
    if (!id || !window.confirm("Apagar esta nota?")) return;
    try {
      await deleteNote(id);
      navigate("/notas");
    } catch (e) {
      setError(apiError(e));
    }
  };

  if (loading) return <div className="center">Carregando…</div>;
  if (!note) return <div className="center">{error ?? "Nota não encontrada."}</div>;

  const sd = (note.structured_data ?? {}) as Record<string, unknown>;
  const isGeneral = note.template_id === null && note.status === "processed";
  const generalBody = note.body;
  const tasks = asTasks(sd.tarefas);
  const kanbanTaskIds = Array.isArray(sd.kanban_task_ids) ? sd.kanban_task_ids : [];
  const entries = note.structured_data ? Object.entries(note.structured_data) : [];
  const templatedEntries = entries.filter(([k]) =>
    k !== "kanban_task_ids" && (k !== "tipo" || note.template_id !== null),
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Detalhes da Nota</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>{new Date(note.created_at).toLocaleString()}</p>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      <StatusBadge status={note.status} templateId={note.template_id} />
      {note.title && <h2 style={{ margin: "10px 0 0", fontFamily: "Sora, Inter, sans-serif" }}>{note.title}</h2>}

      <details className="card">
        <summary>Transcrição original</summary>
        <p>{note.raw_transcription || "Ainda transcrevendo…"}</p>
      </details>

      <section className="card reminder-card">
        <div>
          <b>Prazo e lembrete</b>
          <p className="muted">Escolha quando precisa acontecer e quando quer ser avisado.</p>
        </div>
        <div className="reminder-fields">
          <label>Prazo<input type="datetime-local" value={scheduleDue} onChange={(event) => setScheduleDue(event.target.value)} /></label>
          <label>Lembrar em<input type="datetime-local" value={scheduleReminder} onChange={(event) => setScheduleReminder(event.target.value)} /></label>
        </div>
        <div className="row reminder-actions">
          <button disabled={scheduleBusy} onClick={() => void saveSchedule()}>{scheduleBusy ? "Salvando…" : "Salvar horário"}</button>
          <button className="btn-secondary" onClick={() => void enableDeviceNotifications()}>Ativar avisos neste dispositivo</button>
        </div>
        {notificationStatus && <p className="muted reminder-status" role="status">{notificationStatus}</p>}
        <p className="muted reminder-footnote">Os alertas do navegador aparecem quando o Anotaí está aberto. Se você conectou o Telegram, o aviso também pode chegar por lá.</p>
      </section>

      {taskMessage && <p className="connections-message" role="status">{taskMessage}</p>}

      {note.status === "pending_review" && (
        <div className="card">
          <p>O documento ainda não foi gerado. A transcrição original está preservada.</p>
          <button disabled={retrying || !note.raw_transcription.trim()} onClick={() => void retryGeneration()}>
            {retrying ? "Gerando…" : "Tentar gerar novamente"}
          </button>
        </div>
      )}

      {note.status === "processed" && (
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <b>{isGeneral ? "Nota geral" : "Documento"}</b>
            {!editing
              ? <button className="btn-secondary" onClick={startEdit}>Editar</button>
              : <button className="btn-secondary" onClick={() => setEditing(false)} disabled={saving}>Cancelar</button>}
          </div>
          {!editing ? (
            <>
              {note.summary && <p className="muted">{note.summary}</p>}
              {generalBody ? <Markdown text={generalBody} /> : <p className="muted">Documento sem conteúdo.</p>}
              {tasks.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <b>Tarefas identificadas ({tasks.length})</b>
                  <ul style={{ paddingLeft: 20, margin: "6px 0" }}>
                    {tasks.map((t, i) => (
                      <li key={i}>
                        {t.title}
                        {t.due_date && <span className="muted"> · Prazo: {new Date(t.due_date).toLocaleString("pt-BR")}</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {(note.due_date || note.reminder_at) && (
                <p className="muted" style={{ marginTop: 8 }}>
                  {note.due_date && <>Prazo: {new Date(note.due_date).toLocaleString("pt-BR")} </>}
                  {note.reminder_at && <>· Lembrete: {new Date(note.reminder_at).toLocaleString("pt-BR")}</>}
                </p>
              )}
            </>
          ) : (
            <>
              <label>Título</label>
              <input value={draftTitle} disabled={saving} onChange={(e) => setDraftTitle(e.target.value)} />
              <label>Resumo</label>
              <textarea value={draftSummary} disabled={saving} onChange={(e) => setDraftSummary(e.target.value)} />
              <label style={{ marginTop: 8 }}>Nota (markdown: listas, tópicos…)</label>
              <textarea
                value={draftBody}
                disabled={saving}
                rows={8}
                style={{ width: "100%" }}
                onChange={(e) => setDraftBody(e.target.value)}
              />
              <div style={{ marginTop: 10 }}>
                <button disabled={saving} onClick={() => void saveEdit()}>
                  {saving ? "Salvando…" : "Salvar alterações"}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {note.status === "processed" && (
        <div className="card">
          <b>Kanban</b>
          {kanbanTaskIds.length > 0 && (
            <p className="muted">{kanbanTaskIds.length} tarefa(s) desta nota já estão no quadro.</p>
          )}
          <p className="muted">Crie uma tarefa vinculada a este documento.</p>
          <div className="row">
            {(
              <button disabled={kanbanBusy} onClick={() => void toKanban()}>
                {kanbanBusy ? "Criando…" : "Criar tarefa no quadro"}
              </button>
            )}
            {tasks.length > 0 && kanbanTaskIds.length === 0 && (
              <button className="btn-secondary" disabled={tasksBusy} onClick={() => void tasksToKanban()}>
                {tasksBusy ? "Criando…" : `Criar ${tasks.length} card(s) das tarefas`}
              </button>
            )}
          </div>
        </div>
      )}

      {entries.length > 0 && (
        <details className="card">
          <summary>Dados estruturados (campos auxiliares)</summary>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <b>Dados estruturados</b>
          </div>
          {templatedEntries.length === 0 && <p className="muted">—</p>}
          {!editing
            ? templatedEntries.map(([k, v]) => <div key={k} className="kv"><b>{k}</b><span>{fmt(v)}</span></div>)
            : templatedEntries.map(([k]) => (
              <div key={k}>
                <label>{k}</label>
                <input value={draft[k] ?? ""} disabled={saving}
                  onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))} />
              </div>
            ))}
        </details>
      )}

      {note.logs.length > 0 && (
        <div className="card">
          <b>Histórico de geração</b>
          {note.logs.map((lg) => (
            <div key={lg.id} className="log">
              <b>{lg.action_type}</b> — {lg.status === "success" ? "ok" : "falhou"}
              {lg.error_message && <div style={{ color: "var(--red)" }}>{lg.error_message}</div>}
            </div>
          ))}
        </div>
      )}

      <div className="row">
        <Link to="/fluxos" className="btn btn-secondary">Ver execuções das ações</Link>
        <Link to="/inbox" className="btn btn-secondary">Revisar aprovações</Link>
        {note.status === "processed" && note.template_id && (
          <button className="btn-secondary" disabled={dispatching} onClick={() => void redispatch()}>
            {dispatching ? "Preparando…" : "Preparar ações"}
          </button>
        )}
        {note.status !== "discarded" && (
          <button className="btn-secondary" onClick={() => void openPicker()}>Trocar template</button>
        )}
        <button className="btn-danger" onClick={() => void remove()}>Apagar nota</button>
      </div>

      {picker && (
        <div className="modal-back" onClick={() => setPicker(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>Classificar com um template</h2>
            {templates.length === 0 && <p className="muted">Sem templates — clone um no Marketplace.</p>}
            {templates.map((t) => (
              <div key={t.id} className="card">
                <b>{t.title}</b>
                <p className="muted">{t.semantic_trigger}</p>
                <button disabled={busyTpl !== null} onClick={() => void choose(t.id)}>
                  {busyTpl === t.id ? "Aplicando…" : "Usar este"}
                </button>
              </div>
            ))}
            <button className="btn-secondary" style={{ width: "100%" }} onClick={() => setPicker(false)}>Fechar</button>
          </div>
        </div>
      )}
    </div>
  );
}
