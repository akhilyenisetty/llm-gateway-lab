import { useCallback, useMemo, useState, type ReactNode } from "react";

import { AuthContext } from "./authContext";

// The admin key lives in React state, mirrored to sessionStorage so a page refresh doesn't
// sign you out. sessionStorage is per tab and cleared when the tab closes; it can be
// unavailable (private mode, blocked storage), so every access is guarded.
const STORAGE_KEY = "gateway-admin-key";

function readStored(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStored(value: string | null): void {
  try {
    if (value) sessionStorage.setItem(STORAGE_KEY, value);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage unavailable: the key still works for this page view
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [adminKey, setAdminKey] = useState<string | null>(readStored);
  const signIn = useCallback((key: string) => {
    writeStored(key);
    setAdminKey(key);
  }, []);
  const signOut = useCallback(() => {
    writeStored(null);
    setAdminKey(null);
  }, []);
  const value = useMemo(() => ({ adminKey, signIn, signOut }), [adminKey, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
