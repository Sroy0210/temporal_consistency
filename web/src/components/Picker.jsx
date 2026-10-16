import { useEffect, useMemo, useRef, useState } from "react";
import { day, stripPrefix } from "../api.js";

// Searchable list of decisions, opened from the top bar.
export default function Picker({ decisions, current, onSelect }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [onlyFixed, setOnlyFixed] = useState(false);
  const [onlyPrepared, setOnlyPrepared] = useState(false);
  const box = useRef(null);
  const search = useRef(null);

  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const onKey = (e) => e.key === "Escape" && setOpen(false);
    const onClick = (e) => box.current && !box.current.contains(e.target) && setOpen(false);
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return decisions.filter(
      (d) =>
        (!onlyFixed || d.later_fixed) &&
        (!onlyPrepared || d.prepared) &&
        (!q || d.subject.toLowerCase().includes(q) || d.commit.startsWith(q) || (d.author || "").toLowerCase().includes(q))
    );
  }, [decisions, query, onlyFixed, onlyPrepared]);

  return (
    <div className="picker" ref={box}>
      <button className="picker-button" onClick={() => setOpen(!open)} aria-expanded={open} aria-haspopup="dialog">
        <span className="picker-hint">Decision</span>
        <span className="picker-current">{current ? stripPrefix(current.subject) : "Choose a decision"}</span>
        <span className="picker-caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="picker-panel" role="dialog" aria-label="Choose a decision">
          <input ref={search} className="search" type="search" placeholder="Search by subject, author or hash"
                 value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search decisions" />
          <div className="filters">
            <label><input type="checkbox" checked={onlyFixed} onChange={(e) => setOnlyFixed(e.target.checked)} /> Later fixed</label>
            <label><input type="checkbox" checked={onlyPrepared} onChange={(e) => setOnlyPrepared(e.target.checked)} /> Has prepared answers</label>
            <span className="count">{shown.length} of {decisions.length}</span>
          </div>
          <ul className="picker-list">
            {shown.map((d) => (
              <li key={d.commit}>
                <button className={current?.commit === d.commit ? "pick on" : "pick"}
                        onClick={() => { onSelect(d.commit); setOpen(false); }}>
                  <span className="pick-subject">{stripPrefix(d.subject)}</span>
                  <span className="pick-meta">
                    {day(d.applied)}, {d.known} earlier patches known
                    {d.later_fixed && <span className="tag fixed">later fixed</span>}
                    {d.prepared && <span className="tag ready">prepared</span>}
                  </span>
                </button>
              </li>
            ))}
            {shown.length === 0 && <li className="none">No decision matches. Clear the search or the filters.</li>}
          </ul>
        </div>
      )}
    </div>
  );
}
