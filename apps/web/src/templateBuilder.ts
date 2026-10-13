/** AnotAI Web — construtor visual de templates: tipos + conversão JSON <-> builder. */

/* ------------------------------------------------------------------ tipos */

export type FieldKind = "texto" | "número" | "data" | "lista" | "sim/não" | "opções";

export interface FieldDef {
  name: string;
  kind: FieldKind;
  required: boolean;
  options: string; // "opções": valores separados por vírgula
  extra: Record<string, unknown>; // chaves desconhecidas (description, minimum…) preservadas
}

export interface ArgRow {
  k: string;
  v: string;
}

export interface ActionDef {
  uid: number;
  kind: "telegram" | "webhook" | "mcp" | "email" | "create_task";
  chatId: string;
  message: string;
  url: string;
  method: string;
  authSecret: string;
  serverId: string;
  toolName: string;
  dynamic: boolean;
  args: ArgRow[];
  enabled: boolean;
  requireConfirmation: boolean;
  recipient: string;
  subject: string;
  body: string;
  taskTitle: string;
  taskSummary: string;
  dueAt: string;
  configJson: string;
  config: Record<string, unknown>;
}

export const KINDS: FieldKind[] = ["texto", "número", "data", "lista", "sim/não", "opções"];

let uidCounter = 1;
export const newAction = (kind: ActionDef["kind"]): ActionDef => ({
  uid: uidCounter++,
  kind,
  chatId: "",
  message: "{summary}",
  url: "",
  method: "POST",
  authSecret: "",
  serverId: "",
  toolName: "",
  // Noob-friendly: ação MCP já nasce no modo "IA decide tudo".
  // O modo manual (escolher ferramenta + mapear campos) fica no "Avançado".
  dynamic: kind === "mcp",
  args: [{ k: "", v: "" }],
  // Ação criada pelo usuário no construtor é sempre intencional: nasce ativa.
  enabled: true,
  requireConfirmation: false,
  recipient: "",
  subject: "{title}",
  body: "{summary}",
  taskTitle: "{title}",
  taskSummary: "{summary}",
  dueAt: "",
  configJson: "{}",
  config: {},
});

/* ------------------------------------------------- conversão: schema JSON */

export function schemaToFields(schema: unknown): {
  fields: FieldDef[];
  extra: Record<string, unknown>;
  rootExtra: Record<string, unknown>;
} {
  const fields: FieldDef[] = [];
  const extra: Record<string, unknown> = {};
  const rootExtra: Record<string, unknown> = {};
  if (typeof schema !== "object" || schema === null) return { fields, extra, rootExtra };
  const s = schema as Record<string, unknown>;
  for (const [k, v] of Object.entries(s)) {
    if (k !== "type" && k !== "required" && k !== "properties") rootExtra[k] = v;
  }
  const props = (s.properties as Record<string, unknown> | undefined) ?? {};
  const required = new Set(Array.isArray(s.required) ? (s.required as unknown[]).map(String) : []);
  for (const [name, raw] of Object.entries(props)) {
    if (typeof raw !== "object" || raw === null) {
      extra[name] = raw;
      continue;
    }
    const p = raw as Record<string, unknown>;
    const types = Array.isArray(p.type) ? (p.type as unknown[]).map(String) : [String(p.type ?? "string")];
    const base = types.find((t) => t !== "null") ?? "string";
    if (base !== "string" && base !== "number" && base !== "integer" && base !== "boolean" && base !== "array") {
      extra[name] = raw; // objeto aninhado etc.: preserva sem tentar editar
      continue;
    }
    let kind: FieldKind = "texto";
    let options = "";
    if (base === "array") kind = "lista";
    else if (base === "number" || base === "integer") kind = "número";
    else if (base === "boolean") kind = "sim/não";
    else if (p.format === "date") kind = "data";
    else if (Array.isArray(p.enum)) {
      kind = "opções";
      options = (p.enum as unknown[]).map(String).join(", ");
    }
    // Preserva chaves que o construtor não edita (description, minimum…).
    const rest: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(p)) {
      if (k === "type" || k === "enum" || k === "format") continue;
      if (k === "items" && base === "array") {
        if (JSON.stringify(v) === JSON.stringify({ type: "string" })) continue;
      }
      rest[k] = v;
    }
    fields.push({ name, kind, required: required.has(name), options, extra: rest });
  }
  return { fields, extra, rootExtra };
}

