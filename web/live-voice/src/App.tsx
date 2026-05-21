import { useState } from "react";
import { Header } from "./components/Header";
import { PersonaPicker } from "./components/PersonaPicker";
import { SessionCard } from "./components/SessionCard";
import { AgendaCard } from "./components/AgendaCard";
import { LiveScreen } from "./components/LiveScreen";
import { ReviewScreen } from "./components/ReviewScreen";
import { endSession, retryDebrief, type Persona, type SessionReview } from "./api";

type View =
  | { kind: "picker" }
  | { kind: "session"; persona: Persona }
  | { kind: "card"; persona: Persona; sessionId: string }
  | { kind: "live"; persona: Persona; sessionId: string }
  | { kind: "review"; persona: Persona; review: SessionReview };

export default function App() {
  const [view, setView] = useState<View>({ kind: "picker" });

  /** Called by LiveScreen when the user chooses to end the session.
   *  When LiveScreen has already resolved the final review (debrief
   *  completed or not needed) it passes the review directly; otherwise
   *  LiveScreen manages the debrief lifecycle internally and calls
   *  this with the resolved review when ready. */
  function handleLiveEnd(review?: SessionReview) {
    if (review) {
      setView({
        kind: "review",
        persona: (view as { kind: "live"; persona: Persona }).persona,
        review,
      });
      return;
    }
    // Fallback: call endSession directly (LiveScreen didn't pre-resolve).
    const sessionId = (view as { kind: "live"; sessionId: string }).sessionId;
    const persona = (view as { kind: "live"; persona: Persona }).persona;
    void (async () => {
      try {
        const r = await endSession(sessionId);
        setView({ kind: "review", persona, review: r });
      } catch {
        setView({ kind: "picker" });
      }
    })();
  }

  /** Called by LiveScreen to retry a failed debrief. */
  async function handleRetryDebrief(sessionId: string): Promise<void> {
    await retryDebrief(sessionId);
  }

  return (
    <div className="min-h-screen bg-veas-bg text-slate-100">
      <Header />
      <main>
        {view.kind === "picker" && (
          <PersonaPicker
            onPick={(persona) => setView({ kind: "session", persona })}
          />
        )}
        {view.kind === "session" && (
          <SessionCard
            persona={view.persona}
            onCancel={() => setView({ kind: "picker" })}
            onStarted={(sessionId) =>
              setView({ kind: "card", persona: view.persona, sessionId })
            }
          />
        )}
        {view.kind === "card" && (
          <AgendaCard
            persona={view.persona}
            sessionId={view.sessionId}
            onCancel={() => setView({ kind: "session", persona: view.persona })}
            onConfirm={() =>
              setView({ kind: "live", persona: view.persona, sessionId: view.sessionId })
            }
          />
        )}
        {view.kind === "live" && (
          <LiveScreen
            persona={view.persona}
            sessionId={view.sessionId}
            onEnd={handleLiveEnd}
            onRetryDebrief={handleRetryDebrief}
          />
        )}
        {view.kind === "review" && (
          <ReviewScreen
            persona={view.persona}
            review={view.review}
            onSaved={() => setView({ kind: "picker" })}
            onDiscard={() => setView({ kind: "picker" })}
          />
        )}
      </main>
      <footer className="mx-auto max-w-5xl px-6 py-6 text-center text-xs text-veas-muted">
        Veas mediator · Live Voice Agent
      </footer>
    </div>
  );
}
