import { dayFromUtc, stripPrefix } from "../api.js";

const DAY = 86400;

// The decision line. Known patches sit to its left, later events to its right.
// Moving "as of" changes what the page reveals; it never changes what the model saw.
export default function Timeline({ d, later, asOf, setAsOf }) {
  const t = d.applied_utc;
  const firstKnown = d.known.length ? d.known[0].seen_utc : t - 30 * DAY;
  const start = Math.min(firstKnown, t - 14 * DAY);
  const end = Math.max(later.length ? later[later.length - 1].applied_utc : t, t + 30 * DAY);
  const pos = (s) => `${((s - start) / (end - start)) * 100}%`;
  const after = asOf > t;
  const happened = later.filter((x) => x.applied_utc <= asOf).length;

  return (
    <section className="timeline" aria-label="Timeline">
      <div className="track">
        <div className="past" style={{ width: pos(t) }} />
        {d.known.map((k) => (
          <span key={k.commit} className={k.seen_utc <= asOf ? "dot known" : "dot known dim"} style={{ left: pos(k.seen_utc) }}
                title={`${dayFromUtc(k.seen_utc)}: ${stripPrefix(k.subject)}`} />
        ))}
        {later.map((x) => (
          <span key={x.commit} className={`dot ${x.type === "fix" ? "fix" : "later"}${x.applied_utc <= asOf ? "" : " dim"}`}
                style={{ left: pos(x.applied_utc) }} title={`${dayFromUtc(x.applied_utc)}: ${stripPrefix(x.subject)}`} />
        ))}
        <span className="decision-line" style={{ left: pos(t) }}>
          <span className="decision-label">Applied {dayFromUtc(t)}</span>
        </span>
        <span className="asof-line" style={{ left: pos(asOf) }} aria-hidden="true" />
      </div>
      <input className="slider" type="range" min={start} max={end} step={DAY / 4} value={asOf}
             onChange={(e) => setAsOf(Number(e.target.value))}
             aria-label="Show the record as of a date" aria-valuetext={dayFromUtc(asOf)} />
      <div className="timeline-foot">
        <span>{dayFromUtc(start)}</span>
        <span className="asof-text">
          {asOf === t
            ? "Showing the record at the moment of the decision."
            : after
              ? `As of ${dayFromUtc(asOf)}: ${happened} of ${later.length} later events have happened.`
              : `As of ${dayFromUtc(asOf)}: before the decision.`}
          {asOf !== t && (
            <button className="link" onClick={() => setAsOf(t)}>Return to the decision</button>
          )}
        </span>
        <span>{dayFromUtc(end)}</span>
      </div>
      <p className="legend">
        <span><i className="dot known" /> earlier patch the decider took part in</span>
        <span><i className="dot later" /> later change to the same files</span>
        <span><i className="dot fix" /> later fix of this patch</span>
      </p>
    </section>
  );
}
