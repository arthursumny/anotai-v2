import { useCallback, useEffect, useRef, useState } from "react";
import Nango, { type ConnectUI } from "@nangohq/frontend";
import {
  apiError,
  connectWhatsApp,
  createIntegrationSession,
  deleteGmailPolicy,
  deleteIntegrationConnection,
  deleteWhatsAppPolicy,
  disconnectWhatsApp,
  getGmailPolicy,
  getWhatsAppPolicy,
  getWhatsAppQr,
  getWhatsAppStatus,
  listIntegrationConnections,
  listWhatsAppContacts,
  listWhatsAppGroups,
  putGmailPolicy,
  putWhatsAppPolicy,
  resumeGmailConnection,
} from "../api";
import type {
  IntegrationConnection,
  IntegrationProvider,
  PolicyDirection,
  PolicyMode,
  SourcePolicy,
  WhatsAppConversationMeta,
  WhatsAppStatus,
} from "../types";
import Icon from "../components/Icon";

/* ----------------------------------------------------------------- apoio */

const PROVIDERS: {
  id: IntegrationProvider;
  name: string;
  tagline: string;
  permission: string;
  footnote: string;
}[] = [
  {
    id: "google_gmail",
    name: "Gmail",
    tagline: "O AnotAI pode ficar de olho em e-mails que chegam — só se você pedir.",
    permission: "Leitura de e-mails recebidos. Conectar não ativa acompanhamento: isso é uma escolha sua, separada, aqui embaixo.",
    footnote: "Você escolhe de quem o AnotAI pode ver mensagens — e apaga tudo ao parar.",
  },
  {
    id: "google_calendar",
    name: "Agenda Google",
    tagline: "Prepara a criação de compromissos a partir das suas notas.",
    permission: "Acesso à sua agenda. Por enquanto a conexão só deixa tudo pronto: nada é criado na agenda sem você configurar e ver antes.",
    footnote: "Criação de compromissos a partir de fluxos chega em breve.",
  },
];

const STATUS_LABELS: Record<string, string> = {
  pending: "Conectando…",
  active: "Conectada",
  error: "Precisa reconectar",
  disconnected: "Desconectada",
};

const POLICY_MODES: { value: PolicyMode; label: string }[] = [
  { value: "ALL", label: "Todos os remetentes" },
  { value: "ALLOWLIST", label: "Só remetentes que eu escolher" },
  { value: "BLOCKLIST", label: "Todos, exceto os que eu escolher" },
];

const parseEmails = (raw: string): string[] =>
  raw.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);

const looksLikeEmail = (s: string): boolean => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s);

/* Retenção: "" = regra padrão (30 dias, sem tocar no que está salvo).
   Só enviamos retention_days quando o usuário MUDA a escolha — assim editar
   outros campos nunca reseta a retenção sem querer. */
type RetentionChoice = { select: string; custom: string };
const RETENTION_PRESETS = ["7", "30", "90", "365"];

const retentionFromDays = (days: number | null | undefined): RetentionChoice => {
  if (days == null) return { select: "", custom: "" };
  const s = String(days);
  return RETENTION_PRESETS.includes(s) ? { select: s, custom: "" } : { select: "custom", custom: s };
};

/** null = omitir do PUT (preserva); number/null definido = enviar. */
const retentionPatch = (c: RetentionChoice, currentDays: number | null | undefined): number | null | undefined => {
  let days: number | null;
  if (c.select === "") days = null; // regra padrão (reset explícito)
  else if (c.select === "custom") {
    const n = Number.parseInt(c.custom, 10);
    if (!Number.isFinite(n) || n < 1 || n > 365) return undefined; // inválido: validado antes no save
    days = n;
  } else days = Number.parseInt(c.select, 10);
  return days === (currentDays ?? null) ? undefined : days; // inalterado -> omite
};

const retentionValid = (c: RetentionChoice): boolean => {
  if (c.select !== "custom") return true;
  const n = Number.parseInt(c.custom, 10);
  return Number.isFinite(n) && n >= 1 && n <= 365;
};

const policySummary = (p: SourcePolicy): string => {
  const n = p.selected_ids.length;
  if (p.mode === "ALL") return "Todos os remetentes, na caixa de entrada.";
  if (p.mode === "ALLOWLIST")
    return `Só ${n === 1 ? "o remetente escolhido" : `os ${n} remetentes escolhidos`}.`;
  return `Todos, menos ${n === 1 ? "o remetente excluído" : `os ${n} remetentes excluídos`}.`;
};

/* ------------------------------------------------ WhatsApp pessoal (W3b) */

const WA_MODES: { value: PolicyMode; label: string }[] = [
  { value: "ALLOWLIST", label: "Só conversas que eu escolher" },
  { value: "BLOCKLIST", label: "Todas, menos as que eu escolher" },
  { value: "ALL", label: "Todas as conversas" },
];

const WA_DIRECTIONS: { value: PolicyDirection; label: string }[] = [
  { value: "incoming", label: "Só recebidas" },
  { value: "outgoing", label: "Só enviadas por você" },
  { value: "both", label: "Recebidas e enviadas" },
];

/** Número com código do país -> identificador de protocolo do WhatsApp. */
const toWhatsAppJid = (raw: string): string | null => {
  const digits = raw.replace(/\D/g, "");
  return digits.length >= 8 && digits.length <= 15 ? `${digits}@s.whatsapp.net` : null;
};

