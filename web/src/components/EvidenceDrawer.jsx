import { useEffect, useRef } from "react";
import { findQuote, shortHash } from "../api.js";

// Shows one evidence item in full, with the cited quote highlighted.
// `sources` is tried in order: the first text that contains its quote is shown.
export default function EvidenceDrawer({ label, commit, sources, note, onClose }) {
  const markRef = useRef(null);
  const closeRef = useRef(null);

  let chosen = sources[0];
  let hit = null;
  for (const s of sources) {
    const m = s.quote ? findQuote(s.text, s.quote) : null;
    if (m) {
      chosen = s;
      hit = m;
      break;
    }
  }
  const missing = sources.some((s) => s.quote) && !hit;

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    markRef.current?.scrollIntoView({ block: "center" });
  }, [hit]);

  const text = chosen.text || "";
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={label} onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <div>
            <h4>{label}</h4>
            {commit && <p className="mono fine">{shortHash(commit)}</p>}
          </div>
          <button ref={closeRef} className="close" onClick={onClose}>Close</button>
        </header>
        {(note || chosen.note) && <p className="drawer-note">{chosen.note || note}</p>}
        {missing && <p className="drawer-note">The quote could not be located in this text; the full text is shown.</p>}
        <pre className="source">
          {hit ? (
            <>
              {text.slice(0, hit.start)}
              <mark ref={markRef}>{text.slice(hit.start, hit.end)}</mark>
              {text.slice(hit.end)}
            </>
          ) : (
            text
          )}
        </pre>
      </aside>
    </div>
  );
}
