import { useCallback, useEffect, useState } from "react";
import { apiError, deleteSecret, getTelegramBot, listSecrets, testTelegram, upsertSecret } from "../api";
import type { Secret } from "../types";

// O bot é o oficial da plataforma — o usuário NUNCA configura token de bot.
// Aqui só conectamos o chat de destino. Outras chaves ficam no "avançado".
const TELEGRAM_CHAT_KEY = "TELEGRAM_CHAT_ID";
// Legado: alguns usuários antigos têm bot próprio salvo. Não pedimos mais,
// mas também não apagamos sozinho — só avisamos que é ignorado.
const LEGACY_BOT_KEY = "TELEGRAM_BOT_TOKEN";

export default function Secrets() {
  const [items, setItems] = useState<Secret[]>([]);
  const [chatId, setChatId] = useState("");
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [savingChat, setSavingChat] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);
  const [botUsername, setBotUsername] = useState<string | null>(null);
  const [botChecked, setBotChecked] = useState(false);

  const load = useCallback(async () => {
    try {
      const [secrets, bot] = await Promise.all([
        listSecrets(),
        getTelegramBot().catch(() => ({ configured: false as boolean, username: null as string | null })),
      ]);
      setItems(secrets);
      setBotUsername(bot.configured ? bot.username : null);
      setBotChecked(true);
    } catch (e) {
      setError(apiError(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const connected = items.some((s) => s.key === TELEGRAM_CHAT_KEY);
  const legacyBot = items.find((s) => s.key === LEGACY_BOT_KEY);
  const others = items.filter((s) => s.key !== TELEGRAM_CHAT_KEY && s.key !== LEGACY_BOT_KEY);

  const saveChat = async () => {
    const v = chatId.trim();
    if (!v) {
      setError("Cole o ID do seu chat do Telegram (só números, ex: 123456789).");
      return;
    }
    if (!/^-?\d+$/.test(v)) {
      setError("Esse ID parece errado: ele tem só números (ex: 123456789). Veja o passo 2 do guia abaixo.");
      return;
    }
    setSavingChat(true);
    setError(null);
    setOkMsg(null);
    try {
      await upsertSecret(TELEGRAM_CHAT_KEY, v);
      setChatId("");
      await load();
      setOkMsg("Telegram conectado! Agora toque em “Enviar mensagem de teste” para confirmar.");
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSavingChat(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    setError(null);
    setOkMsg(null);
    try {
      await testTelegram();
      setOkMsg("Mensagem de teste enviada ao Telegram.");
    } catch (e) {
      setError(apiError(e));
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    if (!key.trim() || !value) {
      setError("Informe chave e valor.");
      return;
    }
    if (key.trim().toUpperCase() === LEGACY_BOT_KEY) {
      setError("Você não precisa de token de bot — o envio usa o bot oficial da plataforma. Conecte só o chat acima.");
      return;
    }
    setSaving(true);
    setError(null);
    setOkMsg(null);
    try {
      await upsertSecret(key.trim(), value);
      setKey("");
      setValue("");
      await load();
      setOkMsg("Segredo salvo.");
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (k: string) => {
    if (!window.confirm(`Apagar "${k}"?`)) return;
    try {
      await deleteSecret(k);
      await load();
    } catch (e) {
      setError(apiError(e));
    }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Dados de acesso</h1>
          <p className="muted" style={{ margin: "6px 0 0" }}>Onde suas notas chegam e as chaves dos seus sistemas. Valores nunca são exibidos.</p>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {okMsg && <p style={{ color: "#15803d" }}>{okMsg}</p>}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>
          Meu Telegram {connected && <span className="badge processed">Conectado</span>}
        </h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Enviamos tudo pelo <b>bot oficial da plataforma</b> — você não precisa criar bot
          nem token. São 3 passos e leva uns 2 minutos:
        </p>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <b>Passo 1 — Ative o bot oficial</b>
            <p className="muted" style={{ margin: "4px 0" }}>
              {botChecked && botUsername ? (
                <>Toque no botão, abra a conversa e aperte <b>COMEÇAR</b> (ou escreva <code>/start</code> e envie). Sem isso o bot não tem permissão para te mandar nada.</>
              ) : (
                <>Peça ao suporte o link do bot oficial, abra a conversa e aperte <b>COMEÇAR</b> (ou escreva <code>/start</code> e envie). Sem isso o bot não tem permissão para te mandar nada.</>
              )}
            </p>
            {botChecked && botUsername && (
              <a
                className="btn-secondary"
                href={`https://t.me/${botUsername}`}
                target="_blank"
                rel="noreferrer"
                style={{ textDecoration: "none", display: "inline-block" }}
              >
                Abrir o bot no Telegram
              </a>
            )}
          </div>

          <div>
            <b>Passo 2 — Descubra seu ID</b>
            <p className="muted" style={{ margin: "4px 0" }}>Seu ID é um número que identifica seu chat. Para descobrir:</p>
            <ol className="muted" style={{ margin: "0 0 0 18px", padding: 0 }}>
              <li>No Telegram, toque na <b>lupa</b> e procure por <code>@userinfobot</code>.</li>
              <li>Abra a conversa e aperte <b>COMEÇAR</b>.</li>
              <li>Ele responde com seus dados — copie o número ao lado de <b>Id</b> (só números, ex: <code>123456789</code>). Não é o nome, é o número.</li>
            </ol>
          </div>

          <div>
            <b>Passo 3 — Cole aqui e teste</b>
            <p className="muted" style={{ margin: "4px 0" }}>Cole o número no campo abaixo, salve e envie uma mensagem de teste.</p>
            <label>{connected ? "Trocar de chat (ID atual conectado)" : "ID do meu chat"}</label>
            <div className="row">
              <input
                style={{ flex: 1 }}
                value={chatId}
                onChange={(e) => setChatId(e.target.value)}
                placeholder="Ex: 123456789"
                inputMode="numeric"
              />
              <button disabled={savingChat} onClick={() => void saveChat()}>
                {savingChat ? "…" : connected ? "Atualizar" : "Conectar"}
              </button>
            </div>
            {connected && (
              <div style={{ marginTop: 8 }}>
                <button className="btn-secondary" disabled={testing} onClick={() => void sendTest()}>
                  {testing ? "Enviando…" : "Enviar mensagem de teste"}
                </button>
              </div>
            )}
          </div>

          <details>
            <summary className="muted" style={{ cursor: "pointer" }}>Não chegou? Veja os erros mais comuns</summary>
            <ul className="muted" style={{ margin: "6px 0 0 18px", padding: 0 }}>
              <li>Você apertou <b>COMEÇAR</b> no bot oficial (passo 1)? Sem isso nada chega.</li>
              <li>O número colado é o <b>Id</b> (não o nome de usuário)? Ele tem só números.</li>
              <li>Cada pessoa tem seu número — não use o ID de outra pessoa.</li>
              <li>Quer receber num <b>grupo</b>? Fale com o suporte — grupo precisa de configuração extra.</li>
            </ul>
          </details>
        </div>

        {legacyBot && (
          <p className="muted">
            Você tem um <code>TELEGRAM_BOT_TOKEN</code> antigo salvo — ele é ignorado
            (usamos o bot oficial). Pode apagar abaixo sem medo.
          </p>
        )}
      </div>

      <details style={{ marginTop: 12 }}>
        <summary className="muted" style={{ cursor: "pointer" }}>
          Chaves de outros sistemas (webhooks, integrações — avançado)
        </summary>
        <div className="card" style={{ marginTop: 8 }}>
          <div className="row" style={{ marginBottom: 8 }}>
            <button className="btn-secondary" onClick={() => setKey("WEBHOOK_AUTH_TOKEN")}>
              WEBHOOK_AUTH_TOKEN
            </button>
          </div>
          <label>Chave</label>
          <input value={key} onChange={(e) => setKey(e.target.value.toUpperCase())} placeholder="EX: MINHA_CHAVE" />
          <label>Valor</label>
          <input type="password" value={value} onChange={(e) => setValue(e.target.value)} />
          <div style={{ marginTop: 10 }}>
            <button disabled={saving} onClick={() => void save()}>{saving ? "…" : "Salvar segredo"}</button>
          </div>
        </div>
      </details>

      {others.length > 0 && (
        <>
          <h3>Outras chaves</h3>
          {others.map((s) => (
            <div key={s.key} className="card row" style={{ justifyContent: "space-between" }}>
              <div><b style={{ fontFamily: "monospace" }}>{s.key}</b> <span className="muted" style={{ fontFamily: "monospace" }}>{s.value_masked}</span></div>
              <button className="btn-danger" onClick={() => void remove(s.key)}>Apagar</button>
            </div>
          ))}
        </>
      )}
      {legacyBot && (
        <>
          <h3>Ignorado pela plataforma</h3>
          <div className="card row" style={{ justifyContent: "space-between" }}>
            <div>
              <b style={{ fontFamily: "monospace" }}>{legacyBot.key}</b>{" "}
              <span className="muted" style={{ fontFamily: "monospace" }}>{legacyBot.value_masked}</span>
              <p className="muted" style={{ margin: "4px 0 0" }}>Token de bot antigo — não é mais usado.</p>
            </div>
            <button className="btn-danger" onClick={() => void remove(legacyBot.key)}>Apagar</button>
          </div>
        </>
      )}
    </div>
  );
}
