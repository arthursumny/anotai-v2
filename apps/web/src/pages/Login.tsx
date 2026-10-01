import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth";
import { apiError } from "../api";
import Brand from "../components/Brand";
import Icon from "../components/Icon";

export default function Login() {
  const { signIn, signUp } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const initial = params.get("modo") === "cadastro" ? "register" : "login";
  const [mode, setMode] = useState<"login" | "register">(initial);
  const [showPw, setShowPw] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (busy) return;
    const values = new FormData(e.currentTarget);
    const name = String(values.get("name") ?? "").trim();
    const email = String(values.get("email") ?? "").trim().toLowerCase();
    const password = String(values.get("password") ?? "");
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || password.length < 8 || (mode === "register" && name.length < 2)) {
      setError("Confira seu e-mail e senha antes de continuar.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") await signIn(email, password);
      else await signUp(name, email, password);
      navigate("/");
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="authwrap">
      {/* Lado marca */}
      <aside className="auth-hero">
        <Link to="/" className="auth-brand-link">
          <Brand />
        </Link>

        <div>
          <h2 className="auth-quote">
            Tire as ideias da cabeça. <span className="grad-text">Encontre espaço para o que vem agora.</span>
          </h2>
          <p className="muted" style={{ fontSize: 15, maxWidth: "46ch" }}>
            Registre o que não quer esquecer, encontre suas notas depois e acompanhe as tarefas que pedem atenção.
          </p>
          <div className="auth-points">
            <div className="auth-point"><span className="auth-point-icon"><Icon name="mic" /></span><span><b>Registre sem interromper</b><br />Guarde uma ideia falando, sem parar para digitar.</span></div>
            <div className="auth-point"><span className="auth-point-icon"><Icon name="search" /></span><span><b>Encontre quando precisar</b><br />Pesquise suas notas pelo assunto ou por um trecho.</span></div>
            <div className="auth-point"><span className="auth-point-icon"><Icon name="board" /></span><span><b>Saiba o que vem a seguir</b><br />Acompanhe tarefas e prazos em um só lugar.</span></div>
          </div>
        </div>

        <div className="muted auth-footnote">Um espaço simples para suas ideias e tarefas.</div>
      </aside>

      {/* Lado form */}
      <div className="auth-form-side">
        <form key={mode} className="authcard" onSubmit={(e) => void submit(e)}>
          <Link to="/" className="auth-brand-link">
            <Brand compact tagline={false} />
          </Link>
          <h1>{mode === "login" ? "Bem-vindo de volta" : "Crie sua conta"}</h1>
          <p className="muted" style={{ margin: "0 0 6px" }}>
            {mode === "login"
              ? "Entre para encontrar e organizar suas notas."
              : "Crie sua conta para começar a organizar suas ideias."}
          </p>

          {params.get("motivo") === "sessao" && (
            <p className="session-expired-message" role="status">
              Sua sessão expirou. Entre novamente para continuar.
            </p>
          )}

          {mode === "register" && (
            <>
              <label htmlFor="name">Nome</label>
              <input
                id="name"
                name="name"
                minLength={2}
                required
                placeholder="Como podemos te chamar?"
                autoComplete="name"
              />
            </>
          )}
          <label htmlFor="email">E-mail</label>
          <input
            id="email"
            name="email"
            type="email"
            required
            placeholder="voce@exemplo.com"
            autoComplete="email"
          />
          <label htmlFor="password">Senha <span className="muted" style={{ fontWeight: 400 }}>(mín. 8 caracteres)</span></label>
          <div className="pw-wrap">
            <input
              id="password"
              name="password"
              type={showPw ? "text" : "password"}
              minLength={8}
              required
              placeholder="••••••••"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              style={{ paddingRight: 70 }}
            />
            <button
              type="button"
              className="pw-toggle"
              onClick={() => setShowPw((s) => !s)}
              aria-label={showPw ? "Ocultar senha" : "Mostrar senha"}
              aria-pressed={showPw}
            >
              {showPw ? "ocultar" : "mostrar"}
            </button>
          </div>

          {error && <p className="error" role="alert">{error}</p>}

          <div style={{ marginTop: 16 }}>
            <button className="auth-submit" disabled={busy} type="submit" aria-busy={busy}>
              {busy ? "Carregando…" : mode === "login" ? "Entrar →" : "Criar conta grátis →"}
            </button>
          </div>
          <button
            type="button"
            className="linkbtn"
            onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(null); }}
          >
            {mode === "login" ? "Não tem conta? Cadastre-se grátis" : "Já tem conta? Entre"}
          </button>
          <p className="muted" style={{ textAlign: "center", fontSize: 12, marginTop: 4 }}>
            <Link to="/" style={{ color: "inherit" }}>← voltar para a página inicial</Link>
          </p>
        </form>
      </div>
    </div>
  );
}
