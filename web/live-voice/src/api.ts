export interface Persona {
  bot_id: string;
  display_name: string;
  description?: string;
}

export interface CreateSessionRequest {
  bot_id: string;
  steering_text: string;
  mode: "open_ended" | "guided";
  skip_prep?: boolean;
}

export interface CreateSessionResponse {
  session_id: string;
  status?: string;
  prep_pending?: boolean;
}

export class LiveApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 503) {
    throw new LiveApiError(
      "Live conversations are not yet available on this deployment.",
      503,
    );
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body === "object" && "detail" in body) {
        const raw = (body as { detail: unknown }).detail;
        if (typeof raw === "string") {
          detail = raw;
        } else if (Array.isArray(raw)) {
          detail = raw
            .map((d: unknown) =>
              d && typeof d === "object" && "msg" in d
                ? String((d as { msg: unknown }).msg)
                : JSON.stringify(d),
            )
            .join("; ");
        } else {
          detail = JSON.stringify(raw);
        }
      }
    } catch {
      // ignore
    }
    throw new LiveApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

// ── Auth helpers ─────────────────────────────────────────────────────────────

/**
 * Read the magic-link JWT from sessionStorage.
 *
 * Stored under ``veas.live.token`` by ReviewScreen (or future login flow).
 * Returns ``null`` when no token is present — callers using ``authFetch``
 * will then make unauthenticated requests (dev fallback).
 */
export function getAuthToken(): string | null {
  try {
    if (typeof window !== "undefined" && window.sessionStorage) {
      return window.sessionStorage.getItem("veas.live.token") || null;
    }
  } catch {
    // sessionStorage unavailable (SSR / sandbox).
  }
  return null;
}

/**
 * Thin wrapper around ``fetch`` that injects the ``Authorization: Bearer``
 * header when ``getAuthToken()`` returns a token, then delegates to ``handle``.
 *
 * All live-voice API calls route through this function so the backend can
 * enforce conversation ownership when ``LIVE_VOICE_AUTH_ENABLED`` is true.
 */
