import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  acceptPendency,
  apiError,
  confirmWorkflowExecution,
  dismissWorkflowExecution,
  editPendency,
  getWorkflowExecution,
  ignorePendency,
  isStaleItemError,
  listPendencies,
  resolveFollowUp,
  snoozePendency,
} from "../api";
import type { ActionExecutionDetail, PendencyItem, PendencyKind } from "../types";
import Icon from "../components/Icon";

/* ----------------------------------------------------------------- apoio */

const KIND_META: Record<PendencyKind, { label: string; badge: string }> = {
  note_suggestion: { label: "Nota", badge: "pending_review" },
  task: { label: "Tarefa", badge: "general" },
  reminder: { label: "Lembrete", badge: "general" },
  follow_up: { label: "Acompanhamento", badge: "no_match" },
  action: { label: "Ação sugerida", badge: "pending_review" },
  approval: { label: "Aprovação", badge: "suggested" },
};

const SOURCE_LABELS: Record<string, string> = {
  audio: "Gravação",
  gmail: "Gmail", // só aparece quando o acompanhamento do Gmail foi ativado por você
  whatsapp: "WhatsApp",
};

const APPROVAL_SOURCE: Record<string, string> = {
  DOCUMENT_GENERATED: "porque uma nota ficou pronta",
  REMINDER_DUE: "porque um lembrete venceu",
};

const APPROVAL_ACTION_LABELS: Record<string, string> = {
  CREATE_TASK: "Criar tarefa",
  CREATE_REMINDER: "Criar lembrete",
  SEND_TELEGRAM: "Enviar mensagem no seu Telegram",
  CREATE_CALENDAR_EVENT: "Criar evento na agenda",
};

const deviceTz = (): string => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

const fmtTime = (iso: string | null | undefined): string => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("pt-BR");
};

/** datetime-local (horário do dispositivo) -> ISO com fuso, como o backend espera. */
const localToIso = (local: string): string | undefined => {
  if (!local) return undefined;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
};

