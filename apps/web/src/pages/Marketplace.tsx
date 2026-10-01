import { useCallback, useEffect, useMemo, useState } from "react";
import { apiError, cloneTemplate, listMarketplace } from "../api";
import type { Template } from "../types";
import Icon from "../components/Icon";

const ACTION_LABELS: Record<string, string> = {
  telegram: "Telegram",
  whatsapp: "WhatsApp",
  webhook: "Serviço conectado",
  mcp_tool: "Aplicativo conectado",
  email: "E-mail",
};

function actionLabel(type: string): string {
  return ACTION_LABELS[type] ?? "Outra integração";
}

export default function Marketplace() {
  const [items, setItems] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [actionFilter, setActionFilter] = useState("all");
  const [cloned, setCloned] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setItems(await listMarketplace());
    } catch (e) {
      setError(apiError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const actionOptions = useMemo(() => {
    const types = new Set(items.flatMap((item) => item.actions.map((action) => actionLabel(action.type))));
    return [...types].sort((a, b) => a.localeCompare(b, "pt-BR"));
  }, [items]);

  const filtered = useMemo(() => {
    const q = query.trim().toLocaleLowerCase("pt-BR");
    return items.filter((item) => {
      const matchesQuery = !q || `${item.title} ${item.purpose} ${item.semantic_trigger}`.toLocaleLowerCase("pt-BR").includes(q);
      const matchesAction = actionFilter === "all" || item.actions.some((action) => actionLabel(action.type) === actionFilter);
      return matchesQuery && matchesAction;
    });
  }, [items, query, actionFilter]);

  const addTemplate = async (template: Template) => {
    setBusy(template.id);
    setMsg(null);
    try {
      await cloneTemplate(template.id);
      setCloned((current) => new Set(current).add(template.id));
      setMsg(`“${template.title}” foi adicionado aos seus modelos.`);
    } catch (e) {
      setMsg(apiError(e));
    } finally {
      setBusy(null);
    }
  };

  if (loading) return <div className="center">Carregando…</div>;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Modelos prontos</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>Escolha um modelo para adaptar às tarefas que se repetem no seu dia a dia.</p>
        </div>
        <button className="btn-secondary" onClick={() => void load()}><Icon name="refresh" size={16} /> Atualizar</button>
      </div>
      {(error || msg) && <p className={error ? "error" : "marketplace-message"} role={error ? "alert" : "status"}>{error ?? msg}</p>}
      {items.length > 0 && (
        <div className="marketplace-filters">
          <div className="searchbar">
            <Icon name="search" className="sico" size={17} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar modelos…" aria-label="Buscar modelos" />
          </div>
          <label className="filter-select-wrap">
            <span>Conecta com</span>
            <select value={actionFilter} onChange={(event) => setActionFilter(event.target.value)} aria-label="Filtrar modelos por integração">
              <option value="all">Todas</option>
              {actionOptions.map((action) => <option key={action} value={action}>{action}</option>)}
            </select>
          </label>
          <span className="marketplace-count">{filtered.length} {filtered.length === 1 ? "modelo" : "modelos"}</span>
        </div>
      )}
      {items.length === 0 && !error && <div className="empty"><b>Nenhum modelo disponível ainda</b><p className="muted">Quando houver modelos públicos, eles aparecerão aqui.</p></div>}
      {items.length > 0 && filtered.length === 0 && <div className="empty-filter"><b>Nenhum modelo encontrado</b><p className="muted">Tente outro termo ou escolha uma integração diferente.</p></div>}
      <div className="marketplace-grid">
        {filtered.map((template) => (
          <article key={template.id} className="card marketplace-card">
            <div className="marketplace-card-top">
              <span className="marketplace-card-icon"><Icon name="grid" size={19} /></span>
              {cloned.has(template.id) && <span className="marketplace-added"><Icon name="check" size={14} /> Adicionado</span>}
            </div>
            <h2>{template.title}</h2>
            {template.purpose && <p className="marketplace-purpose">{template.purpose}</p>}
            {template.semantic_trigger && (
              <details className="marketplace-when">
                <summary>Ver quando usar</summary>
                <p>{template.semantic_trigger}</p>
              </details>
            )}
            <div className="marketplace-tags">
              {[...new Set(template.actions.map((action) => actionLabel(action.type)))].map((label) => (
                <span key={label} className="marketplace-tag">{label}</span>
              ))}
            </div>
            <button className="marketplace-add" disabled={cloned.has(template.id) || busy !== null} onClick={() => void addTemplate(template)}>
              {busy === template.id ? "Adicionando…" : cloned.has(template.id) ? "Já está nos seus modelos" : "Usar este modelo"}
              {!cloned.has(template.id) && busy !== template.id && <Icon name="arrowRight" size={16} />}
            </button>
          </article>
        ))}
      </div>
    </div>
  );
}
