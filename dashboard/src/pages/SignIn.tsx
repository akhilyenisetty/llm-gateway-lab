import { useState, type FormEvent } from "react";

import { request, UnauthorizedError, ApiError } from "../api";
import { useAuth } from "../authContext";

/** Asks for the gateway's ADMIN_API_KEY and checks it against the API before keeping it. */
export function SignInPage() {
  const { signIn } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setChecking(true);
    setError(null);
    try {
      await request(key.trim(), "/admin/keys");
      signIn(key.trim());
    } catch (err) {
      setError(
        err instanceof UnauthorizedError
          ? "That admin key was rejected."
          : err instanceof ApiError && err.status === 503
            ? "The admin API is disabled: set ADMIN_API_KEY on the gateway."
            : `Couldn't reach the gateway (${String(err)}).`,
      );
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="signin card">
      <h1 className="page-title">LLM Gateway</h1>
      <form onSubmit={onSubmit}>
        <label htmlFor="admin-key">Admin key</label>
        <input
          id="admin-key"
          type="password"
          autoComplete="off"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="ADMIN_API_KEY from the gateway's .env"
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
        Kept for this browser tab only (sessionStorage), and sent only to this gateway.
      </p>
    </div>
  );
}
