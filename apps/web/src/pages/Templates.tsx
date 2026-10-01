import { useCallback, useEffect, useState } from "react";
import {
  apiError,
  createTemplate,
  deleteTemplate,
  draftTemplate,
  listSecrets,
  listTemplates,
  updateTemplate,
} from "../api";
import type { RoutingRule, Template } from "../types";

import type { ActionDef, FieldDef, FieldKind } from "../templateBuilder";
import {
  KINDS,
  actionsToDefs,
  defsToActions,
  fieldsToSchema,
  newAction,
  schemaToFields,
} from "../templateBuilder";

/* ------------------------------------------------------------------ tela */

const EMPTY_FORM = { title: "", purpose: "", example: "", is_public: false };

/** Metadados de roteamento (categoria, casos de uso, descrição) — textos livres no form. */
const EMPTY_META = { category: "", useCases: "", routingDescription: "" };

const parseUseCases = (raw: string): string[] =>
  raw.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 20);

const EMPTY_RULE_FORM = { priority: "100" };

/**
 * Hoje o pipeline de roteamento só aplica regras GLOBAIS em gravação intencional
 * (context=null; capture_mode='intentional' ou null, que é coringa — e o worker
 * só roteia no modo intencional). Regras com contexto ou modo contínuo criadas
 * por API são preservadas e exibidas, mas não executam — a UI não deve prometer
 * o contrário.
 */
const ruleSupported = (r: RoutingRule): boolean =>
  r.context === null && (r.capture_mode === "intentional" || r.capture_mode === null);

/** Resumo legível da regra forte, com alcance explícito (nunca "Sempre"). */
const ruleSummary = (r: RoutingRule): string => {
  const base = ruleSupported(r)
    ? "Para novas gravações intencionais"
    : r.context && r.capture_mode === "continuous"
      ? `Contexto “${r.context}” em gravação contínua`
      : r.context
        ? `Contexto “${r.context}”`
        : r.capture_mode === "continuous"
          ? "Gravação contínua"
          : "Regra personalizada";
  return `${base} · prioridade ${r.priority}`;
};

