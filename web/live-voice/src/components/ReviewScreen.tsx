import { useState } from "react";
import {
  saveReview,
  type Persona,
  type ReviewItem,
  type ReviewNote,
  type SessionReview,
} from "../api";

interface Props {
  persona: Persona;
  review: SessionReview;
  onSaved: () => void;
  onDiscard: () => void;
}

/** Extract a displayable text from a review content item.
 *  Items may be plain strings (deterministic synthesis) or
 *  ``{text, source, ...}`` objects (debrief artifact adapter). */
function itemText(item: unknown): string {
  if (typeof item === "string") return item;
  if (item && typeof item === "object" && "text" in item) {
    return String((item as { text: unknown }).text ?? "");
  }
  return "";
}

/** Extract a title from a review item that may come from either the
 *  deterministic synthesis (``{item_id, title, ...}``) or the debrief
 *  adapter (``{text, title?, item_id?, ...}``). */
function itemTitle(item: Record<string, unknown>): string {
  if (typeof item.title === "string" && item.title) return item.title;
  if (typeof item.text === "string" && item.text) return item.text;
  return "";
}

/** Extract an id for keying from an item. */
function itemKey(item: Record<string, unknown>, fallback: number): string {
  if (typeof item.item_id === "string") return item.item_id;
  if (typeof item.note_id === "string") return item.note_id;
  return String(fallback);
}

/**
 * Post-session review screen.  Four sections (per
 * docs/live-conversation-mode.md §UI):
 *
 *   * What Rosi heard      — primary user transcript bullets
 *   * What you decided     — covered agenda items
 *   * Still open           — pending/active items
 *   * What Rosi remembers  — conversation_notes
 *
 * Renders gracefully for both debrief-artifact-derived data (arrays/objects
 * from the live_debrief adapter) and fallback deterministic synthesis data
 * without separate component paths.  Strings and ``{text, source}`` objects
 * in ``what_heard`` are handled uniformly.  Items in ``what_decided`` /
 * ``still_open`` may lack ``item_id`` / ``title`` when sourced from the
 * debrief adapter — in that case they render as plain text items.
 *
 * Covered items and notes are editable inline.  Save persists edits
 * through `POST /api/live/sessions/{id}/review/save`; Discard skips the
 * write-through but keeps the transcript + conversation row.
 */
