import { useState } from "react";

import { ApiError, downloadText } from "../api";
import { useAuth } from "../authContext";
import { dateTime, int, usd } from "../format";
import { useHandoff, useHandoffs, useSessionActions } from "../queries";

/** The session's handoff documents: generate one, read the latest, download it. Offered
 * prominently once the session has an alert, and available any time. */
export function HandoffCard({ keyId, sessionId, hasAlerts }: { keyId: string; sessionId: string; hasAlerts: boolean }) {
  const { adminKey } = useAuth();
  const { generateHandoff } = useSessionActions(keyId, sessionId);
  const handoffs = useHandoffs(keyId, sessionId);
  const latestReady = handoffs.data?.find((h) => h.status === "ready");
  const latest = useHandoff(latestReady?.id);
  const [expanded, setExpanded] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const error = generateHandoff.error;
  const errorText =
    error instanceof ApiError
      ? error.status === 409
        ? "No stored content for this session: handoffs need STORE_CONTENT=true while the session runs."
        : error.message
      : error
        ? String(error)
        : null;

  const download = async () => {
    if (!latestReady || !adminKey) return;
    setDownloadError(null);
    try {
      const stamp = latestReady.created_at.slice(0, 16).replace(/[-:T]/g, "");
      await downloadText(adminKey, `/admin/handoffs/${latestReady.id}/markdown`, `handoff-${sessionId.slice(0, 40)}-${stamp}.md`);
    } catch (e) {
      setDownloadError(String(e));
    }
  };

  return (
    <section className={`card ${hasAlerts && !latestReady ? "offer" : ""}`} id="handoff">
      <div className="card-head">
        <div>
          <h2>Handoff</h2>
          <div className="sub">
            {hasAlerts && !latestReady
              ? "⚠ This session reached an alert threshold. A handoff lets a fresh session continue the work."
              : "A document a fresh session can continue from: goal, decisions, current state, open tasks."}
          </div>
        </div>
        <button className="primary" onClick={() => generateHandoff.mutate()} disabled={generateHandoff.isPending}>
          {generateHandoff.isPending ? "Writing… (up to a minute)" : latestReady ? "Generate new version" : "Generate handoff"}
        </button>
      </div>
      {errorText && (
        <div className="error-box" role="alert" style={{ textAlign: "left", padding: "0 0 8px" }}>
          {errorText}
        </div>
      )}
      {latestReady && (
        <>
          <div className="form-row" style={{ justifyContent: "space-between" }}>
            <span className="sub">
              Latest: {dateTime(latestReady.created_at)} · covers {int(latestReady.session_calls)} calls ·{" "}
              <span className="mono">{latestReady.model}</span> · {usd(latestReady.cost_usd)}
              {latestReady.omitted_blocks > 0 && ` · ${int(latestReady.omitted_blocks)} early blocks left out`}
            </span>
            <span className="form-row">
              <button className="link-button" onClick={() => setExpanded((v) => !v)}>
                {expanded ? "Hide" : "Read it"}
              </button>
              <button className="link-button" onClick={download}>
                Download .md
              </button>
            </span>
          </div>
          {downloadError && <div className="error-box">{downloadError}</div>}
          {expanded && latest.data?.markdown && <pre className="handoff-md">{latest.data.markdown}</pre>}
        </>
      )}
      {handoffs.data && handoffs.data.length > 1 && (
        <details style={{ marginTop: 8 }}>
          <summary className="sub">All versions ({handoffs.data.length})</summary>
          <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 13 }}>
            {handoffs.data.map((h) => (
              <li key={h.id} className={h.status === "error" ? "status-critical" : undefined}>
                {dateTime(h.created_at)} · {h.status === "ready" ? `${int(h.session_calls)} calls · ${usd(h.cost_usd)}` : `failed: ${h.error}`} · by {h.requested_by}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
