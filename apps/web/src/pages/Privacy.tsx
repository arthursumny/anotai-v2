import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  apiError,
  deletePrivacyData,
  getLocalization,
  getPrivacySummary,
  listAudit,
  patchLocalization,
} from "../api";
import type {
  AuditEventItem,
  AuditPage,
  LocalizationSettings,
  PrivacyDeleteResult,
  PrivacySource,
  PrivacySummary,
} from "../types";
import Icon from "../components/Icon";

/* ----------------------------------------------------------------- apoio */

const SOURCES: { value: PrivacySource; label: string }[] = [
  { value: "all", label: "Tudo" },
  { value: "audio", label: "Gravações" },
  { value: "gmail", label: "E-mails (Gmail)" },
  { value: "whatsapp", label: "WhatsApp" },
];

const SOURCE_LABELS: Record<string, string> = {
  all: "Tudo",
  audio: "Gravações",
  gmail: "E-mails",
  whatsapp: "WhatsApp",
  internal: "Sistema",
};

const AUDIT_LABELS: Record<string, string> = {
  USER_DELETED_SOURCE_DATA: "Você apagou dados de uma fonte",
};

/** Fallback neutro: nunca mostra convenções internas (ex: USER_DELETED_*) crus. */
const auditLabel = (item: AuditEventItem): string =>
  AUDIT_LABELS[item.event] ?? `Atividade de ${SOURCE_LABELS[item.source] ?? "sistema"}`;

const AUDIT_STATUS: Record<string, string> = {
  success: "Concluído",
  partial: "Parcial",
  failed: "Não concluído",
};

const fmtTime = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("pt-BR");
};

