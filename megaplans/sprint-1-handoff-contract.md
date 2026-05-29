# Sprint 1 Hand-off Contract — Live-Voice Identity & Discovery

> Finalised after Sprint 1 implementation (T1–T10).
> Reviewers: Sprint 2 executor, Discord gateway maintainer.

## (a) Conversation ownership

### Where ownership is set

Ownership is written at session creation in
`POST /api/live/sessions` → `app/routers/live_voice.py` `create_session()`.

The `INSERT INTO mediator.conversations` row is stamped with:

| Column | Source |
|--------|--------|
| `user_id` | `user_id: UUID = Depends(get_current_user)` — the JWT-authenticated caller |
| `partner_user_id` | Resolved via `_resolve_live_partner_user_id(pool, user_id, bot_id)` — the *other* dyad member for the caller's bot binding, or `NULL` for solo bindings |

**Sprint 2 rule**: when `create_conversation_plan` (or any future Discord-side session creation) writes a conversations row, it MUST set `user_id = ctx.user.id` (the Discord-authenticated user).  `partner_user_id` is optional — `NULL` means a solo session.

### Where ownership is checked

The helper `_require_ownership(pool, session_id, user_id)` in
`app/routers/live_voice.py:178` runs:

```sql
SELECT id, user_id, partner_user_id, status
FROM mediator.conversations
WHERE id = $1
```

It compares `row['user_id'] == user_id` OR
(`row['partner_user_id'] IS NOT NULL AND row['partner_user_id'] == user_id`).

* `None` row → 404
* Neither matches → 403
* Otherwise → returns `dict(row)`

This helper is **always** gated behind the env flag:

```
LIVE_VOICE_AUTH_ENABLED=true
```

When `LIVE_VOICE_AUTH_ENABLED=false` (the default), ownership checks are
skipped entirely — the caller resolves to a configured test user id
(via `get_current_user`).  This is the **dev fallback** and is
intentionally identical on both HTTP and WS channels.

### Guarded HTTP routes

All of these call `_require_ownership` conditionally
(`if get_settings().live_voice_auth_enabled: …`):

| Method | Path | Line ~ |
|--------|------|--------|
| GET | `/api/live/sessions/{id}/card` | 592 |
| GET | `/api/live/sessions/{id}` | 1634 |
| GET | `/api/live/sessions/{id}/review` | 1001 |
| POST | `/api/live/sessions/{id}/end` | 916 |
| POST | `/api/live/sessions/{id}/review/save` | 1118 |
| POST | `/api/live/sessions/{id}/prep/retry` | 700 |
| POST | `/api/live/sessions/{id}/debrief/retry` | 774 |
| POST | `/api/live/sessions/{id}/consent` | 850 |
| GET | `/api/live/sessions/{id}/tts/{turn_id}` | 1591 |

### Guarded WebSocket

The WS handler at `/ws/live/{session_id}` enforces ownership
*immediately after `accept()` and before any phase frames or status UPDATE*:

1. Convert `authed_user_id` (str from JWT) → `UUID(authed_user_id)`.
   **Do not compare the str directly** — asyncpg returns `uuid.UUID` objects,
   and `UUID.__eq__(str)` returns `NotImplemented`, silently rejecting every
   owner.

2. Fetch `SELECT user_id, partner_user_id FROM mediator.conversations WHERE id=$1::uuid`.

3. If row is `None` or neither column matches → `await websocket.close(code=4003)` and return.

When the user is authenticated, the status UPDATE is also scoped:

```sql
UPDATE mediator.conversations
SET status = 'active', started_at = COALESCE(started_at, now())
WHERE id = $1::uuid
  AND status IN ('ready', 'live')
  AND (user_id = $2 OR partner_user_id = $2)
```

In dev fallback (auth off, `authed_user_id` is `None`), the scoping
predicate is omitted and the original single-parameter UPDATE is used.

The WS auth requirement is:

```python
require_auth = (
    get_settings().live_voice_auth_enabled
    or os.environ.get("LIVE_VOICE_WS_AUTH_REQUIRED") == "1"
)
```