export async function authFetch<T>(
  url: string,
  opts?: RequestInit,
): Promise<T> {
  const token = getAuthToken();
  const headers = new Headers(opts?.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  const res = await fetch(url, { ...opts, headers });
  return handle<T>(res);
}

// ── Personas ─────────────────────────────────────────────────────────────────

export async function fetchPersonas(): Promise<Persona[]> {
  const data = await authFetch<{ personas?: Persona[] } | Persona[]>(
    "/api/live/personas",
  );
  if (Array.isArray(data)) return data;
  return data.personas ?? [];
}

// ── Sessions ─────────────────────────────────────────────────────────────────

export async function createSession(
  req: CreateSessionRequest,
): Promise<CreateSessionResponse> {
  return authFetch<CreateSessionResponse>("/api/live/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export interface SessionSummary {
  id: string;
  status: string;
  bot_id: string;
  topic_label: string;
  prep_summary: string | null;
  steering_text: string | null;
  item_count: number;
  created_at: string;
}

/**
 * Fetch the caller's sessions (own + partner), newest first.
 *
 * Optionally filtered by ``status`` (canonical or legacy — the backend
 * normalises internally).
 */
export async function fetchSessions(
  status?: string,
): Promise<SessionSummary[]> {
  let url = "/api/live/sessions";
  if (status) {
    url += `?status=${encodeURIComponent(status)}`;
  }
  const data = await authFetch<{ sessions: SessionSummary[] }>(url);
  return data.sessions ?? [];
}

// ── WebSocket URL ────────────────────────────────────────────────────────────

export function liveSocketUrl(sessionId: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  // Use getAuthToken so the WS handler can authenticate the connection.
  const token = getAuthToken();
  const tokenSuffix = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${proto}//${window.location.host}/ws/live/${encodeURIComponent(sessionId)}${tokenSuffix}`;
}

// ── Canonical live-conversation status types ───────────────────────────────

/**
 * Canonical statuses used by the live-conversation API (post-Sprint-5).
 * These are the values the frontend should expect after status normalization.
 */
export type CanonicalStatus =
  | "preparing"
  | "ready"
  | "active"
  | "debriefing"
  | "review_pending"
  | "completed"
  | "prep_failed"
  | "debrief_failed";

/**
 * Legacy statuses that may appear during migration rollout.
 * The application layer normalizes these to canonical values, but
 * type guards handle both for backward compatibility.
 */
export type LegacyStatus =
  | "prepping"
  | "live"
  | "synthesized"
  | "ended"
  | "synthesizing";

/**
 * Union of all possible status strings the API may return.
 */
export type LiveStatus = CanonicalStatus | LegacyStatus;

/**
 * Map a status string to its canonical form.
 * Canonical values pass through unchanged.
 */
export function canonicalizeStatus(status: string): CanonicalStatus {
  const LEGACY_MAP: Record<string, CanonicalStatus> = {
    prepping: "preparing",
    live: "active",
    synthesized: "completed",
    ended: "completed",
    synthesizing: "debriefing",
  };
  const mapped = LEGACY_MAP[status];
  if (mapped) return mapped;
  // Known canonical values pass through; unknown values are cast defensively.
  return status as CanonicalStatus;
}

/**
 * Type guard: is the status one of the canonical active/pending statuses?
 */
export function isActiveStatus(status: string): boolean {
  const canonical = canonicalizeStatus(status);
  return ["preparing", "ready", "active", "review_pending"].includes(canonical);
}

/**
 * Type guard: is the status a terminal/completed status?
 */
export function isCompletedStatus(status: string): boolean {
  const canonical = canonicalizeStatus(status);
  return canonical === "completed";
}

/**
 * Type guard: is the status a failed status?
 */
export function isFailedStatus(status: string): boolean {
  const canonical = canonicalizeStatus(status);
  return canonical === "prep_failed" || canonical === "debrief_failed";
}

export type CoverageEvidence =
  | "explicit_answer"
  | "emotional_shift"
  | "concrete_decision"
  | "blocker_named";

export interface AgendaItemCard {
  id: string;
  title: string;
  intent: string | null;
  ask: string | null;
  done_when: string | null;
  kind: "planned" | "dynamic" | "thread";
  priority: "must" | "should" | "optional";
  speaker_scope: "primary" | "partner" | "both";
  coverage_evidence_required: CoverageEvidence;
  theme: { slug: string; label: string } | null;
}

export interface SessionCardPayload {
  session_id: string;
  bot_id: string;
  mode: string;
  status: string;
  prep_summary: string | null;
  current_item_id: string | null;
  items: AgendaItemCard[];
  /** Present when session is in prep_failed state (Sprint 5). */
  failure_reason?: string | null;
}

export async function fetchSessionCard(
  sessionId: string,
): Promise<SessionCardPayload> {
  return authFetch<SessionCardPayload>(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/card`,
  );
}

export interface ReviewItem {
  item_id: string;
  title: string;
  summary?: string;
  evidence_quote?: string;
  priority?: string;
  intent?: string;
}

export interface ReviewNote {
  note_id: string;
  kind: string;
  text: string;
}

export interface DebriefFailedMeta {
  reason?: string;
  error?: string;
  failed_at?: string;
}

export interface SessionReview {
  session_id: string;
  bot_id?: string;
  status?: string;
  started_at?: string | null;
  ended_at?: string | null;
  prep_summary?: string | null;
  what_heard: string[];
  what_decided: ReviewItem[];
  still_open: ReviewItem[];
  what_to_remember: ReviewNote[];
  is_empty: boolean;
  /** True when the session is in 'debriefing' status (Sprint 5). */
  debrief_pending?: boolean;
  /** Present when debrief has failed (Sprint 5). */
  debrief_failed?: DebriefFailedMeta;
  /** Debrief artifact payload when debrief succeeded (Sprint 5). */
  live_debrief?: unknown;
  /** Review summary text extracted from review_summary artifact (Sprint 5). */
  review_summary?: string;
}

export async function postConsent(
  sessionId: string,
  body: { kind: "solo" | "partner_present"; partner_label?: string },
): Promise<{ ok: boolean }> {
  return authFetch<{ ok: boolean }>(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/consent`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

export async function endSession(sessionId: string): Promise<SessionReview> {
  return authFetch<SessionReview>(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/end`,
    { method: "POST" },
  );
}

export async function saveReview(
  sessionId: string,
  body: {
    keep_items: { item_id: string; summary?: string }[];
    keep_notes: { note_id: string; text: string }[];
  },
): Promise<{ ok: boolean; status: string }> {
  return authFetch<{ ok: boolean; status: string }>(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/review/save`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

// ── Retry & review helpers (Sprint 5) ──────────────────────────────────────

export interface RetryPrepResponse {
  session_id: string;
  status: "preparing";
  prep_pending: true;
}

/**
 * Retry a failed live-prep session.
 * Only valid when the session is in 'prep_failed' status (409 otherwise).
 */
export async function retryPrep(
  sessionId: string,
): Promise<RetryPrepResponse> {
  return authFetch<RetryPrepResponse>(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/prep/retry`,
    { method: "POST" },
  );
}

export interface RetryDebriefResponse {
  session_id: string;
  status: "debriefing";
  debrief_pending: true;
}

/**
 * Retry a failed live-debrief session.
 * Only valid when the session is in 'debrief_failed' status (409 otherwise).
 */
export async function retryDebrief(
  sessionId: string,
): Promise<RetryDebriefResponse> {
  return authFetch<RetryDebriefResponse>(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/debrief/retry`,
    { method: "POST" },
  );
}

/**
 * Fetch the deterministic review for a session, enriched with debrief
 * artifacts and failure metadata when available.
 *
 * Never blocks waiting for debrief — always returns the deterministic
 * synthesis first.  When debrief is in progress, ``debrief_pending`` is
 * set to ``true``.  When debrief has failed, ``debrief_failed`` metadata
 * is surfaced.  When debrief succeeded, ``live_debrief`` and optional
 * ``review_summary`` fields are included.
 */
export async function fetchReview(
  sessionId: string,
): Promise<SessionReview> {
  return authFetch<SessionReview>(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/review`,
  );
}