export function ReviewScreen({ persona, review, onSaved, onDiscard }: Props) {
  const [items, setItems] = useState<ReviewItem[]>(review.what_decided);
  const [notes, setNotes] = useState<ReviewNote[]>(review.what_to_remember);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function patchItem(i: number, summary: string) {
    setItems((prev) => prev.map((it, idx) => (idx === i ? { ...it, summary } : it)));
  }

  function patchNote(i: number, text: string) {
    setNotes((prev) => prev.map((n, idx) => (idx === i ? { ...n, text } : n)));
  }

  function dropNote(i: number) {
    setNotes((prev) => prev.map((n, idx) => (idx === i ? { ...n, text: "" } : n)));
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      await saveReview(review.session_id, {
        keep_items: items.map((it) => ({
          item_id: it.item_id,
          summary: it.summary || undefined,
        })),
        keep_notes: notes.map((n) => ({ note_id: n.note_id, text: n.text })),
      });
      onSaved();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="mx-auto max-w-2xl px-6 py-10">
      <header className="mb-6">
        <p className="text-xs uppercase tracking-widest text-veas-muted">
          Review with {persona.display_name}
        </p>
        <h2 className="text-xl font-semibold text-white">
          Before we close out — anything to keep or fix?
        </h2>
        <p className="mt-2 text-sm text-veas-muted">
          Edit anything that's off. Hit Save and we'll remember it. Hit Discard
          and we'll keep the transcript only.
        </p>
        {/* Show debrief-powered indicator when live_debrief artifact is present. */}
        {!!review.live_debrief && (
          <p className="mt-1 text-xs text-emerald-300/70">
            ✦ Enhanced review from conversation analysis
          </p>
        )}
        {review.review_summary && (
          <div className="mt-3 rounded-md border border-emerald-500/20 bg-emerald-500/5 p-3">
            <p className="text-xs uppercase tracking-widest text-emerald-300/70">
              Summary
            </p>
            <p className="mt-1 text-sm text-white/90 leading-relaxed">
              {review.review_summary}
            </p>
          </div>
        )}
      </header>

      {review.is_empty && (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
          The session ended before any user or bot turns landed — nothing to review.
        </div>
      )}

      <Section title="What I heard from you">
        {review.what_heard.length === 0 ? (
          <p className="text-xs text-veas-muted">(no user turns)</p>
        ) : (
          <ul className="space-y-1 text-sm text-white/90">
            {review.what_heard.map((line, i) => {
              const text = itemText(line);
              const source =
                typeof line === "object" && line !== null && "source" in line
                  ? (line as { source?: string }).source
                  : undefined;
              return (
                <li key={i} className="flex items-baseline gap-1.5">
                  <span>•</span>
                  <span>{text || String(line)}</span>
                  {source === "live_debrief" && (
                    <span className="text-[10px] text-emerald-400/50" title="AI-generated">
                      ✦
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      <Section title="What you decided">
        {items.length === 0 ? (
          <p className="text-xs text-veas-muted">(no items covered)</p>
        ) : (
          <ul className="space-y-3">
            {items.map((it, i) => {
              const rec = it as unknown as Record<string, unknown>;
              const title = itemTitle(rec);
              const evidenceQuote =
                typeof rec.evidence_quote === "string"
                  ? rec.evidence_quote
                  : undefined;
              // Debrief adapter items may not have item_id — use index fallback.
              const key = itemKey(rec, i);
              return (
                <li
                  key={key}
                  className="rounded-md border border-white/5 bg-veas-bg/40 p-3"
                >
                  {title ? (
                    <h4 className="text-sm font-medium text-white">{title}</h4>
                  ) : (
                    <p className="text-sm italic text-veas-muted">
                      (item text unavailable)
                    </p>
                  )}
                  {evidenceQuote && (
                    <p className="mt-1 text-xs italic text-veas-muted">
                      "{evidenceQuote}"
                    </p>
                  )}
                  <textarea
                    value={it.summary || ""}
                    onChange={(e) => patchItem(i, e.target.value)}
                    rows={2}
                    className="mt-2 w-full rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-xs text-white"
                  />
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      <Section title="Still open">
        {review.still_open.length === 0 ? (
          <p className="text-xs text-veas-muted">(nothing left unhandled)</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {review.still_open.map((it, i) => {
              const rec = it as unknown as Record<string, unknown>;
              const title = itemTitle(rec);
              const intent =
                typeof rec.intent === "string" ? rec.intent : undefined;
              const key = itemKey(rec, i);
              return (
                <li
                  key={key}
                  className="rounded-md border border-white/5 bg-veas-bg/40 p-3"
                >
                  {title ? (
                    <p className="text-white">{title}</p>
                  ) : (
                    <p className="text-sm italic text-veas-muted">
                      (item text unavailable)
                    </p>
                  )}
                  {intent && (
                    <p className="mt-1 text-xs text-veas-muted">{intent}</p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      <Section title="What I should remember">
        {notes.length === 0 ? (
          <p className="text-xs text-veas-muted">(no notes captured)</p>
        ) : (
          <ul className="space-y-2">
            {notes.map((n, i) => {
              const rec = n as unknown as Record<string, unknown>;
              const kind =
                typeof rec.kind === "string" ? rec.kind : "note";
              const text =
                typeof rec.text === "string"
                  ? rec.text
                  : itemText(rec);
              const key = itemKey(rec, i);
              return (
                <li
                  key={key}
                  className="flex items-start gap-2 rounded-md border border-white/5 bg-veas-bg/40 p-3"
                >
                  <span className="rounded-full border border-white/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-veas-muted">
                    {kind}
                  </span>
                  <textarea
                    value={text}
                    onChange={(e) => patchNote(i, e.target.value)}
                    rows={2}
                    className="flex-1 rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-xs text-white"
                  />
                  <button
                    type="button"
                    onClick={() => dropNote(i)}
                    className="text-xs text-rose-300 hover:text-rose-200"
                  >
                    drop
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      {error && (
        <p className="mt-4 text-sm text-rose-300">Save failed: {error}</p>
      )}

      <div className="mt-8 flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={onDiscard}
          disabled={saving}
          className="rounded-md px-4 py-2 text-sm text-veas-muted hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          Discard
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className="rounded-md bg-veas-accent px-5 py-2 text-sm font-medium text-veas-bg hover:bg-veas-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-6">
      <h3 className="mb-2 text-xs uppercase tracking-widest text-veas-muted">
        {title}
      </h3>
      <div className="rounded-lg border border-white/5 bg-veas-surface p-4">
        {children}
      </div>
    </div>
  );
}
