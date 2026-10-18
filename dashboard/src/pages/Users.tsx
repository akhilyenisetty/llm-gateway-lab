import { Link } from "react-router-dom";

import { Filters, QueryView } from "../components";
import { dateTime, int, usd } from "../format";
import { useUsers } from "../queries";

export function UsersPage() {
  const users = useUsers();
  return (
    <>
      <h1 className="page-title">Users</h1>
      <Filters showUser={false} />
      <section className="card">
        <QueryView query={users} empty={(rows) => rows.length === 0}>
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>User (gateway key)</th>
                    <th className="num">Spend</th>
                    <th className="num">Calls</th>
                    <th className="num">Sessions</th>
                    <th className="num">Errors</th>
                    <th>Last call</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((u) => (
                    <tr key={u.key_id}>
                      <td>
                        <Link to={`/sessions?user=${u.key_id}`}>{u.name}</Link>{" "}
                        <span className="mono" style={{ color: "var(--text-muted)" }}>
                          {u.key_prefix}…
                        </span>{" "}
                        {u.revoked && <span className="tag">revoked</span>}
                      </td>
                      <td className="num">{usd(u.spend_usd)}</td>
                      <td className="num">{int(u.calls)}</td>
                      <td className="num">{int(u.sessions)}</td>
                      <td className="num">{int(u.errors)}</td>
                      <td>{dateTime(u.last_call_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryView>
      </section>
    </>
  );
}