export function fieldsToSchema(
  fields: FieldDef[],
  extra: Record<string, unknown>,
  rootExtra: Record<string, unknown> = {},
): Record<string, unknown> {
  const properties: Record<string, unknown> = { ...extra };
  const required: string[] = [];
  for (const f of fields) {
    const name = f.name.trim();
    if (!name) continue;
    if (f.required) required.push(name);
    const nullable = !f.required;
    const extra = f.extra ?? {};
    switch (f.kind) {
      case "texto":
        properties[name] = { ...extra, type: nullable ? ["string", "null"] : "string" };
        break;
      case "número":
        properties[name] = { ...extra, type: nullable ? ["number", "null"] : "number" };
        break;
      case "data":
        properties[name] = { ...extra, type: nullable ? ["string", "null"] : "string", format: "date" };
        break;
      case "lista":
        properties[name] = { type: "array", items: { type: "string" }, ...extra };
        break;
      case "sim/não":
        properties[name] = { ...extra, type: nullable ? ["boolean", "null"] : "boolean" };
        break;
      case "opções":
        properties[name] = {
          ...extra,
          type: nullable ? ["string", "null"] : "string",
          enum: f.options.split(",").map((o) => o.trim()).filter(Boolean),
        };
        break;
    }
  }
  return { type: "object", required, properties, ...rootExtra };
}

/* ------------------------------------------------- conversão: actions JSON */

export function actionsToDefs(actions: unknown): { defs: ActionDef[]; extra: unknown[] } {
  const defs: ActionDef[] = [];
  const extra: unknown[] = [];
  if (!Array.isArray(actions)) return { defs, extra };
  for (const raw of actions) {
    if (typeof raw !== "object" || raw === null) continue;
    const a = raw as Record<string, unknown>;
    const cfg = (a.config as Record<string, unknown> | undefined) ?? {};
    const str = (v: unknown): string => (typeof v === "string" ? v : "");
    const knownEnabled = a.enabled === true;
    const prepare = (kind: ActionDef["kind"]) => ({
      ...newAction(kind), enabled: knownEnabled,
      requireConfirmation: a.require_confirmation === true, config: cfg,
    });
    if (a.type === "telegram") {
      const d = prepare("telegram");
      const chat = str(cfg.chat_id);
      d.chatId = chat;
      d.message = str(cfg.message ?? cfg.template ?? "{summary}");
      defs.push(d);
    } else if (a.type === "webhook") {
      const d = prepare("webhook");
      d.url = str(cfg.url);
      d.method = str(cfg.method || "POST");
      const headers = (cfg.headers as Record<string, unknown> | undefined) ?? {};
      const auth = str(headers.Authorization ?? headers.authorization);
      const m = auth.match(/^Bearer \$\{(.+)\}$/);
      if (m) d.authSecret = m[1];
      const { url: _url, method: _method, ...advanced } = cfg;
      d.configJson = JSON.stringify(advanced, null, 2);
      defs.push(d);
    } else if (a.type === "email") {
      const d = prepare("email");
      d.recipient = str(cfg.to);
      d.subject = str(cfg.subject ?? "{title}");
      d.body = str(cfg.body ?? "{summary}");
      defs.push(d);
    } else if (a.type === "create_task") {
      const d = prepare("create_task");
      d.taskTitle = str(cfg.title ?? "{title}");
      d.taskSummary = str(cfg.summary ?? "{summary}");
      d.dueAt = str(cfg.due_at);
      defs.push(d);
    } else if (a.type === "mcp_tool") {
      const d = prepare("mcp");
      d.configJson = JSON.stringify(cfg, null, 2);
      defs.push(d);
    } else {
      // Exibidas no editor; salvar exige remoção explícita, nunca roundtrip silencioso.
      extra.push(raw);
    }
  }
  return { defs, extra };
}

export function defsToActions(defs: ActionDef[], extra: unknown[]): unknown[] {
  if (extra.length > 0) throw new Error("Remova as ações não suportadas antes de salvar.");
  const out: unknown[] = [];
  const parseConfig = (text: string): Record<string, unknown> => {
    const value: unknown = JSON.parse(text);
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new Error("A configuração da ação deve ser um objeto JSON.");
    }
    return value as Record<string, unknown>;
  };
  for (const d of defs) {
    let action: Record<string, unknown>;
    if (d.kind === "telegram") {
      action = {
        type: "telegram",
        config: {
          ...d.config,
          ...(d.chatId.trim() ? { chat_id: d.chatId.trim() } : { chat_id: null }),
          template: d.message, message: null,
        },
      };
    } else if (d.kind === "webhook") {
      const config: Record<string, unknown> = { ...parseConfig(d.configJson), url: d.url.trim(), method: d.method };
      if (d.authSecret.trim()) {
        const headers = config.headers;
        config.headers = {
          ...(typeof headers === "object" && headers !== null ? headers : {}),
          Authorization: `Bearer \${${d.authSecret.trim()}}`,
        };
      }
      action = { type: "webhook", config };
    } else if (d.kind === "email") {
      action = { type: "email", config: {
        subject: d.subject, body: d.body,
        ...(d.recipient.trim() ? { to: d.recipient.trim() } : {}),
      } };
    } else if (d.kind === "create_task") {
      action = { type: "create_task", config: {
        title: d.taskTitle, summary: d.taskSummary,
        ...(d.dueAt.trim() ? { due_at: d.dueAt.trim() } : {}),
      } };
    } else {
      action = { type: "mcp_tool", config: parseConfig(d.configJson) };
    }
    action.enabled = d.enabled;
    action.require_confirmation = d.requireConfirmation;
    out.push(action);
  }
  return out;
}