const isoToLocalInput = (iso: string | null | undefined): string => {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

/** 404/409 na fila significam "o item mudou": recarrega e diz em português claro. */
const actError = (e: unknown): string =>
  isStaleItemError(e) ? "Este item mudou de estado — a lista foi atualizada." : apiError(e);

/** Preview da aprovação vem de uma lista segura do backend: renderiza só chaves conhecidas. */
const previewStr = (preview: Record<string, unknown> | null, key: string): string => {
  const v = preview?.[key];
  return typeof v === "string" ? v : "";
};

/* ------------------------------------------------------------ aprovação */

function ApprovalBody({ item, onDone }: { item: PendencyItem; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detail, setDetail] = useState<ActionExecutionDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [decided, setDecided] = useState<"confirm" | "dismiss" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const toggle = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    setDetail(null);
    setDecided(null);
    setError(null);
    setLoadingDetail(true);
    try {
      setDetail(await getWorkflowExecution(item.id));
    } catch (e) {
      setError(actError(e));
      if (isStaleItemError(e)) onDone();
    } finally {
      setLoadingDetail(false);
    }
  };

  const decide = async (kind: "confirm" | "dismiss") => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (kind === "confirm") await confirmWorkflowExecution(item.id);
      else await dismissWorkflowExecution(item.id);
      setDecided(kind);
      onDone();
    } catch (e) {
      setError(actError(e));
      if (isStaleItemError(e)) onDone();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ marginTop: 10 }}>
      {!open ? (
        <button className="btn-secondary" onClick={() => void toggle()}>Revisar o conteúdo</button>
      ) : (
        <div style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}>
          {loadingDetail && <p className="muted">Buscando o conteúdo para você revisar…</p>}
          {error && <p className="error" role="alert">{error}</p>}
          {decided === "confirm" && (
            <p className="muted"><b>Confirmado — aguardando execução.</b></p>
          )}
          {decided === "dismiss" && (
            <p className="muted"><b>Dispensado.</b> Esta ação não vai acontecer.</p>
          )}
          {!decided && detail && (
            detail.preview_available && detail.preview ? (
              <>
                {detail.preview.action_type === "CREATE_TASK" && (
                  <>
                    <div className="kv"><b>Tarefa</b><span>{previewStr(detail.preview, "title")}</span></div>
                    {previewStr(detail.preview, "summary") && (
                      <div className="kv"><b>Detalhes</b><span>{previewStr(detail.preview, "summary")}</span></div>
                    )}
                    <div className="kv"><b>Prazo</b>
                      <span>{previewStr(detail.preview, "due_at") ? fmtTime(previewStr(detail.preview, "due_at")) : "sem prazo"}</span>
                    </div>
                  </>
                )}
                {detail.preview.action_type === "CREATE_REMINDER" && (
                  <>
                    <div className="kv"><b>Lembrete</b><span>{previewStr(detail.preview, "title")}</span></div>
                    <div className="kv"><b>Quando</b>
                      <span>{previewStr(detail.preview, "remind_at") ? fmtTime(previewStr(detail.preview, "remind_at")) : "—"}</span>
                    </div>
                  </>
                )}
                {detail.preview.action_type === "SEND_TELEGRAM" && (
                  <>
                    <p className="muted" style={{ margin: "4px 0" }}>Mensagem que vai chegar no seu Telegram:</p>
                    <div className="log">{previewStr(detail.preview, "text")}</div>
                  </>
                )}
                {detail.preview.action_type === "CREATE_CALENDAR_EVENT" && (
                  <>
                    <div className="kv"><b>Evento</b><span>{previewStr(detail.preview, "summary")}</span></div>
                    <div className="kv"><b>Início</b>
                      <span>{previewStr(detail.preview, "start_at") ? fmtTime(previewStr(detail.preview, "start_at")) : "—"}</span>
                    </div>
                    <div className="kv"><b>Fim</b>
                      <span>{previewStr(detail.preview, "end_at") ? fmtTime(previewStr(detail.preview, "end_at")) : "—"}</span>
                    </div>
                    {previewStr(detail.preview, "timezone") && (
                      <div className="kv"><b>Fuso horário</b><span>{previewStr(detail.preview, "timezone")}</span></div>
                    )}
                  </>
                )}
                {!["CREATE_TASK", "CREATE_REMINDER", "SEND_TELEGRAM", "CREATE_CALENDAR_EVENT"].includes(String(detail.preview.action_type)) && (
                  <p className="muted">Este tipo de ação não tem prévia aqui.</p>
                )}
                <div className="row" style={{ marginTop: 10 }}>
                  <button disabled={busy} onClick={() => void decide("confirm")}>
                    {busy ? "Confirmando…" : "Confirmar"}
                  </button>
                  <button className="btn-danger" disabled={busy} onClick={() => void decide("dismiss")}>
                    {busy ? "Dispensando…" : "Dispensar"}
                  </button>
                  <button className="btn-secondary" disabled={busy} onClick={() => setOpen(false)}>Fechar</button>
                </div>
              </>
            ) : (
              <>
                <p className="muted">
                  O conteúdo desta ação não está disponível para revisão. Por segurança,
                  ela não pode ser confirmada — você pode dispensá-la.
                </p>
                <div className="row" style={{ marginTop: 10 }}>
                  <button className="btn-danger" disabled={busy} onClick={() => void decide("dismiss")}>
                    {busy ? "Dispensando…" : "Dispensar"}
                  </button>
                  <button className="btn-secondary" disabled={busy} onClick={() => setOpen(false)}>Fechar</button>
                </div>
              </>
            )
          )}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------- cartão do feed */

function PendencyCard({ item, onDone }: { item: PendencyItem; onDone: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [title, setTitle] = useState(item.title);
  const [due, setDue] = useState(isoToLocalInput(item.due_at));
  const [remind, setRemind] = useState(isoToLocalInput(item.due_at));
  const [until, setUntil] = useState("");

  const meta = KIND_META[item.kind];
  // follow_up: "suggestion" (aceita/edita/adia) vs "active" (só resolve/ignora) vs desconhecido (fail-closed)
  const fuSub = item.kind === "follow_up" ? (item.subkind ?? null) : null;
  const badgeLabel = item.kind === "follow_up"
    ? fuSub === "active" ? "Acompanhamento ativo"
      : fuSub === "suggestion" ? "Sugestão de acompanhamento"
      : "Acompanhamento"
    : meta.label;
  const sourceLabel = item.kind === "approval"
    ? (item.source ? APPROVAL_SOURCE[item.source] ?? "de um fluxo seu" : "de um fluxo seu")
    : (item.source ? SOURCE_LABELS[item.source] : undefined);

  const act = async (what: string, fn: () => Promise<void>, doneMsg?: string) => {
    if (busy) return;
    setBusy(what);
    setError(null);
    setNote(null);
    try {
      await fn();
      if (doneMsg) setNote(doneMsg);
      onDone();
    } catch (e) {
      setError(actError(e));
      if (isStaleItemError(e)) onDone();
    } finally {
      setBusy(null);
    }
  };

  const accept = () => {
    if (item.kind === "note_suggestion") {
      return act("accept", () =>
        acceptPendency(item.kind, item.id, {
          due_at: localToIso(due),
          remind_at: localToIso(remind),
        }), "Adicionada às tarefas.");
    }
    if (item.kind === "task") {
      return act("accept", () =>
        acceptPendency(item.kind, item.id, {
          title: title.trim() || undefined,
          due_at: localToIso(due),
        }), "Virou um cartão nas suas Tarefas.");
    }
    if (item.kind === "reminder") {
      if (!remind) {
        setError("Escolha a data e a hora do lembrete antes de agendar.");
        return Promise.resolve();
      }
      return act("accept", () =>
        acceptPendency(item.kind, item.id, { remind_at: localToIso(remind) }), "Lembrete agendado.");
    }
    if (item.kind === "follow_up") {
      if (!remind && !item.due_at) {
        setError("Escolha quando você quer retomar esse acompanhamento.");
        return Promise.resolve();
      }
      return act("accept", () =>
        acceptPendency(item.kind, item.id, {
          title: title.trim() || undefined,
          remind_at: localToIso(remind),
        }), "Marcado como aguardando resposta.");
    }
    return Promise.resolve();
  };

  const saveEdits = () =>
    act("edit", () =>
      editPendency(item.kind, item.id, {
        title: title.trim() || undefined,
        due_at: localToIso(due),
        remind_at: item.kind === "reminder" || item.kind === "follow_up" ? localToIso(remind) : undefined,
      }), "Ajustes salvos.");

  const snooze = () => {
    const iso = localToIso(until);
    if (!iso) {
      setError("Escolha até quando adiar.");
      return Promise.resolve();
    }
    return act("snooze", () => snoozePendency(item.kind, item.id, iso), "Adiado.");
  };

  const resolve = () =>
    act("resolve", () => resolveFollowUp(item.id), "Marcado como resolvido.");

  const ignore = () =>
    act("ignore", () => ignorePendency(item.kind, item.id));

  const isSuggestionKind = item.kind !== "approval";
  // Edição/adiar só valem para sugestões reais; acompanhamento ativo na API só resolve/ignora.
  const canAdjust = isSuggestionKind && item.kind !== "action"
    && !(item.kind === "follow_up" && fuSub !== "suggestion");
  const canSnooze = isSuggestionKind && !(item.kind === "follow_up" && fuSub !== "suggestion");
  const canIgnore = isSuggestionKind && !(item.kind === "follow_up" && fuSub === null);

  return (
    <article className="card inbox-card">
      <div className="inbox-card-head">
        <span className={`badge ${meta.badge}`}>{badgeLabel}</span>
        <time className="muted" dateTime={item.created_at}>{fmtTime(item.created_at)}</time>
      </div>
      <h2>
        {item.kind === "approval"
          ? (APPROVAL_ACTION_LABELS[item.title] ?? item.title)
          : item.title || KIND_META[item.kind].label}
      </h2>
      {sourceLabel && (
        <p className="muted" style={{ margin: "0 0 4px" }}>
          {item.kind === "approval" ? `Pedido de um fluxo seu, ${sourceLabel}.` : `Origem: ${sourceLabel}.`}
        </p>
      )}
      {item.summary && item.kind !== "approval" && <p className="inbox-transcription">{item.summary}</p>}
      {item.due_at && <p className="muted" style={{ margin: 0 }}>Para: {fmtTime(item.due_at)}</p>}

      {/* Resumo do que acontece ao aceitar — sem prometer nada além do real */}
      {item.kind === "task" && (
        <p className="muted" style={{ margin: "4px 0 0" }}>Ao aceitar, vira um cartão nas suas Tarefas.</p>
      )}
      {item.kind === "reminder" && (
        <p className="muted" style={{ margin: "4px 0 0" }}>Ao aceitar, fica agendado e você é avisado no horário.</p>
      )}
      {item.kind === "follow_up" && fuSub === "suggestion" && (
        <p className="muted" style={{ margin: "4px 0 0" }}>
          Ao aceitar, fica como <b>aguardando resposta</b> — um acompanhamento, diferente de lembrete comum.
        </p>
      )}
      {item.kind === "follow_up" && fuSub === "active" && (
        <p className="muted" style={{ margin: "4px 0 0" }}>
          Está <b>aguardando resposta</b>. Quando a conversa andar, marque como resolvido.
        </p>
      )}
      {item.kind === "follow_up" && fuSub === null && (
        <p className="muted" style={{ margin: "4px 0 0" }}>
          Este acompanhamento veio em um formato que esta tela ainda não reconhece.
          Por segurança, nenhuma ação é oferecida — atualize a página ou fale com o suporte.
        </p>
      )}
      {item.kind === "action" && (
        <p className="muted" style={{ margin: "4px 0 0" }}>
          Sugestão só para você ver — nada é executado a partir daqui.
        </p>
      )}
      {item.kind === "approval" && (
        <p className="muted" style={{ margin: "4px 0 0" }}>
          Abra a revisão para ver o conteúdo antes de confirmar — nunca confirme às cegas.
        </p>
      )}

      {item.kind === "approval" && <ApprovalBody item={item} onDone={onDone} />}

      {canAdjust && (
        <details className="inbox-scheduling">
          <summary>
            {item.kind === "reminder" ? "Definir data e hora" : "Ajustar detalhes"} <span>(opcional)</span>
          </summary>
          <div className="inbox-scheduling-fields">
            {(item.kind === "task" || item.kind === "follow_up") && (
              <label>Título
                <input value={title} onChange={(e) => setTitle(e.target.value)} />
              </label>
            )}
            {item.kind === "note_suggestion" && (
              <label>Prazo
                <input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
              </label>
            )}
            {item.kind === "task" && (
              <label>Prazo
                <input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
              </label>
            )}
            {(item.kind === "reminder" || item.kind === "follow_up") && (
              <label>{item.kind === "reminder" ? "Lembrar em" : "Retomar em"}
                <input type="datetime-local" value={remind} onChange={(e) => setRemind(e.target.value)} />
              </label>
            )}
            {item.kind === "note_suggestion" && (
              <label>Lembrete
                <input type="datetime-local" value={remind} onChange={(e) => setRemind(e.target.value)} />
              </label>
            )}
          </div>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Horário do seu dispositivo ({deviceTz()}), salvo corretamente.
          </p>
        </details>
      )}

      {canSnooze && (
        <details className="inbox-scheduling">
          <summary>Adiar <span>(volta mais tarde para a fila)</span></summary>
          <div className="inbox-scheduling-fields">
            <label>Adiar até
              <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
            </label>
          </div>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Horário do seu dispositivo ({deviceTz()}).
          </p>
          <button className="btn-secondary" style={{ marginTop: 6 }} disabled={busy !== null}
            onClick={() => void snooze()}>
            {busy === "snooze" ? "Adiando…" : "Adiar"}
          </button>
        </details>
      )}

      {error && <p className="error" role="alert">{error}</p>}
      {note && <p className="muted" role="status"><b>{note}</b></p>}

      <div className="row inbox-actions">
        {item.kind === "note_suggestion" && (
          <button disabled={busy !== null} onClick={() => void accept()}>
            {busy === "accept" ? "Salvando…" : "Adicionar às tarefas"}
          </button>
        )}
        {item.kind === "task" && (
          <button disabled={busy !== null} onClick={() => void accept()}>
            {busy === "accept" ? "Salvando…" : "Aceitar"}
          </button>
        )}
        {item.kind === "reminder" && (
          <button disabled={busy !== null} onClick={() => void accept()}>
            {busy === "accept" ? "Agendando…" : "Agendar lembrete"}
          </button>
        )}
        {item.kind === "follow_up" && fuSub === "suggestion" && (
          <button disabled={busy !== null} onClick={() => void accept()}>
            {busy === "accept" ? "Salvando…" : "Aguardar resposta"}
          </button>
        )}
        {item.kind === "follow_up" && fuSub === "active" && (
          <button className="btn-secondary" disabled={busy !== null} onClick={() => void resolve()}>
            {busy === "resolve" ? "Salvando…" : "Marcar resolvido"}
          </button>
        )}
        {canAdjust && item.kind !== "note_suggestion" && (
          <button className="btn-secondary" disabled={busy !== null} onClick={() => void saveEdits()}>
            {busy === "edit" ? "Salvando…" : "Salvar ajustes"}
          </button>
        )}
        {canIgnore && (
          <button className="btn-secondary" disabled={busy !== null} onClick={() => void ignore()}>
            {busy === "ignore" ? "…" : "Ignorar"}
          </button>
        )}
        {item.kind === "note_suggestion" && (
          <Link to={`/notas/${item.id}`} className="btn btn-secondary">Ver nota</Link>
        )}
      </div>
    </article>
  );
}

/* ------------------------------------------------------------------ tela */

const PAGE = 25;

export default function Inbox() {
  const [items, setItems] = useState<PendencyItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const feed = await listPendencies(PAGE, 0);
      setItems(feed.items);
      setTotal(feed.total);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMore = async () => {
    setLoadingMore(true);
    setError(null);
    try {
      const feed = await listPendencies(PAGE, items.length);
      setItems((current) => [...current, ...feed.items]);
      setTotal(feed.total);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setLoadingMore(false);
    }
  };

  useEffect(() => { void load(); }, [load]);

  if (loading) return <div className="center">Carregando…</div>;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Para revisar</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Tudo que espera uma decisão sua, em ordem: notas, tarefas, lembretes, acompanhamentos e aprovações de fluxos.
          </p>
        </div>
        <button className="btn-secondary" onClick={() => void load()} aria-label="Atualizar a fila">
          <Icon name="refresh" size={16} /> Atualizar
        </button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {items.length === 0 && (
        <div className="empty">
          <div className="empty-icon"><Icon name="check" size={24} /></div>
          <b>Tudo em dia</b>
          <p className="muted">Quando houver algo para revisar, você encontrará aqui.</p>
          <Link to="/" className="text-link">Fazer uma gravação</Link>
        </div>
      )}
      <div className="inbox-list">
        {items.map((item) => (
          <PendencyCard key={`${item.kind}:${item.id}`} item={item} onDone={load} />
        ))}
      </div>
      {items.length < total && (
        <div className="row" style={{ justifyContent: "center", marginTop: 12 }}>
          <button className="btn-secondary" disabled={loadingMore} onClick={() => void loadMore()}>
            {loadingMore ? "Carregando…" : `Carregar mais (restam ${total - items.length})`}
          </button>
        </div>
      )}
    </div>
  );
}
