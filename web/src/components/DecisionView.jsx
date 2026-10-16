import { useEffect, useMemo, useState } from "react";
import { getJSON } from "../api.js";
import Hero from "./Hero.jsx";
import RecordCard from "./RecordCard.jsx";
import AskPanel from "./AskPanel.jsx";
import Hindsight from "./Hindsight.jsx";
import EvidenceDrawer from "./EvidenceDrawer.jsx";

const ROLE_WORDS = { reviewed_by: "Reviewed by", acked_by: "Acked by", tested_by: "Tested by", reported_by: "Reported by" };

export default function DecisionView({ commit, status, anon }) {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [asOf, setAsOf] = useState(null);
  const [drawer, setDrawer] = useState(null);

  useEffect(() => {
    getJSON(`/api/decision/${commit}`)
      .then((x) => {
        setD(x);
        setAsOf(x.applied_utc);
      })
      .catch((e) => setError(e.message));
  }, [commit]);

  const later = useMemo(() => {
    if (!d) return [];
    return [
      ...d.later_fixes.map((x) => ({ ...x, type: "fix" })),
      ...d.later_same_files.map((x) => ({ ...x, type: "same" })),
    ].sort((a, b) => a.applied_utc - b.applied_utc);
  }, [d]);

  if (error) return <p className="error page-error">{error}</p>;
  if (!d) return <p className="loading">Loading the decision…</p>;

  const name = (p) => (anon ? p.name_anon : p.name);
  const reviewers = Object.entries(ROLE_WORDS)
    .map(([role, words]) => [words, d.people.filter((p) => p.roles.includes(role)).map(name)])
    .filter(([, names]) => names.length);

  // Record quotes are stored with real names; when names are hidden, fall back to the named text to highlight.
  const openMessage = (quote) =>
    setDrawer({
      label: "The patch's own message", commit: d.commit,
      sources: anon
        ? [{ text: d.message_anon, quote }, { text: d.message, quote, note: "Shown with names: this quote was stored with names." }]
        : [{ text: d.message, quote }],
    });

  return (
    <article className="decision">
      <Hero d={d} later={later} asOf={asOf} setAsOf={setAsOf} anon={anon} />

      <div className="split">
        <section className="now" aria-label="At the time">
          <div className="col-head">
            <h2>At the time</h2>
            <p>
              What the patch said and who had looked at it.{" "}
              {reviewers.length > 0
                ? reviewers.map(([w, n]) => `${w} ${n.join(", ")}`).join(". ") + "."
                : "No reviewer is recorded on the patch."}{" "}
              <button className="link" onClick={() => openMessage(null)}>Read the full commit message</button>
            </p>
          </div>
          <RecordCard record={d.record} onQuote={openMessage} />
          <AskPanel d={d} status={status} anon={anon} onEvidence={setDrawer} />
        </section>

        <Hindsight d={d} later={later} asOf={asOf}
                   onReveal={() => setAsOf(later.length ? later[later.length - 1].applied_utc : d.applied_utc)}
                   onOpen={(fix) => setDrawer({ label: fix.subject, commit: fix.commit,
                                                sources: [{ text: fix.message, quote: null }],
                                                note: "Written after the decision. The model never sees this." })} />
      </div>

      {drawer && <EvidenceDrawer {...drawer} onClose={() => setDrawer(null)} />}
    </article>
  );
}
