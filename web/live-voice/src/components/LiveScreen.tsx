import { useEffect, useRef, useState } from "react";
import { liveSocketUrl, postConsent, type Persona } from "../api";
import { ConsentGate, type ConsentSelection } from "./ConsentGate";
import { openMic, type MicSession } from "../mic";

interface Props {
  persona: Persona;
  sessionId: string;
  onEnd: () => void;
}

interface PhaseEvent {
  ts: number;
  text: string;
  kind: "phase" | "ack" | "echo" | "info" | "error";
}

type Status = "consent" | "connecting" | "open" | "live" | "closed" | "error";

export function LiveScreen({ persona, sessionId, onEnd }: Props) {
  const [events, setEvents] = useState<PhaseEvent[]>([]);
  const [status, setStatus] = useState<Status>("consent");
  const [consent, setConsent] = useState<ConsentSelection | null>(null);
  const [micError, setMicError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [frameCount, setFrameCount] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicSession | null>(null);

  function pushEvent(text: string, kind: PhaseEvent["kind"] = "info") {
    setEvents((prev) => [...prev, { ts: Date.now(), text, kind }]);
  }

  useEffect(() => {
    if (!consent) return;
    setStatus("connecting");
    const ws = new WebSocket(liveSocketUrl(sessionId));
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus("open");
      pushEvent("WebSocket connected.", "info");
    };
    ws.onclose = () =>
      setStatus((s) => (s === "error" ? s : "closed"));
    ws.onerror = () => {
      setStatus("error");
      pushEvent("WebSocket error.", "error");
    };
    ws.onmessage = (msg) => {
      let text: string;
      let kind: PhaseEvent["kind"] = "info";
      try {
        const parsed = JSON.parse(msg.data);
        if (parsed?.type === "frame_ack") {
          kind = "ack";
          text = `ack: ${parsed.frames} frames / ${parsed.bytes} bytes`;
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
        } else if (parsed?.type === "transcript_error") {
          kind = "error";
          text = `STT error: ${parsed.message ?? "unknown"}`;
        } else if (parsed?.type === "bot_turn") {
          kind = "phase";
          text = `${persona.display_name}: ${parsed.utterance}`;
          // v0 TTS fallback: speak the bot utterance via the browser's
          // SpeechSynthesis API. Real ElevenLabs Flash lands in Sprint 3b.
          try {
            if (typeof window !== "undefined" && "speechSynthesis" in window) {
              const u = new SpeechSynthesisUtterance(parsed.utterance);
              u.rate = 1.0;
              u.pitch = 1.0;
              window.speechSynthesis.cancel();
              window.speechSynthesis.speak(u);
            }
          } catch {
            // ignore — TTS is best-effort
          }
        } else if (parsed?.type === "bot_turn_error") {
          kind = "error";
          text = `Bot turn failed: ${parsed.message ?? "unknown"}`;
        } else {
          text = parsed.label ?? parsed.phase ?? parsed.text ?? JSON.stringify(parsed);
        }
      } catch {
        text = String(msg.data);
      }
      // Cap to last 50 to keep the DOM light.
      setEvents((prev) => [...prev.slice(-49), { ts: Date.now(), text, kind }]);
    };

    return () => {
      try {
        ws.close();
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
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        ws.send(pcm);
        setFrameCount((c) => c + 1);
      },
      onError: (err) => {
        if (!cancelled) setMicError(err.message);
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

  function handleEnd() {
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
    onEnd();
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
    <section className="mx-auto max-w-2xl px-6 py-10">
      <div className="rounded-lg border border-white/5 bg-veas-surface p-6">
        <header className="mb-6 flex items-center justify-between">
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
          <span className="inline-flex items-center gap-2 rounded-full bg-white/5 px-3 py-1 text-xs text-white">
            <span
              className={`h-2 w-2 rounded-full ${statusColor[status]}`}
              aria-hidden
            />
            {statusLabel[status]}
          </span>
        </header>

        <div className="rounded-md border border-white/5 bg-veas-bg/60 px-4 py-3 text-sm">
          {status === "live" ? (
            <p className="text-white/90">
              {paused
                ? "Mic paused. Press Resume to continue."
                : "Listening — speak when you're ready."}
            </p>
          ) : (
            <p className="text-veas-muted">Connecting…</p>
          )}
          {micError && (
            <p className="mt-2 text-xs text-rose-300">Mic error: {micError}</p>
          )}
          <p className="mt-2 text-[11px] text-veas-muted">
            Frames sent: {frameCount}
          </p>
        </div>

        <div className="mt-6 grid grid-cols-3 gap-2">
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
            onClick={handleEnd}
            className="rounded-md bg-rose-500/90 px-3 py-2 text-sm font-medium text-white hover:bg-rose-500"
          >
            Stop for everyone
          </button>
        </div>

        <div className="mt-6">
          <h3 className="text-xs uppercase tracking-widest text-veas-muted">
            Activity
          </h3>
          <div className="mt-2 max-h-72 min-h-[8rem] overflow-y-auto rounded-md border border-white/5 bg-veas-bg/40 p-3 text-sm">
            {events.length === 0 ? (
              <p className="text-veas-muted">Waiting for events…</p>
            ) : (
              <ul className="space-y-2">
                {events.map((e, i) => (
                  <li
                    key={i}
                    className={`font-mono text-xs ${
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
        </div>

        <p className="mt-4 text-[11px] text-veas-muted">
          Session id: <span className="font-mono">{sessionId}</span>
        </p>
      </div>
    </section>
  );
}
