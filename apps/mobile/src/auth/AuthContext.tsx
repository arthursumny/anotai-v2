/** AnotAI mobile — AuthContext: JWT + usuário atual. */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { fetchMe, login as apiLogin, register as apiRegister, setAuthToken } from "../services/api";
import { clearToken, loadToken, saveToken } from "../services/session";
import type { User } from "../types";

interface AuthContextValue {
  user: User | null;
  initializing: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (name: string, email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }): JSX.Element {
  const [user, setUser] = useState<User | null>(null);
  const [initializing, setInitializing] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const token = await loadToken();
        if (token) {
          setAuthToken(token);
          setUser(await fetchMe());
        }
      } catch {
        setAuthToken(null);
        await clearToken();
      } finally {
        setInitializing(false);
      }
    })();
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    const token = await apiLogin(email, password);
    await saveToken(token);
    setAuthToken(token);
    setUser(await fetchMe());
  }, []);

  const signUp = useCallback(async (name: string, email: string, password: string) => {
    await apiRegister(name, email, password);
    const token = await apiLogin(email, password);
    await saveToken(token);
    setAuthToken(token);
    setUser(await fetchMe());
  }, []);

  const signOut = useCallback(async () => {
    await clearToken();
    setAuthToken(null);
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, initializing, signIn, signUp, signOut }),
    [user, initializing, signIn, signUp, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth deve ser usado dentro de <AuthProvider>");
  return ctx;
}
