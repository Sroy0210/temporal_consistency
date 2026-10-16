// Thin wrappers around the backend API. Errors carry the server's own message.

async function handle(res) {
  if (res.ok) return res.json();
  let detail = res.statusText;
  try {
    detail = (await res.json()).detail || detail;
  } catch {
    /* not JSON */
  }
  throw new Error(detail);
}

export const getJSON = (path) => fetch(path).then(handle);

export const postJSON = (path, body) =>
  fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(handle);

export const shortHash = (h) => (h || "").slice(0, 12);

export const day = (iso) => {
  // "2026-01-23 21:49 UTC" -> "23 Jan 2026"
  if (!iso) return "";
  const d = new Date(iso.replace(" UTC", "Z").replace(" ", "T"));
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
};

export const dayFromUtc = (secs) =>
  new Date(secs * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

export const stripPrefix = (subject) => (subject || "").replace(/^ext4:\s*/, "");

// Find a quote inside a source text, forgiving only whitespace differences (as the server's check does).
export function findQuote(text, quote) {
  if (!text || !quote) return null;
  const words = quote.trim().replace(/[“”]/g, '"').replace(/[‘’]/g, "'").split(/\s+/);
  const pattern = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+");
  const m = new RegExp(pattern).exec(text);
  return m ? { start: m.index, end: m.index + m[0].length } : null;
}

export const isYesNo = (q) => /^\s*(does|did|is|was|were|has|had|can|could|do)\b/i.test(q || "");
