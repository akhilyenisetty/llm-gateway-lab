import { createContext, useContext } from "react";

/** "admin": the gateway's ADMIN_API_KEY, which sees everything. "user": a gateway key,
 * which sees only its own sessions (the My usage view). */
export type Role = "admin" | "user";

export interface Auth {
  apiKey: string | null;
  role: Role | null;
  signIn: (key: string, role: Role) => void;
  signOut: () => void;
}

export const AuthContext = createContext<Auth | null>(null);

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth must be used inside <AuthProvider>");
  return auth;
}
