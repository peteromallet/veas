import { useEffect, useRef, useState } from "react";
import {
  liveSocketUrl,
  postConsent,
  endSession,
  fetchReview,
  getAuthToken,
  type Persona,
  type SessionReview,
} from "../api";
import { ConsentGate, type ConsentSelection } from "./ConsentGate";
import { openMic, type MicSession, type VoiceState } from "../mic";
import { RobotFace } from "./RobotFace";

interface Props {
  persona: Persona;
  sessionId: string;
  onEnd: (review?: SessionReview) => void;
  onRetryDebrief: (sessionId: string) => Promise<void>;
}

interface PhaseEvent {
  ts: number;
  text: string;
  kind: "phase" | "ack" | "echo" | "info" | "error";
}

interface TranscriptLine {
  ts: number;
  speaker: "user" | "bot";
  text: string;
}

type Status = "consent" | "connecting" | "open" | "live" | "closed" | "error";

function speakViaSpeechSynthesis(
  utterance: string,
  setBotSpeaking: (v: boolean) => void,
  botSpeakingRef: React.MutableRefObject<boolean>,
  setTtsUnavailable: (v: boolean) => void,
) {
  try {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      const u = new SpeechSynthesisUtterance(utterance);
      u.rate = 1.0;
      u.pitch = 1.0;
      window.speechSynthesis.cancel();
      setBotSpeaking(true);
      botSpeakingRef.current = true;
      u.onend = () => {
        setBotSpeaking(false);
        botSpeakingRef.current = false;
      };
      u.onerror = () => {
        setTtsUnavailable(true);
        setBotSpeaking(false);
        botSpeakingRef.current = false;
      };
      window.speechSynthesis.speak(u);
      window.setTimeout(() => {
        if (
          !botSpeakingRef.current &&
          !window.speechSynthesis.speaking &&
          !window.speechSynthesis.pending
        ) {
          setTtsUnavailable(true);
        }
      }, 250);
    } else {
      setTtsUnavailable(true);
    }
  } catch {
    setTtsUnavailable(true);
  }
}

function TextInputFallback({
  disabled,
  onSend,
  className = "mt-4",
}: {
  disabled: boolean;
  onSend: (text: string) => void;
  className?: string;
}) {
  const [value, setValue] = useState("");
  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setValue("");
  }
  return (
    <form onSubmit={handleSubmit} className={`${className} flex items-center gap-2`}>
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Type if you can't speak right now…"
        disabled={disabled}
        className="flex-1 rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-sm text-white placeholder:text-veas-muted focus:border-veas-accent focus:outline-none focus:ring-1 focus:ring-veas-accent/60 disabled:cursor-not-allowed disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="rounded-md border border-white/10 px-3 py-2 text-sm text-white hover:border-white/30 disabled:cursor-not-allowed disabled:opacity-50"
      >
        Send
      </button>
    </form>
  );
}

