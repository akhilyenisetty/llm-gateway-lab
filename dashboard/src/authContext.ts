import { createContext, useContext } from "react";

export interface Auth {
  adminKey: string | null;
  signIn: (key: string) => void;
  signOut: () => void;
}

export const AuthContext = createContext<Auth | null>(null);

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth must be used inside <AuthProvider>");
  return auth;
}
