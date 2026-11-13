import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  apiError,
  createWorkflow,
  deleteWorkflow,
  getLocalization,
  getWorkflowSettings,
  listIntegrationConnections,
  listTemplates,
  listWorkflowExecutions,
  listWorkflows,
  putWorkflowSettings,
  updateWorkflow,
} from "../api";
import type {
  ActionExecution,
  AutomationMode,
  AutomationSettings,
  Template,
  Workflow,
  WorkflowStep,
} from "../types";

/* ----------------------------------------------------------------- apoio */

/** Gatilhos: só os suportados pelo runtime ficam escolhíveis; os demais aparecem
 *  como "em preparação" (e seguem visíveis ao editar fluxos já salvos com eles). */
const TRIGGERS: { value: string; label: string; ready: boolean }[] = [
  { value: "DOCUMENT_GENERATED", label: "Quando uma nota ficar pronta", ready: true },
  { value: "REMINDER_DUE", label: "Quando um lembrete vencer", ready: true },
  { value: "MANUAL", label: "Quando eu pedir (em preparação)", ready: false },
  { value: "TASK_CREATED", label: "Quando uma tarefa for criada (em preparação)", ready: false },
  { value: "FOLLOW_UP_DUE", label: "Quando um acompanhamento vencer (em preparação)", ready: false },
];

const triggerLabel = (value: string): string =>
  TRIGGERS.find((t) => t.value === value)?.label ?? value;

const ACTION_LABELS: Record<string, string> = {
  CREATE_TASK: "Criar tarefa",
  CREATE_REMINDER: "Criar lembrete",
  SEND_TELEGRAM: "Enviar mensagem no meu Telegram",
  SEND_EMAIL: "Enviar email",
  SEND_WEBHOOK: "Enviar webhook",
  CALL_MCP_TOOL: "Executar ferramenta MCP",
  CREATE_CALENDAR_EVENT: "Criar evento na agenda",
};

/**
 * Ações "com efeito fora da tela": sem confirmação (per-step) ou sozinhas
 * (preferências) só quando o modo geral for Automático.
 */
const AUTO_GATE_ACTIONS = new Set(["SEND_TELEGRAM", "SEND_EMAIL", "SEND_WEBHOOK", "CALL_MCP_TOOL", "CREATE_CALENDAR_EVENT"]);

const deviceTimeZone = (): string => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

