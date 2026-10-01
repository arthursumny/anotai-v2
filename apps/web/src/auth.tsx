import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { fetchMe, getAuthToken, login as apiLogin, register as apiRegister, setAuthToken } from "./api";
import type { User } from "./types";

interface AuthValue {
  user: User | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (name: string, email: string, password: string) => Promise<void>;
  signOut: () => void;
}

const Ctx = createContext<AuthValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    const handleUnauthorized = () => {
      setAuthToken(null);
      setUser(null);
      navigate("/login?motivo=sessao", { replace: true });
    };
    window.addEventListener("anotai:unauthorized", handleUnauthorized);
    return () => window.removeEventListener("anotai:unauthorized", handleUnauthorized);
  }, [navigate]);

  useEffect(() => {
    (async () => {
      try {
        if (getAuthToken()) setUser(await fetchMe());
      } catch {
        setAuthToken(null);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    setAuthToken(await apiLogin(email, password));
    setUser(await fetchMe());
  }, []);

  const signUp = useCallback(async (name: string, email: string, password: string) => {
    await apiRegister(name, email, password);
    setAuthToken(await apiLogin(email, password));
    setUser(await fetchMe());
  }, []);

  const signOut = useCallback(() => {
    setAuthToken(null);
    setUser(null);
  }, []);

  const value = useMemo(() => ({ user, loading, signIn, signUp, signOut }), [user, loading, signIn, signUp, signOut]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth fora do AuthProvider");
  return v;
}
