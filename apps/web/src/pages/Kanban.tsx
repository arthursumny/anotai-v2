import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiError, getBoard, moveCard } from "../api";
import type { KanbanBoard, Note } from "../types";
import Icon from "../components/Icon";

type ColumnKey = keyof KanbanBoard;

const COLS: { key: ColumnKey; title: string }[] = [
  { key: "todo", title: "A fazer" },
  { key: "in_progress", title: "Em andamento" },
  { key: "done", title: "Concluídas" },
];

function fmtDue(iso: string | null): string {
  if (!iso) return "Sem prazo definido";
  return new Date(iso).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

export default function Kanban() {
  const [board, setBoard] = useState<KanbanBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<ColumnKey | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setBoard(await getBoard());
    } catch (e) {
      setError(apiError(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const moveTo = async (note: Note, target: ColumnKey) => {
    if ((note.kanban_status ?? "todo") === target || busy) return;
    setError(null);
    setBusy(note.id);
    try {
      const updated = await moveCard(note.id, target);
      const actualTarget = COLS.some((column) => column.key === updated.kanban_status)
        ? updated.kanban_status as ColumnKey
        : target;
      setBoard((current) => {
        if (!current) return current;
        const next: KanbanBoard = {
          todo: current.todo.filter((item) => item.id !== note.id),
          in_progress: current.in_progress.filter((item) => item.id !== note.id),
          done: current.done.filter((item) => item.id !== note.id),
        };
        next[actualTarget] = [...next[actualTarget], updated];
        return next;
      });
    } catch (e) {
      setError(apiError(e));
    } finally {
      setBusy(null);
      setDragging(null);
      setDropTarget(null);
    }
  };

  if (!board) return <div className="center">{error ?? "Carregando…"}</div>;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Tarefas</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            <span className="kanban-desktop-hint">Arraste cartões entre etapas ou escolha uma etapa no cartão.</span>
            <span className="kanban-mobile-hint">Deslize para ver as etapas; escolha o destino no menu da tarefa.</span>
          </p>
        </div>
        <button className="btn-secondary" onClick={() => void load()}><Icon name="refresh" size={16} /> Atualizar</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="kanban-board" aria-label="Quadro de tarefas">
        {COLS.map((column) => (
          <section
            key={column.key}
            className={`kanban-col ${dropTarget === column.key ? "drop-target" : ""}`}
            aria-label={`${column.title}: ${board[column.key].length} tarefas`}
            onDragOver={(event) => { event.preventDefault(); setDropTarget(column.key); }}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropTarget(null);
            }}
            onDrop={(event) => {
              event.preventDefault();
              const id = event.dataTransfer.getData("text/plain") || dragging;
              const note = id ? COLS.flatMap((item) => board[item.key]).find((item) => item.id === id) : undefined;
              setDropTarget(null);
              if (note) void moveTo(note, column.key);
            }}
          >
            <h2>{column.title}<span className="count-chip">{board[column.key].length}</span></h2>
            <div className="kanban-cards">
              {board[column.key].map((note) => (
                <article
                  key={note.id}
                  className={`card kanban-card ${dragging === note.id ? "is-dragging" : ""}`}
                  draggable={busy !== note.id}
                  onDragStart={(event) => {
                    event.dataTransfer.effectAllowed = "move";
                    event.dataTransfer.setData("text/plain", note.id);
                    setDragging(note.id);
                  }}
                  onDragEnd={() => { setDragging(null); setDropTarget(null); }}
                >
                  <div className="kanban-card-title">
                    <b>{note.title || "Nota sem título"}</b>
                    <span className="drag-grip" aria-hidden="true"><Icon name="grip" size={17} /></span>
                  </div>
                  <p className="kanban-date"><Icon name="calendar" size={15} /> {fmtDue(note.due_date)}</p>
                  {note.reminder_at && !note.reminder_sent && (
                    <p className="kanban-date"><Icon name="clock" size={15} /> Lembrete {new Date(note.reminder_at).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" })}</p>
                  )}
                  <div className="kanban-card-actions">
                    <label className="kanban-move-label">
                      <span className="sr-only">Mover {note.title || "nota"} para</span>
                      <select value={column.key} disabled={busy !== null} onChange={(event) => void moveTo(note, event.target.value as ColumnKey)}>
                        {COLS.map((option) => <option value={option.key} key={option.key}>{option.title}</option>)}
                      </select>
                    </label>
                    <Link to={`/notas/${note.id}`} className="btn btn-secondary">Abrir nota</Link>
                  </div>
                </article>
              ))}
              {board[column.key].length === 0 && (
                <div className="kanban-empty">{dragging ? "Solte a tarefa aqui" : "Nenhuma tarefa nesta etapa"}</div>
              )}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
