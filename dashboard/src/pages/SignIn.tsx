import { useState, type FormEvent } from "react";

import { ApiError, request, UnauthorizedError } from "../api";
import { useAuth, type Role } from "../authContext";

/** Sign in with either key: the gateway's ADMIN_API_KEY (everything) or your own
 * gateway key (your own usage). The key is checked against the API before it's kept. */
export function SignInPage() {
  const { signIn } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const roleOf = async (candidate: string): Promise<Role | null> => {
    try {
      await request(candidate, "/admin/keys");
      return "admin";
    } catch (err) {
      // 401: not the admin key; 503: admin API disabled. Either way, try it as a gateway key.
      if (!(err instanceof UnauthorizedError || (err instanceof ApiError && err.status === 503))) throw err;
    }
    try {
      await request(candidate, "/gateway/me");
      return "user";
    } catch (err) {
      if (err instanceof UnauthorizedError) return null;
      throw err;
    }
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setChecking(true);
    setError(null);
    const candidate = key.trim();
    try {
      const role = await roleOf(candidate);
      if (role) signIn(candidate, role);
      else setError("That key was rejected: it's neither the admin key nor an active gateway key.");
    } catch (err) {
      setError(`Couldn't reach the gateway (${String(err)}).`);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="signin card">
      <h1 className="page-title">LLM Gateway</h1>
      <form onSubmit={onSubmit}>
        <label htmlFor="api-key">Admin key or your gateway key</label>
        <input
          id="api-key"
          type="password"
          autoComplete="off"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="ADMIN_API_KEY, or your gw_… key"
        />
        <button className="primary" disabled={!key.trim() || checking}>
          {checking ? "Checking…" : "Sign in"}
        </button>
        {error && (
          <div className="error-box" role="alert" style={{ padding: 0, textAlign: "left" }}>
            {error}
          </div>
        )}
      </form>
      <p style={{ color: "var(--text-muted)", fontSize: 12 }}>
        The admin key shows every user's usage; a gateway key shows only your own. Kept for this
        browser tab only (sessionStorage), and sent only to this gateway.
      </p>
    </div>
  );
}
