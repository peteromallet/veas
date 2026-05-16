export interface Persona {
  bot_id: string;
  display_name: string;
  description?: string;
}

export interface CreateSessionRequest {
  bot_id: string;
  steering_text: string;
  mode: "open_ended" | "guided";
}

export interface CreateSessionResponse {
  session_id: string;
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

export async function fetchPersonas(): Promise<Persona[]> {
  const res = await fetch("/api/live/personas", {
    headers: { Accept: "application/json" },
  });
  const data = await handle<{ personas?: Persona[] } | Persona[]>(res);
  if (Array.isArray(data)) return data;
  return data.personas ?? [];
}

export async function createSession(
  req: CreateSessionRequest,
): Promise<CreateSessionResponse> {
  const res = await fetch("/api/live/sessions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(req),
  });
  return handle<CreateSessionResponse>(res);
}

export function liveSocketUrl(sessionId: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  // Magic-link JWT (set by ReviewScreen.tsx / future login) is stored on
  // sessionStorage under "veas.live.token". When present, append it as a
  // query param so the WS handler can authenticate the connection.
  let token = "";
  try {
    if (typeof window !== "undefined" && window.sessionStorage) {
      token = window.sessionStorage.getItem("veas.live.token") || "";
    }
  } catch {
    token = "";
  }
  const tokenSuffix = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${proto}//${window.location.host}/ws/live/${encodeURIComponent(sessionId)}${tokenSuffix}`;
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
}

export async function fetchSessionCard(
  sessionId: string,
): Promise<SessionCardPayload> {
  const res = await fetch(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/card`,
    { headers: { Accept: "application/json" } },
  );
  return handle<SessionCardPayload>(res);
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
}

export async function postConsent(
  sessionId: string,
  body: { kind: "solo" | "partner_present"; partner_label?: string },
): Promise<{ ok: boolean }> {
  const res = await fetch(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/consent`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(body),
    },
  );
  return handle<{ ok: boolean }>(res);
}

export async function endSession(sessionId: string): Promise<SessionReview> {
  const res = await fetch(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/end`,
    {
      method: "POST",
      headers: { Accept: "application/json" },
    },
  );
  return handle<SessionReview>(res);
}

export async function saveReview(
  sessionId: string,
  body: {
    keep_items: { item_id: string; summary?: string }[];
    keep_notes: { note_id: string; text: string }[];
  },
): Promise<{ ok: boolean; status: string }> {
  const res = await fetch(
    `/api/live/sessions/${encodeURIComponent(sessionId)}/review/save`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(body),
    },
  );
  return handle<{ ok: boolean; status: string }>(res);
}