export default function Templates() {
  const [items, setItems] = useState<Template[]>([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [fields, setFields] = useState<FieldDef[]>([]);
  const [extraProps, setExtraProps] = useState<Record<string, unknown>>({});
  const [rootExtra, setRootExtra] = useState<Record<string, unknown>>({});
  const [actions, setActions] = useState<ActionDef[]>([]);
  const [extraActions, setExtraActions] = useState<unknown[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [derivedTrigger, setDerivedTrigger] = useState("");
  const [detectedAtExample, setDetectedAtExample] = useState("");
  // Extras (description, minimum…) de templates legados, por nome de campo,
  // para não perder fidelidade ao salvar pelo novo fluxo.
  const [legacyExtras, setLegacyExtras] = useState<Record<string, Record<string, unknown>>>({});
  const [aiOpen, setAiOpen] = useState(false);
  const [aiBrief, setAiBrief] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  // Telegram da plataforma: o bot é oficial, o usuário só conecta o chat em Segredos.
  const [telegramOk, setTelegramOk] = useState<boolean | null>(null);
  // Metadados de roteamento do template: origMeta evita reenviar null indevido em saves legados.
  const [meta, setMeta] = useState(EMPTY_META);
  const [origMeta, setOrigMeta] = useState(EMPTY_META);
  const [editingVersion, setEditingVersion] = useState<number | null>(null);
  // Regras fortes de roteamento (só existem para template já salvo).
  const [rules, setRules] = useState<RoutingRule[]>([]);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [rulesError, setRulesError] = useState<string | null>(null);
  const [ruleForm, setRuleForm] = useState(EMPTY_RULE_FORM);
  const [ruleSaving, setRuleSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [tpls, secrets] = await Promise.all([
        listTemplates(),
        listSecrets().catch(() => []),
      ]);
      setItems(tpls);
      setTelegramOk(secrets.some((s) => s.key === "TELEGRAM_CHAT_ID"));
    } catch (e) {
      const { apiError } = await import("../api");
      setError(apiError(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load ]);

  const reset = () => {
    setForm(EMPTY_FORM);
    setFields([]);
    setExtraProps({});
    setRootExtra({});
    setActions([]);
    setExtraActions([]);
    setEditingId(null);
    setShowForm(false);
    setError(null);
    setDerivedTrigger("");
    setDetectedAtExample("");
    setLegacyExtras({});
    setMeta(EMPTY_META);
    setOrigMeta(EMPTY_META);
    setEditingVersion(null);
    setRules([]);
    setRulesError(null);
    setRuleForm(EMPTY_RULE_FORM);
  };

  /** Exemplo inicial para templates antigos (sem exemplo): esqueleto dos campos. */
  const stubExample = (t: Template): string => {
    const { fields: f } = schemaToFields(t.output_schema);
    if (f.length === 0) return "";
    const lines = ["Exemplo de como a nota deve sair:", ""];
    for (const field of f) lines.push(`- ${field.name}: ...`);
    return lines.join("\n");
  };

  /** Roda a detecção (POST /derive) salvo se o exemplo não mudou desde a última. */
  const ensureDerived = async (): Promise<{ trigger: string; fields: FieldDef[] } | null> => {
    const example = form.example.trim();
    if (example.length < 20) {
      setError("Cole um exemplo mais completo de como a nota deve sair (mínimo 20 caracteres).");
      return null;
    }
    if (detectedAtExample === example && fields.length > 0) {
      return { trigger: derivedTrigger, fields };
    }
    setDetecting(true);
    setError(null);
    try {
      const { deriveTemplate } = await import("../api");
      const res = await deriveTemplate(example);
      const kinds: FieldDef["kind"][] = ["texto", "número", "data", "lista", "sim/não", "opções"];
      const mapped: FieldDef[] = res.fields.map((fld) => ({
        name: fld.name,
        kind: (kinds as string[]).includes(fld.kind) ? (fld.kind as FieldDef["kind"]) : "texto",
        required: fld.required,
        options: (fld.options ?? []).join(", "),
        extra: {},
      }));
      setFields(mapped);
      setDerivedTrigger(res.semantic_trigger);
      setDetectedAtExample(example);
      return { trigger: res.semantic_trigger, fields: mapped };
    } catch (e) {
      const { apiError } = await import("../api");
      setError(apiError(e));
      return null;
    } finally {
      setDetecting(false);
    }
  };

  const setF = (k: keyof typeof EMPTY_FORM, v: string | boolean) =>
    setForm((f) => ({ ...f, [k]: v }));

  const patchAction = (uid: number, p: Partial<ActionDef>) =>
    setActions((list) => list.map((a) => (a.uid === uid ? { ...a, ...p } : a)));

  const save = async () => {
    setError(null);
    if (!form.title.trim()) {
      setError("Dê um nome ao template.");
      return;
    }
    if (!form.purpose.trim()) {
      setError("Diga para que serve o template (obrigatório para a IA escolher).");
      return;
    }
    const derived = await ensureDerived();
    if (!derived) return;
    const named = derived.fields.filter((f) => f.name.trim());
    if (named.length === 0) {
      setError("A IA não identificou informações no exemplo — detalhe mais o exemplo.");
      return;
    }
    for (const a of actions) {
      // Telegram da plataforma: destino padrão = chat conectado em Segredos.
      // chatId preenchido = override opcional para outro chat/grupo.
      if (a.kind === "telegram" && !a.message.trim()) {
        setError("Escreva a mensagem que vai chegar no Telegram.");
        return;
      }
      if (a.kind === "webhook" && !a.url.trim()) {
        setError("Toda ação de Webhook precisa da URL.");
        return;
      }
    }
    setSaving(true);
    try {
      const enriched = named.map((f) => ({ ...f, extra: { ...(legacyExtras[f.name] ?? {}), ...f.extra } }));
      const body: Record<string, unknown> = {
        title: form.title.trim(),
        purpose: form.purpose.trim(),
        semantic_trigger: derived.trigger,
        example_output: form.example.trim(),
        output_schema: fieldsToSchema(enriched, extraProps, rootExtra),
        actions: defsToActions(actions, extraActions),
        is_public: form.is_public,
      };
      // Metadados de roteamento: só envia preenchidos; vazio vira null apenas se
      // o usuário limpou um valor que existia (limpeza intencional). Assim,
      // templates legados nunca perdem categoria/casos de uso por acidente.
      const cat = meta.category.trim();
      const rdesc = meta.routingDescription.trim();
      const ucs = parseUseCases(meta.useCases);
      if (cat) body.category = cat;
      else if (editingId && origMeta.category.trim()) body.category = null;
      if (rdesc) body.routing_description = rdesc;
      else if (editingId && origMeta.routingDescription.trim()) body.routing_description = null;
      if (ucs.length > 0) body.use_cases = ucs;
      else if (editingId && parseUseCases(origMeta.useCases).length > 0) body.use_cases = [];
      if (editingId) await updateTemplate(editingId, body);
      else await createTemplate(body);
      reset();
      await load();
    } catch (e) {
      const { apiError } = await import("../api");
      setError(apiError(e));
    } finally {
      setSaving(false);
    }
  };

  const loadRules = async (templateId: string) => {
    setRulesLoading(true);
    setRulesError(null);
    try {
      const { listRoutingRules } = await import("../api");
      setRules(await listRoutingRules(templateId));
    } catch (e) {
      const { apiError } = await import("../api");
      setRulesError(apiError(e));
      setRules([]);
    } finally {
      setRulesLoading(false);
    }
  };

  const addRule = async () => {
    if (!editingId) return;
    // Enquanto o pipeline só roteia gravações intencionais sem filtro de contexto,
    // a criação pela UI é sempre regra global intencional.
    const body: Record<string, unknown> = { capture_mode: "intentional" };
    const prio = Number.parseInt(ruleForm.priority, 10);
    if (Number.isFinite(prio)) body.priority = Math.max(0, Math.min(1000, prio));
    setRuleSaving(true);
    setRulesError(null);
    try {
      const { createRoutingRule } = await import("../api");
      await createRoutingRule(editingId, body);
      setRuleForm(EMPTY_RULE_FORM);
      await loadRules(editingId);
    } catch (e) {
      const { apiError } = await import("../api");
      setRulesError(apiError(e));
    } finally {
      setRuleSaving(false);
    }
  };

  const toggleRule = async (r: RoutingRule) => {
    if (!editingId) return;
    setRulesError(null);
    // Otimista: reflete na hora e desfaz se a API falhar.
    setRules((l) => l.map((x) => (x.id === r.id ? { ...x, enabled: !r.enabled } : x)));
    try {
      const { updateRoutingRule } = await import("../api");
      await updateRoutingRule(editingId, r.id, { enabled: !r.enabled });
    } catch (e) {
      const { apiError } = await import("../api");
      setRulesError(apiError(e));
      setRules((l) => l.map((x) => (x.id === r.id ? { ...x, enabled: r.enabled } : x)));
    }
  };

  const removeRule = async (r: RoutingRule) => {
    if (!editingId) return;
    setRulesError(null);
    try {
      const { deleteRoutingRule } = await import("../api");
      await deleteRoutingRule(editingId, r.id);
      setRules((l) => l.filter((x) => x.id !== r.id));
    } catch (e) {
      const { apiError } = await import("../api");
      setRulesError(apiError(e));
    }
  };

  const edit = (t: Template) => {
    const { defs, extra: xa } = actionsToDefs(t.actions);
    const { fields: legacy } = schemaToFields(t.output_schema);
    const extras: Record<string, Record<string, unknown>> = {};
    for (const f of legacy) extras[f.name] = f.extra ?? {};
    setLegacyExtras(extras);
    setForm({
      title: t.title,
      purpose: t.purpose || "",
      example: t.example_output || stubExample(t),
      is_public: t.is_public,
    });
    // Campos são re-detectados do exemplo ao salvar (fonte da verdade).
    setFields([]);
    setExtraProps({});
    setRootExtra({});
    setDerivedTrigger("");
    setDetectedAtExample("");
    setActions(defs);
    setExtraActions(xa);
    const loadedMeta = {
      category: t.category ?? "",
      useCases: (t.use_cases ?? []).join("\n"),
      routingDescription: t.routing_description ?? "",
    };
    setMeta(loadedMeta);
    setOrigMeta(loadedMeta);
    setEditingVersion(t.version);
    setRules([]);
    setRulesError(null);
    setRuleForm(EMPTY_RULE_FORM);
    void loadRules(t.id);
    setEditingId(t.id);
    setShowForm(true);
    setError(null);
    window.scrollTo(0, 0);
  };

  const remove = async (t: Template) => {
    if (!window.confirm(`Apagar o template "${t.title}"? Notas já processadas mantêm os dados.`)) return;
    try {
      const { deleteTemplate } = await import("../api");
      await deleteTemplate(t.id);
      await load();
    } catch (e) {
      const { apiError } = await import("../api");
      setError(apiError(e));
    }
  };

  const fieldNames = fields.map((f) => f.name.trim()).filter(Boolean);

  /** Ações mcp_tool legadas: não editáveis aqui, preservadas inteiras no save. */
  const legacyAppActions = extraActions.filter(
    (x) => typeof x === "object" && x !== null && (x as { type?: unknown }).type === "mcp_tool",
  ).length;

  /** Ações salvas como desativadas (ex: clone do Marketplace) — exibidas na lista. */
  const disabledActions = (t: Template) => (t.actions ?? []).filter((a) => a.enabled === false).length;

  const generateWithAi = async () => {
    if (aiBrief.trim().length < 10) {
      setAiError("Descreva com mais detalhes (mínimo 10 caracteres).");
      return;
    }
    setAiBusy(true);
    setAiError(null);
    try {
      const draft = await draftTemplate(aiBrief.trim());
      const kinds: FieldDef["kind"][] = ["texto", "número", "data", "lista", "sim/não", "opções"];
      const example = draft.suggested_example?.trim() || "";
      setForm({
        title: draft.title,
        purpose: draft.purpose ?? "",
        example,
        is_public: false,
      });
      const mapped: FieldDef[] = draft.fields.map((f) => ({
        name: f.name,
        kind: (kinds as string[]).includes(f.kind) ? (f.kind as FieldDef["kind"]) : "texto",
        required: f.required,
        options: (f.options ?? []).join(", "),
        extra: {},
      }));
      setFields(mapped);
      setDerivedTrigger(draft.semantic_trigger);
      setDetectedAtExample(example);
      setLegacyExtras({});
      setExtraProps({});
      setRootExtra({});
      // Não ativa envios externos sem escolha explícita do usuário.
      setActions([]);
      setExtraActions([]);
      setMeta(EMPTY_META);
      setOrigMeta(EMPTY_META);
      setEditingVersion(null);
      setRules([]);
      setRulesError(null);
      setRuleForm(EMPTY_RULE_FORM);
      setEditingId(null);
      setAiOpen(false);
      setAiBrief("");
      setShowForm(true);
      setError(null);
      window.scrollTo(0, 0);
    } catch (e) {
      setAiError(apiError(e));
    } finally {
      setAiBusy(false);
    }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Automações</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>Defina o que acontece com uma nota depois que você grava.</p>
        </div>
        <div className="row">
          <button className="btn-blue" onClick={() => { setAiOpen(true); setAiError(null); }}>
            Criar com ajuda da IA
          </button>
          <button className="btn-secondary" onClick={() => (showForm ? reset() : setShowForm(true))}>
            {showForm ? "Fechar" : "Criar automação"}
          </button>
        </div>
      </div>
      <p className="muted">
        Descreva um resultado que você quer repetir. Depois, escolha se a nota também deve criar uma tarefa, avisar você no Telegram ou sair para outro sistema.
      </p>
      {error && <p className="error">{error}</p>}

      {showForm && (
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
            <h3 style={{ marginTop: 0 }}>{editingId ? "Editar automação" : "Nova automação"}</h3>
            {editingId && editingVersion !== null && (
              <span className="muted">versão {editingVersion}</span>
            )}
          </div>

          <label>Nome</label>
          <input value={form.title} onChange={(e) => setF("title", e.target.value)}
            placeholder="Ex: Ata de reunião, Consulta veterinária…" />

          <label>O que ela ajuda você a fazer?</label>
          <textarea rows={2} value={form.purpose}
            onChange={(e) => setF("purpose", e.target.value)}
            placeholder="Ex: Transformar o áudio de uma consulta veterinária em prontuário estruturado para registro." />

          <label>Como você quer que o resultado fique?</label>
          <textarea rows={8} value={form.example}
            onChange={(e) => setF("example", e.target.value)}
            placeholder={`Ex:\nAta de reunião — 12/09\n\nPontos discutidos:\n- Atraso na entrega\n- Novo prazo: sexta\n\nEncaminhamentos:\n- Maria envia a planilha`} />
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn-secondary" disabled={detecting}
              onClick={() => void ensureDerived()}>
              {detecting ? "Detectando…" : "Detectar informações do exemplo"}
            </button>
          </div>
          {fields.length > 0 && (
            <div className="row" style={{ marginTop: 8 }}>
              <span className="muted">A IA vai extrair:</span>
              {fields.map((f) => (
                <span key={f.name} className="badge pending_review">
                  {f.name}{f.required ? "" : " (opcional)"}
                </span>
              ))}
            </div>
          )}

          <details style={{ marginTop: 16 }} open={Boolean(meta.category || meta.useCases.trim() || meta.routingDescription)}>
            <summary className="muted" style={{ cursor: "pointer" }}>
              Ajudar a IA a escolher esta automação (opcional)
            </summary>
            <label style={{ marginTop: 8 }}>Categoria</label>
            <input value={meta.category} placeholder="Ex: Reuniões, Saúde, Casa…"
              onChange={(e) => setMeta((m) => ({ ...m, category: e.target.value }))} />
            <label>Em que situações você costuma usar?</label>
            <textarea rows={2} value={meta.useCases}
              onChange={(e) => setMeta((m) => ({ ...m, useCases: e.target.value }))}
              placeholder={"Uma situação por linha. Ex:\nReunião semanal de equipe\nAlinhamento com cliente"} />
            <label>Quando a IA deve preferir esta automação?</label>
            <textarea rows={2} value={meta.routingDescription}
              onChange={(e) => setMeta((m) => ({ ...m, routingDescription: e.target.value }))}
              placeholder="Ex: Prefira esta quando a reunião for de projeto, com decisões e prazos." />
            <p className="muted" style={{ marginTop: 4 }}>
              Tudo opcional: sem preencher, a IA decide pelo nome, pela finalidade e pelo exemplo acima.
            </p>
          </details>

          <label style={{ marginTop: 16 }}>O que deve acontecer depois?</label>
          <p className="muted" style={{ marginTop: 0 }}>As ações que você escolher são executadas automaticamente quando uma gravação combinar com esta automação.</p>
          {actions.map((a) => (
            <div key={a.uid} className="card" style={{ background: a.enabled === false ? "#f3f4f6" : "#f9fafb" }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <select style={{ width: "auto" }} value={a.kind}
                  onChange={(e) => patchAction(a.uid, { kind: e.target.value as ActionDef["kind"] })}>
                  <option value="telegram">Enviar no meu Telegram</option>
                  <option value="webhook">Enviar para outro sistema (webhook)</option>
                </select>
                <button className="btn-danger"
                  onClick={() => setActions((l) => l.filter((x) => x.uid !== a.uid))}>Remover</button>
              </div>

              <label className="row" style={{ gap: 8, marginTop: 8 }}>
                <input type="checkbox" style={{ width: "auto" }} checked={a.enabled !== false}
                  onChange={(e) => patchAction(a.uid, { enabled: e.target.checked })} />
                Ação ativada
              </label>
              {a.enabled === false && (
                <div className="card" style={{ background: "#fffbeb", border: "1px solid #fde68a", marginTop: 8 }}>
                  <b>Esta ação está desativada.</b>
                  <p className="muted" style={{ margin: "4px 0 0" }}>
                    Ative-a acima apenas se quiser enviar os dados da nota para outro serviço.
                  </p>
                </div>
              )}

              {a.kind === "telegram" && (
                <>
                  <label>Enviar no meu Telegram</label>
                  {telegramOk === false && (
                    <div className="card" style={{ background: "#fffbeb", border: "1px solid #fde68a", marginTop: 0 }}>
                      <b>Telegram ainda não conectado.</b>
                      <p className="muted" style={{ margin: "4px 0 8px" }}>
                        Leva 1 minuto: conecte seu chat em Segredos e volte aqui. O envio usa
                        o bot oficial da plataforma — sem token, sem configuração.
                      </p>
                      <a className="btn-secondary" href="/segredos" style={{ textDecoration: "none" }}>
                        Conectar Telegram
                      </a>
                    </div>
                  )}
                  <label>Mensagem</label>
                  <textarea rows={3} value={a.message}
                    onChange={(e) => patchAction(a.uid, { message: e.target.value })}
                    placeholder="Ex: Nova consulta: {animal_nome}" />
                  {fieldNames.length > 0 && (
                    <div className="row" style={{ marginTop: 6 }}>
                      <span className="muted">Inserir:</span>
                      {fieldNames.map((n) => (
                        <button key={n} className="btn-secondary"
                          onClick={() => patchAction(a.uid, { message: `${a.message}{${n}}` })}>
                          {n}
                        </button>
                      ))}
                    </div>
                  )}
                  <details style={{ marginTop: 8 }}>
                    <summary className="muted" style={{ cursor: "pointer" }}>
                      Enviar para outro chat ou grupo (opcional)
                    </summary>
                    <input style={{ marginTop: 6 }} value={a.chatId} placeholder="ID do outro chat — vazio = meu Telegram"
                      onChange={(e) => patchAction(a.uid, { chatId: e.target.value })} />
                  </details>
                  <p className="muted">Enviado pelo bot oficial para o Telegram conectado em Segredos.</p>
                </>
              )}

              {a.kind === "webhook" && (
                <>
                  <label>URL do sistema</label>
                  <input value={a.url} placeholder="https://seusistema.com/hook"
                    onChange={(e) => patchAction(a.uid, { url: e.target.value })} />
                  <div className="grid2">
                    <div>
                      <label>Método</label>
                      <select value={a.method} onChange={(e) => patchAction(a.uid, { method: e.target.value })}>
                        <option>POST</option>
                        <option>PUT</option>
                        <option>GET</option>
                      </select>
                    </div>
                    <div>
                      <label>Segredo p/ autorização (opcional)</label>
                      <input value={a.authSecret} placeholder="Ex: WEBHOOK_TOKEN"
                        onChange={(e) => patchAction(a.uid, { authSecret: e.target.value.toUpperCase() })} />
                    </div>
                  </div>
                  <p className="muted">O AnotAI envia a nota completa em JSON. Se informar o segredo, ele vai como <code>Bearer</code>.</p>
                </>
              )}

            </div>
          ))}
          <div className="row">
            <button className="btn-secondary" onClick={() => setActions((l) => [...l, newAction("telegram")])}>
              ＋ Telegram
            </button>
            <button className="btn-secondary" onClick={() => setActions((l) => [...l, newAction("webhook")])}>
              ＋ Outro sistema
            </button>
          </div>
          {legacyAppActions > 0 && (
            <div className="card" style={{ background: "#f3f4f6", marginTop: 8 }}>
              <p className="muted" style={{ margin: 0 }}>
                Esta automação guarda {legacyAppActions === 1
                  ? "uma configuração antiga de app conectado"
                  : `${legacyAppActions} configurações antigas de apps conectados`} que não aparece
                aqui nem pode ser recriada. Ela fica salva exatamente como está — o AnotAI não ativa
                nem apaga nada por conta própria.
              </p>
            </div>
          )}

          {editingId && (
            <>
              <label style={{ marginTop: 16 }}>Quando aplicar sem perguntar (regra forte)</label>
              <p className="muted" style={{ marginTop: 0 }}>
                Com uma regra forte, toda nova gravação intencional usa este modelo direto — antes de qualquer
                análise da IA e sem passar pela sua revisão. É diferente das ações acima: elas acontecem depois
                que a nota sai pronta — a regra forte escolhe o modelo antes de tudo.
              </p>
              {rulesError && <p className="error">{rulesError}</p>}
              {rulesLoading ? (
                <p className="muted">Carregando regras…</p>
              ) : (
                <>
                  {rules.length === 0 && (
                    <p className="muted">Sem regra forte — a IA sugere e você confirma antes de aplicar.</p>
                  )}
                  {rules.some(ruleSupported) && (
                    <p className="muted">
                      Se mais de uma automação tiver regra forte, vale a de maior prioridade; no empate, a mais antiga primeiro.
                    </p>
                  )}
                  {rules.map((r) => (
                    <div key={r.id} className="card"
                      style={{ background: r.enabled && ruleSupported(r) ? "#f9fafb" : "#f3f4f6" }}>
                      <div className="row" style={{ justifyContent: "space-between" }}>
                        <span>{ruleSummary(r)}</span>
                        <div className="row">
                          {ruleSupported(r) && (
                            <label className="row" style={{ gap: 8, margin: 0 }}>
                              <input type="checkbox" style={{ width: "auto" }} checked={r.enabled}
                                onChange={() => void toggleRule(r)} />
                              Ativada
                            </label>
                          )}
                          <button className="btn-danger" onClick={() => void removeRule(r)}>Remover</button>
                        </div>
                      </div>
                      {!ruleSupported(r) ? (
                        <div style={{ marginTop: 6 }}>
                          <span className="badge pending_review">Não aplicável no fluxo atual</span>
                          <p className="muted" style={{ margin: "6px 0 0" }}>
                            Hoje só regras para gravação intencional são aplicadas. Esta regra não será
                            executada, mas seus detalhes ficam preservados — remova apenas se quiser.
                          </p>
                        </div>
                      ) : !r.enabled ? (
                        <p className="muted" style={{ margin: "6px 0 0" }}>
                          Desativada: esta regra não decide nada até você ativá-la de novo.
                        </p>
                      ) : null}
                    </div>
                  ))}
                  <details style={{ marginTop: 8 }}>
                    <summary className="muted" style={{ cursor: "pointer" }}>Adicionar regra forte</summary>
                    <div style={{ marginTop: 8 }}>
                      <p className="muted" style={{ margin: "0 0 8px" }}>
                        Vai valer para todas as suas novas gravações intencionais: elas usam este modelo
                        direto, antes de qualquer análise da IA e sem passar pela sua revisão.
                      </p>
                      <label>Prioridade (maior vence empates com outras regras)</label>
                      <input type="number" min={0} max={1000} value={ruleForm.priority}
                        onChange={(e) => setRuleForm((f) => ({ ...f, priority: e.target.value }))} />
                      <div className="row" style={{ marginTop: 8 }}>
                        <button className="btn-secondary" disabled={ruleSaving}
                          onClick={() => void addRule()}>
                          {ruleSaving ? "Adicionando…" : "Adicionar regra"}
                        </button>
                      </div>
                    </div>
                  </details>
                </>
              )}
            </>
          )}

          <label className="row" style={{ gap: 8, marginTop: 16 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={form.is_public}
              onChange={(e) => setF("is_public", e.target.checked)} />
            Público no Marketplace
          </label>

          <div className="row" style={{ marginTop: 12 }}>
            <button disabled={saving} onClick={() => void save()}>{saving ? "…" : editingId ? "Salvar" : "Criar automação"}</button>
            <button className="btn-secondary" onClick={reset}>Cancelar</button>
          </div>
        </div>
      )}

      {items.length === 0 && !showForm && (
        <p className="muted">Você ainda não criou automações. Comece descrevendo uma tarefa repetitiva.</p>
      )}
      {items.map((t) => (
        <div key={t.id} className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <b>{t.title}</b>
            <span className="muted">{t.is_public ? "público" : "privado"} · v{t.version}</span>
          </div>
          {t.purpose && <p className="muted">{t.purpose}</p>}
          {disabledActions(t) > 0 && (
            <div className="row" style={{ marginTop: 4 }}>
              <span className="badge pending_review">
                {disabledActions(t) === 1 ? "1 ação desativada" : `${disabledActions(t)} ações desativadas`}
              </span>
              <span className="muted">nada é enviado para fora até você ativar no editor</span>
            </div>
          )}
          <div className="row">
            <button className="btn-secondary" onClick={() => edit(t)}>Editar</button>
            <button className="btn-danger" onClick={() => void remove(t)}>Apagar</button>
          </div>
        </div>
      ))}

      {aiOpen && (
        <div className="modal-back" onClick={() => setAiOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2 style={{ marginTop: 0 }}>Criar uma automação</h2>
            <p className="muted">
              Descreva com suas palavras o que você quer organizar. Exemplo:
              "quando eu gravar uma reunião, quero decisões, responsáveis e prazos em uma lista de tarefas".
            </p>
            <textarea rows={5} value={aiBrief}
              onChange={(e) => setAiBrief(e.target.value)}
              placeholder="Ex: Quero registrar atas das minhas reuniões com título, pontos discutidos e data…" />
            {aiError && <p className="error">{aiError}</p>}
            <div className="row" style={{ marginTop: 12 }}>
              <button className="btn-blue" disabled={aiBusy} onClick={() => void generateWithAi()}>
                {aiBusy ? "Montando…" : "Gerar rascunho"}
              </button>
              <button className="btn-secondary" onClick={() => setAiOpen(false)}>Fechar</button>
            </div>
            <p className="muted">Nada é salvo: o rascunho abre no construtor para você revisar.</p>
          </div>
        </div>
      )}
    </div>
  );
}
