import { useState } from "react";
import { Header } from "./components/Header";
import { PersonaPicker } from "./components/PersonaPicker";
import { SessionCard } from "./components/SessionCard";
import { AgendaCard } from "./components/AgendaCard";
import { LiveScreen } from "./components/LiveScreen";
import { ReviewScreen } from "./components/ReviewScreen";
import { MagicLinkLogin } from "./components/MagicLinkLogin";
import { SessionsList } from "./components/SessionsList";
import {
  endSession,
  retryDebrief,
  fetchReview,
  getAuthToken,
  canonicalizeStatus,
  type Persona,
  type SessionReview,
  type SessionSummary,
} from "./api";

type View =
  | { kind: "magic_link_login" }
  | { kind: "sessions" }
  | { kind: "picker" }
  | { kind: "session"; persona: Persona }
  | { kind: "card"; persona: Persona; sessionId: string }
  | { kind: "live"; persona: Persona; sessionId: string }
  | { kind: "review"; persona: Persona; review: SessionReview };

function initialView(): View {
  return getAuthToken() ? { kind: "sessions" } : { kind: "magic_link_login" };
}

export default function App() {
  const [view, setView] = useState<View>(initialView);

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
        setView({ kind: "sessions" });
      }
    })();
  }

  /** Called by LiveScreen to retry a failed debrief. */
  async function handleRetryDebrief(sessionId: string): Promise<void> {
    await retryDebrief(sessionId);
  }

  /** Resume an existing session — route to card, live, or review
   *  based on its canonical status. */
  function handleResumeSession(session: SessionSummary) {
    const persona: Persona = {
      bot_id: session.bot_id,
      display_name: session.topic_label,
    };
    const c = canonicalizeStatus(session.status);

    if (c === "active") {
      setView({ kind: "live", persona, sessionId: session.id });
    } else if (
      c === "completed" ||
      c === "review_pending" ||
      c === "debriefing" ||
      c === "debrief_failed"
    ) {
      void (async () => {
        try {
          const review = await fetchReview(session.id);
          setView({ kind: "review", persona, review });
        } catch {
          setView({ kind: "sessions" });
        }
      })();
    } else {
      // preparing, ready, prep_failed → agenda card
      setView({ kind: "card", persona, sessionId: session.id });
    }
  }

  return (
    <div className="min-h-screen bg-veas-bg text-slate-100">
      <Header />
      <main>
        {view.kind === "magic_link_login" && (
          <MagicLinkLogin
            onAuthed={() => setView({ kind: "sessions" })}
          />
        )}
        {view.kind === "sessions" && (
          <SessionsList
            onNewConversation={() => setView({ kind: "picker" })}
            onResumeSession={handleResumeSession}
          />
        )}
        {view.kind === "picker" && (
          <PersonaPicker
            onPick={(persona) => setView({ kind: "session", persona })}
          />
        )}
        {view.kind === "session" && (
          <SessionCard
            persona={view.persona}
            onCancel={() => setView({ kind: "sessions" })}
            onStarted={(sessionId, opts) =>
              setView({
                kind: opts?.skipPrep ? "live" : "card",
                persona: view.persona,
                sessionId,
              })
            }
          />
        )}
        {view.kind === "card" && (
          <AgendaCard
            persona={view.persona}
            sessionId={view.sessionId}
            onCancel={() => setView({ kind: "sessions" })}
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
            onSaved={() => setView({ kind: "sessions" })}
            onDiscard={() => setView({ kind: "sessions" })}
          />
        )}
      </main>
      <footer className="mx-auto max-w-5xl px-6 py-6 text-center text-xs text-veas-muted">
        Veas mediator · Live Voice Agent
      </footer>
    </div>
  );
}
