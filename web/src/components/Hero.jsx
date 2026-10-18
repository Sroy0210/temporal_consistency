import { useEffect, useState } from "react";
import { day, dayFromUtc, shortHash, stripPrefix } from "../api.js";

const DAY = 86400;
const reduceMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// Tick height says how the decider was involved in an earlier patch.
function involvement(roles) {
  if (roles.includes("author")) return { h: 92, word: "wrote it" };
  if (roles.some((r) => ["reviewed_by", "acked_by", "tested_by"].includes(r))) return { h: 68, word: "reviewed it" };
  if (roles.includes("committer")) return { h: 46, word: "applied it" };
  return { h: 32, word: "was copied on it" };
}

// The decision, its moment, and the fog over everything that had not happened yet.
export default function Hero({ d, later, asOf, setAsOf, anon }) {
  const t = d.applied_utc;
  const firstKnown = d.known.length ? d.known[0].seen_utc : t - 30 * DAY;
  const start = Math.min(firstKnown, t - 10 * DAY);
  const lastLater = later.length ? later[later.length - 1].applied_utc : t;
  const end = Math.max(lastLater + 7 * DAY, t + 30 * DAY);
  const pct = (s) => ((s - start) / (end - start)) * 100;
  const [hover, setHover] = useState(null);
  const [phase, setPhase] = useState(reduceMotion() ? "done" : "start");

  // One orchestrated moment: the fog rolls in from the future and settles on the decision.
  useEffect(() => {
    if (phase === "start") {
      const id = setTimeout(() => setPhase("rolling"), 80);
      return () => clearTimeout(id);
    }
    if (phase === "rolling") {
      const id = setTimeout(() => setPhase("done"), 1300);
      return () => clearTimeout(id);
    }
  }, [phase]);

  const fogLeft = phase === "start" ? 100 : pct(asOf);

  // Hover is worked out from the pointer position, because the scrub control sits on top of the ticks.
  const marks = [
    ...d.known.map((k) => ({ at: k.seen_utc, when: k.seen, text: stripPrefix(k.subject), how: `the decider ${involvement(k.roles).word}` })),
    ...later.map((x) => ({ at: x.applied_utc, when: x.applied, text: stripPrefix(x.subject),
                           how: x.type === "fix" ? "fixes this patch" : "changes the same files" })),
  ];
  function onMove(e) {
    const box = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - box.left) / box.width) * 100;
    let best = null;
    for (const m of marks) {
      const dist = Math.abs(pct(m.at) - x);
      if (dist < 0.9 && (!best || dist < best.dist)) best = { ...m, dist };
    }
    setHover(best);
  }
  const seenNow = d.known.filter((k) => k.seen_utc <= asOf).length;
  const happened = later.filter((x) => x.applied_utc <= asOf);
  const fixesLeft = d.later_fixes.filter((f) => f.applied_utc > asOf).length;
  const decider = anon ? d.decider_anon : d.decider;
  const authors = d.people.filter((p) => p.roles.includes("author")).map((p) => (anon ? p.name_anon : p.name));

  let readout;
  if (asOf < t) {
    readout = `${dayFromUtc(asOf)}: the decision is ${Math.round((t - asOf) / DAY)} days away. The decider has seen ${seenNow} of the ${d.known.length} patches they will know about.`;
  } else if (asOf === t) {
    readout = d.known.length
      ? `${dayFromUtc(t)}: the decider has taken part in ${d.known.length} earlier patches. ${later.length ? `${later.length} later ${later.length === 1 ? "event is" : "events are"} still in the fog.` : "Nothing later touches this patch."}`
      : `${dayFromUtc(t)}: the decider has no earlier involvement in this history. The patch's own message is all the evidence there is.`;
  } else {
    readout = `${dayFromUtc(asOf)}: ${happened.length} of ${later.length} later events have happened${fixesLeft ? `; a fix of this patch is still ahead` : happened.some((x) => x.type === "fix") ? ", including a fix of this patch" : ""}.`;
  }

  return (
    <section className="hero" aria-label="The decision in time">
      <div className="hero-head">
        <p className="hero-when">
          Applied {day(d.applied)} by {decider}. Written {day(d.authored)} by {authors.join(", ") || "unknown"}.
          <span className="mono hash">{shortHash(d.commit)}</span>
        </p>
        <h1 className="hero-title">{d.subject}</h1>
      </div>

      <div className="strip" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <div className="lit" style={{ width: `${pct(t)}%` }} />
        {d.known.map((k) => {
          const inv = involvement(k.roles);
          return (
            <span key={k.commit} className="tick known" style={{ left: `${pct(k.seen_utc)}%`, height: `${inv.h}%` }} />
          );
        })}
        {later.map((x) => (
          <span key={x.commit} className={x.type === "fix" ? "tick fix" : "tick later"} style={{ left: `${pct(x.applied_utc)}%` }} />
        ))}
        <span className="moment" style={{ left: `${pct(t)}%` }}>
          <span className="moment-label">The decision</span>
        </span>
        <div className={phase === "rolling" ? "fog rolling" : "fog"} style={{ left: `${fogLeft}%` }} aria-hidden="true">
          <span className="fog-edge">
            <span className="fog-date">{dayFromUtc(asOf)}</span>
          </span>
        </div>
        <input className="scrub" type="range" min={start} max={end} step={DAY / 4} value={asOf}
               onChange={(e) => setAsOf(Number(e.target.value))}
               aria-label="Move through time" aria-valuetext={dayFromUtc(asOf)} />
      </div>

      <div className="hero-foot">
        <p className="readout" aria-live="polite">{hover ? <><strong>{day(hover.when)}</strong>: {hover.text} ({hover.how})</> : readout}</p>
        <div className="hero-actions">
          {asOf !== t && <button className="ghost" onClick={() => setAsOf(t)}>Back to the decision</button>}
          {later.length > 0 && asOf < lastLater && (
            <button className="ghost" onClick={() => setAsOf(lastLater)}>Clear the fog</button>
          )}
        </div>
      </div>
      <p className="key">
        <span><i className="k known" /> earlier patch, taller the closer the decider's involvement</span>
        <span><i className="k later" /> later change to the same files</span>
        <span><i className="k fix" /> later fix of this patch</span>
        <span>Drag across the strip to move through time.</span>
      </p>
    </section>
  );
}
