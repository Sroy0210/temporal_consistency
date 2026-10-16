import { useEffect, useMemo, useRef, useState } from "react";
import { day, stripPrefix } from "../api.js";

export default function DecisionList({ decisions, selected, onSelect }) {
  const [query, setQuery] = useState("");
  const [onlyFixed, setOnlyFixed] = useState(false);
  const [onlyPrepared, setOnlyPrepared] = useState(false);
  const selectedRef = useRef(null);
  useEffect(() => {
    selectedRef.current?.scrollIntoView({ block: "nearest" });
  }, [selected]);

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
    <aside className="list" aria-label="Decisions">
      <input className="search" type="search" placeholder="Search subject, author or hash" value={query}
             onChange={(e) => setQuery(e.target.value)} aria-label="Search decisions" />
      <div className="filters">
        <label><input type="checkbox" checked={onlyFixed} onChange={(e) => setOnlyFixed(e.target.checked)} /> Later fixed</label>
        <label><input type="checkbox" checked={onlyPrepared} onChange={(e) => setOnlyPrepared(e.target.checked)} /> Has prepared answers</label>
      </div>
      <p className="count">{shown.length} of {decisions.length} decisions</p>
      <ul>
        {shown.map((d) => (
          <li key={d.commit}>
            <button ref={d.commit === selected ? selectedRef : null} className={d.commit === selected ? "item on" : "item"} onClick={() => onSelect(d.commit)}
                    aria-current={d.commit === selected ? "true" : undefined}>
              <span className="item-subject">{stripPrefix(d.subject)}</span>
              <span className="item-meta">
                <span>{day(d.applied)}</span>
                {d.later_fixed && <span className="flag later">later fixed</span>}
                {d.prepared && <span className="flag ready">prepared</span>}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
