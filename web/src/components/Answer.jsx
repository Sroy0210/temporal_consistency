import { useState } from "react";
import { day, isYesNo, stripPrefix } from "../api.js";

const STATUS_WORDS = {
  answered: "Answered from the evidence",
  not_in_evidence: "The evidence does not say",
  premise_false: "The record contradicts the question",
};

function sourceLabel(ev) {
  if (!ev) return "an unknown item";
  if (ev.kind === "message") return "the patch message";
  if (ev.kind === "facts") return "the recorded facts";
  return `an earlier patch, applied ${day(ev.applied)}`;
}

export default function Answer({ r, anon, onEvidence }) {
  const [details, setDetails] = useState(false);
  const claims = r.claims.map((c, i) => (anon ? { ...c, ...r.anon.claims[i] } : c));
  const summary = anon ? r.anon.summary : r.summary;

  function open(i) {
    const c = claims[i];
    const ev = r.evidence[c.source];
    if (!ev) return;
    const hiddenQuote = r.anon.claims[i]?.quote;
    const label = ev.kind === "earlier" ? stripPrefix(ev.label) : ev.label;
    const note = ev.future ? "Applied after the decision: the decider could not have seen this." : null;
    // Quotes were checked against the text the model saw (names hidden), so that text always matches.
    const sources = anon
      ? [{ text: ev.text_anon, quote: hiddenQuote }]
      : [{ text: ev.text, quote: c.quote },
         { text: ev.text_anon, quote: hiddenQuote, note: "Shown as the model saw it, with names hidden: the quote uses hidden names." }];
    onEvidence({ label, commit: ev.commit, sources, note });
  }

  return (
    <div className="answer">
      <p className={`status ${r.status}`}>
        {STATUS_WORDS[r.status] || r.status}
        {isYesNo(r.question) && ["yes", "no"].includes(r.verdict) && <>: <strong>{r.verdict}</strong></>}
      </p>
      {summary && <p className="summary">{summary}</p>}

      {claims.length > 0 && (
        <ol className="claims">
          {claims.map((c, i) => (
            <li key={i} className={c.future ? "claim future" : "claim"}>
              <span className="claim-text">{c.claim}</span>
              <button className="quote" onClick={() => open(i)} title="Show this quote in its source">{c.quote}</button>
              <span className="claim-source">
                from {sourceLabel(r.evidence[c.source])}
                {c.future && <span className="tag leak">not public until after the decision</span>}
              </span>
            </li>
          ))}
        </ol>
      )}
      {r.unknowns?.length > 0 && (
        <div className="unknowns">
          <p>Not in the evidence</p>
          <ul>{r.unknowns.map((u, i) => <li key={i}>{u}</li>)}</ul>
        </div>
      )}

      <p className="checks">
        {r.evidence_available} earlier patches allowed; read {Object.keys(r.evidence).length} items
        {r.future_shown > 0 && <span className="warn">, {r.future_shown} from after the decision</span>}.{" "}
        {claims.length} {claims.length === 1 ? "claim" : "claims"} kept
        {r.rejected.length ? `, ${r.rejected.length} removed by the quote check` : ""}.{" "}
        <button className="link" onClick={() => setDetails(!details)}>{details ? "Hide details" : "Details"}</button>
      </p>
      {details && (
        <div className="details">
          <p>{r.prepared ? "Prepared answer" : `Answered live in ${r.seconds}s`} by {r.model}, {r.loop} lookups.</p>
          {r.rejected.length > 0 && (
            <>
              <p>Removed claims:</p>
              <ul className="rejected">{r.rejected.map((x, i) => <li key={i} className="mono">{x}</li>)}</ul>
            </>
          )}
          <p>Evidence read:</p>
          <ul className="read">
            {Object.entries(r.evidence).map(([id, ev]) => (
              <li key={id} className={ev.future ? "future" : ""}>
                <span className="mono">{id}</span> {ev.kind === "earlier" ? stripPrefix(ev.label) : sourceLabel(ev)}
                {ev.future && <span className="tag leak">after the decision</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
