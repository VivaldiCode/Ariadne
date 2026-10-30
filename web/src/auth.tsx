import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { clearKey, getKey, setKey, setUnauthorizedHandler, whoamiWith } from "./api/client";
import type { WhoAmI } from "./api/types";
import { t } from "./i18n";

interface AuthState {
  me: WhoAmI | null;
  isAdmin: boolean;
  /** Valida a chave e, se for de um utilizador, guarda-a. Lança com uma mensagem pronta a mostrar. */
  login: (key: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [me, setMe] = useState<WhoAmI | null>(null);
  const [ready, setReady] = useState(false);

  const logout = useCallback(() => {
    clearKey();
    setMe(null);
    queryClient.clear();
  }, [queryClient]);

  const login = useCallback(
    async (key: string) => {
      const identity = await whoamiWith(key.trim());
      if (identity.kind !== "user") {
        throw new Error(t("login.agentKey"));
      }
      setKey(key.trim());
      queryClient.clear();
      setMe(identity);
    },
    [queryClient],
  );

  // Ao abrir a página: se há uma chave na sessão, confirma que ainda vale.
  useEffect(() => {
    const stored = getKey();
    if (!stored) {
      setReady(true);
      return;
    }
    whoamiWith(stored)
      .then((identity) => {
        if (identity.kind === "user") setMe(identity);
        else clearKey();
      })
      .catch(() => clearKey())
      .finally(() => setReady(true));
  }, []);

  // Uma chave revogada a meio da sessão: volta ao ecrã de entrada.
  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  const value = useMemo<AuthState>(
    () => ({ me, isAdmin: me?.role === "admin", login, logout }),
    [me, login, logout],
  );

  if (!ready) return null;
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth fora do AuthProvider");
  return ctx;
}
