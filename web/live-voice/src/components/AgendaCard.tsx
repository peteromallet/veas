import { useEffect, useRef, useState } from "react";
import {
  LiveApiError,
  fetchSessionCard,
  retryPrep,
  type AgendaItemCard,
  type Persona,
  type SessionCardPayload,
} from "../api";

interface Props {
  persona: Persona;
  sessionId: string;
  onConfirm: () => void;
  onCancel: () => void;
}

function groupByTheme(items: AgendaItemCard[]) {
  const themed = new Map<string, { label: string; items: AgendaItemCard[] }>();
  const orphans: AgendaItemCard[] = [];
  for (const item of items) {
    if (item.theme) {
      const bucket = themed.get(item.theme.slug);
      if (bucket) bucket.items.push(item);
      else themed.set(item.theme.slug, { label: item.theme.label, items: [item] });
    } else {
      orphans.push(item);
    }
  }
  return { themed, orphans };
}

const PRIORITY_BADGE: Record<AgendaItemCard["priority"], string> = {
  must: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  should: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  optional: "bg-white/5 text-veas-muted border-white/10",
};

function ItemRow({ item }: { item: AgendaItemCard }) {
  return (
    <li className="rounded-md border border-white/5 bg-veas-bg/40 p-4">
      <div className="flex items-start justify-between gap-3">
        <h4 className="text-sm font-medium text-white">{item.title}</h4>
        <span
          className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider ${PRIORITY_BADGE[item.priority]}`}
        >
          {item.priority}
        </span>
      </div>
      {item.intent && (
        <p className="mt-1.5 text-xs text-veas-muted">{item.intent}</p>
      )}
      {item.ask && (
        <p className="mt-2 text-xs text-white/80">
          <span className="text-veas-muted">Likely ask: </span>
          {item.ask}
        </p>
      )}
      {item.done_when && (
        <p className="mt-1 text-xs text-white/60">
          <span className="text-veas-muted">Handled when: </span>
          {item.done_when}
        </p>
      )}
    </li>
  );
}

/** Statuses that indicate prep is still in progress — poll until ready/failed. */
const PREP_PENDING_STATUSES = new Set(["preparing", "prepping"]);

export function AgendaCard({ persona, sessionId, onConfirm, onCancel }: Props) {
  const [card, setCard] = useState<SessionCardPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const cancelledRef = useRef(false);

  const loadCard = () => {
    fetchSessionCard(sessionId)
      .then((c) => {
        if (cancelledRef.current) return;
        setCard(c);
        setError(null);
        // Stop polling when prep is no longer pending
        if (!PREP_PENDING_STATUSES.has(c.status)) {
          if (pollRef.current) {
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
        }
      })
      .catch((err: unknown) => {
        if (cancelledRef.current) return;
        setError(
          err instanceof LiveApiError
            ? err.message
            : "Could not load the session card.",
        );
      });
  };

  useEffect(() => {
    cancelledRef.current = false;
    // Initial load
    loadCard();
    // Poll every 2 seconds while prep is pending
    pollRef.current = setInterval(() => {
      fetchSessionCard(sessionId)
        .then((c) => {
          if (cancelledRef.current) return;
          setCard(c);
          setError(null);
          if (!PREP_PENDING_STATUSES.has(c.status)) {
            if (pollRef.current) {
              clearInterval(pollRef.current);
              pollRef.current = null;
            }
          }
        })
        .catch(() => {
          // Silently ignore poll errors — the initial load error is surfaced
        });
    }, 2000);

    return () => {
      cancelledRef.current = true;
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [sessionId]);

  const handleRetry = async () => {
    setRetrying(true);
    setError(null);
    try {
      await retryPrep(sessionId);
      // Retry succeeded — the session is now in 'preparing' state.
      // Restart polling.
      setCard(null);
      // Poll will resume automatically via the interval (still running).
      // But we need an immediate load to show the preparing state.
      loadCard();
    } catch (err: unknown) {
      setError(
        err instanceof LiveApiError
          ? err.message
          : "Retry failed. Please try again.",
      );
    } finally {
      setRetrying(false);
    }
  };

  // ── Error state ──────────────────────────────────────────────────────
  if (error) {
    return (
      <section className="mx-auto max-w-2xl px-6 py-10">
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
          {error}
        </div>
        <button
          type="button"
          onClick={onCancel}
          className="mt-4 rounded-md px-4 py-2 text-sm text-veas-muted hover:text-white"
        >
          Back
        </button>
      </section>
    );
  }

  // ── Loading (preparing / no card yet) ────────────────────────────────
  if (!card || PREP_PENDING_STATUSES.has(card.status)) {
    return (
      <section className="mx-auto max-w-2xl px-6 py-10">
        <div className="flex flex-col items-center gap-4">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-veas-accent border-t-transparent" />
          <p className="text-sm text-veas-muted">
            {card?.status === "preparing" || card?.status === "prepping"
              ? "Preparing your session agenda…"
              : "Catching up on where you are…"}
          </p>
          <p className="text-xs text-veas-muted/60">
            This may take up to 30 seconds
          </p>
        </div>
      </section>
    );
  }

  // ── Prep failed ──────────────────────────────────────────────────────
  if (card.status === "prep_failed") {
    return (
      <section className="mx-auto max-w-2xl px-6 py-10">
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-6">
          <h3 className="text-sm font-medium text-red-300">
            Session preparation failed
          </h3>
          {card.failure_reason && (
            <p className="mt-2 text-sm text-red-200/80">
              {card.failure_reason}
            </p>
          )}
          <div className="mt-4 flex items-center gap-3">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md px-4 py-2 text-sm text-veas-muted hover:text-white"
            >
              Back
            </button>
            <button
              type="button"
              onClick={handleRetry}
              disabled={retrying}
              className="rounded-md bg-veas-accent px-5 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90 disabled:opacity-50"
            >
              {retrying ? "Retrying…" : "Retry"}
            </button>
          </div>
        </div>
      </section>
    );
  }

  const { themed, orphans } = groupByTheme(card.items);
  const canStart = card.status === "ready" && card.items.length > 0;

  return (
    <section className="mx-auto max-w-2xl px-6 py-10">
      <header className="mb-4">
        <p className="text-xs uppercase tracking-widest text-veas-muted">
          Session prepared with
        </p>
        <h2 className="text-xl font-semibold text-white">{persona.display_name}</h2>
      </header>

      {card.prep_summary && (
        <div className="rounded-lg border border-white/5 bg-veas-surface p-5">
          <p className="text-sm leading-relaxed text-white/90">{card.prep_summary}</p>
        </div>
      )}

      <div className="mt-6 space-y-6">
        {Array.from(themed.entries()).map(([slug, group]) => (
          <div key={slug}>
            <h3 className="mb-2 text-xs uppercase tracking-widest text-veas-muted">
              {group.label}
            </h3>
            <ul className="space-y-2">
              {group.items.map((item) => (
                <ItemRow key={item.id} item={item} />
              ))}
            </ul>
          </div>
        ))}
        {orphans.length > 0 && (
          <div>
            {themed.size > 0 && (
              <h3 className="mb-2 text-xs uppercase tracking-widest text-veas-muted">
                Other focus areas
              </h3>
            )}
            <ul className="space-y-2">
              {orphans.map((item) => (
                <ItemRow key={item.id} item={item} />
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="mt-8 flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md px-4 py-2 text-sm text-veas-muted hover:text-white"
        >
          Back
        </button>
        <button
          type="button"
          onClick={onConfirm}
          disabled={!canStart}
          className="rounded-md bg-veas-accent px-5 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Start the conversation
        </button>
      </div>
    </section>
  );
}
