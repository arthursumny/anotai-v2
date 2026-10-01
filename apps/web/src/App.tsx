import { useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import Landing from "./pages/Landing";
import Login from "./pages/Login";
import Record from "./pages/Record";
import Notes from "./pages/Notes";
import NoteDetail from "./pages/NoteDetail";
import Marketplace from "./pages/Marketplace";
import Templates from "./pages/Templates";
import Workflows from "./pages/Workflows";
import Inbox from "./pages/Inbox";
import Kanban from "./pages/Kanban";
import Secrets from "./pages/Secrets";
import Integrations from "./pages/Integrations";
import Privacy from "./pages/Privacy";
import Brand, { BrandMark } from "./components/Brand";
import Icon, { type IconName } from "./components/Icon";

const NAV_MAIN = [
  { to: "/", label: "Gravar", ico: "mic" as const, end: true },
  { to: "/notas", label: "Notas", ico: "note" as const },
  { to: "/inbox", label: "Revisar", ico: "inbox" as const },
  { to: "/kanban", label: "Tarefas", ico: "board" as const },
];

const NAV_BUILD = [
  { to: "/marketplace", label: "Modelos prontos", ico: "grid" as const },
  { to: "/templates", label: "Automações", ico: "settings" as const },
  { to: "/fluxos", label: "Fluxos", ico: "refresh" as const },
  { to: "/integracoes", label: "Integrações", ico: "plug" as const },
  { to: "/privacidade", label: "Privacidade", ico: "lock" as const },
  { to: "/segredos", label: "Dados de acesso", ico: "settings" as const },
];

function AppShell() {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  const logout = () => {
    signOut();
    setOpen(false);
    navigate("/login");
  };

  const initial = (user?.name ?? user?.email ?? "?").trim().charAt(0).toUpperCase() || "?";

  useEffect(() => {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    let active = true;
    let checking = false;
    const checkReminders = async () => {
      if (checking) return;
      checking = true;
      try {
        const { listNotes } = await import("./api");
        const notes = await listNotes();
        if (!active) return;
        const now = Date.now();
        for (const note of notes) {
          if (!note.reminder_at || note.reminder_sent || note.kanban_status === "done" || new Date(note.reminder_at).getTime() > now) continue;
          const key = `anotai_reminder_seen:${note.id}:${note.reminder_at}`;
          if (localStorage.getItem(key)) continue;
          localStorage.setItem(key, "1");
          new Notification("Lembrete do Anotaí", {
            body: note.title || "Você tem um compromisso ou tarefa chegando.",
            tag: note.id,
          });
        }
      } catch {
        // Notificação do dispositivo é complementar; o restante da tela continua funcionando.
      } finally {
        checking = false;
      }
    };
    void checkReminders();
    const timer = window.setInterval(() => void checkReminders(), 60_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  return (
    <div className="layout">
      <div className="topbar-mobile">
        <button
          className="btn-secondary hamburger"
          onClick={() => setOpen((o) => !o)}
          aria-label={open ? "Fechar menu" : "Abrir menu"}
          aria-expanded={open}
          aria-controls="primary-navigation"
        >
          <Icon name={open ? "close" : "menu"} />
        </button>
        <Link to="/" className="mobile-brand-link" onClick={() => setOpen(false)}>
          <Brand compact tagline={false} />
        </Link>
        <div style={{ flex: 1 }} />
        <Link to="/" className="btn mobile-record-link" onClick={() => setOpen(false)}>
          <Icon name="plus" size={16} /> Gravar
        </Link>
      </div>

      {open && <div className="scrim" onClick={() => setOpen(false)} />}

      <nav id="primary-navigation" className={`sidebar ${open ? "open" : ""}`}>
        <Link to="/" className="sidebar-brand" onClick={() => setOpen(false)}>
          <Brand />
        </Link>

        <div className="nav-section">Organize o dia</div>
        {NAV_MAIN.map((n) => (
          <NavLink key={n.to} className="navlink" to={n.to} end={n.end} onClick={() => setOpen(false)}>
          <span className="ico"><Icon name={n.ico as IconName} size={17} /></span> {n.label}
          </NavLink>
        ))}

        <div className="nav-section">Ajuste seu espaço</div>
        {NAV_BUILD.map((n) => (
          <NavLink key={n.to} className="navlink" to={n.to} onClick={() => setOpen(false)}>
          <span className="ico"><Icon name={n.ico as IconName} size={17} /></span> {n.label}
          </NavLink>
        ))}

        <div className="spacer" />
        <div className="userbox">
          <span className="avatar">{initial}</span>
          <span className="who">
            <b>{user?.name}</b>
            <small>{user?.email}</small>
          </span>
        </div>
        <button className="btn-secondary" onClick={logout}>Sair</button>
      </nav>

      <main className="main">
        <Routes>
          <Route path="/" element={<Record />} />
          <Route path="/notas" element={<Notes />} />
          <Route path="/notas/:id" element={<NoteDetail />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/kanban" element={<Kanban />} />
          <Route path="/marketplace" element={<Marketplace />} />
          <Route path="/templates" element={<Templates />} />
          <Route path="/fluxos" element={<Workflows />} />
          <Route path="/integracoes" element={<Integrations />} />
          <Route path="/privacidade" element={<Privacy />} />
          {/* Antiga área de conexões: usuário sempre cai na Integrações nova. */}
          <Route path="/mcp" element={<Navigate to="/integracoes" replace />} />
          <Route path="/segredos" element={<Secrets />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Root() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="center" style={{ flexDirection: "column", gap: 14 }}>
        <BrandMark className="brand-mark--large" />
        <span>Afinando o microfone…</span>
      </div>
    );
  }

  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/" replace /> : <Login />} />
      <Route path="/" element={user ? <AppShell /> : <Landing />} />
      <Route path="/*" element={user ? <AppShell /> : <Navigate to="/login" replace />} />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Root />
    </AuthProvider>
  );
}
