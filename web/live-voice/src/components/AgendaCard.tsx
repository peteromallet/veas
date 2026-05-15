import { useEffect, useState } from "react";
import {
  LiveApiError,
  fetchSessionCard,
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

export function AgendaCard({ persona, sessionId, onConfirm, onCancel }: Props) {
  const [card, setCard] = useState<SessionCardPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchSessionCard(sessionId)
      .then((c) => {
        if (!cancelled) setCard(c);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof LiveApiError
            ? err.message
            : "Could not load the session card.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

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

  if (!card) {
    return (
      <section className="mx-auto max-w-2xl px-6 py-10">
        <p className="text-sm text-veas-muted">Catching up on where you are…</p>
      </section>
    );
  }

  const { themed, orphans } = groupByTheme(card.items);

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
          className="rounded-md bg-veas-accent px-5 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90"
        >
          Start the conversation
        </button>
      </div>
    </section>
  );
}
