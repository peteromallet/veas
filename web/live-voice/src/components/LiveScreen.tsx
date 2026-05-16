import { useEffect, useRef, useState } from "react";
import { liveSocketUrl, postConsent, type Persona } from "../api";
import { ConsentGate, type ConsentSelection } from "./ConsentGate";
import { openMic, type MicSession, type VoiceState } from "../mic";

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

function TextInputFallback({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (text: string) => void;
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
    <form onSubmit={handleSubmit} className="mt-4 flex items-center gap-2">
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

export function LiveScreen({ persona, sessionId, onEnd }: Props) {
  const [events, setEvents] = useState<PhaseEvent[]>([]);
  const [status, setStatus] = useState<Status>("consent");
  const [consent, setConsent] = useState<ConsentSelection | null>(null);
  const [micError, setMicError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [frameCount, setFrameCount] = useState(0);
  const [voice, setVoice] = useState<VoiceState>("silent");
  const [botSpeaking, setBotSpeaking] = useState(false);
  const [ttsUnavailable, setTtsUnavailable] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<MicSession | null>(null);
  const botSpeakingRef = useRef(false);
  const lastUserActivityRef = useRef<number>(0);
  const silenceTimerRef = useRef<number | null>(null);

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
              // If utterance hasn't started speaking within 250ms,
              // assume TTS is unavailable and degrade to text-only.
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
          if (ttsUnavailable && !text.endsWith("(voice unavailable)")) {
            text = `${text} (voice unavailable)`;
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

  // 10s silence fallback: after a quiet stretch with no voice_active
  // OR transcript_final in 10 seconds, ping the backend so the bot can
  // open a gentle "are you still there?" turn.
  useEffect(() => {
    if (status !== "live") return;
    silenceTimerRef.current = window.setInterval(() => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (botSpeakingRef.current) return;
      const idleMs = Date.now() - (lastUserActivityRef.current || Date.now());
      if (idleMs >= 10_000) {
        ws.send(JSON.stringify({ type: "silence_prompt", idle_ms: idleMs }));
        lastUserActivityRef.current = Date.now(); // reset so we don't spam
      }
    }, 2000);
    return () => {
      if (silenceTimerRef.current !== null) {
        clearInterval(silenceTimerRef.current);
        silenceTimerRef.current = null;
      }
    };
  }, [status]);

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
      onVoiceState: (state) => {
        if (cancelled) return;
        setVoice(state);
        lastUserActivityRef.current = Date.now();
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
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
          {reconnecting ? (
            <p className="text-amber-200">
              Connection dropped — reconnecting (attempt {reconnectAttempt})…
            </p>
          ) : status === "live" ? (
            <p className="text-white/90">
              {paused
                ? "Mic paused. Press Resume to continue."
                : "Listening — speak when you're ready."}
            </p>
          ) : status === "error" ? (
            <p className="text-rose-300">
              Trouble hearing you. Type below — we can keep going.
            </p>
          ) : (
            <p className="text-veas-muted">Connecting…</p>
          )}
          {ttsUnavailable && (
            <p className="mt-1 text-[11px] text-veas-muted">
              Voice playback unavailable — bot turns will show as text.
            </p>
          )}
          {micError && (
            <p className="mt-2 text-xs text-rose-300">Mic error: {micError}</p>
          )}
          <div className="mt-2 flex items-center gap-3 text-[11px] text-veas-muted">
            <span>Frames sent: {frameCount}</span>
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
          </div>
        </div>

        <TextInputFallback
          disabled={status !== "live"}
          onSend={(text) => {
            const ws = wsRef.current;
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: "text_input", text }));
            }
          }}
        />

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
