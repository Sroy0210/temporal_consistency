import { day, dayFromUtc, shortHash, stripPrefix } from "../api.js";

// Everything after the decision. It stays in the fog until the timeline passes it, and the model never sees it.
export default function Hindsight({ d, later, asOf, onReveal, onOpen }) {
  const happened = later.filter((x) => x.applied_utc <= asOf);
  const pending = later.filter((x) => x.applied_utc > asOf);

  return (
    <section className="later-col" aria-label="Later">
      <div className="col-head">
        <h2>Later</h2>
        <p>What happened after {dayFromUtc(d.applied_utc)}. The decider could not know it, and the model is never shown it.</p>
      </div>

      {later.length === 0 && <p className="muted">Nothing later in this history touches this patch or its files.</p>}

      <ol className="events">
        {happened.map((x) => (
          <li key={x.commit} className={x.type === "fix" ? "event fix" : "event"}>
            <p className="event-when">{day(x.applied)}, {x.type === "fix" ? "a fix of this patch" : "a change to the same files"}</p>
            <p className="event-subject">{stripPrefix(x.subject)}</p>
            {x.type === "fix" && (
              <>
                <p className="event-excerpt">{firstParagraph(x.message)}</p>
                <button className="link" onClick={() => onOpen(x)}>Read the fix's explanation</button>
              </>
            )}
            <p className="mono fine">{shortHash(x.commit)}</p>
          </li>
        ))}
        {pending.map((x) => (
          <li key={x.commit} className="event fogged" aria-label="Not happened yet">
            <p className="event-when">{x.type === "fix" ? "A fix of this patch" : "A change to the same files"}</p>
            <p className="event-subject" aria-hidden="true">{stripPrefix(x.subject)}</p>
          </li>
        ))}
      </ol>
      {pending.length > 0 && (
        <button className="secondary" onClick={onReveal}>Clear the fog</button>
      )}
    </section>
  );
}

function firstParagraph(message) {
  const parts = (message || "").split(/\n\s*\n/);
  const body = parts.slice(1).find((p) => p.trim() && !/^[A-Za-z-]+:\s/.test(p.trim()));
  const text = (body || parts[0] || "").replace(/\s+/g, " ").trim();
  return text.length > 320 ? text.slice(0, 317) + "…" : text;
}
