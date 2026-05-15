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
  return `${proto}//${window.location.host}/ws/live/${encodeURIComponent(sessionId)}`;
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
