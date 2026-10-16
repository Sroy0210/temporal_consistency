import { useEffect, useState } from "react";
import { getJSON } from "../api.js";

const SETUP_WORDS = {
  "B1 no time filter": ["No time filter", "Every patch in the history, past and future"],
  "B2a cutoff on author date": ["Cutoff on author date", "Patches written before the decision"],
  "B2b cutoff on applied date": ["Cutoff on applied date", "Patches public before the decision"],
  "B3 information set": ["Information set", "Patches the decider took part in, as seen by then"],
};
const pct = ([a, b]) => (b ? `${Math.round((100 * a) / b)}%` : "–");
const frac = ([a, b]) => (b ? `${a} of ${b}` : "");

export default function Results() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    getJSON("/api/results").then(setData).catch((e) => setError(e.message));
  }, []);
  if (error) return <p className="error pad">{error}</p>;
  if (!data) return <p className="muted pad">Computing results…</p>;
  const rt = data.retrieval;

  return (
    <main className="results">
      <section>
        <h2>How often each way of choosing evidence lets the future in</h2>
        <p className="lede">
          For each of the {rt.decisions} decisions, a keyword search picks the top {rt.top_k} earlier patches under
          each setup. No model is involved: a model can only leak what it is shown.
        </p>
        <div className="exposure">
          {rt.rows.map((r) => {
            const [name, what] = SETUP_WORDS[r.setup] || [r.setup, ""];
            return (
              <div key={r.setup} className={r.future_pct ? "exp-row leaky" : "exp-row sealed"}>
                <div className="exp-name"><strong>{name}</strong><span>{what}</span></div>
                <div className="exp-bar" role="img" aria-label={`${r.future_pct}% of decisions shown a patch from the future`}>
                  <span style={{ width: `${Math.max(r.future_pct, 0.6)}%` }} />
                </div>
                <div className="exp-num">{r.future_pct}%</div>
                <div className="exp-more">later fix shown {r.later_fix_in_context} of {rt.later_fixed}; nothing found {r.empty}</div>
              </div>
            );
          })}
        </div>
        <p className="fine">
          The author-date cutoff looks safe but is not: kernel patches are often written weeks before they are
          applied, so a patch dated before the decision can still be unknown to the decider.
        </p>
      </section>

      <section>
        <h2>Answers with a model in the loop</h2>
        {data.evaluation.length === 0 ? (
          <p className="muted">No evaluation runs in this database yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Evidence allowed</th><th>Model</th><th>Runs</th><th>Shown future patches</th><th>Cited one</th>
                <th>Quotes verified</th><th>Reviewer questions right</th><th>False premises caught</th>
              </tr>
            </thead>
            <tbody>
              {data.evaluation.map((r) => (
                <tr key={`${r.policy}${r.loop}${r.model}`}>
                  <th scope="row">{r.policy === "B3" ? "Information set" : r.policy === "B1" ? "No time filter" : r.policy}
                    <span className="fine block">{r.loop}</span></th>
                  <td>{r.model}</td>
                  <td>{r.runs}</td>
                  <td>{pct(r.future_shown)} <span className="fine">{frac(r.future_shown)}</span></td>
                  <td>{pct(r.cited_future)} <span className="fine">{frac(r.cited_future)}</span></td>
                  <td>{pct(r.quotes_valid)}</td>
                  <td>{pct(r.presence_right)} <span className="fine">{frac(r.presence_right)}</span></td>
                  <td>{pct(r.fake_premise_caught)} <span className="fine">{frac(r.fake_premise_caught)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="fine">
          Small samples: read the shown-future column first. It measures the boundary itself, which does not depend
          on how the model behaves.
        </p>
      </section>
    </main>
  );
}