const jidLabel = (jid: string, metas: WhatsAppConversationMeta[]): string => {
  const found = metas.find((m) => m.jid === jid);
  if (found?.display_name) return found.display_name;
  return jid.replace(/@.*$/, "");
};

const waSummary = (p: SourcePolicy): string => {
  const n = p.selected_ids.length;
  const who =
    p.mode === "ALL" ? "todas as conversas"
    : p.mode === "ALLOWLIST" ? n === 1 ? "1 conversa escolhida" : `${n} conversas escolhidas`
    : n === 1 ? "todas, menos 1 conversa" : `todas, menos ${n} conversas`;
  const dir =
    p.direction === "incoming" ? "recebidas"
    : p.direction === "outgoing" ? "enviadas por você"
    : "recebidas e enviadas";
  const kinds = [p.allow_individuals && "individuais", p.allow_groups && "grupos"]
    .filter(Boolean).join(" e ");
  return `Mensagens ${dir}, em conversas ${kinds || "—"}, de ${who}.`;
};

function WhatsAppSection() {
  const [status, setStatus] = useState<WhatsAppStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [qrOpen, setQrOpen] = useState(false);
  // O QR fica só em memória neste modal — nunca localStorage, nunca log.
  const [qrImg, setQrImg] = useState<string | null>(null);
  const pollGen = useRef(0);
  // Política de acompanhamento (opt-in separado da conexão).
  const [policy, setPolicy] = useState<SourcePolicy | null>(null);
  const [pOpen, setPOpen] = useState(false);
  const [pMode, setPMode] = useState<PolicyMode>("ALLOWLIST");
  const [pDirection, setPDirection] = useState<PolicyDirection>("incoming");
  const [pIndividuals, setPIndividuals] = useState(true);
  const [pGroups, setPGroups] = useState(false);
  const [selected, setSelected] = useState<{ jid: string; label: string }[]>([]);
  const [metas, setMetas] = useState<WhatsAppConversationMeta[]>([]);
  const [metasErr, setMetasErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [manual, setManual] = useState("");
  const [scopeOk, setScopeOk] = useState(false);
  const [pSaving, setPSaving] = useState(false);
  const [pError, setPError] = useState<string | null>(null);
  const [pRetention, setPRetention] = useState<RetentionChoice>({ select: "", custom: "" });
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [st, pol] = await Promise.all([
        getWhatsAppStatus().catch(() => null), // 404 = nunca conectado
        getWhatsAppPolicy().catch(() => null),
      ]);
      setStatus(st);
      setPolicy(pol);
    } catch (e) {
      setError(apiError(e));
    }
  }, []);

  useEffect(() => {
    void load();
    return () => { pollGen.current += 1; }; // interrompe polling ao desmontar
  }, [load]);

  const closeQr = () => {
    pollGen.current += 1;
    setQrOpen(false);
    setQrImg(null);
    void load();
  };

  /** Polling limitado: para sozinho ao conectar, ao fechar o modal ou ao expirar. */
  const pollUntilConnected = async (gen: number) => {
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      if (pollGen.current !== gen) return;
      try {
        const st = await getWhatsAppStatus();
        if (pollGen.current !== gen) return;
        if (st.status === "connected") {
          setDiscDegraded(false);
          setNote("WhatsApp conectado.");
          setBusy(null);
          setQrOpen(false);
          setQrImg(null);
          await load();
          return;
        }
      } catch {
        // tenta de novo até o limite de tempo
      }
    }
    if (pollGen.current === gen) setError("O código expirou. Feche e gere um novo.");
    setBusy(null);
  };

  const openQr = async () => {
    setError(null);
    setNote(null);
    setDiscDegraded(false);
    setBusy("connect");
    try {
      const st = await getWhatsAppStatus().catch(() => null);
      if (st && st.status === "connected") {
        await load();
        return;
      }
      if (!st || st.status === "disconnected" || st.status === "error") {
        await connectWhatsApp();
      }
      const { qr } = await getWhatsAppQr();
      setQrImg(qr);
      setQrOpen(true);
      pollGen.current += 1;
      void pollUntilConnected(pollGen.current);
    } catch (e) {
      setError(apiError(e));
      setBusy(null);
    }
  };

  // Desconexão: revogação LOCAL sempre acontece; a saída do aparelho no WhatsApp
  // pode ficar pendente (backend avisa) e aí o usuário remove manualmente.
  const [discDegraded, setDiscDegraded] = useState(false);

  const disconnect = async () => {
    if (!window.confirm(
      "Desconectar o WhatsApp?\n\n" +
      "• O acompanhamento local é desativado na hora — configuração, contextos e acompanhamentos pendentes são apagados.\n" +
      "• A saída do aparelho no WhatsApp costuma ser automática, mas pode precisar que você remova " +
      "o aparelho vinculado em WhatsApp → Aparelhos conectados. Avisamos se for o caso.",
    )) return;
    setError(null);
    setNote(null);
    setDiscDegraded(false);
    setBusy("disconnect");
    try {
      const res = await disconnectWhatsApp();
      // Policy zera imediatamente, independente do resultado remoto.
      setPolicy(null);
      setPOpen(false);
      // Mensagem do backend: degraded pede remoção manual; a normal menciona termo técnico,
      // então trocamos por copy limpa. Ambas persistem após o load().
      if (/não foi possível confirmar/i.test(res.message ?? "")) {
        setDiscDegraded(true);
        setNote(res.message);
      } else {
        setNote("WhatsApp desconectado. O acompanhamento local foi apagado.");
      }
      await load();
    } catch (e) {
      // Falha local (ex: banco): mostra erro e NÃO marca como desconectado — UI mantém estado.
      setError(apiError(e));
    } finally {
      setBusy(null);
    }
  };

  const loadMetas = async () => {
    setMetasErr(null);
    try {
      const [contacts, groups] = await Promise.all([listWhatsAppContacts(), listWhatsAppGroups()]);
      setMetas([...groups.map((g) => ({ ...g, is_group: true })), ...contacts]);
    } catch {
      setMetas([]);
      setMetasErr("Não consegui carregar contatos e grupos agora — você ainda pode adicionar números manualmente.");
    }
  };

  const openPolicyForm = () => {
    const current = policy;
    setPMode(current?.mode ?? "ALLOWLIST");
    setPDirection((current?.direction as PolicyDirection) ?? "incoming");
    setPIndividuals(current?.allow_individuals ?? true);
    setPGroups(current?.allow_groups ?? false);
    setPRetention(retentionFromDays(current?.retention_days));
    setScopeOk(false);
    setPError(null);
    setNote(null);
    setDiscDegraded(false);
    // Chips resolvem o nome de exibição no render, quando os metadados chegarem.
    setSelected((current?.selected_ids ?? []).map((jid) => ({ jid, label: jid })));
    setPOpen(true);
    void loadMetas();
  };

  const toggleSelected = (m: WhatsAppConversationMeta) =>
    setSelected((list) =>
      list.some((s) => s.jid === m.jid)
        ? list.filter((s) => s.jid !== m.jid)
        : [...list, { jid: m.jid, label: m.display_name || m.jid }],
    );

  const addManual = () => {
    const jid = toWhatsAppJid(manual);
    if (!jid) {
      setPError("Esse número não parece válido. Use código do país + DDD + número, ex: 5511987654321.");
      return;
    }
    setPError(null);
    setSelected((list) => (list.some((s) => s.jid === jid) ? list : [...list, { jid, label: manual.trim() }]));
    setManual("");
  };

  const savePolicy = async () => {
    setPError(null);
    if (!pIndividuals && !pGroups) {
      setPError("Marque pelo menos conversas individuais ou grupos.");
      return;
    }
    if (!retentionValid(pRetention)) {
      setPError("O período de guarda precisa ser entre 1 e 365 dias.");
      return;
    }
    if (!scopeOk) {
      setPError("Confirme o escopo antes de ativar.");
      return;
    }
    setPSaving(true);
    try {
      const body: Record<string, unknown> = {
        monitor_enabled: true,
        mode: pMode,
        selected_ids: selected.map((s) => s.jid),
        direction: pDirection,
        allow_groups: pGroups,
        allow_individuals: pIndividuals,
      };
      const days = retentionPatch(pRetention, policy?.retention_days);
      if (days !== undefined) body.retention_days = days; // omissão preserva, null = regra padrão
      const saved = await putWhatsAppPolicy(body);
      setPolicy(saved);
      setPOpen(false);
      setDiscDegraded(false);
      setNote("Acompanhamento ativado com o escopo que você confirmou.");
    } catch (e) {
      setPError(apiError(e));
    } finally {
      setPSaving(false);
    }
  };

  const stopPolicy = async () => {
    if (!window.confirm(
      "Parar de acompanhar conversas? A configuração é apagada e os contextos e acompanhamentos " +
      "pendentes desta fonte são cancelados.",
    )) return;
    setPError(null);
    setPSaving(true);
    try {
      await deleteWhatsAppPolicy();
      setPolicy(null);
      setPOpen(false);
    } catch (e) {
      setPError(apiError(e));
    } finally {
      setPSaving(false);
    }
  };

  const connected = status?.status === "connected";
  const riskyScope = pMode === "ALL" || pDirection === "both";
  const visible = metas.filter((m) =>
    !filter || (m.display_name || m.jid).toLowerCase().includes(filter.toLowerCase()));
  const contacts = visible.filter((m) => !m.is_group);
  const groups = visible.filter((m) => m.is_group);

  return (
    <section className="connection-group">
      <div className="connection-group-head">
        <h2>WhatsApp</h2>
        <span>Pessoal · opcional</span>
      </div>
      <div className="card" style={{ margin: 0 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <b>Conversas do WhatsApp</b>
          {connected
            ? <span className="connection-connected"><Icon name="check" size={13} /> Conectado{status?.phone ? ` · ${status.phone}` : ""}</span>
            : status?.status === "pending_qr" || busy === "connect"
              ? <span className="connection-beta">Aguardando leitura do código</span>
              : <span className="connection-beta">Desconectado</span>}
        </div>
        <p className="muted" style={{ margin: "8px 0 0" }}>
          O AnotAI pode acompanhar só as conversas que você escolher. A conexão usa uma ponte de
          código aberto e pode cair de vez em quando — se cair, você pode tentar reconectar por aqui.
        </p>
        {error && <p className="error" role="alert" style={{ margin: "8px 0 0" }}>{error}</p>}
        {note && (discDegraded ? (
          <div className="card" role="status" style={{ background: "#fffbeb", border: "1px solid #fde68a", marginTop: 8 }}>
            <b>Desconectado aqui, com uma pendência no WhatsApp.</b>
            <p className="muted" style={{ margin: "4px 0 0" }}>{note}</p>
          </div>
        ) : (
          <p className="muted" role="status" style={{ margin: "8px 0 0" }}><b>{note}</b></p>
        ))}
        <div className="row" style={{ marginTop: 10 }}>
          {connected ? (
            <button className="btn-secondary" disabled={busy === "disconnect"} onClick={() => void disconnect()}>
              {busy === "disconnect" ? "Desconectando…" : "Desconectar"}
            </button>
          ) : (
            <button disabled={busy === "connect"} onClick={() => void openQr()}>
              {busy === "connect" ? "Gerando código…" : "Conectar WhatsApp"}
            </button>
          )}
        </div>

        {/* QR transitório, em memória, em modal próprio */}
        {qrOpen && (
          <div className="modal-back" onClick={closeQr} role="presentation">
            <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Conectar WhatsApp">
              <h2 style={{ marginTop: 0 }}>Conectar seu WhatsApp</h2>
              <p className="muted">
                No celular: WhatsApp → Aparelhos conectados → Conectar aparelho, e aponte para este código.
              </p>
              {qrImg
                ? <img src={qrImg} alt="Código QR para conectar o WhatsApp" style={{ width: "100%", maxWidth: 280, display: "block", margin: "0 auto" }} />
                : <p className="muted">Gerando código…</p>}
              <p className="muted" style={{ marginTop: 10 }}>
                O código vale pouco tempo e não é salvo pelo AnotAI. Ao ler, esta janela fecha sozinha.
              </p>
              <div className="row" style={{ marginTop: 12 }}>
                <button className="btn-secondary" onClick={closeQr}>Fechar</button>
              </div>
            </div>
          </div>
        )}

        {/* Conexão ≠ acompanhamento: opt-in separado */}
        {connected && (
          <div style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 14 }}>
            {policy?.monitor_enabled ? (
              <>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <b>Acompanhando conversas</b>
                  <span className="badge processed">ativo</span>
                </div>
                <p className="muted" style={{ margin: "8px 0 0" }}>{waSummary(policy)}</p>
                <div className="row" style={{ marginTop: 10 }}>
                  <button className="btn-secondary" onClick={openPolicyForm}>Editar</button>
                  <button className="btn-danger" disabled={pSaving} onClick={() => void stopPolicy()}>
                    {pSaving ? "Parando…" : "Parar de acompanhar"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <b>Acompanhar conversas</b>
                <p className="muted" style={{ margin: "6px 0 10px" }}>
                  Desativado. Conectar o WhatsApp não ativa nada — o acompanhamento só começa
                  se você configurar e confirmar o escopo aqui.
                </p>
                <button className="btn-secondary" onClick={openPolicyForm}>Configurar acompanhamento</button>
              </>
            )}

            {pOpen && (
              <div style={{ marginTop: 12, borderTop: "1px solid var(--line)", paddingTop: 12 }}>
                <label>Quais conversas?</label>
                <select value={pMode} onChange={(e) => { setPMode(e.target.value as PolicyMode); setScopeOk(false); }}>
                  {WA_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
                </select>

                {pMode !== "ALL" && (
                  <>
                    <label>Escolher conversas</label>
                    <input value={filter} placeholder="Filtrar por nome ou número…"
                      onChange={(e) => setFilter(e.target.value)} aria-label="Filtrar conversas" />
                    {metasErr && <p className="muted" style={{ margin: "6px 0 0" }}>{metasErr}</p>}
                    {groups.length > 0 && (
                      <>
                        <p className="muted" style={{ margin: "8px 0 4px" }}><b>Grupos</b></p>
                        {groups.slice(0, 12).map((m) => (
                          <label key={m.jid} className="row" style={{ gap: 8, margin: "2px 0" }}>
                            <input type="checkbox" style={{ width: "auto" }}
                              checked={selected.some((s) => s.jid === m.jid)}
                              onChange={() => toggleSelected(m)} />
                            {m.display_name || m.jid}
                          </label>
                        ))}
                      </>
                    )}
                    {contacts.length > 0 && (
                      <>
                        <p className="muted" style={{ margin: "8px 0 4px" }}><b>Pessoas</b></p>
                        {contacts.slice(0, 12).map((m) => (
                          <label key={m.jid} className="row" style={{ gap: 8, margin: "2px 0" }}>
                            <input type="checkbox" style={{ width: "auto" }}
                              checked={selected.some((s) => s.jid === m.jid)}
                              onChange={() => toggleSelected(m)} />
                            {m.display_name || m.jid}
                          </label>
                        ))}
                      </>
                    )}
                    {metas.length > 12 && filter === "" && (
                      <p className="muted" style={{ margin: "6px 0 0" }}>Mostrando as primeiras — use o filtro para achar as demais.</p>
                    )}
                    <p className="muted" style={{ margin: "8px 0 4px" }}>
                      Não achou? Adicione o número com código do país:
                    </p>
                    <div className="row">
                      <input style={{ flex: 2 }} value={manual} placeholder="Ex: 5511987654321"
                        onChange={(e) => setManual(e.target.value)} aria-label="Número manual com código do país" />
                      <button className="btn-secondary" onClick={addManual}>Adicionar número</button>
                    </div>
                    {selected.length > 0 && (
                      <div className="row" style={{ marginTop: 8 }}>
                        {selected.map((s) => (
                          <span key={s.jid} className="badge general">
                            {jidLabel(s.jid, metas) }
                            <button className="btn-danger" style={{ minHeight: 22, padding: "2px 7px", fontSize: 11, marginLeft: 6 }}
                              aria-label={`Remover ${s.label}`}
                              onClick={() => setSelected((l) => l.filter((x) => x.jid !== s.jid))}>×</button>
                          </span>
                        ))}
                      </div>
                    )}
                    {pMode === "ALLOWLIST" && selected.length === 0 && (
                      <div className="card" style={{ background: "#fffbeb", border: "1px solid #fde68a", marginTop: 8 }}>
                        <p className="muted" style={{ margin: 0 }}>
                          Lista vazia: <b>nenhuma conversa será analisada</b> até você escolher alguém.
                          Isso não é o mesmo que "Todas as conversas".
                        </p>
                      </div>
                    )}
                  </>
                )}

                <div className="grid2" style={{ marginTop: 8 }}>
                  <div>
                    <label>Quais mensagens?</label>
                    <select value={pDirection} onChange={(e) => { setPDirection(e.target.value as PolicyDirection); setScopeOk(false); }}>
                      {WA_DIRECTIONS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
                    </select>
                  </div>
                  <div>
                    <label>Tipo de conversa</label>
                    <label className="row" style={{ gap: 8, margin: "4px 0" }}>
                      <input type="checkbox" style={{ width: "auto" }} checked={pIndividuals}
                        onChange={(e) => { setPIndividuals(e.target.checked); setScopeOk(false); }} />
                      Individuais
                    </label>
                    <label className="row" style={{ gap: 8, margin: 0 }}>
                      <input type="checkbox" style={{ width: "auto" }} checked={pGroups}
                        onChange={(e) => { setPGroups(e.target.checked); setScopeOk(false); }} />
                      Grupos
                    </label>
                  </div>
                </div>

                {pDirection !== "both" && (
                  <p className="muted" style={{ margin: "8px 0 0" }}>
                    Para o AnotAI perceber que alguém respondeu você em um acompanhamento,
                    inclua também as mensagens enviadas por você. Sem isso, não prometemos detectar resposta.
                  </p>
                )}
                {riskyScope && (
                  <div className="card" style={{ background: "#fffbeb", border: "1px solid #fde68a", marginTop: 8 }}>
                    <b>Escopo amplo.</b>
                    <p className="muted" style={{ margin: "4px 0 0" }}>
                      Você escolheu abranger muitas conversas. Confira com atenção antes de ativar —
                      nada aqui liga sozinho.
                    </p>
                  </div>
                )}
                <p className="muted" style={{ margin: "8px 0 0" }}>
                  Os nomes aparecem nesta tela; ao salvar, o AnotAI guarda apenas os identificadores
                  das conversas escolhidas.
                </p>

                <RetentionField value={pRetention} onChange={setPRetention} />

                <label className="row" style={{ gap: 8, marginTop: 8 }}>
                  <input type="checkbox" style={{ width: "auto" }} checked={scopeOk}
                    onChange={(e) => setScopeOk(e.target.checked)} />
                  Confirmo: quero acompanhar exatamente o descrito acima.
                </label>
                {pError && <p className="error" role="alert" style={{ margin: "8px 0 0" }}>{pError}</p>}
                <div className="row" style={{ marginTop: 10 }}>
                  <button disabled={pSaving || !scopeOk} onClick={() => void savePolicy()}>
                    {pSaving ? "Salvando…" : "Salvar e ativar acompanhamento"}
                  </button>
                  <button className="btn-secondary" onClick={() => { setPOpen(false); setPError(null); }}>
                    Cancelar
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
        {!connected && (status === null) && (
          <p className="muted" style={{ margin: "10px 0 0" }}>
            Conecte primeiro para liberar a configuração de acompanhamento.
          </p>
        )}
      </div>
    </section>
  );
}

/** Bloco de retenção reutilizado nos editores Gmail e WhatsApp. */
function RetentionField({ value, onChange }: {
  value: RetentionChoice;
  onChange: (v: RetentionChoice) => void;
}) {
  return (
    <>
      <label>Guardar detalhes por</label>
      <select value={value.select}
        onChange={(e) => onChange({ select: e.target.value, custom: value.custom })}>
        <option value="">Regra padrão (30 dias)</option>
        {RETENTION_PRESETS.map((d) => <option key={d} value={d}>{d} dias</option>)}
        <option value="custom">Outro período…</option>
      </select>
      {value.select === "custom" && (
        <input type="number" min={1} max={365} value={value.custom} placeholder="Dias (1 a 365)"
          onChange={(e) => onChange({ select: "custom", custom: e.target.value })}
          aria-label="Dias de guarda personalizados" />
      )}
      <p className="muted" style={{ marginTop: 4 }}>
        Vale para o conteúdo dos eventos dessa fonte. Suas Notas (formato antigo) continuam
        guardadas até você apagá-las em Notas.
      </p>
    </>
  );
}

/* ------------------------------------------------------------------ tela */

export default function Integrations() {
  const [connections, setConnections] = useState<IntegrationConnection[]>([]);
  const [policy, setPolicy] = useState<SourcePolicy | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Conexão: token de sessão vive só em memória (nunca localStorage/log).
  const connectUIRef = useRef<ConnectUI | null>(null);
  const [connecting, setConnecting] = useState<IntegrationProvider | null>(null);
  const [finalizing, setFinalizing] = useState<IntegrationProvider | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  // Política Gmail (editor)
  const [policyOpen, setPolicyOpen] = useState(false);
  const [pMode, setPMode] = useState<PolicyMode>("ALL");
  const [pSenders, setPSenders] = useState("");
  const [pLabel, setPLabel] = useState("INBOX");
  const [pSaving, setPSaving] = useState(false);
  const [pError, setPError] = useState<string | null>(null);
  const [pRetention, setPRetention] = useState<RetentionChoice>({ select: "", custom: "" });
  // Retomada após intervalo sem análise: exige opt-in explícito (checkbox) antes do POST.
  const [acceptGap, setAcceptGap] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const [resumeMsg, setResumeMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [rows, gmail] = await Promise.all([
        listIntegrationConnections(),
        getGmailPolicy().catch(() => null),
      ]);
      setConnections(rows);
      setPolicy(gmail);
      setAcceptGap(false); // decisão de recomeço nunca fica pré-marcada
    } catch (e) {
      setError(apiError(e));
    }
  }, []);

  useEffect(() => {
    void load();
    return () => connectUIRef.current?.close();
  }, [load]);

  const connectionOf = (provider: IntegrationProvider) =>
    connections.find((c) => c.provider === provider);

  /** Depois do "connect" da janela do Google, a confirmação chega em segundo plano:
   *  buscamos de novo algumas vezes, com limite, até a conexão aparecer como ativa. */
  const finalize = useCallback(async (provider: IntegrationProvider) => {
    setConnecting(null);
    setFinalizing(provider);
    try {
      for (let i = 0; i < 15; i++) {
        const rows = await listIntegrationConnections().catch(() => null);
        if (rows) {
          setConnections(rows);
          if (rows.some((r) => r.provider === provider && r.status === "active")) return;
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      setError(
        "A autorização foi concluída no Google, mas a conexão ainda não apareceu aqui. Atualize a página em alguns instantes.",
      );
    } finally {
      setFinalizing(null);
    }
  }, []);

  const startConnect = async (provider: IntegrationProvider) => {
    setError(null);
    setConnecting(provider);
    try {
      const { session_token } = await createIntegrationSession(provider);
      const nango = new Nango();
      const ui = nango.openConnectUI({
        // Token vai direto para a janela segura; nunca guardamos nem registramos.
        sessionToken: session_token,
        onEvent: (event) => {
          if (event.type === "connect") {
            connectUIRef.current?.close();
            connectUIRef.current = null;
            void finalize(provider);
          } else if (event.type === "close") {
            connectUIRef.current = null;
            setConnecting((p) => (p === provider ? null : p));
          }
        },
      });
      connectUIRef.current = ui;
    } catch (e) {
      setError(apiError(e));
      setConnecting(null);
    }
  };

  const disconnect = async (c: IntegrationConnection) => {
    const name = PROVIDERS.find((p) => p.id === c.provider)?.name ?? "esta conta";
    const extra = c.provider === "google_gmail"
      ? " O acompanhamento de e-mails, se estiver configurado, é apagado junto."
      : "";
    if (!window.confirm(`Desconectar ${name}?${extra}`)) return;
    setError(null);
    setRemoving(c.id);
    try {
      await deleteIntegrationConnection(c.id);
      await load();
    } catch (e) {
      setError(apiError(e));
    } finally {
      setRemoving(null);
    }
  };

  const openPolicyForm = () => {
    setPMode(policy?.mode ?? "ALL");
    setPSenders((policy?.selected_ids ?? []).join("\n"));
    setPLabel((policy?.gmail_label_ids ?? ["INBOX"]).join(", "));
    setPRetention(retentionFromDays(policy?.retention_days));
    setPError(null);
    setResumeMsg(null);
    setResumeError(null);
    setPolicyOpen(true);
  };

  const savePolicy = async () => {
    setPError(null);
    const senders = parseEmails(pSenders);
    if (pMode !== "ALL") {
      if (senders.length === 0) {
        setPError("Liste pelo menos um endereço de e-mail — um por linha.");
        return;
      }
      if (senders.some((s) => !looksLikeEmail(s))) {
        setPError("Um dos endereços não parece um e-mail válido. Confira e tente de novo.");
        return;
      }
    }
    if (!retentionValid(pRetention)) {
      setPError("O período de guarda precisa ser entre 1 e 365 dias.");
      return;
    }
    const labels = pLabel.split(",").map((s) => s.trim()).filter(Boolean);
    const body: Record<string, unknown> = {
      monitor_enabled: true,
      mode: pMode,
      selected_ids: senders,
      direction: "incoming",
      allow_groups: true,
      allow_individuals: true,
      gmail_label_ids: labels.length > 0 ? labels : ["INBOX"],
      gmail_allowed_senders: [],
    };
    const days = retentionPatch(pRetention, policy?.retention_days);
    if (days !== undefined) body.retention_days = days; // omissão preserva, null = regra padrão
    setPSaving(true);
    try {
      const saved = await putGmailPolicy(body);
      setPolicy(saved);
      setPolicyOpen(false);
    } catch (e) {
      setPError(apiError(e));
    } finally {
      setPSaving(false);
    }
  };

  const stopPolicy = async () => {
    if (!window.confirm("Parar de acompanhar e-mails? Essa configuração é apagada por completo.")) return;
    setPError(null);
    setPSaving(true);
    try {
      const cleared = await deleteGmailPolicy();
      setPolicy(cleared);
      setPolicyOpen(false);
    } catch (e) {
      setPError(apiError(e));
    } finally {
      setPSaving(false);
    }
  };

  const gmailConnected = connectionOf("google_gmail")?.status === "active";

  /** Recomeço a partir de agora: POST accept_gap:true só depois do opt-in do usuário. */
  const resumeGmail = async () => {
    const conn = connectionOf("google_gmail");
    if (!conn || !acceptGap || conn.status !== "active") return;
    setResuming(true);
    setResumeError(null);
    setResumeMsg(null);
    try {
      await resumeGmailConnection(conn.id);
      setResumeMsg("Monitoramento reativado a partir de agora.");
      await load();
    } catch (e) {
      // 409/503 do backend: estado antigo permanece; mostra a mensagem e recarrega.
      setResumeError(apiError(e));
      void load();
    } finally {
      setResuming(false);
    }
  };

  return (
    <div className="connections-page">
      <div className="page-head">
        <div>
          <h1>Integrações</h1>
          <p className="muted">Conecte sua conta Google com o mínimo de permissão. A autorização acontece no próprio Google.</p>
        </div>
        <button className="btn-secondary" onClick={() => void load()}>
          <Icon name="refresh" size={16} /> Atualizar
        </button>
      </div>

      <section className="connections-intro card">
        <span className="connections-intro-mark"><Icon name="lock" size={18} /></span>
        <div>
          <b>Cada integração pede só o que precisa — e você vê antes.</b>
          <p className="muted">
            Entrar com a sua conta não lê e-mail nem toca na agenda sozinho. Desconecte quando quiser.
          </p>
        </div>
      </section>

      {error && <p className="error" role="alert">{error}</p>}

      <section className="connection-group">
        <div className="connection-group-head">
          <h2>Google</h2>
        </div>
        <div className="connection-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))" }}>
          {PROVIDERS.map((p) => {
            const conn = connectionOf(p.id);
            const busy = connecting === p.id || finalizing === p.id;
            return (
              <article className="card connection-card" key={p.id}>
                <div className="connection-card-top">
                  <span className="connection-logo connection-logo-google" aria-hidden="true">G</span>
                  <div className="connection-card-title">
                    <small>Google</small>
                    <h3>{p.name}</h3>
                  </div>
                  {conn && conn.status === "active" && (
                    <span className="connection-connected">
                      <Icon name="check" size={13} /> {STATUS_LABELS[conn.status]}
                    </span>
                  )}
                  {conn && conn.status !== "active" && (
                    <span className="connection-beta">{STATUS_LABELS[conn.status] ?? conn.status}</span>
                  )}
                </div>
                <p>{p.tagline}</p>
                <div className="connection-permission"><Icon name="lock" size={15} /> {p.permission}</div>
                {conn?.status === "active" && (conn.display_name || conn.account_email) && (
                  <p className="muted" style={{ minHeight: 0 }}>
                    Conta: {conn.display_name ?? conn.account_email}
                  </p>
                )}
                <div className="connection-card-footer">
                  {conn?.status === "active" ? (
                    <button className="btn-secondary" disabled={removing === conn.id}
                      onClick={() => void disconnect(conn)}>
                      {removing === conn.id ? "Desconectando…" : "Desconectar"}
                    </button>
                  ) : (
                    <button disabled={busy} onClick={() => void startConnect(p.id)} style={{ width: "100%" }}>
                      {connecting === p.id
                        ? "Abrindo janela segura…"
                        : finalizing === p.id
                          ? "Finalizando a conexão…"
                          : conn ? "Conectar de novo" : "Conectar"}
                    </button>
                  )}
                </div>
                <p className="connection-setup-hint" style={{ borderTop: "none", paddingTop: 2 }}>
                  {p.footnote}
                </p>
              </article>
            );
          })}
        </div>
      </section>

      <WhatsAppSection />

      <section className="connection-group">
        <div className="connection-group-head">
          <h2>Acompanhamento de e-mails</h2>
          <span>Opcional e separado da conexão</span>
        </div>
        <div className="card" style={{ margin: 0 }}>
          {!gmailConnected ? (
            <p className="muted" style={{ margin: 0 }}>
              Conecte o Gmail acima primeiro. Só depois da conta conectada dá para configurar
              o acompanhamento — e nada acontece sem você ativar aqui.
            </p>
          ) : policy?.monitor_enabled ? (
            (() => {
              const watch = connectionOf("google_gmail")?.gmail_watch_status ?? null;
              const expires = connectionOf("google_gmail")?.expiration_at ?? null;
              return (
                <>
                  {watch === "active" && (
                    <>
                      <div className="row" style={{ justifyContent: "space-between" }}>
                        <b>Monitoramento ativo</b>
                        <span className="badge processed">acompanhando</span>
                      </div>
                      <p className="muted" style={{ margin: "8px 0 0" }}>
                        {policySummary(policy)} E-mails novos que combinam com a sua escolha
                        passam a aparecer na sua revisão.
                        {expires && ` Sincronização válida até ${new Date(expires).toLocaleString()}.`}
                      </p>
                    </>
                  )}
                  {watch === "needs_resync" && (
                    <div className="card" style={{ background: "#fffbeb", border: "1px solid #fde68a", marginTop: 0 }}>
                      <b>Intervalo sem análise de e-mails.</b>
                      <p className="muted" style={{ margin: "4px 0 0" }}>
                        O acompanhamento ficou pausado por um tempo. E-mails desse período não serão
                        recuperados automaticamente — para voltar a acompanhar, você decide se prefere
                        recomeçar a partir de agora.
                      </p>
                      <label className="row" style={{ gap: 8, marginTop: 8 }}>
                        <input type="checkbox" style={{ width: "auto" }} checked={acceptGap}
                          onChange={(e) => { setAcceptGap(e.target.checked); setResumeError(null); }} />
                        Entendo que esse intervalo não será recuperado e quero recomeçar a partir de agora.
                      </label>
                      <div className="row" style={{ marginTop: 8 }}>
                        <button className="btn-secondary"
                          disabled={!acceptGap || resuming || !gmailConnected}
                          onClick={() => void resumeGmail()}>
                          {resuming ? "Reativando…" : "Recomeçar a partir de agora"}
                        </button>
                      </div>
                      {resumeError && <p className="error" style={{ margin: "8px 0 0" }}>{resumeError}</p>}
                      {resumeMsg && <p className="muted" style={{ margin: "8px 0 0" }}><b>{resumeMsg}</b></p>}
                    </div>
                  )}
                  {(watch === "paused" || watch === null) && (
                    <>
                      <div className="row" style={{ justifyContent: "space-between" }}>
                        <b>Acompanhamento configurado — aguardando sincronização</b>
                        <span className="badge no_match">não garante e-mails chegando ainda</span>
                      </div>
                      <p className="muted" style={{ margin: "8px 0 0" }}>
                        {policySummary(policy)} A preparação acontece em segundo plano; assim que os e-mails
                        começarem a valer, eles aparecem na sua revisão.
                      </p>
                    </>
                  )}
                  <div className="row" style={{ marginTop: 10 }}>
                    <button className="btn-secondary" onClick={openPolicyForm}>Editar</button>
                    <button className="btn-danger" disabled={pSaving} onClick={() => void stopPolicy()}>
                      {pSaving ? "Parando…" : "Parar de acompanhar"}
                    </button>
                  </div>
                </>
              );
            })()
          ) : (
            <>
              <b>Acompanhar meus e-mails recebidos</b>
              <p className="muted" style={{ margin: "6px 0 10px" }}>
                Desativado. Conectar a conta não ativa nada — o acompanhamento só começa se você
                configurar e ativar aqui, dizendo de quem o AnotAI pode ver mensagens.
              </p>
              <button className="btn-secondary" onClick={openPolicyForm}>Configurar acompanhamento</button>
            </>
          )}

          {policyOpen && gmailConnected && (
            <div style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 14 }}>
              <label>De quem o AnotAI pode ver e-mails?</label>
              <select value={pMode} onChange={(e) => setPMode(e.target.value as PolicyMode)}>
                {POLICY_MODES.map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
              {pMode !== "ALL" && (
                <>
                  <label>{pMode === "ALLOWLIST" ? "Remetentes escolhidos" : "Remetentes excluídos"}</label>
                  <textarea rows={3} value={pSenders}
                    onChange={(e) => setPSenders(e.target.value)}
                    placeholder={"Um endereço por linha. Ex:\nmaria@empresa.com\naviso@banco.com"} />
                </>
              )}
              <label>Onde olhar no Gmail</label>
              <input value={pLabel} placeholder="INBOX"
                onChange={(e) => setPLabel(e.target.value)} />
              <p className="muted" style={{ marginTop: 4 }}>
                O padrão é a caixa de entrada (INBOX). Só mude se você souber o nome de outra etiqueta.
              </p>
              <RetentionField value={pRetention} onChange={setPRetention} />
              {pError && <p className="error">{pError}</p>}
              <div className="row" style={{ marginTop: 10 }}>
                <button disabled={pSaving} onClick={() => void savePolicy()}>
                  {pSaving ? "Salvando…" : "Salvar e ativar acompanhamento"}
                </button>
                <button className="btn-secondary" onClick={() => { setPolicyOpen(false); setPError(null); }}>
                  Cancelar
                </button>
              </div>
              <p className="muted" style={{ marginTop: 8 }}>
                Ao ativar, mostramos "aguardando sincronização" até a preparação terminar —
                não assumimos que e-mails já estão chegando.
              </p>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
