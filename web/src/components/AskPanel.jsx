import { useEffect, useRef, useState } from "react";
import { day, postJSON } from "../api.js";
import Answer from "./Answer.jsx";

// Same question, two answers: bounded to what the decider knew, and with no time boundary.
export default function AskPanel({ d, status, anon, onEvidence }) {
  const prepared = d.prepared_questions || [];
  const [question, setQuestion] = useState(prepared[0] || "Why was this patch applied?");
  const [draft, setDraft] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const timer = useRef(null);

  async function run(q) {
    const isPrepared = prepared.includes(q);
    setQuestion(q);
    setBusy(true);
    setError(null);
    setResult(null);
    setElapsed(0);
    const t0 = Date.now();
    timer.current = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000);
    try {
      setResult({ ...(await postJSON("/api/compare", { commit: d.commit, question: q })), live: !isPrepared });
    } catch (e) {
      setError(e.message);
    } finally {
      clearInterval(timer.current);
      setBusy(false);
    }
  }

  // Prepared answers cost nothing, so the first one opens with the page.
  useEffect(() => {
    if (prepared.length) run(prepared[0]);
    return () => clearInterval(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d.commit]);

  const canType = Boolean(status?.live);
  const open = result?.B1;
  const bounded = result?.B3;

  return (
    <section className="ask" aria-label="Ask about this decision">
      <h3>Same question, two answers</h3>
      <p className="ask-intro">
        One answer may use only what the decider could see on {day(d.applied)}. The other may use the whole history,
        the way most tools do.
      </p>

      <div className="questions" role="group" aria-label="Questions">
        {prepared.map((q) => (
          <button key={q} className={q === question ? "qbtn on" : "qbtn"} onClick={() => run(q)} disabled={busy}>{q}</button>
        ))}
      </div>

      <form className="typed" onSubmit={(e) => { e.preventDefault(); if (draft.trim()) run(draft.trim()); }}>
        <input value={draft} onChange={(e) => setDraft(e.target.value)} disabled={!canType || busy}
               placeholder={canType ? "Ask your own question about this decision" : "Typed questions are off on this server; choose a question above"}
               aria-label="Your question" />
        <button className="primary" disabled={!canType || busy || !draft.trim()}>Ask</button>
      </form>

      {busy && (
        <p className="working" role="status">
          {prepared.includes(question) ? "Opening the prepared answers…" : `Answering twice, once bounded to ${day(d.applied)}… ${elapsed}s`}
          {!prepared.includes(question) && elapsed > 20 && " A local model can take a minute or two."}
        </p>
      )}
      {error && <p className="error">{error}</p>}
      {!prepared.length && !canType && (
        <p className="muted">No prepared answers for this decision. Choose one marked "prepared" in the decision list.</p>
      )}

      {result && (
        <>
          <p className={open.future_shown ? "verdict-line leak" : "verdict-line"}>
            {open.future_shown
              ? <>Without a time boundary, the model was handed <strong>{open.future_shown} {open.future_shown === 1 ? "patch" : "patches"} from after the decision</strong>{open.future_cites ? <> and cited <strong>{open.future_cites}</strong> as if the decider had known.</> : ", though it did not cite them this time."} With the boundary, it was handed none.</>
              : "Here the unbounded search happened to find nothing from after the decision, so the two answers rest on the same kind of evidence."}
          </p>
          <div className="pair">
            <div className="side bounded">
              <h4>Bounded to what the decider knew</h4>
              <Answer r={bounded} anon={anon} onEvidence={onEvidence} />
            </div>
            <div className="side open">
              <h4>The whole history, no boundary</h4>
              <Answer r={open} anon={anon} onEvidence={onEvidence} />
            </div>
          </div>
        </>
      )}
    </section>
  );
}
