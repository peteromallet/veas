import { useState } from "react";

interface Props {
  onAuthed: () => void;
}

type Step = "input_discord_id" | "code_sent" | "verifying";

/**
 * Minimal magic-link login component.
 *
 * 1. User enters their Discord ID → POST /api/auth/discord-magic-link/request
 * 2. If ``ok: true`` the backend DMs a 6-digit code (or logs it in dev).
 * 3. User enters the code → POST /api/auth/discord-magic-link/verify
 * 4. On ``{ok: true, token}`` the token is written to
 *    ``sessionStorage['veas.live.token']`` and ``onAuthed()`` is called.
 */
export function MagicLinkLogin({ onAuthed }: Props) {
  const [step, setStep] = useState<Step>("input_discord_id");
  const [discordId, setDiscordId] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleRequest() {
    const trimmed = discordId.trim();
    if (!trimmed || !/^\d+$/.test(trimmed)) {
      setError("Enter a valid Discord user ID (numbers only).");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const res = await fetch("/api/auth/discord-magic-link/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ discord_id: trimmed }),
      });
      const body = (await res.json()) as {
        ok: boolean;
        ttl_minutes?: number;
        dispatched?: boolean;
      };
      if (!body.ok) {
        setError("Could not send a code right now. Try again later.");
        return;
      }
      setStep("code_sent");
    } catch {
      setError("Network error. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  async function handleVerify() {
    const trimmedCode = code.trim();
    if (!trimmedCode || !/^\d{4,8}$/.test(trimmedCode)) {
      setError("Enter the numeric code you received.");
      return;
    }
    setError(null);
    setBusy(true);
    setStep("verifying");
    try {
      const res = await fetch("/api/auth/discord-magic-link/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ discord_id: discordId.trim(), code: trimmedCode }),
      });
      const body = (await res.json()) as {
        ok: boolean;
        token?: string;
        reason?: string;
      };
      if (!body.ok || !body.token) {
        setStep("code_sent");
        setError(
          body.reason === "bad_code"
            ? "Wrong code. Please try again."
            : body.reason === "expired"
              ? "Code expired. Request a new one."
              : body.reason === "too_many_attempts"
                ? "Too many tries. Request a new code."
                : "Verification failed. Please try again.",
        );
        return;
      }
      try {
        window.sessionStorage.setItem("veas.live.token", body.token);
      } catch {
        setStep("code_sent");
        setError("Could not store credentials. Check your browser settings.");
        return;
      }
      onAuthed();
    } catch {
      setStep("code_sent");
      setError("Network error. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent, action: () => void) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (!busy) action();
    }
  }

  return (
    <section className="mx-auto max-w-md px-6 py-10">
      <header className="mb-6">
        <h2 className="text-xl font-semibold text-white">Log in</h2>
        <p className="mt-1 text-sm text-veas-muted">
          We'll DM a one-time code to your Discord account.
        </p>
      </header>

      {error && (
        <div className="mb-4 rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
          {error}
        </div>
      )}

      {step === "input_discord_id" && (
        <div>
          <label className="block text-sm text-veas-muted">
            Discord user ID
            <input
              type="text"
              inputMode="numeric"
              value={discordId}
              onChange={(e) => setDiscordId(e.target.value)}
              onKeyDown={(e) => handleKeyDown(e, handleRequest)}
              placeholder="e.g. 123456789012345678"
              disabled={busy}
              className="mt-2 w-full rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-sm text-white placeholder:text-veas-muted focus:border-veas-accent focus:outline-none focus:ring-1 focus:ring-veas-accent/60 disabled:opacity-50"
            />
          </label>
          <button
            type="button"
            onClick={handleRequest}
            disabled={busy}
            className="mt-4 w-full rounded-md bg-veas-accent px-4 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "Sending…" : "Send code"}
          </button>
        </div>
      )}

      {(step === "code_sent" || step === "verifying") && (
        <div>
          <p className="mb-4 text-sm text-veas-muted">
            A 6-digit code was DM'd to your Discord account. It expires in 10
            minutes.
          </p>
          <label className="block text-sm text-veas-muted">
            Verification code
            <input
              type="text"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => handleKeyDown(e, handleVerify)}
              placeholder="000000"
              maxLength={8}
              disabled={busy}
              className="mt-2 w-full rounded-md border border-white/10 bg-veas-bg/60 px-3 py-2 text-sm text-white placeholder:text-veas-muted focus:border-veas-accent focus:outline-none focus:ring-1 focus:ring-veas-accent/60 disabled:opacity-50"
            />
          </label>
          <div className="mt-4 flex gap-3">
            <button
              type="button"
              onClick={() => {
                setStep("input_discord_id");
                setCode("");
                setError(null);
              }}
              disabled={busy}
              className="rounded-md px-4 py-2 text-sm text-veas-muted hover:text-white disabled:opacity-50"
            >
              Back
            </button>
            <button
              type="button"
              onClick={handleVerify}
              disabled={busy || code.trim().length === 0}
              className="flex-1 rounded-md bg-veas-accent px-4 py-2 text-sm font-medium text-veas-bg transition hover:bg-veas-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? "Verifying…" : "Verify"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
