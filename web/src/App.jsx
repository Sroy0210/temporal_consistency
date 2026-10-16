import { useEffect, useMemo, useState } from "react";
import { getJSON } from "./api.js";
import Picker from "./components/Picker.jsx";
import DecisionView from "./components/DecisionView.jsx";
import Results from "./components/Results.jsx";
import About from "./components/About.jsx";

const VIEWS = [
  ["explore", "Decisions"],
  ["results", "Results"],
  ["about", "How it works"],
];

export default function App() {
  const [view, setView] = useState("explore");
  const [status, setStatus] = useState(null);
  const [decisions, setDecisions] = useState([]);
  const [selected, setSelected] = useState(null);
  const [anon, setAnon] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.all([getJSON("/api/status"), getJSON("/api/decisions")])
      .then(([s, d]) => {
        setStatus(s);
        setDecisions(d);
        const first = d.find((x) => x.prepared && x.later_fixed) || d.find((x) => x.prepared) || d[0];
        setSelected(first ? first.commit : null);
      })
      .catch((e) => setError(e.message));
  }, []);

  const current = useMemo(() => decisions.find((d) => d.commit === selected), [decisions, selected]);
  const choose = (commit) => {
    setSelected(commit);
    setView("explore");
    window.scrollTo({ top: 0 });
  };

  return (
    <div className="app">
      <header className="topbar">
        <button className="brand" onClick={() => setView("explore")}>Temporal Consistency</button>
        {decisions.length > 0 && <Picker decisions={decisions} current={current} onSelect={choose} />}
        <nav className="views" aria-label="Sections">
          {VIEWS.map(([key, label]) => (
            <button key={key} className={view === key ? "view on" : "view"} onClick={() => setView(key)}
                    aria-current={view === key ? "page" : undefined}>
              {label}
            </button>
          ))}
        </nav>
        <label className="anon-toggle" title="Replace people's names with the placeholders the model saw">
          <input type="checkbox" checked={anon} onChange={(e) => setAnon(e.target.checked)} />
          Names as the model saw them
        </label>
      </header>

      {error && <p className="error page-error">The server did not respond: {error}. Is the backend running on port 8000?</p>}

      {view === "explore" &&
        (current ? (
          <DecisionView key={current.commit} commit={current.commit} status={status} anon={anon} />
        ) : (
          !error && <p className="loading">Loading decisions…</p>
        ))}
      {view === "results" && <Results />}
      {view === "about" && <About status={status} />}

      <footer className="footer">
        <span>
          Linux kernel ext4 history. Every answer is built only from evidence the decider could have seen, and every claim
          quotes it word for word.
        </span>
        {status && (
          <span>
            {status.decisions} decisions, {status.prepared_answers} prepared answers.{" "}
            {status.live ? `Typed questions answered live by ${status.live_model}.` : "Typed questions are off on this server."}
          </span>
        )}
      </footer>
    </div>
  );
}