So the WS can be hardened independently of HTTP via `LIVE_VOICE_WS_AUTH_REQUIRED=1`.

---

## (b) `GET /api/live/sessions` response shape

The endpoint lives at `app/routers/live_voice.py:369` `list_sessions()`.

**Query** (scoped to caller):

```sql
SELECT id, status, bot_id, prep_summary, steering_text, created_at,
       (SELECT COUNT(*) FROM mediator.conversation_items ci
        WHERE ci.conversation_id = c.id) AS item_count
FROM mediator.conversations c
WHERE (user_id = $1 OR partner_user_id = $1)
  [AND status = $2]
ORDER BY created_at DESC
```

**Response**:

```json
{
  "sessions": [
    {
      "id": "<uuid>",
      "status": "active",
      "topic_label": "Tante Rosi",
      "prep_summary": "… or null",
      "steering_text": "… or null",
      "item_count": 5,
      "created_at": "2026-05-29T…"
    }
  ]
}
```

### `topic_label` derivation

`topic_label` is **not** a database column — it is derived in Python:

```python
bot_spec = BOT_SPECS.get(row["bot_id"])
topic_label = bot_spec.display_name if bot_spec else row["bot_id"]
```

If `bot_id` is not found in `BOT_SPECS`, the raw `bot_id` string is used
as a fallback.  The frontend field name is `topic_label` for UX clarity.

The `mediator.conversations` table has no `topic_label` column
(verified against migration `0042_live_conversations.sql`).

### Status canonicalisation

Legacy statuses are normalised:
- `prepping` → `preparing`
- `live` → `active`
- `synthesizing` → `debriefing`
- `synthesized` / `ended` → `completed`

Canonical statuses pass through unchanged.

---

## (c) Identity parity: web auth == Discord auth

The web auth `user_id` is extracted from the JWT `sub` claim:

```python
# app/services/auth/jwt.py
claims = live_jwt.verify(token)
return UUID(claims.user_id)
```

The JWT is minted against `mediator.users.id` (a UUID column).

The Discord gateway authenticates via `ctx.user.id` (Discord snowflake) and
resolves it to `mediator.users.id` through the user-identity mapping.

In Sprint 2, `create_conversation_plan` will write
`user_id = ctx.user.id` (the `mediator.users.id` UUID), which is the **same**
value that `get_current_user()` returns for the web caller.  The ownership
check `row['user_id'] == user_id` will therefore work identically regardless
of whether the call came from the web frontend or the Discord gateway.

---

## (d) Known gap: unguarded operator endpoints

The following endpoints are intentionally **not** guarded by
`_require_ownership` and remain accessible without per-session auth:

| Endpoint | Reason |
|----------|--------|
| `POST /api/live/sessions/{id}/replay/{turn_id}` | Operator debug — re-runs turn calling logic |
| `GET /api/live/ops/metrics` | Operator metrics snapshot |
| `GET /api/live/ops/sessions/{id}/debug` | Operator debug introspection |

A future operator-auth mechanism (e.g., an `ADMIN_PASSWORD`-protected
token or a separate operator JWKS) should gate these before production
exposure.  This is tracked as a Sprint-Next follow-up ticket.

---

## Summary for Sprint 2

1. **Insert conversations with `user_id = ctx.user.id`** — this is the
   identity key for all ownership checks.

2. **`partner_user_id` is optional** — `NULL` means a solo session, and
   the ownership guard correctly handles `partner_user_id IS NOT NULL`
   before comparison (no crash on solo sessions).

3. **Both HTTP and WS gates are behind `LIVE_VOICE_AUTH_ENABLED`** — keep
   dev fallback identical across channels.

4. **Do not add `topic_label` as a DB column** — derive it in the app
   layer from `BOT_SPECS`.

5. **The `LIVE_VOICE_JWT_SECRET` must match** between the Discord gateway
   (which mints JWTs for magic-link DMs) and the web backend (which
   verifies them).