export function LiveScreen({ persona, sessionId, onEnd, onRetryDebrief }: Props) {
  const [events, setEvents] = useState<PhaseEvent[]>([]);
  const [status, setStatus] = useState<Status>("consent");
  const [consent, setConsent] = useState<ConsentSelection | null>(null);
  const [micError, setMicError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [frameCount, setFrameCount] = useState(0);
  const [sentFrameCount, setSentFrameCount] = useState(0);
  const [ackedFrameCount, setAckedFrameCount] = useState(0);
  const [lastRms, setLastRms] = useState(0);
  const [voice, setVoice] = useState<VoiceState>("silent");
  const [botSpeaking, setBotSpeaking] = useState(false);
  const [botThinking, setBotThinking] = useState(false);
  const [ttsUnavailable, setTtsUnavailable] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [spend, setSpend] = useState<{ cents: number; cap: number } | null>(null);
  const [showTextInput, setShowTextInput] = useState(false);
  const [showTranscript, setShowTranscript] = useState(false);
  const [showActivity, setShowActivity] = useState(false);

  // ── Debrief lifecycle state ──────────────────────────────────────────
  type DebriefState = "idle" | "waiting" | "failed" | "done";
  const [debriefState, setDebriefState] = useState<DebriefState>("idle");
  const [debriefError, setDebriefError] = useState<string | null>(null);
  const [debriefReview, setDebriefReview] = useState<SessionReview | null>(null);
  const debriefPollRef = useRef<number | null>(null);
  const debriefCancelledRef = useRef(false);

  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicSession | null>(null);
  const botSpeakingRef = useRef(false);
  const lastUserActivityRef = useRef<number>(0);

  function pushEvent(text: string, kind: PhaseEvent["kind"] = "info") {
    setEvents((prev) => [...prev, { ts: Date.now(), text, kind }]);
  }

  useEffect(() => {
    if (!consent) return;
    let cancelledLifetime = false;
    let attempt = 0;

    function connect() {
      attempt += 1;
      setReconnectAttempt(attempt);
      setStatus("connecting");
      const ws = new WebSocket(liveSocketUrl(sessionId));
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        setStatus("open");
        setReconnecting(false);
        pushEvent(
          attempt === 1
            ? "WebSocket connected."
            : `Reconnected (attempt ${attempt}).`,
          "info",
        );
      };
      ws.onclose = (ev) => {
        if (cancelledLifetime) return;
        setBotThinking(false);
        // Clean close (1000 / 1001) = end-session; don't reconnect.
        if (ev.code === 1000 || ev.code === 1001) {
          setStatus((s) => (s === "error" ? s : "closed"));
          return;
        }
        setReconnecting(true);
        pushEvent(`Connection dropped (code ${ev.code}). Reconnecting…`, "error");
        // Reconnect within 2s (briefing target).
        window.setTimeout(() => {
          if (!cancelledLifetime) connect();
        }, 1500);
      };
      ws.onerror = () => {
        setStatus((s) => (s === "open" || s === "live" ? "error" : s));
        setBotThinking(false);
        pushEvent("WebSocket error.", "error");
      };
      hookMessageHandler(ws);
    }

    function hookMessageHandler(ws: WebSocket) {
    ws.onmessage = (msg) => {  // closed below in connect()
      let text: string;
      let kind: PhaseEvent["kind"] = "info";
      try {
        const parsed = JSON.parse(msg.data);
        if (parsed?.type === "frame_ack") {
          kind = "ack";
          text = `ack: ${parsed.frames} frames / ${parsed.bytes} bytes`;
          setAckedFrameCount(Number(parsed.frames) || 0);
        } else if (parsed?.type === "phase" || parsed?.type === "ready") {
          kind = "phase";
          text = parsed.label ?? "(phase)";
          if (parsed.type === "ready") setStatus("live");
        } else if (parsed?.type === "transcript_partial") {
          kind = "info";
          text = `… ${parsed.text}`;
        } else if (parsed?.type === "transcript_final") {
          kind = "phase";
          text = `you: ${parsed.text}`;
          if (String(parsed.text ?? "").trim()) {
            setBotThinking(true);
            setTranscript((prev) => [
              ...prev.slice(-40),
              { ts: Date.now(), speaker: "user", text: parsed.text },
            ]);
          }
        } else if (parsed?.type === "transcript_error") {
          kind = "error";
          text = `STT error: ${parsed.message ?? "unknown"}`;
          setBotThinking(false);
        } else if (parsed?.type === "bot_turn") {
          kind = "phase";
          text = `${persona.display_name}: ${parsed.utterance}`;
          setBotThinking(false);
          setTranscript((prev) => [
            ...prev.slice(-40),
            { ts: Date.now(), speaker: "bot", text: parsed.utterance },
          ]);
          // Try ElevenLabs Flash via /tts/{turn_id} first; the backend
          // returns audio/mpeg when a real key is set, or an empty
          // stream when on the stub.  Empty stream -> we fall back to
          // browser SpeechSynthesis below.
          if (parsed.tts_url) {
            void (async () => {
              try {
                // Use getAuthToken to inject Authorization header for TTS
                // requests (the authFetch wrapper expects JSON responses, but
                // TTS returns audio/mpeg blobs — build the fetch manually).
                const ttoken = getAuthToken();
                const theaders = new Headers();
                if (ttoken) theaders.set("Authorization", `Bearer ${ttoken}`);
                const res = await fetch(parsed.tts_url, { headers: theaders });
                if (!res.ok || (res.headers.get("X-TTS-Provider") || "") === "stub") {
                  throw new Error("tts unavailable");
                }
                const blob = await res.blob();
                if (blob.size === 0) throw new Error("empty tts stream");
                const url = URL.createObjectURL(blob);
                const audio = new Audio(url);
                botSpeakingRef.current = true;
                setBotSpeaking(true);
                audio.onended = () => {
                  setBotSpeaking(false);
                  botSpeakingRef.current = false;
                  URL.revokeObjectURL(url);
                };
                audio.onerror = () => {
                  setBotSpeaking(false);
                  botSpeakingRef.current = false;
                  setTtsUnavailable(true);
                  URL.revokeObjectURL(url);
                };
                await audio.play();
                return; // ElevenLabs path succeeded, skip SpeechSynthesis.
              } catch {
                // Fall through to SpeechSynthesis fallback below.
              }
              speakViaSpeechSynthesis(
                parsed.utterance,
                setBotSpeaking,
                botSpeakingRef,
                setTtsUnavailable,
              );
            })();
          } else {
            speakViaSpeechSynthesis(
              parsed.utterance,
              setBotSpeaking,
              botSpeakingRef,
              setTtsUnavailable,
            );
          }
          if (ttsUnavailable && !text.endsWith("(voice unavailable)")) {
            text = `${text} (voice unavailable)`;
          }
        } else if (parsed?.type === "back_up_acked") {
          kind = "info";
          text = parsed.rewound_item_id
            ? "Rewound to previous focus area."
            : `Couldn't rewind: ${parsed.detail ?? "nothing covered yet"}`;
        } else if (parsed?.type === "bot_turn_error") {
          kind = "error";
          text = `Bot turn failed: ${parsed.message ?? "unknown"}`;
          setBotThinking(false);
        } else if (parsed?.type === "budget_soft_warned") {
          kind = "info";
          text = `Heads-up: this session has spent $${(parsed.cents / 100).toFixed(2)} of the $${(parsed.hard_cap_cents / 100).toFixed(2)} cap.`;
          setSpend({ cents: parsed.cents, cap: parsed.hard_cap_cents });
        } else if (parsed?.type === "budget_hard_capped") {
          kind = "error";
          text = `This session has hit its $${(parsed.hard_cap_cents / 100).toFixed(2)} cost cap — bot turns are paused. End and save when you're ready.`;
          setSpend({ cents: parsed.cents, cap: parsed.hard_cap_cents });
          setBotThinking(false);
        } else {
          text = parsed.label ?? parsed.phase ?? parsed.text ?? JSON.stringify(parsed);
        }
      } catch {
        text = String(msg.data);
      }
      // Cap to last 50 to keep the DOM light.
      setEvents((prev) => [...prev.slice(-49), { ts: Date.now(), text, kind }]);
    };
    }

    connect();

    return () => {
      cancelledLifetime = true;
      try {
        wsRef.current?.close(1000);
      } catch {
        // ignore
      }
      wsRef.current = null;
    };
  }, [sessionId, consent]);

  // Open the mic once the WS is live AND consent is granted.
  useEffect(() => {
    if (status !== "live" || !consent || micRef.current) return;
    let cancelled = false;
    openMic({
      onFrame: (pcm) => {
        if (cancelled) return;
        if (botSpeakingRef.current) return;
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        ws.send(pcm);
        setSentFrameCount((c) => c + 1);
      },
      onAllFrames: (meta) => {
        // Increment counter for every captured frame, dropped or sent,
        // so the UI matches what the mic actually sees.
        if (!cancelled) {
          setFrameCount((c) => c + 1);
          setLastRms(meta.rms);
        }
      },
      onError: (err) => {
        if (!cancelled) setMicError(err.message);
      },
      onVoiceState: (state) => {
        if (cancelled) return;
        setVoice(state);
        lastUserActivityRef.current = Date.now();
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        if (botSpeakingRef.current) return;
        if (state === "active") {
          // Barge-in: if the bot is currently speaking via TTS, cancel
          // playback locally AND tell the backend to abort the in-flight
          // turn so the next bot turn isn't queued behind a stale one.
          if (botSpeakingRef.current) {
            try {
              if (typeof window !== "undefined" && "speechSynthesis" in window) {
                window.speechSynthesis.cancel();
              }
            } catch {
              // ignore
            }
            ws.send(JSON.stringify({ type: "barge_in" }));
            botSpeakingRef.current = false;
            setBotSpeaking(false);
          }
          ws.send(JSON.stringify({ type: "voice_active" }));
        } else {
          ws.send(JSON.stringify({ type: "turn_end" }));
        }
      },
    })
      .then((session) => {
        if (cancelled) {
          session.stop();
          return;
        }
        micRef.current = session;
        pushEvent(`Mic open @ ${session.contextSampleRate} Hz.`, "info");
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setMicError(err.message);
          pushEvent(`Mic error: ${err.message}`, "error");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [status, consent]);

  function handlePauseToggle() {
    setPaused((p) => {
      const next = !p;
      micRef.current?.setPaused(next);
      pushEvent(next ? "Paused — mic muted." : "Resumed — mic open.", "info");
      return next;
    });
  }

  function handleAdvance() {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "advance" }));
    }
  }

  function handleBackUp() {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "back_up" }));
    }
  }

  // ── Debrief polling effect ────────────────────────────────────────────
  useEffect(() => {
    if (debriefState !== "waiting") return;
    debriefCancelledRef.current = false;

    async function poll() {
      if (debriefCancelledRef.current) return;
      try {
        const review = await fetchReview(sessionId);
        if (debriefCancelledRef.current) return;

        // Check for debrief_failed
        if (review.debrief_failed) {
          setDebriefState("failed");
          setDebriefError(
            review.debrief_failed.reason ||
              review.debrief_failed.error ||
              "Debrief failed",
          );
          // Keep the review accessible (fallback synthesis).
          setDebriefReview(review);
          return;
        }

        // Check if debrief is still pending
        if (review.debrief_pending) {
          // Continue polling
          debriefPollRef.current = window.setTimeout(poll, 2000);
          return;
        }

        // Debrief completed successfully
        setDebriefReview(review);
        setDebriefState("done");
        // Navigate to review screen after a brief delay so the user
        // can see the "complete" state.
        window.setTimeout(() => {
          if (!debriefCancelledRef.current) {
            onEnd(review);
          }
        }, 600);
      } catch {
        if (!debriefCancelledRef.current) {
          // Polling error — keep trying.
          debriefPollRef.current = window.setTimeout(poll, 2000);
        }
      }
    }

    debriefPollRef.current = window.setTimeout(poll, 1500);

    return () => {
      debriefCancelledRef.current = true;
      if (debriefPollRef.current !== null) {
        clearTimeout(debriefPollRef.current);
        debriefPollRef.current = null;
      }
    };
  }, [debriefState, sessionId, onEnd]);

  function handleEnd() {
    // Close mic and WS immediately so the user sees a responsive stop.
    try {
      micRef.current?.stop();
    } catch {
      // ignore
    }
    micRef.current = null;
    try {
      wsRef.current?.send(JSON.stringify({ type: "end_session" }));
      wsRef.current?.close();
    } catch {
      // ignore
    }
    wsRef.current = null;

    // Call endSession to trigger debrief on the backend and get the
    // initial review + debrief_pending flag.
    void (async () => {
      try {
        const review = await endSession(sessionId);
        const reviewStatus = review.status || "";

        // If debrief is pending or status is 'debriefing', enter waiting state.
        if (review.debrief_pending || reviewStatus === "debriefing") {
          setDebriefState("waiting");
          setDebriefReview(review); // Keep the deterministic synthesis visible.
          return;
        }

        // If debrief already failed (edge case), show failure.
        if (review.debrief_failed) {
          setDebriefState("failed");
          setDebriefError(
            review.debrief_failed.reason ||
              review.debrief_failed.error ||
              "Debrief failed",
          );
          setDebriefReview(review);
          return;
        }

        // No debrief needed — go straight to review.
        onEnd(review);
      } catch {
        // endSession failed entirely — go back to picker.
        onEnd(undefined);
      }
    })();
  }

  /** Retry a failed debrief on the same session. */
  async function handleRetryDebrief() {
    setDebriefState("waiting");
    setDebriefError(null);
    try {
      await onRetryDebrief(sessionId);
      // The backend schedules the retry; polling picks up the new attempt.
    } catch (err: unknown) {
      setDebriefState("failed");
      setDebriefError(
        err instanceof Error ? err.message : "Retry debrief failed",
      );
    }
  }

  if (status === "consent") {
    return (
      <ConsentGate
        persona={persona}
        onConfirm={async (sel) => {
          // Record consent BEFORE opening the mic; the WS effect runs
          // once `consent` is set in state below.
          try {
            await postConsent(sessionId, {
              kind: sel.kind,
              partner_label: sel.kind === "partner_present" ? sel.partner_label : undefined,
            });
          } catch (err) {
            // Soft-fail: still proceed so the user isn't stuck in dev runs
            // without the conversation_consent_events table.
            console.warn("consent persist failed", err);
          }
          setConsent(sel);
        }}
        onCancel={onEnd}
      />
    );
  }

  const statusLabel: Record<Status, string> = {
    consent: "Consent",
    connecting: "Connecting…",
    open: "Negotiating…",
    live: "Live",
    closed: "Disconnected",
    error: "Connection error",
  };
  const statusColor: Record<Status, string> = {
    consent: "bg-slate-500",
    connecting: "bg-amber-400",
    open: "bg-amber-400",
    live: "bg-emerald-400",
    closed: "bg-slate-500",
    error: "bg-rose-500",
  };

  return (
    <section className="mx-auto flex min-h-[calc(100vh-8rem)] max-w-4xl px-4 py-6 sm:px-6">
      <div className="flex min-h-[42rem] flex-1 flex-col rounded-lg border border-white/5 bg-veas-surface p-4 sm:p-6">
        <header className="flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-widest text-veas-muted">
              In session with
            </p>
            <h2 className="text-xl font-semibold text-white">
              {persona.display_name}
            </h2>
            {consent?.kind === "partner_present" && (
              <p className="mt-1 text-xs text-veas-muted">
                With {consent.partner_label}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            {spend && (
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wider ${
                  spend.cents >= spend.cap
                    ? "bg-rose-500/15 text-rose-300 border border-rose-500/30"
                    : spend.cents >= spend.cap / 2
                      ? "bg-amber-500/15 text-amber-300 border border-amber-500/30"
                      : "bg-white/5 text-veas-muted border border-white/10"
                }`}
                title={`Session spend / cap`}
              >
                ${(spend.cents / 100).toFixed(2)} / ${(spend.cap / 100).toFixed(2)}
              </span>
            )}
            <span className="inline-flex items-center gap-2 rounded-full bg-white/5 px-3 py-1 text-xs text-white">
              <span
                className={`h-2 w-2 rounded-full ${statusColor[status]}`}
                aria-hidden
              />
              {statusLabel[status]}
            </span>
          </div>
        </header>

        <div className="flex flex-1 flex-col items-center justify-center py-8 text-center sm:py-12">
          <RobotFace
            botName={persona.display_name}
            status={status}
            voice={voice}
            botSpeaking={botSpeaking}
            thinking={botThinking}
            paused={paused}
            reconnecting={reconnecting}
            size="large"
          />
          <div className="mt-8 max-w-xl">
            {reconnecting ? (
              <p className="text-base text-amber-200">
                Connection dropped — reconnecting (attempt {reconnectAttempt})…
              </p>
            ) : status === "live" ? (
              <p className="text-lg text-white/90">
                {paused
                  ? "Mic paused. Press Resume to continue."
                  : botSpeaking
                    ? `${persona.display_name} is speaking.`
                    : voice === "active"
                      ? "Hearing you."
                      : botThinking
                        ? `${persona.display_name} is thinking.`
                      : "Listening — speak when you're ready."}
              </p>
            ) : status === "error" ? (
              <p className="text-base text-rose-300">
                Trouble hearing you. Type below — we can keep going.
              </p>
            ) : (
              <p className="text-base text-veas-muted">Connecting…</p>
            )}
            {ttsUnavailable && (
              <p className="mt-1 text-[11px] text-veas-muted">
                Voice playback unavailable — bot turns will show as text.
              </p>
            )}
            {micError && (
              <p className="mt-2 text-xs text-rose-300">Mic error: {micError}</p>
            )}
            <div className="mt-3 flex flex-wrap items-center justify-center gap-3 text-[11px] text-veas-muted">
              <span>Frames: {frameCount} captured / {sentFrameCount} sent / {ackedFrameCount} acked</span>
              <span>RMS: {lastRms.toFixed(3)}</span>
              <span className="inline-flex items-center gap-1">
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    voice === "active" ? "bg-emerald-400" : "bg-slate-500"
                  }`}
                  aria-hidden
                />
                {voice === "active" ? "voice detected" : "silence"}
              </span>
              {botSpeaking && (
                <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-sky-300">
                  bot speaking
                </span>
              )}
              {botThinking && !botSpeaking && voice !== "active" && (
                <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-amber-200">
                  thinking
                </span>
              )}
            </div>
          </div>
        </div>

        {/* ── Debrief overlay ────────────────────────────────────────── */}
        {debriefState !== "idle" && (
          <div className="mt-6 rounded-md border border-amber-500/30 bg-amber-500/10 p-4">
            {debriefState === "waiting" && (
              <div className="flex items-center gap-3">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-amber-400 border-t-transparent" />
                <div>
                  <p className="text-sm font-medium text-amber-200">
                    Putting together your review…
                  </p>
                  <p className="mt-0.5 text-xs text-amber-300/70">
                    The session is being analyzed. This usually takes 15–60 seconds.
                  </p>
                </div>
              </div>
            )}

            {debriefState === "failed" && (
              <div>
                <p className="text-sm font-medium text-rose-300">
                  Review generation failed
                </p>
                {debriefError && (
                  <p className="mt-1 text-xs text-rose-300/70">{debriefError}</p>
                )}
                <p className="mt-2 text-xs text-amber-200/80">
                  Your conversation transcript is still preserved. You can retry
                  the review generation or proceed with the basic summary.
                </p>
                <div className="mt-3 flex items-center gap-3">
                  <button
                    type="button"
                    onClick={handleRetryDebrief}
                    className="rounded-md bg-veas-accent px-4 py-1.5 text-xs font-medium text-veas-bg hover:bg-veas-accent/90"
                  >
                    Retry Debrief
                  </button>
                  {debriefReview && (
                    <button
                      type="button"
                      onClick={() => onEnd(debriefReview)}
                      className="rounded-md border border-white/10 px-4 py-1.5 text-xs text-white hover:border-white/30"
                    >
                      View Basic Summary
                    </button>
                  )}
                </div>
              </div>
            )}

            {debriefState === "done" && (
              <div className="flex items-center gap-3">
                <svg
                  className="h-5 w-5 text-emerald-400"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2}
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M5 13l4 4L19 7"
                  />
                </svg>
                <p className="text-sm font-medium text-emerald-200">
                  Review ready — loading…
                </p>
              </div>
            )}
          </div>
        )}

        <div className="mt-auto border-t border-white/5 pt-4">
          {showTextInput && (
            <div className="mb-3 rounded-md border border-white/5 bg-veas-bg/40 p-3">
              <TextInputFallback
                disabled={status !== "live"}
                className=""
                onSend={(text) => {
                  const ws = wsRef.current;
                  if (ws?.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: "text_input", text }));
                  }
                }}
              />
            </div>
          )}

          <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
            <button
              type="button"
              onClick={handlePauseToggle}
              disabled={status !== "live"}
              className="rounded-md border border-white/10 px-3 py-2 text-sm text-white hover:border-white/30 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {paused ? "Resume" : "Pause"}
            </button>
            <button
              type="button"
              onClick={handleAdvance}
              disabled={status !== "live"}
              className="rounded-md border border-white/10 px-3 py-2 text-sm text-white hover:border-white/30 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Advance
            </button>
            <button
              type="button"
              onClick={handleBackUp}
              disabled={status !== "live"}
              className="rounded-md border border-white/10 px-3 py-2 text-sm text-white hover:border-white/30 disabled:cursor-not-allowed disabled:opacity-50"
              title="Rewind the most recently covered item — 'that's not what I meant'"
            >
              Back up
            </button>
            <button
              type="button"
              onClick={() => setShowTextInput((s) => !s)}
              className="rounded-md border border-white/10 px-3 py-2 text-sm text-white hover:border-white/30"
            >
              {showTextInput ? "Hide text" : "Type"}
            </button>
            <button
              type="button"
              onClick={() => setShowTranscript((s) => !s)}
              className="rounded-md border border-white/10 px-3 py-2 text-sm text-white hover:border-white/30"
            >
              {showTranscript ? "Hide log" : "Log"}
            </button>
            <button
              type="button"
              onClick={handleEnd}
              className="rounded-md bg-rose-500/90 px-3 py-2 text-sm font-medium text-white hover:bg-rose-500"
            >
              Stop
            </button>
          </div>
        </div>

        {showTranscript && (
          <div className="mt-4">
            <h3 className="text-xs uppercase tracking-widest text-veas-muted">
              Transcript
            </h3>
            <div className="mt-2 max-h-72 min-h-[10rem] overflow-y-auto rounded-md border border-white/5 bg-veas-bg/40 p-3 text-sm">
              {transcript.length === 0 ? (
                <p className="text-veas-muted">No turns yet.</p>
              ) : (
                <ul className="space-y-3">
                  {transcript.map((t, i) => (
                    <li key={i} className={`flex ${t.speaker === "user" ? "justify-end" : "justify-start"}`}>
                      <div
                        className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${
                          t.speaker === "user"
                            ? "bg-veas-accent/20 text-white"
                            : "bg-white/5 text-white"
                        }`}
                      >
                        <p className="text-[10px] uppercase tracking-wider text-veas-muted">
                          {t.speaker === "user" ? "you" : persona.display_name}
                        </p>
                        <p className="mt-0.5 whitespace-pre-wrap leading-relaxed">{t.text}</p>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        <div className="mt-3">
          <button
            type="button"
            onClick={() => setShowActivity((s) => !s)}
            className="text-[11px] text-veas-muted hover:text-white"
          >
            {showActivity ? "Hide" : "Show"} activity ({events.length})
          </button>
          {showActivity && (
            <div className="mt-2 max-h-60 overflow-y-auto rounded-md border border-white/5 bg-veas-bg/30 p-3 text-xs">
              {events.length === 0 ? (
                <p className="text-veas-muted">Waiting for events…</p>
              ) : (
                <ul className="space-y-1.5">
                  {events.map((e, i) => (
                    <li
                      key={i}
                      className={`font-mono text-[11px] ${
                        e.kind === "error"
                          ? "text-rose-300"
                          : e.kind === "phase"
                            ? "text-white"
                            : e.kind === "ack"
                              ? "text-veas-muted/60"
                              : "text-slate-200"
                      }`}
                    >
                      <span className="text-veas-muted">
                        {new Date(e.ts).toLocaleTimeString()}
                      </span>{" "}
                      {e.text}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <p className="mt-4 text-[11px] text-veas-muted">
          Session id: <span className="font-mono">{sessionId}</span>
        </p>
      </div>
    </section>
  );
}
