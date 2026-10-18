import { useCallback, useMemo, useState, type ReactNode } from "react";

import { AuthContext, type Role } from "./authContext";

// The key and its role live in React state, mirrored to sessionStorage so a page refresh
// doesn't sign you out. sessionStorage is per tab and cleared when the tab closes; it can
// be unavailable (private mode, blocked storage), so every access is guarded.
const KEY_STORAGE = "gateway-dashboard-key";
const ROLE_STORAGE = "gateway-dashboard-role";

function readStored(name: string): string | null {
  try {
    return sessionStorage.getItem(name);
  } catch {
    return null;
  }
}

function writeStored(name: string, value: string | null): void {
  try {
    if (value) sessionStorage.setItem(name, value);
    else sessionStorage.removeItem(name);
  } catch {
    // storage unavailable: the key still works for this page view
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [apiKey, setApiKey] = useState<string | null>(() => readStored(KEY_STORAGE));
  const [role, setRole] = useState<Role | null>(() => {
    const stored = readStored(ROLE_STORAGE);
    return stored === "admin" || stored === "user" ? stored : null;
  });
  const signIn = useCallback((key: string, newRole: Role) => {
    writeStored(KEY_STORAGE, key);
    writeStored(ROLE_STORAGE, newRole);
    setApiKey(key);
    setRole(newRole);
  }, []);
  const signOut = useCallback(() => {
    writeStored(KEY_STORAGE, null);
    writeStored(ROLE_STORAGE, null);
    setApiKey(null);
    setRole(null);
  }, []);
  const value = useMemo(() => ({ apiKey, role, signIn, signOut }), [apiKey, role, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
