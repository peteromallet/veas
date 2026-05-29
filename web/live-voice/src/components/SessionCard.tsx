import { useState } from "react";
import { createSession, LiveApiError, type Persona } from "../api";

interface Props {
  persona: Persona;
  onCancel: () => void;
  onStarted: (sessionId: string, opts?: { skipPrep?: boolean }) => void;
}

export function SessionCard({ persona, onCancel, onStarted }: Props) {
  const [steering, setSteering] = useState("");
  const [openEnded, setOpenEnded] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function startSession(skipPrep: boolean) {
    setSubmitting(true);
    setError(null);
    try {
      const { session_id } = await createSession({
        bot_id: persona.bot_id,
        steering_text: steering.trim(),
        mode: openEnded ? "open_ended" : "guided",
        skip_prep: skipPrep,
      });
      onStarted(session_id, { skipPrep });
    } catch (err: unknown) {
      if (err instanceof LiveApiError) {
        setError(err.message);
      } else {
        setError("Could not start the session. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    await startSession(false);
  }

  return (
    <section className="mx-auto max-w-2xl px-6 py-10">
      <button
        type="button"
        onClick={onCancel}
        className="mb-4 inline-flex items-center gap-1 text-sm text-veas-muted hover:text-white"
      >
        <svg
          viewBox="0 0 24 24"
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M19 12H5" />
          <path d="m12 19-7-7 7-7" />
        </svg>
        Back to personas
      </button>

      <form
        onSubmit={handleSubmit}
        className="rounded-lg border border-white/5 bg-veas-surface p-6"
      >
        <header className="mb-4">
          <p className="text-xs uppercase tracking-widest text-veas-muted">
            Persona
          </p>
          <h2 className="text-xl font-semibold text-white">
            {persona.display_name}
          </h2>
        </header>

        <label className="block text-sm font-medium text-white">
          What do you want to talk about?
          <textarea
            value={steering}
            onChange={(e) => setSteering(e.target.value)}
            rows={4}
            placeholder="Optional — a topic, a feeling, a question…"
            className="mt-2 w-full rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-sm text-white placeholder:text-veas-muted focus:border-veas-accent focus:outline-none focus:ring-1 focus:ring-veas-accent/60"
          />
        </label>

        <label className="mt-4 flex items-center gap-3 text-sm text-white">
          <input
            type="checkbox"
            checked={openEnded}
            onChange={(e) => setOpenEnded(e.target.checked)}
            className="h-4 w-4 rounded border-white/20 bg-veas-bg text-veas-accent focus:ring-veas-accent/60"
          />
          Open-ended chat
          <span className="text-xs text-veas-muted">
            (let the persona steer freely instead of staying on topic)
          </span>
        </label>

        {error && (
          <div className="mt-4 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
            {error}
          </div>
        )}

        <div className="mt-6 flex flex-wrap items-center justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md px-4 py-2 text-sm text-veas-muted hover:text-white"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={submitting}
            onClick={() => void startSession(true)}
            className="rounded-md border border-white/15 px-5 py-2 text-sm font-medium text-white transition hover:border-white/30 hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? "Starting…" : "Just speak"}
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="rounded-md bg-veas-accent px-5 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {submitting ? "Starting…" : "Begin session"}
          </button>
        </div>
      </form>
    </section>
  );
}
