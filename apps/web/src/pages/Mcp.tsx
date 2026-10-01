import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  apiError,
  createServer,
  deleteServer,
  listMcpProviders,
  listServers,
  startMcpProviderOAuth,
  testServer,
  upsertSecret,
} from "../api";
import type { MCPProvider, MCPServer } from "../types";
import Icon from "../components/Icon";

export default function Mcp() {
  const [providers, setProviders] = useState<MCPProvider[]>([]);
  const [items, setItems] = useState<MCPServer[]>([]);
  const [busyProvider, setBusyProvider] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [tools, setTools] = useState<Record<string, { name: string; description?: string }[]>>({});
  const [searchParams, setSearchParams] = useSearchParams();
  const [customName, setCustomName] = useState("");
  const [customUrl, setCustomUrl] = useState("");
  const [customToken, setCustomToken] = useState("");
  const [customBusy, setCustomBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [catalog, servers] = await Promise.all([listMcpProviders(), listServers()]);
      setProviders(catalog);
      setItems(servers);
    } catch (e) {
      setError(apiError(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const status = searchParams.get("oauth");
    if (!status) return;
    const providerId = searchParams.get("provider");
    const provider = providers.find((item) => item.id === providerId);
    if (status === "success") {
      setMessage(`${provider?.name ?? "Aplicativo"} conectado. Você já pode usar essa conexão em uma automação.`);
      void load();
    } else {
      setError("Não foi possível concluir a conexão. Você pode tentar novamente em instantes.");
    }
    setSearchParams({}, { replace: true });
  }, [load, providers, searchParams, setSearchParams]);

  const connectedByProvider = useMemo(() => new Set(items.map((item) => item.provider_id).filter(Boolean)), [items]);

  const connect = async (provider: MCPProvider) => {
    if (!provider.is_ready || busyProvider) return;
    setBusyProvider(provider.id);
    setError(null);
    setMessage(null);
    try {
      const result = await startMcpProviderOAuth(provider.id);
      window.location.assign(result.authorization_url);
    } catch (e) {
      setError(apiError(e));
      setBusyProvider(null);
    }
  };

  const test = async (server: MCPServer) => {
    setTesting(server.id);
    setError(null);
    setMessage(null);
    try {
      const result = await testServer(server.id);
      setTools((current) => ({ ...current, [server.id]: result.tools }));
      setMessage(result.tools.length
        ? `Conexão ativa: ${result.tools.length} recurso(s) disponível(is) em ${server.name}.`
        : `${server.name} respondeu, mas não disponibilizou ações.`);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setTesting(null);
    }
  };

  const remove = async (server: MCPServer) => {
    if (!window.confirm(`Desconectar ${server.name}? As automações que usam esse app deixarão de executar.`)) return;
    setRemoving(server.id);
    setError(null);
    try {
      await deleteServer(server.id);
      setTools((current) => {
        const next = { ...current };
        delete next[server.id];
        return next;
      });
      await load();
    } catch (e) {
      setError(apiError(e));
    } finally {
      setRemoving(null);
    }
  };

  const createCustom = async () => {
    if (!customName.trim() || !customUrl.trim()) {
      setError("Informe um nome e o endereço do seu aplicativo.");
      return;
    }
    setCustomBusy(true);
    setError(null);
    try {
      let secretName: string | null = null;
      if (customToken.trim()) {
        secretName = `MCP_${customName.trim().toUpperCase().replace(/[^A-Z0-9]+/g, "_").slice(0, 40) || "TOKEN"}`;
        await upsertSecret(secretName, customToken.trim());
      }
      const server = await createServer({
        name: customName.trim(),
        server_url: customUrl.trim(),
        auth_header_key: secretName ? "Authorization" : null,
        auth_secret_name: secretName,
      });
      setCustomName("");
      setCustomUrl("");
      setCustomToken("");
      await load();
      setMessage(`${server.name} adicionado. Teste a conexão para conferir as ações disponíveis.`);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setCustomBusy(false);
    }
  };

  const grouped = useMemo(() => {
    const order = ["Google", "Notas e organização"];
    return order.map((group) => ({
      name: group,
      items: providers.filter((provider) => group === "Google"
        ? provider.brand === "Google"
        : provider.brand !== "Google"),
    })).filter((group) => group.items.length > 0);
  }, [providers]);

  return (
    <div className="connections-page">
      <div className="page-head">
        <div>
          <h1>Conexões</h1>
          <p className="muted">Escolha onde suas notas podem ajudar você. A autorização acontece no próprio serviço.</p>
        </div>
        <button className="btn-secondary" onClick={() => void load()}><Icon name="refresh" size={16} /> Atualizar</button>
      </div>

      <section className="connections-intro card">
        <span className="connections-intro-mark"><Icon name="lock" size={18} /></span>
        <div>
          <b>Você escolhe o que conectar</b>
          <p className="muted">Entre na sua conta e aprove o acesso solicitado. Desconecte um aplicativo a qualquer momento.</p>
        </div>
      </section>

      {error && <p className="error" role="alert">{error}</p>}
      {message && <p className="connections-message" role="status"><Icon name="check" size={16} /> {message}</p>}

      {grouped.map((group) => (
        <section className="connection-group" key={group.name}>
          <div className="connection-group-head">
            <h2>{group.name}</h2>
            <span>{group.items.length} {group.items.length === 1 ? "app" : "apps"}</span>
          </div>
          <div className="connection-grid">
            {group.items.map((provider) => {
              const connected = connectedByProvider.has(provider.id) || provider.is_connected;
              const connecting = busyProvider === provider.id;
              return (
                <article className="card connection-card" key={provider.id}>
                  <div className="connection-card-top">
                    <span className={`connection-logo connection-logo-${provider.brand.toLowerCase().replace(/[^a-z]/g, "")}`} aria-hidden="true">
                      {provider.brand === "Google" ? "G" : provider.brand === "Notion" ? "N" : "E"}
                    </span>
                    <div className="connection-card-title">
                      <small>{provider.brand}</small>
                      <h3>{provider.name}</h3>
                    </div>
                    {connected && <span className="connection-connected"><Icon name="check" size={13} /> Conectado</span>}
                    {provider.beta && <span className="connection-beta">Acesso gradual</span>}
                  </div>
                  <p>{provider.description}</p>
                  <div className="connection-permission"><Icon name="lock" size={15} /> {provider.scope_text}</div>
                  <div className="connection-card-footer">
                    <button
                      className={connected ? "btn-secondary" : ""}
                      disabled={!provider.is_ready || busyProvider !== null}
                      onClick={() => void connect(provider)}
                    >
                      {connecting ? "Abrindo autorização…" : connected ? "Reconectar" : "Conectar"}
                      {!connecting && <Icon name="arrowRight" size={16} />}
                    </button>
                  </div>
                  {!provider.is_ready && (
                    <p className="connection-setup-hint">Esta conexão ainda não está disponível nesta plataforma. Avise o responsável ou tente novamente mais tarde.</p>
                  )}
                </article>
              );
            })}
          </div>
        </section>
      ))}

      <section className="connection-group">
        <div className="connection-group-head">
          <h2>Aplicativos conectados</h2>
          <span>{items.length}</span>
        </div>
        {items.length === 0 ? (
          <div className="empty-filter connection-empty"><b>Nenhum aplicativo conectado</b><p className="muted">Quando você conectar um app, ele aparecerá aqui.</p></div>
        ) : (
          <div className="connected-list">
            {items.map((server) => (
              <article className="card connected-row" key={server.id}>
                <div className="connected-row-copy">
                  <span className="connected-status-dot" />
                  <div><b>{server.name}</b><small>{server.provider_id ? "Acesso autorizado pela sua conta" : server.server_url}</small></div>
                </div>
                {tools[server.id] && <span className="muted">{tools[server.id].length} ações disponíveis</span>}
                <div className="connected-row-actions">
                  <button className="btn-secondary" disabled={testing !== null} onClick={() => void test(server)}>
                    {testing === server.id ? "Testando…" : "Testar"}
                  </button>
                  <button className="btn-danger" disabled={removing !== null} onClick={() => void remove(server)}>
                    {removing === server.id ? "Desconectando…" : "Desconectar"}
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <details className="custom-connection">
        <summary>Adicionar outro aplicativo</summary>
        <p className="muted">Para integrações próprias ou serviços fora da lista. Essa opção exige um endereço MCP e, em alguns casos, uma chave de acesso.</p>
        <div className="custom-connection-fields">
          <label>Nome<input value={customName} onChange={(event) => setCustomName(event.target.value)} placeholder="Ex.: Meu sistema" /></label>
          <label>Endereço MCP<input value={customUrl} onChange={(event) => setCustomUrl(event.target.value)} placeholder="https://.../mcp" inputMode="url" /></label>
          <label>Chave de acesso <span>(opcional)</span><input type="password" value={customToken} onChange={(event) => setCustomToken(event.target.value)} placeholder="Cole a chave fornecida pelo serviço" autoComplete="new-password" /></label>
          <button disabled={customBusy} onClick={() => void createCustom()}>{customBusy ? "Adicionando…" : "Adicionar conexão"}</button>
        </div>
      </details>
    </div>
  );
}