const isValidTimeZone = (tz: string): boolean => {
  try {
    new Intl.DateTimeFormat("pt-BR", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
};

const stepLabel = (actionType: string): string => ACTION_LABELS[actionType] ?? "Passo interno";

const MODES: { value: AutomationMode; label: string }[] = [
  { value: "suggestive", label: "Sugestivo — só sugere; você decide tudo" },
  { value: "semi_automatic", label: "Semi-automático — adianta o trabalho, mas confirma com você" },
  { value: "automatic", label: "Automático — age sozinho dentro das permissões abaixo" },
  { value: "disabled", label: "Desativado — nenhum fluxo age" },
];

const DISPOSITIONS: { value: string; label: string }[] = [
  { value: "require_confirmation", label: "Só com a minha confirmação" },
  { value: "automatic", label: "Sozinho, sem perguntar" },
  { value: "disabled", label: "Nunca" },
];

/** Status do livro-razão de execuções, em português claro (sem prometer além do real). */
const STATUS_LABELS: Record<string, string> = {
  awaiting_confirmation: "Esperando você",
  pending: "Confirmado — aguardando execução",
  claimed: "Em execução",
  succeeded: "Concluído",
  failed: "Não concluído",
};

const statusLabel = (s: string): string => STATUS_LABELS[s] ?? s;

const fmtTime = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
};

const minuteToHhmm = (m: number | null | undefined): string =>
  m == null ? "" : `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

/** "" → null (vazio), "HH:mm" válido → minutos, caso contrário → "invalid". */
const hhmmToMinute = (s: string): number | null | "invalid" => {
  if (!s.trim()) return null;
  const match = s.trim().match(/^([01]\d|2[0-3]):([0-5]\d)$/);
  return match ? Number(match[1]) * 60 + Number(match[2]) : "invalid";
};

/** Chaves que o formulário conhece e sobrescreve; o resto do input_template segue intacto. */
const FORM_INPUT_KEYS = new Set([
  "type", "title", "summary", "due_at", "remind_at", "text", "start_at", "end_at", "timezone",
]);

interface StepDef {
  uid: number;
  actionType: string;
  title: string; // tarefa/lembrete: title · evento: summary
  summary: string;
  when: string; // datetime-local local (tarefa: prazo · lembrete: quando)
  text: string;
  startWhen: string; // evento: início (datetime-local)
  endWhen: string; // evento: fim (datetime-local)
  calendarTz: string; // evento: fuso IANA — padrão explícito do dispositivo
  requireConfirmation: boolean;
  enabled: boolean;
  rawInput: Record<string, unknown>; // preserva chaves que o formulário não edita
}

let stepUid = 1;
const newStep = (actionType: string): StepDef => ({
  uid: stepUid++,
  actionType,
  title: "",
  summary: "",
  when: "",
  text: "",
  startWhen: "",
  endWhen: "",
  calendarTz: deviceTimeZone(),
  // Nunca automático por padrão: confirmação ligada — desmarcar é escolha explícita.
  requireConfirmation: true,
  enabled: true,
  rawInput: {},
});

const toLocalInput = (v: unknown): string => {
  if (typeof v !== "string" || !v) return "";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

const stepFromRead = (s: WorkflowStep): StepDef => {
  const inp = (s.input_template ?? {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  return {
    uid: stepUid++,
    actionType: s.action_type,
    // Evento de agenda usa "summary" como título visível.
    title: s.action_type === "CREATE_CALENDAR_EVENT" ? str(inp.summary) : str(inp.title),
    summary: str(inp.summary),
    when: toLocalInput(inp.due_at ?? inp.remind_at),
    text: str(inp.text),
    startWhen: toLocalInput(inp.start_at),
    endWhen: toLocalInput(inp.end_at),
    calendarTz: str(inp.timezone) || deviceTimeZone(),
    requireConfirmation: Boolean(s.require_confirmation),
    enabled: Boolean(s.enabled),
    rawInput: inp,
  };
};

const stepToApi = (s: StepDef, position: number) => {
  const input: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(s.rawInput)) {
    if (!FORM_INPUT_KEYS.has(k)) input[k] = v; // confidence e afins: roundtrip intacto
  }
  if (s.actionType === "CREATE_TASK") {
    input.title = s.title.trim();
    if (s.summary.trim()) input.summary = s.summary.trim();
    if (s.when) input.due_at = new Date(s.when).toISOString();
  } else if (s.actionType === "CREATE_REMINDER") {
    input.title = s.title.trim();
    if (s.when) input.remind_at = new Date(s.when).toISOString();
  } else if (s.actionType === "SEND_TELEGRAM") {
    input.text = s.text.trim();
  } else if (s.actionType === "CREATE_CALENDAR_EVENT") {
    // Sem calendarId/convidados/connectionId — o destino é a conta conectada em Integrações.
    input.summary = s.title.trim();
    if (s.startWhen) input.start_at = new Date(s.startWhen).toISOString();
    if (s.endWhen) input.end_at = new Date(s.endWhen).toISOString();
    if (s.calendarTz.trim()) input.timezone = s.calendarTz.trim();
  }
  return {
    position,
    action_type: s.actionType,
    input_template: input,
    require_confirmation: s.requireConfirmation,
    enabled: s.enabled,
  };
};

/* ------------------------------------------------------------------ tela */

export default function Workflows() {
  const [items, setItems] = useState<Workflow[]>([]);
  const [settings, setSettings] = useState<AutomationSettings | null>(null);
  const [mode, setMode] = useState<AutomationMode>("suggestive");
  const [actionConfig, setActionConfig] = useState<Record<string, string>>({});
  // Avisos por Telegram + horário de silêncio ("HH:mm" no fuso do espaço; "" = sem silêncio)
  const [notifEnabled, setNotifEnabled] = useState(true);
  const [quietStart, setQuietStart] = useState("");
  const [quietEnd, setQuietEnd] = useState("");
  const [wsTz, setWsTz] = useState<string | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsMsg, setSettingsMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingVersion, setEditingVersion] = useState<number | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [trigger, setTrigger] = useState("DOCUMENT_GENERATED");
  // Condições são preservadas sem mexer no save; o seletor "Modelo" edita só conditions.source.
  const [conditions, setConditions] = useState<Record<string, unknown>>({});
  // Modelos do usuário para o filtro "nota do modelo X" (trigger DOCUMENT_GENERATED).
  const [templates, setTemplates] = useState<Template[]>([]);
  const [wfEnabled, setWfEnabled] = useState(false);
  const [steps, setSteps] = useState<StepDef[]>([]);
  const [saving, setSaving] = useState(false);
  // Livro-razão de execuções (resumo): aprovações pendentes apontam para a Inbox —
  // a decisão com preview carregado acontece lá, nunca às cegas.
  const [executions, setExecutions] = useState<ActionExecution[]>([]);
  // Agenda Google conectada? (só para avisar no editor; rascunho é sempre permitido)
  const [calendarReady, setCalendarReady] = useState<boolean | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [wfs, st, execs, conns, loc, tpls] = await Promise.all([
        listWorkflows(),
        getWorkflowSettings().catch(() => null),
        listWorkflowExecutions().catch(() => [] as ActionExecution[]),
        listIntegrationConnections().catch(() => []),
        getLocalization().catch(() => null),
        listTemplates().catch(() => [] as Template[]),
      ]);
      setItems(wfs);
      setTemplates(tpls);
      setExecutions(execs);
      setCalendarReady(conns.some((c) => c.provider === "google_calendar" && c.status === "active"));
      setWsTz(loc?.timezone || null);
      if (st) {
        setSettings(st);
        setMode(st.automation_mode);
        setActionConfig(st.action_config ?? {});
        setNotifEnabled(st.notifications_enabled !== false);
        setQuietStart(minuteToHhmm(st.quiet_start_minute));
        setQuietEnd(minuteToHhmm(st.quiet_end_minute));
      }
    } catch (e) {
      setError(apiError(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const reset = () => {
    setEditingId(null);
    setEditingVersion(null);
    setTitle("");
    setDescription("");
    setTrigger("DOCUMENT_GENERATED");
    setConditions({});
    setWfEnabled(false);
    setSteps([]);
    setShowForm(false);
  };

  const patchStep = (uid: number, p: Partial<StepDef>) =>
    setSteps((l) => l.map((s) => (s.uid === uid ? { ...s, ...p } : s)));

  /**
   * Tirar a confirmação de um passo nunca é padrão: Telegram exige modo geral
   * Automático (regra do F3); evento de agenda exige ainda a preferência
   * "Criar evento na agenda: Sozinho, sem perguntar" (regra do F4-C).
   */
  const canSkipConfirmation = (actionType: string): boolean => {
    if (actionType === "CREATE_CALENDAR_EVENT") {
      return mode === "automatic" && (actionConfig.CREATE_CALENDAR_EVENT ?? "require_confirmation") === "automatic";
    }
    return mode === "automatic";
  };

  const moveStep = (uid: number, dir: -1 | 1) =>
    setSteps((l) => {
      const i = l.findIndex((s) => s.uid === uid);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= l.length) return l;
      const next = [...l];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });

  /** Modelo escolhido no seletor; "" = "Qualquer modelo" (condição não enviada). */
  const conditionSource = typeof conditions.source === "string" ? conditions.source : "";

  /** Edita só conditions.source, preservando as demais condições existentes. */
  const setConditionSource = (value: string) =>
    setConditions((c) => {
      const next = { ...c };
      if (value) next.source = value;
      else delete next.source;
      return next;
    });

  const save = async () => {
    setError(null);
    if (!title.trim()) {
      setError("Dê um nome ao fluxo.");
      return;
    }
    for (const s of steps) {
      if (s.actionType === "CREATE_TASK" && !s.title.trim()) {
        setError("Toda tarefa precisa de um título.");
        return;
      }
      if (s.actionType === "CREATE_REMINDER" && (!s.title.trim() || !s.when)) {
        setError("Todo lembrete precisa de um texto e de uma data e hora.");
        return;
      }
      if (s.actionType === "SEND_TELEGRAM") {
        if (!s.text.trim()) {
          setError("Escreva a mensagem que vai chegar no Telegram.");
          return;
        }
        if (!s.requireConfirmation && !canSkipConfirmation(s.actionType)) {
          setError("Envio no Telegram sem confirmação só pode ser liberado quando o modo geral for Automático.");
          return;
        }
      }
      if (s.actionType === "CREATE_CALENDAR_EVENT") {
        if (!s.title.trim()) {
          setError("Todo evento da agenda precisa de um título.");
          return;
        }
        if (!s.startWhen || !s.endWhen) {
          setError("Todo evento da agenda precisa de início e fim.");
          return;
        }
        if (new Date(s.endWhen).getTime() <= new Date(s.startWhen).getTime()) {
          setError("O fim do evento precisa ser depois do início.");
          return;
        }
        if (s.calendarTz.trim() && !isValidTimeZone(s.calendarTz.trim())) {
          setError("O fuso horário do evento não parece válido (ex: America/Sao_Paulo).");
          return;
        }
        if (!s.requireConfirmation && !canSkipConfirmation(s.actionType)) {
          setError(
            "Evento na agenda sem confirmação só pode ser liberado com o modo geral Automático e " +
            "\"Criar evento na agenda\" marcado como \"Sozinho, sem perguntar\" nas preferências.",
          );
          return;
        }
      }
    }
    setSaving(true);
    try {
      // conditions.source só filtra o gatilho "nota pronta"; nos demais ele
      // deixaria o fluxo sem correspondência, então não é enviado.
      const conditionsToSend = { ...conditions };
      if (trigger !== "DOCUMENT_GENERATED") delete conditionsToSend.source;
      const body = {
        title: title.trim(),
        description: description.trim(),
        trigger_type: trigger,
        conditions: conditionsToSend,
        enabled: wfEnabled,
        steps: steps.map((s, i) => stepToApi(s, i)),
      };
      if (editingId) await updateWorkflow(editingId, body);
      else await createWorkflow(body);
      reset();
      await load();
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSaving(false);
    }
  };

  const edit = (w: Workflow) => {
    setEditingId(w.id);
    setEditingVersion(w.version);
    setTitle(w.title);
    setDescription(w.description ?? "");
    setTrigger(w.trigger_type);
    setConditions(w.conditions ?? {});
    setWfEnabled(w.enabled);
    setSteps(w.steps.map(stepFromRead));
    setShowForm(true);
    setError(null);
    window.scrollTo(0, 0);
  };

  const toggle = async (w: Workflow) => {
    setError(null);
    setItems((l) => l.map((x) => (x.id === w.id ? { ...x, enabled: !w.enabled } : x)));
    try {
      await updateWorkflow(w.id, { enabled: !w.enabled });
    } catch (e) {
      setItems((l) => l.map((x) => (x.id === w.id ? { ...x, enabled: w.enabled } : x)));
      setError(apiError(e));
    }
  };

  const remove = async (w: Workflow) => {
    if (!window.confirm(`Apagar o fluxo "${w.title}"?`)) return;
    setError(null);
    try {
      await deleteWorkflow(w.id);
      await load();
    } catch (e) {
      setError(apiError(e));
    }
  };

  const saveSettings = async () => {
    setError(null);
    setSettingsMsg(null);
    // Regra de segurança: Telegram/Agenda automáticos só existem junto de modo Automático.
    const cfg: Record<string, string> = {};
    for (const action of Object.keys(ACTION_LABELS)) {
      const disp = actionConfig[action] ?? "require_confirmation";
      cfg[action] = AUTO_GATE_ACTIONS.has(action) && mode !== "automatic" && disp === "automatic"
        ? "require_confirmation"
        : disp;
    }
    // Silêncio é par: início e fim juntos (ou os dois vazios), no fuso do espaço.
    const qs = hhmmToMinute(quietStart);
    const qe = hhmmToMinute(quietEnd);
    if (qs === "invalid" || qe === "invalid") {
      setError("Horário de silêncio inválido — use HH:MM, por exemplo 22:00.");
      return;
    }
    if ((qs === null) !== (qe === null)) {
      setError("Preencha início e fim do silêncio, ou deixe os dois vazios para não ter silêncio.");
      return;
    }
    setSettingsSaving(true);
    try {
      const saved = await putWorkflowSettings({
        automation_mode: mode,
        // Limiares não têm UI aqui: enviamos de volta sem alterar.
        action_thresholds: settings?.action_thresholds ?? {},
        action_config: cfg,
        notifications_enabled: notifEnabled,
        quiet_start_minute: qs,
        quiet_end_minute: qe,
      });
      setSettings(saved);
      setMode(saved.automation_mode);
      setActionConfig(saved.action_config ?? {});
      setSettingsMsg("Preferências salvas.");
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSettingsSaving(false);
    }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Fluxos</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Monte o que deve acontecer depois que algo acontece — tarefa, lembrete ou mensagem.
          </p>
        </div>
        <div className="row">
          <button className="btn-secondary" onClick={() => (showForm ? reset() : setShowForm(true))}>
            {showForm ? "Fechar" : "Criar fluxo"}
          </button>
        </div>
      </div>

      <div className="card" style={{ background: "#f6faf7", border: "1px solid #dcebe1" }}>
        <b>Fluxos valem quando ativados — e cada passo segue as suas permissões.</b>
        <p className="muted" style={{ margin: "4px 0 0" }}>
          Quando um gatilho já suportado acontecer, os passos do fluxo se preparam: os que pedem a sua
          confirmação aparecem aqui em "Aguardando você" e nada é enviado sem a sua decisão; os que você
          marcou como "sem perguntar" só agem dentro do modo geral escolhido abaixo. Gatilhos marcados
          "em preparação" ainda não iniciam nada.
        </p>
      </div>

      {error && <p className="error">{error}</p>}

      {executions.some((e) => e.status === "awaiting_confirmation") && (
        <div className="card" style={{ border: "1px solid #fde68a", background: "#fffbeb" }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <b>
                {executions.filter((e) => e.status === "awaiting_confirmation").length === 1
                  ? "1 ação de fluxo aguardando a sua decisão."
                  : `${executions.filter((e) => e.status === "awaiting_confirmation").length} ações de fluxo aguardando a sua decisão.`}
              </b>
              <p className="muted" style={{ margin: "4px 0 0" }}>
                As aprovações ficam junto com o resto das suas pendências, em um único lugar.
              </p>
            </div>
            <Link to="/inbox" className="btn btn-secondary">Revisar em "Para revisar"</Link>
          </div>
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Como os fluxos podem agir</h3>
        <label>Modo geral</label>
        <select value={mode} onChange={(e) => setMode(e.target.value as AutomationMode)}>
          {MODES.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
        <div className="grid2" style={{ marginTop: 8 }}>
          {Object.entries(ACTION_LABELS).map(([action, label]) => {
            const value = actionConfig[action] ?? "require_confirmation";
            const autoBlocked = AUTO_GATE_ACTIONS.has(action) && mode !== "automatic";
            return (
              <div key={action}>
                <label>{label}</label>
                <select
                  value={autoBlocked && value === "automatic" ? "require_confirmation" : value}
                  onChange={(e) => setActionConfig((c) => ({ ...c, [action]: e.target.value }))}>
                  {DISPOSITIONS.map((d) => (
                    <option key={d.value} value={d.value}
                      disabled={autoBlocked && d.value === "automatic"}>
                      {d.label}{autoBlocked && d.value === "automatic" ? " (só no modo Automático)" : ""}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
        </div>
        <div style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 14 }}>
          <label className="row" style={{ gap: 8, margin: 0 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={notifEnabled}
              onChange={(e) => setNotifEnabled(e.target.checked)} />
            Avisar no Telegram quando algo precisar de você
          </label>
          <div className="grid2" style={{ marginTop: 8 }}>
            <div>
              <label>Silêncio começa</label>
              <input type="time" value={quietStart} onChange={(e) => setQuietStart(e.target.value)} />
            </div>
            <div>
              <label>Silêncio termina</label>
              <input type="time" value={quietEnd} onChange={(e) => setQuietEnd(e.target.value)} />
            </div>
          </div>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            O silêncio vale só para avisos no Telegram, no fuso do seu espaço
            ({wsTz ?? "UTC"}). Tarefas, agenda e o resto dos fluxos seguem normais — nada é suspenso
            nem gasta tentativa. Deixe os dois vazios para não ter horário de silêncio.
            Mude o fuso em <Link to="/privacidade" className="text-link">Privacidade → Fuso do seu espaço</Link>.
          </p>
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn-secondary" disabled={settingsSaving} onClick={() => void saveSettings()}>
            {settingsSaving ? "Salvando…" : "Salvar preferências"}
          </button>
          {settingsMsg && <span className="muted">{settingsMsg}</span>}
        </div>
      </div>

      {showForm && (
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
            <h3 style={{ marginTop: 0 }}>{editingId ? "Editar fluxo" : "Novo fluxo"}</h3>
            {editingId && editingVersion !== null && (
              <span className="muted">versão {editingVersion}</span>
            )}
          </div>

          <label>Nome</label>
          <input value={title} placeholder="Ex: Reunião virou tarefas"
            onChange={(e) => setTitle(e.target.value)} />

          <label>Descrição (opcional)</label>
          <textarea rows={2} value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Ex: Quando uma nota ficar pronta, criar uma tarefa e me avisar no Telegram." />

          <label>Quando começa?</label>
          <select value={trigger} onChange={(e) => setTrigger(e.target.value)}>
            {TRIGGERS.map((t) => (
              <option key={t.value} value={t.value} disabled={!t.ready}>{t.label}</option>
            ))}
          </select>

          {trigger === "DOCUMENT_GENERATED" && (
            <>
              <label>Este fluxo roda para notas de qual modelo?</label>
              <select value={conditionSource} onChange={(e) => setConditionSource(e.target.value)}>
                <option value="">Qualquer modelo</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>{t.title}</option>
                ))}
                {conditionSource && !templates.some((t) => t.id === conditionSource) && (
                  <option value={conditionSource}>Modelo atual (não encontrado na lista)</option>
                )}
              </select>
              <p className="muted" style={{ margin: "4px 0 0" }}>
                {conditionSource
                  ? "Só notas geradas com este modelo iniciam o fluxo; notas de outros modelos e notas gerais ficam de fora."
                  : "Qualquer nota que ficar pronta inicia o fluxo, inclusive notas gerais (sem modelo)."}
              </p>
            </>
          )}

          <label style={{ marginTop: 16 }}>O que acontece, em ordem?</label>
          <p className="muted" style={{ marginTop: 0 }}>
            Escreva os textos sem links, senhas ou dados de acesso — o sistema recusa por segurança.
          </p>
          {steps.map((s, i) => (
            <div key={s.uid} className="card" style={{ background: s.enabled ? "#f9fafb" : "#f3f4f6" }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <b>{i + 1}. {stepLabel(s.actionType)}</b>
                <div className="row">
                  <button className="btn-secondary" disabled={i === 0}
                    onClick={() => moveStep(s.uid, -1)} aria-label="Mover passo para cima">↑</button>
                  <button className="btn-secondary" disabled={i === steps.length - 1}
                    onClick={() => moveStep(s.uid, 1)} aria-label="Mover passo para baixo">↓</button>
                  <button className="btn-danger"
                    onClick={() => setSteps((l) => l.filter((x) => x.uid !== s.uid))}>Remover</button>
                </div>
              </div>

              {s.actionType === "CREATE_TASK" && (
                <>
                  <label>O que a tarefa deve dizer?</label>
                  <input value={s.title} placeholder="Ex: Ligar para o veterinário"
                    onChange={(e) => patchStep(s.uid, { title: e.target.value })} />
                  <label>Detalhes (opcional)</label>
                  <textarea rows={2} value={s.summary}
                    onChange={(e) => patchStep(s.uid, { summary: e.target.value })} />
                  <label>Prazo (opcional)</label>
                  <input type="datetime-local" value={s.when}
                    onChange={(e) => patchStep(s.uid, { when: e.target.value })} />
                </>
              )}

              {s.actionType === "CREATE_REMINDER" && (
                <>
                  <label>Do que você quer lembrar?</label>
                  <input value={s.title} placeholder="Ex: Confirmar consulta"
                    onChange={(e) => patchStep(s.uid, { title: e.target.value })} />
                  <label>Quando?</label>
                  <input type="datetime-local" value={s.when}
                    onChange={(e) => patchStep(s.uid, { when: e.target.value })} />
                </>
              )}

              {s.actionType === "SEND_TELEGRAM" && (
                <>
                  <label>Mensagem que você vai receber</label>
                  <textarea rows={3} value={s.text}
                    placeholder="Ex: A nota da reunião ficou pronta."
                    onChange={(e) => patchStep(s.uid, { text: e.target.value })} />
                  <p className="muted">
                    Enviado pelo bot oficial para o Telegram conectado em Segredos — sem token, sem configuração.
                  </p>
                </>
              )}

              {s.actionType === "CREATE_CALENDAR_EVENT" && (
                <>
                  {calendarReady === false && (
                    <div className="card" style={{ background: "#fffbeb", border: "1px solid #fde68a", marginTop: 0 }}>
                      <b>Agenda Google ainda não conectada.</b>
                      <p className="muted" style={{ margin: "4px 0 8px" }}>
                        Você pode deixar o passo pronto aqui, mas ele só poderá agir depois que a
                        conta estiver conectada na página de Integrações.
                      </p>
                      <a className="btn-secondary" href="/integracoes" style={{ textDecoration: "none" }}>
                        Ir para Integrações
                      </a>
                    </div>
                  )}
                  <label>Título do evento</label>
                  <input value={s.title} placeholder="Ex: Reunião com a Maria"
                    onChange={(e) => patchStep(s.uid, { title: e.target.value })} />
                  <div className="grid2">
                    <div>
                      <label>Início</label>
                      <input type="datetime-local" value={s.startWhen}
                        onChange={(e) => patchStep(s.uid, { startWhen: e.target.value })} />
                    </div>
                    <div>
                      <label>Fim</label>
                      <input type="datetime-local" value={s.endWhen}
                        onChange={(e) => patchStep(s.uid, { endWhen: e.target.value })} />
                    </div>
                  </div>
                  <p className="muted" style={{ margin: "4px 0 0" }}>
                    Fuso horário: <b>{s.calendarTz}</b> (do seu dispositivo).
                    O evento vai para a agenda da conta Google conectada — nada de convidados ou salas é mexido.
                  </p>
                  <details style={{ marginTop: 6 }}>
                    <summary className="muted" style={{ cursor: "pointer" }}>Usar outro fuso horário</summary>
                    <input style={{ marginTop: 6 }} value={s.calendarTz} placeholder="Ex: America/Sao_Paulo"
                      onChange={(e) => patchStep(s.uid, { calendarTz: e.target.value })} />
                  </details>
                </>
              )}

              {!["CREATE_TASK", "CREATE_REMINDER", "SEND_TELEGRAM", "CREATE_CALENDAR_EVENT"].includes(s.actionType) && (
                <p className="muted">
                  Passo interno salvo pelo sistema. Ele não é editável aqui e segue intacto ao salvar.
                </p>
              )}

              <label className="row" style={{ gap: 8, marginTop: 8 }}>
                <input type="checkbox" style={{ width: "auto" }} checked={s.enabled}
                  onChange={(e) => patchStep(s.uid, { enabled: e.target.checked })} />
                Passo ativado
              </label>
              <label className="row" style={{ gap: 8 }}>
                <input type="checkbox" style={{ width: "auto" }} checked={s.requireConfirmation}
                  onChange={(e) => {
                    // Blinda a desmarcação de ações com efeito fora da tela.
                    if (AUTO_GATE_ACTIONS.has(s.actionType) && !e.target.checked && !canSkipConfirmation(s.actionType)) {
                      setError(s.actionType === "CREATE_CALENDAR_EVENT"
                        ? "Evento na agenda sem confirmação só pode ser liberado com o modo geral Automático e \"Criar evento na agenda\" marcado como \"Sozinho, sem perguntar\" nas preferências acima."
                        : "Envio no Telegram sem confirmação só pode ser liberado quando o modo geral for Automático (em \"Como os fluxos podem agir\").");
                      return;
                    }
                    setError(null);
                    patchStep(s.uid, { requireConfirmation: e.target.checked });
                  }} />
                Pedir minha confirmação antes de concluir
              </label>
              {AUTO_GATE_ACTIONS.has(s.actionType) && !s.requireConfirmation && (
                <p className="muted" style={{ margin: "4px 0 0" }}>
                  Este passo conclui sem perguntar quando o fluxo executar — escolha sua, e reversível aqui.
                </p>
              )}
            </div>
          ))}
          <div className="row">
            <button className="btn-secondary" onClick={() => setSteps((l) => [...l, newStep("CREATE_TASK")])}>
              ＋ Criar tarefa
            </button>
            <button className="btn-secondary" onClick={() => setSteps((l) => [...l, newStep("CREATE_REMINDER")])}>
              ＋ Criar lembrete
            </button>
            <button className="btn-secondary" onClick={() => setSteps((l) => [...l, newStep("SEND_TELEGRAM")])}>
              ＋ Mensagem no Telegram
            </button>
            <button className="btn-secondary" onClick={() => setSteps((l) => [...l, newStep("CREATE_CALENDAR_EVENT")])}>
              ＋ Evento na agenda
            </button>
          </div>
          {steps.length === 0 && (
            <p className="muted">Sem passos ainda — adicione pelo menos um antes de ativar.</p>
          )}

          <label className="row" style={{ gap: 8, marginTop: 16 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={wfEnabled}
              onChange={(e) => setWfEnabled(e.target.checked)} />
            Fluxo ativado
          </label>
          <p className="muted" style={{ marginTop: 0 }}>
            Ativado, este fluxo entra em jogo quando o gatilho escolhido já tiver suporte —
            e sempre dentro das permissões que você definiu (confirmação por passo e modo geral acima).
            Gatilhos "em preparação" não iniciam nada ainda.
          </p>

          <div className="row" style={{ marginTop: 12 }}>
            <button disabled={saving} onClick={() => void save()}>
              {saving ? "…" : editingId ? "Salvar" : "Criar fluxo"}
            </button>
            <button className="btn-secondary" onClick={reset}>Cancelar</button>
          </div>
        </div>
      )}

      {items.length === 0 && !showForm && (
        <p className="muted">Você ainda não criou fluxos. Comece com algo simples, como avisar no Telegram quando uma nota ficar pronta.</p>
      )}
      {items.map((w) => (
        <div key={w.id} className="card" style={{ background: w.enabled ? undefined : "#f3f4f6" }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <b>{w.title}</b>
            <div className="row">
              <span className={`badge ${w.enabled ? "processed" : "pending_review"}`}>
                {w.enabled ? "Ativado" : "Desativado"}
              </span>
              <span className="muted">v{w.version}</span>
            </div>
          </div>
          <p className="muted">
            {triggerLabel(w.trigger_type)}
            {w.steps.length > 0
              ? ` · ${w.steps.length} ${w.steps.length === 1 ? "passo" : "passos"}: ${w.steps.map((s) => stepLabel(s.action_type)).join(" → ")}`
              : " · sem passos"}
          </p>
          {w.description && <p className="muted">{w.description}</p>}
          <div className="row">
            <button className="btn-secondary" onClick={() => edit(w)}>Editar</button>
            <button className="btn-secondary" onClick={() => void toggle(w)}>
              {w.enabled ? "Desativar" : "Ativar"}
            </button>
            <button className="btn-danger" onClick={() => void remove(w)}>Apagar</button>
          </div>
        </div>
      ))}

      {executions.some((e) => e.status !== "awaiting_confirmation") && (
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h3 style={{ margin: 0 }}>Histórico recente</h3>
            <button className="btn-secondary" onClick={() => void load()}>Atualizar</button>
          </div>
          {executions.filter((e) => e.status !== "awaiting_confirmation").slice(0, 8).map((ex) => (
            <div key={ex.id} className="kv">
              <span>
                {stepLabel(ex.action_type)}
                <span className="muted"> · {triggerLabel(ex.trigger_type)} · {fmtTime(ex.created_at)}</span>
                {ex.status === "failed" && ex.last_error && (
                  <span className="muted"> · {ex.last_error}</span>
                )}
              </span>
              <span className={`badge ${ex.status === "succeeded" ? "processed" : ex.status === "pending" || ex.status === "claimed" ? "general" : "pending_review"}`}>
                {statusLabel(ex.status)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