const isValidTimeZone = (tz: string): boolean => {
  try {
    new Intl.DateTimeFormat("pt-BR", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
};

const LOCALES = [
  { value: "pt-BR", label: "Português (Brasil)" },
  { value: "en-US", label: "English (US)" },
  { value: "es-ES", label: "Español" },
];

/* ------------------------------------------------------------------ tela */

export default function Privacy() {
  const [source, setSource] = useState<PrivacySource>("all");
  const [summary, setSummary] = useState<PrivacySummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [confirmText, setConfirmText] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [result, setResult] = useState<PrivacyDeleteResult | null>(null);

  const [audit, setAudit] = useState<AuditEventItem[]>([]);
  const [auditOffset, setAuditOffset] = useState(0);
  const [auditHasMore, setAuditHasMore] = useState(false);
  const [auditLoading, setAuditLoading] = useState(false);

  const [loc, setLoc] = useState<LocalizationSettings | null>(null);
  const [tzInput, setTzInput] = useState("");
  const [localeInput, setLocaleInput] = useState("pt-BR");
  const [locSaving, setLocSaving] = useState(false);
  const [locMsg, setLocMsg] = useState<string | null>(null);
  const [locError, setLocError] = useState<string | null>(null);

  const loadSummary = useCallback(async (s: PrivacySource) => {
    setError(null);
    try {
      setSummary(await getPrivacySummary(s));
    } catch (e) {
      setError(apiError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadSummary(source); }, [loadSummary, source]);

  const loadAudit = useCallback(async (offset: number) => {
    setAuditLoading(true);
    try {
      const page: AuditPage = await listAudit(25, offset);
      setAudit((current) => (offset === 0 ? page.items : [...current, ...page.items]));
      setAuditOffset(offset + page.items.length);
      setAuditHasMore(page.items.length === page.limit);
    } catch {
      // auditoria é complementar; erro fica silencioso na seção
    } finally {
      setAuditLoading(false);
    }
  }, []);

  const loadLoc = useCallback(async () => {
    try {
      const l = await getLocalization();
      setLoc(l);
      setTzInput(l.timezone || "UTC");
      setLocaleInput(l.locale || "pt-BR");
    } catch {
      setLoc(null);
    }
  }, []);

  useEffect(() => { void loadAudit(0); void loadLoc(); }, [loadAudit, loadLoc]);

  const changeSource = (value: string) => {
    setSource(value as PrivacySource);
    setSummary(null);
    setLoading(true);
    setResult(null);
    setConfirmText("");
    setError(null);
  };

  const typedOk = confirmText === "APAGAR";

  const doDelete = async () => {
    if (!typedOk || deleting) return;
    if (!window.confirm(
      `Apagar os dados de "${SOURCE_LABELS[source]}"? Essa ação não pode ser desfeita. Suas Notas ficam preservadas.`,
    )) return;
    setDeleting(true);
    setError(null);
    try {
      const res = await deletePrivacyData(source);
      setResult(res);
      setConfirmText("");
      await loadSummary(source);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setDeleting(false);
    }
  };

  const saveLoc = async () => {
    setLocError(null);
    setLocMsg(null);
    const tz = tzInput.trim() || "UTC";
    if (!isValidTimeZone(tz)) {
      setLocError("Fuso inválido. Use um nome como America/Sao_Paulo.");
      return;
    }
    setLocSaving(true);
    try {
      const saved = await patchLocalization({
        timezone: loc?.timezone !== tz ? tz : undefined,
        locale: loc?.locale !== localeInput ? localeInput : undefined,
      });
      setLoc(saved);
      setLocMsg("Fuso e idioma salvos.");
    } catch (e) {
      setLocError(apiError(e));
    } finally {
      setLocSaving(false);
    }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Privacidade</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Veja o que o AnotAI guarda de cada fonte e apague quando quiser. Suas Notas ficam sempre preservadas.
          </p>
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Seus dados por fonte</h3>
        <label>Fonte</label>
        <select value={source} onChange={(e) => changeSource(e.target.value)} aria-label="Escolher fonte">
          {SOURCES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>

        {loading && <p className="muted">Contando…</p>}
        {error && <p className="error" role="alert">{error}</p>}
        {summary && (
          <div style={{ marginTop: 8 }}>
            <div className="kv"><b>Eventos registrados</b><span>{summary.events}</span></div>
            <div className="kv"><b>Documentos gerados a partir desses eventos</b><span>{summary.generated_documents}</span></div>
            <div className="kv"><b>Lembretes ativos que serão removidos</b><span>{summary.reminders_to_cancel}</span></div>
            <div className="kv"><b>Acompanhamentos em aberto que serão removidos</b><span>{summary.follow_ups_to_cancel}</span></div>
            <div className="kv"><b>Notas preservadas (não são apagadas aqui)</b><span>{summary.notes_retained}</span></div>
          </div>
        )}

        <div style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 14 }}>
          <div className="card" style={{ background: "#fffbeb", border: "1px solid #fde68a", marginTop: 0 }}>
            <b>Apagar dados de {SOURCE_LABELS[source]}</b>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              Remove eventos, documentos gerados e os lembretes/acompanhamentos dessa fonte. Essas pendências são removidas junto, mesmo as que estavam agendadas. Suas <b>Notas</b> ficam preservadas — <Link to="/notas" className="text-link">apague-as em Notas</Link> se quiser.
            </p>
          </div>

          {result && (
            <div className="card" style={{ marginTop: 8, background: result.complete ? "#f0faf3" : "#fffbeb", border: `1px solid ${result.complete ? "#cce5d5" : "#fde68a"}` }}>
              <b>{result.complete ? "Dados apagados." : "A exclusão foi parcial. Alguns itens não foram apagados."}</b>
              <div className="kv"><b>Eventos removidos</b><span>{result.counts.events}</span></div>
              <div className="kv"><b>Documentos gerados removidos</b><span>{result.counts.generated_documents}</span></div>
              <div className="kv"><b>Lembretes removidos</b><span>{result.counts.reminders}</span></div>
              <div className="kv"><b>Acompanhamentos removidos</b><span>{result.counts.follow_ups}</span></div>
              <div className="kv"><b>Notas preservadas</b><span>{result.notes_retained}</span></div>
              {!result.complete && (
                <>
                  <div className="kv"><b>Não conseguimos apagar</b><span>{result.counts.failed}</span></div>
                  <p className="muted" style={{ margin: "6px 0 0" }}>
                    Para tentar apagar os itens restantes, digite APAGAR de novo abaixo e confirme —
                    itens que já foram apagados não voltam.
                  </p>
                </>
              )}
            </div>
          )}

          {!result?.complete && (
            <>
              <label style={{ marginTop: 10 }}>Digite APAGAR (em maiúsculas) para liberar</label>
              <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)}
                placeholder="APAGAR" autoComplete="off" aria-label="Digite APAGAR para confirmar a exclusão" />
              <div className="row" style={{ marginTop: 10 }}>
                <button className="btn-danger" disabled={!typedOk || deleting} onClick={() => void doDelete()}>
                  {deleting
                    ? "Apagando…"
                    : result
                      ? "Tentar apagar os dados restantes"
                      : `Apagar dados de ${SOURCE_LABELS[source]}`}
                </button>
              </div>
            </>
          )}
        </div>
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Fuso do seu espaço e idioma</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Vale para interpretar datas futuras e para o horário de silêncio dos avisos.
          Não muda nada que já está agendado — só o que você criar daqui pra frente.
        </p>
        <div className="grid2">
          <div>
            <label>Fuso horário do espaço</label>
            <input value={tzInput} placeholder="Ex: America/Sao_Paulo"
              onChange={(e) => { setTzInput(e.target.value); setLocMsg(null); setLocError(null); }} />
            {!loc && <p className="muted" style={{ margin: "4px 0 0" }}>Sem fuso definido, o espaço usa UTC.</p>}
          </div>
          <div>
            <label>Idioma</label>
            <select value={localeInput} onChange={(e) => { setLocaleInput(e.target.value); setLocMsg(null); setLocError(null); }}>
              {LOCALES.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
            </select>
          </div>
        </div>
        {locError && <p className="error" role="alert" style={{ margin: "8px 0 0" }}>{locError}</p>}
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn-secondary" disabled={locSaving} onClick={() => void saveLoc()}>
            {locSaving ? "Salvando…" : "Salvar"}
          </button>
          {locMsg && <span className="muted" role="status">{locMsg}</span>}
        </div>
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>Registro de atividade</h3>
          <button className="btn-secondary" onClick={() => void loadAudit(0)} aria-label="Atualizar registro">
            <Icon name="refresh" size={16} /> Atualizar
          </button>
        </div>
        <p className="muted">O que aconteceu na sua conta, sem conteúdo de mensagens nem detalhes técnicos.</p>
        {audit.length === 0 && !auditLoading && <p className="muted">Nada registrado ainda.</p>}
        {audit.map((item, i) => {
          const count = typeof item.metadata?.count === "number" ? item.metadata.count : null;
          return (
            <div key={`${item.created_at}-${i}`} className="kv">
              <span>
                {auditLabel(item)}
                {count !== null && <span className="muted"> · {count} {count === 1 ? "item" : "itens"}</span>}
                <span className="muted"> · {fmtTime(item.created_at)}</span>
              </span>
              <span className={`badge ${item.status === "success" ? "processed" : item.status === "partial" ? "warning" : "pending_review"}`}>
                {AUDIT_STATUS[item.status] ?? item.status}
              </span>
            </div>
          );
        })}
        {auditHasMore && (
          <div className="row" style={{ justifyContent: "center", marginTop: 10 }}>
            <button className="btn-secondary" disabled={auditLoading} onClick={() => void loadAudit(auditOffset)}>
              {auditLoading ? "Carregando…" : "Carregar mais"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
