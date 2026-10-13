import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiError, listNotes } from "../api";
import type { Note } from "../types";
import Icon from "../components/Icon";

export function StatusBadge({ status, templateId }: { status: string; templateId?: string | null }) {
  const isGeneral = status === "processed" && templateId === null;
  const labels: Record<string, string> = {
    processed: isGeneral ? "Nota geral" : "Organizada",
    pending_review: "Em análise",
    discarded: "Descartada",
  };
  const cls = isGeneral ? "general" : status;
  return <span className={`badge ${cls}`}>{labels[status] ?? status}</span>;
}

export default function Notes() {
  const [notes, setNotes] = useState<Note[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");

  const load = useCallback(async () => {
    setError(null);
    try {
      setNotes(await listNotes());
    } catch (e) {
      setError(apiError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (loading) return <div className="center">Carregando…</div>;

  const q = query.trim().toLowerCase();
  const filtered = notes.filter((n) => {
    const matchesQuery = !q || `${n.title} ${n.summary} ${n.body} ${n.raw_transcription}`.toLowerCase().includes(q);
    const isGeneral = n.status === "processed" && !n.template_id;
    const matchesFilter = filter === "all"
      || (filter === "general" && isGeneral)
      || (filter === "structured" && Boolean(n.template_id))
      || (filter === "review" && n.status === "pending_review")
      || (filter === "discarded" && n.status === "discarded");
    return matchesQuery && matchesFilter;
  });

  return (
    <div className="note-list">
      <div className="page-head">
        <div>
          <h1>Minhas Notas</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>{filtered.length} de {notes.length} {notes.length === 1 ? "nota" : "notas"}</p>
        </div>
        <div className="row">
          <Link to="/" className="btn" style={{ textDecoration: "none" }}><Icon name="plus" size={17} /> Gravar</Link>
          <button className="btn-secondary" onClick={() => void load()}><Icon name="refresh" size={16} /> Atualizar</button>
        </div>
      </div>
      <div className="note-filters">
        <div className="searchbar">
          <Icon name="search" className="sico" size={17} />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Buscar nas notas…" aria-label="Buscar nas notas" />
        </div>
        <label className="filter-select-wrap">
          <span>Mostrar</span>
          <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filtrar notas">
            <option value="all">Todas as notas</option>
            <option value="general">Notas gerais</option>
            <option value="structured">Com modelo</option>
            <option value="review">Em análise</option>
            <option value="discarded">Descartadas</option>
          </select>
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      {notes.length === 0 && (
        <div className="empty">
          <div className="empty-icon"><Icon name="note" size={24} /></div>
          <b>Nenhuma nota ainda</b>
          <p className="muted">Suas notas aparecerão aqui quando você fizer a primeira gravação.</p>
          <Link to="/" className="btn" style={{ textDecoration: "none" }}>Fazer uma gravação</Link>
        </div>
      )}
      {notes.length > 0 && filtered.length === 0 && (
        <div className="empty-filter"><b>Nenhuma nota encontrada</b><p className="muted">Ajuste a busca ou escolha outro filtro.</p></div>
      )}
      {filtered.map((n) => (
        <Link key={n.id} to={`/notas/${n.id}`} style={{ textDecoration: "none", color: "inherit" }}>
          <div className="card hoverable">
            <StatusBadge status={n.status} templateId={n.template_id} />
            <div style={{ marginTop: 6, fontWeight: n.title ? 700 : 400 }}>
              {n.title || (n.status === "pending_review" ? "Geração pendente" : "Nota sem título")}
            </div>
            {n.summary && (
              <div className="muted" style={{ marginTop: 4 }}>{n.summary.slice(0, 140)}</div>
            )}
            <div className="muted" style={{ marginTop: 6 }}>{new Date(n.created_at).toLocaleString()}</div>
          </div>
        </Link>
      ))}
    </div>
  );
}
