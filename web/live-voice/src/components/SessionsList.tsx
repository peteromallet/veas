import { useEffect, useState } from "react";
import {
  fetchSessions,
  canonicalizeStatus,
  LiveApiError,
  type SessionSummary,
} from "../api";

interface Props {
  onNewConversation: () => void;
  onResumeSession: (session: SessionSummary) => void;
}

function statusBadge(status: string): { label: string; cls: string } {
  const c = canonicalizeStatus(status);
  switch (c) {
    case "active":
      return { label: "Live", cls: "bg-emerald-500/20 text-emerald-300" };
    case "preparing":
      return { label: "Preparing", cls: "bg-amber-500/20 text-amber-300" };
    case "ready":
      return { label: "Ready", cls: "bg-sky-500/20 text-sky-300" };
    case "completed":
      return { label: "Done", cls: "bg-slate-500/20 text-slate-400" };
    case "review_pending":
      return { label: "Review", cls: "bg-violet-500/20 text-violet-300" };
    case "debriefing":
      return { label: "Debriefing", cls: "bg-cyan-500/20 text-cyan-300" };
    case "prep_failed":
      return { label: "Prep failed", cls: "bg-red-500/20 text-red-300" };
    case "debrief_failed":
      return { label: "Debrief failed", cls: "bg-red-500/20 text-red-300" };
    default:
      return { label: c, cls: "bg-slate-500/20 text-slate-400" };
  }
}

function resumeLabel(status: string): string {
  const c = canonicalizeStatus(status);
  switch (c) {
    case "active":
      return "Join";
    case "completed":
    case "review_pending":
    case "debriefing":
    case "debrief_failed":
      return "Review";
    case "prep_failed":
      return "Retry prep";
    default:
      return "Resume";
  }
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function snippet(text: string | null, maxLen = 120): string | null {
  if (!text) return null;
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen).trimEnd() + "…";
}

export function SessionsList({ onNewConversation, onResumeSession }: Props) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchSessions()
      .then((s) => {
        if (cancelled) return;
        setSessions(s);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof LiveApiError) {
          setError(err.message);
        } else {
          setError("Could not load sessions. Try again in a moment.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="mx-auto max-w-3xl px-6 py-10">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold text-white">Conversations</h2>
          <p className="mt-1 text-sm text-veas-muted">
            Your live-voice sessions
          </p>
        </div>
        <button
          type="button"
          onClick={onNewConversation}
          className="rounded-md bg-veas-accent px-4 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90 focus:outline-none focus:ring-2 focus:ring-veas-accent/60"
        >
          New conversation
        </button>
      </div>

      {loading && (
        <div className="rounded-lg border border-white/5 bg-veas-surface/40 p-6 text-veas-muted">
          Loading sessions…
        </div>
      )}

      {error && !loading && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
          {error}
        </div>
      )}

      {!loading && !error && sessions && sessions.length === 0 && (
        <div className="rounded-lg border border-white/5 bg-veas-surface/40 p-8 text-center">
          <p className="text-veas-muted">No conversations yet.</p>
          <button
            type="button"
            onClick={onNewConversation}
            className="mt-4 inline-flex items-center rounded-md bg-veas-accent px-5 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90"
          >
            Start your first conversation
          </button>
        </div>
      )}

      {!loading && !error && sessions && sessions.length > 0 && (
        <ul className="space-y-3">
          {sessions.map((s) => {
            const badge = statusBadge(s.status);
            const ps = snippet(s.prep_summary, 120);
            return (
              <li
                key={s.id}
                className="flex flex-col gap-2 rounded-lg border border-white/5 bg-veas-surface p-4 transition hover:border-white/10 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-white truncate">
                      {s.topic_label}
                    </span>
                    <span
                      className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${badge.cls}`}
                    >
                      {badge.label}
                    </span>
                  </div>
                  {ps && (
                    <p className="mt-1 text-sm text-veas-muted line-clamp-2">
                      {ps}
                    </p>
                  )}
                  <p className="mt-1 text-xs text-veas-muted/70">
                    {formatDate(s.created_at)}
                    {s.item_count > 0 && ` · ${s.item_count} item${s.item_count !== 1 ? "s" : ""}`}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => onResumeSession(s)}
                  className="shrink-0 rounded-md border border-white/15 px-4 py-2 text-sm font-medium text-white transition hover:border-white/30 hover:bg-white/5 focus:outline-none focus:ring-2 focus:ring-veas-accent/60"
                >
                  {resumeLabel(s.status)}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
