import { Link } from "react-router-dom";

import { ChartCard, Filters, QueryView, RankBars } from "../components";
import { int, pct, usd } from "../format";
import { useCategories } from "../queries";

export function CategoriesPage() {
  const categories = useCategories();
  return (
    <>
      <h1 className="page-title">Spend by category</h1>
      <Filters />
      <QueryView query={categories} empty={(rows) => rows.length === 0}>
        {(rows) => {
          const total = rows.reduce((sum, r) => sum + Number(r.spend_usd), 0);
          return (
            <ChartCard
              title="Spend by category"
              sub="a session's client X-Category tag, otherwise the classifier's category"
              chart={
                <RankBars
                  data={rows.map((r) => ({ name: r.category, value: Number(r.spend_usd) }))}
                  format={usd}
                  label="spend"
                />
              }
              table={
                <table>
                  <thead>
                    <tr>
                      <th>Category</th>
                      <th className="num">Spend</th>
                      <th className="num">Share</th>
                      <th className="num">Sessions</th>
                      <th className="num">Calls</th>
                      <th className="num">Errors</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.category}>
                        <td>
                          <Link to={`/sessions?category=${encodeURIComponent(r.category)}`}>{r.category}</Link>
                        </td>
                        <td className="num">{usd(r.spend_usd)}</td>
                        <td className="num">{total ? pct(Number(r.spend_usd) / total) : "—"}</td>
                        <td className="num">{int(r.sessions)}</td>
                        <td className="num">{int(r.calls)}</td>
                        <td className="num">{int(r.errors)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              }
            />
          );
        }}
      </QueryView>
      <p className="sub" style={{ color: "var(--text-muted)", fontSize: 12 }}>
        "uncategorized" sessions have no client tag and no automatic category yet (they are
        categorized after 10 idle minutes, and only with STORE_CONTENT on).
      </p>
    </>
  );
}
