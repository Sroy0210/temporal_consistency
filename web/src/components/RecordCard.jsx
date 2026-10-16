const OTHER_FIELDS = [
  ["problem", "The problem"],
  ["change", "The change"],
  ["context", "Background"],
  ["alternatives", "Alternatives mentioned"],
  ["concerns", "Concerns raised"],
];

// The decision record extracted offline from the commit message. The stated reason leads.
export default function RecordCard({ record, onQuote }) {
  if (!record) return <p className="muted">No extracted record for this decision yet.</p>;
  const reasons = record.fields.rationale || [];
  const others = OTHER_FIELDS.filter(([k]) => (record.fields[k] || []).length);

  return (
    <div className="record">
      {reasons.length > 0 ? (
        <figure className="reason">
          <figcaption>The stated reason{reasons.length > 1 ? "s" : ""}</figcaption>
          {reasons.map((item, i) => (
            <blockquote key={i}>
              <button className="quote big" onClick={() => onQuote(item.quote)} title="Show this quote in the message">
                {item.quote}
              </button>
              <p className="gloss">{item.point}</p>
            </blockquote>
          ))}
        </figure>
      ) : (
        <p className="reason none">The commit message gives no reason for the change.</p>
      )}

      {others.length > 0 && (
        <dl className="record-fields">
          {others.map(([k, words]) => (
            <div key={k} className="record-field">
              <dt>{words}</dt>
              {record.fields[k].map((item, i) => (
                <dd key={i}>
                  {item.point}{" "}
                  <button className="quote" onClick={() => onQuote(item.quote)} title="Show this quote in the message">
                    {item.quote}
                  </button>
                </dd>
              ))}
            </div>
          ))}
        </dl>
      )}
      <p className="fine">
        A {record.kind}, read offline from the commit message by {record.model}. Every quote was checked word for word
        against the message.
      </p>
    </div>
  );
}
