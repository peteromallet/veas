"""Deterministic generator for the FAIR retrieval-eval corpus + golden set.

Run:  python -m eval.retrieval._generate_fixtures

Why this exists
---------------
The first synthetic corpus was rigged against the keyword baseline: every
paraphrase / cross-thread golden query was hand-built to share ZERO character
substrings with its target, pinning the ILIKE baseline at 0% on those types and
inflating the apparent semantic lift. This generator rebuilds the fixtures to
be FAIR:

  * Paraphrase / cross-thread queries share SOME words with their targets (as
    real users do), so the baseline can score nonzero. The semantic win must
    come from genuine meaning-match, not an artificial zero-overlap floor.
  * Harder distractors: near-duplicate incidents and same-word-different-meaning
    messages that are lexically similar to queries but are NOT the answer.
  * Scale: a few hundred messages across many threads/topics, ~60-80 golden
    cases, while staying terse and emotionally-charged dyadic messaging.
  * Determinism preserved: stable ids, strictly-increasing unique timestamps,
    no randomness.

It emits corpus.yaml and golden_set.yaml and prints an overlap audit so the
fairness property is verifiable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Message authoring.
#
# Each thread is a list of (sender, content[, media_analysis]) tuples. Threads
# are grouped under a topic. We assign ids m001.. in emission order and unique,
# strictly increasing timestamps (one per message) so (sent_at DESC, id DESC)
# ordering stays deterministic.
#
# Dyads vary per thread to feel like real one-on-one messaging.
# ---------------------------------------------------------------------------

A, B = "Alice", "Bob"          # work dyad
C, D = "Maya", "Devs"          # second work dyad (Atlas)
E, F = "Sam", "Jordan"         # personal dyad (partners)
G, H = "Priya", "Tariq"        # roommates / household

# threads: list of dicts {thread_id, topic_id, dyad:(s1,s2), msgs:[...]}
THREADS: list[dict] = []


def thread(thread_id, topic_id, dyad, msgs):
    THREADS.append(
        {"thread_id": thread_id, "topic_id": topic_id, "dyad": dyad, "msgs": msgs}
    )


# ===========================================================================
# TOPIC: project_nexus  (work) — threads: kickoff, bugs
# ===========================================================================

thread(
    "thread_nexus_kickoff",
    "topic_project_nexus",
    (A, B),
    [
        "Hey Bob, can we discuss the Nexus project timeline?",
        "Sure, I've got the latest sprint review ready.",
        "Great. How's the authentication module coming along?",
        "The OAuth2 login integration is mostly done. Just need to handle token refresh edge cases.",
        "What about the database migration scripts?",
        "The migration scripts are blocked on the schema review. Jenkins flagged three compatibility issues.",
        "fine.",
        "I've been thinking about the caching layer. A Redis cluster might be overkill for our scale.",
        "We should benchmark the cache first. I asked DevOps to provision a test environment.",
        "The API rate limiting is causing issues in staging. Users hitting 429s during peak.",
        "That's the same rate limit thing Sarah reported last sprint. Did we ever raise the burst limit?",
        "I bumped the rate limit from 50 to 75 requests per minute. Should be enough.",
        "I told you so.",
        ("Check this out", {
            "explanation": "Screenshot of a null pointer exception in the Nexus authentication module at line 342 of AuthService.java during the 2PM load test",
        }),
        "The null pointer from earlier - fixed it. Was a missing null guard on the session factory injection path.",
        ("CI pipeline is green again. The migration scripts passed schema review.", {
            "explanation": "Jenkins build #1847 passed all three compatibility checks after the PostGIS extension version was bumped to 3.4",
        }),
        # DISTRACTOR: 'rate' different meaning (approval rating, not API rate limiting)
        "By the way, the customer satisfaction rate jumped after the new onboarding flow.",
        # DISTRACTOR: 'token' different meaning (a small gift, not auth token)
        "I left a little thank-you token on your desk for covering my on-call.",
    ],
)

thread(
    "thread_nexus_bugs",
    "topic_project_nexus",
    (A, B),
    [
        "Found a regression in the payment processor. It's duplicating transaction IDs since the last deploy.",
        "That's bad. Is it affecting production?",
        "Only staging so far. The idempotency key check is failing under concurrent requests.",
        "Same root cause as the session token bug from March?",
        "Related but not identical. This one's in the PostgreSQL advisory lock layer.",
        "Push a hotfix for the payment bug. I'll review when I'm back from lunch.",
        "The payment processor is duplicating transaction IDs after the deploy. Root cause is an idempotency key collision.",
        "We already discussed the duplicate transactions. Did you push the hotfix?",
        "sure",
        "Hotfix deployed. Monitoring the payment processor error rate now.",
        "Rate limiting tweak helped. 429 count dropped 80% in the last hour.",
        # NEAR-DUPLICATE distractor incident: a DIFFERENT duplication bug (emails, not payments)
        "Separate issue: the notification service is duplicating emails to users on retry. Not the payment one.",
        "Right, the email duplication is the retry queue, totally different from the transaction dup.",
        # DISTRACTOR: 'crash' software vs later 'crash' car
        "The mobile app crashes on startup if the auth token is expired. Stack trace attached.",
        ("here", {
            "summary": "Crash report: NullPointerException in TokenManager.refresh() on cold start when the cached refresh token is null",
        }),
    ],
)

# ===========================================================================
# TOPIC: project_atlas (work, second project + dyad) — threads: planning, incident
# ===========================================================================

thread(
    "thread_atlas_planning",
    "topic_project_atlas",
    (C, D),
    [
        "Maya here. Are we still targeting the Atlas launch for end of quarter?",
        "Yeah, but the data pipeline rewrite is the long pole. Ingestion is slower than spec.",
        "How slow? We promised sub-second query latency to the customer.",
        "Right now p95 latency is around 4 seconds under load. The join on the events table is the bottleneck.",
        "Can we add an index on the events table to speed up that join?",
        "Already tried. The index helped reads but writes got slower. Classic tradeoff.",
        "What about partitioning the events table by day?",
        "Daily partitioning is promising. I'll prototype it this week.",
        "Good. Latency under a second is the hard requirement, everything else can slip.",
        "ok",
        # paraphrase target with overlap: 'team morale' / 'burnt out'
        "Honestly the team is burnt out. Three weekends in a row on Atlas is not sustainable.",
        "I hear you. Let me push the deadline conversation up with leadership.",
        ("look at this", {
            "description": "Grafana dashboard screenshot showing Atlas query p95 latency spiking to 6.2 seconds at 14:00 during the nightly batch ingestion window",
        }),
        # DISTRACTOR: same words 'latency' but about video call, not DB
        "Side note: the video call latency in standups is awful, audio keeps dropping.",
        # near-duplicate: another 'index' but about a book index, not DB
        "Unrelated, did you finish the index for the design doc? The appendix references are off.",
    ],
)

thread(
    "thread_atlas_incident",
    "topic_project_atlas",
    (C, D),
    [
        "Atlas is down. Customers are getting 503s on the dashboard.",
        "On it. Looks like the ingestion worker pool is saturated again.",
        "Is this the same outage as last Tuesday or a new one?",
        "New one. Last Tuesday was a bad deploy; this is the worker pool exhausting connections.",
        "Roll back or scale up?",
        "Scaling the worker pool up to 12 now. Should relieve the connection pressure.",
        "Status?",
        "Recovering. 503 rate down to near zero. Postmortem tomorrow.",
        "Thanks. That was stressful.",
        ("incident timeline", {
            "summary": "Atlas outage postmortem: the ingestion worker pool exhausted the database connection pool at 09:14, causing cascading 503 errors for 22 minutes until the pool was scaled from 6 to 12 workers",
        }),
        # DISTRACTOR: 'down' different meaning (feeling down) + emotional
        "Honestly after that outage I'm feeling pretty down about this whole launch.",
        # near-duplicate distractor: a connection issue but networking, not DB pool
        "Quick heads up, my home internet connection keeps dropping, might miss the sync.",
    ],
)

# ===========================================================================
# TOPIC: weekend_plans (personal) — threads: hike, dinner
# ===========================================================================

thread(
    "thread_weekend_hike",
    "topic_weekend_plans",
    (A, B),
    [
        "Want to do the Blue Ridge trail this Saturday?",
        "Weather looks good. 72 and sunny according to the forecast.",
        "I'll pack the sandwiches. You bring the water filters.",
        "Deal. What time should we start?",
        "6 AM. Beat the crowds and the heat.",
        "Oof. That's early. But fine.",
        "Don't forget your sunscreen this time. Remember the sunburn at Shenandoah.",
        "How could I forget. I looked like a tomato for a week.",
        "Blue Ridge trailhead, 6 AM Saturday, I'll bring food you bring water.",
        "Got it. I'll also bring the first aid kit just in case.",
        "Good call. I almost forgot about that twisted ankle last time.",
        "Yeah that was scary. Took us two hours to get back to the car.",
        "So for the Blue Ridge hike - I'll handle food, you handle water and first aid. 6 AM sharp.",
        "Confirmed. I threw in some trail mix too.",
        "Forecast updated - slight chance of rain Saturday morning. Should I bring ponchos?",
        "Yes, ponchos are smart. The trail gets muddy near the second ridge.",
        ("See attached", {
            "explanation": "Photo of the Blue Ridge trailhead parking lot completely full with cars spilling onto the highway shoulder at 6:45 AM on Saturday",
        }),
        # DISTRACTOR: 'trail' different meaning (audit trail) - cross-topic lexical trap
        "Random work thought: we should add an audit trail to the Nexus login flow.",
        # near-duplicate hike planning, but a DIFFERENT trail (wrong answer for Blue Ridge queries)
        "Or we could do the Whitetail Ridge loop instead, it's shorter and dog-friendly.",
    ],
)

thread(
    "thread_weekend_dinner",
    "topic_weekend_plans",
    (A, B),
    [
        "After the hike, want to grab dinner at that new Italian place?",
        "Luigi's? I heard their osso buco is incredible.",
        "Yes! And they have that rooftop seating. Perfect for sunset.",
        "Should we invite Carol? She's been wanting to try it.",
        "Maybe next time. I want to keep this dinner just us.",
        "Reservation is for 7:30 PM. They said the rooftop fills up fast.",
        "Perfect timing. We should be back from Blue Ridge by 4 PM latest.",
        "Osso buco at Luigi's. Rooftop at 7:30. After the Blue Ridge hike.",
        "Don't forget a jacket. The rooftop gets chilly after dark.",
        "Already on my checklist. Along with the water filters for the morning hike.",
        ("Look at the menu", {
            "description": "PDF menu for Luigi's Ristorante showing the osso buco is a Thursday special, not available on Saturday, with a handwritten note that the mushroom risotto is the chef's recommendation",
        }),
        "Just saw the menu you sent. If the osso buco isn't available Saturday, the mushroom risotto looks amazing.",
        # NEAR-DUPLICATE distractor: a DIFFERENT Italian place (wrong answer for Luigi's queries)
        "There's also Marco's on 5th, their lasagna is supposed to be great, but no rooftop.",
        # DISTRACTOR: 'reservation' different meaning (having reservations/doubts)
        "Honestly I have some reservations about inviting the whole group, it gets loud.",
    ],
)

# ===========================================================================
# TOPIC: relationship_friction (personal, charged) — threads: chores, money
# ===========================================================================

thread(
    "thread_friction_chores",
    "topic_relationship_friction",
    (E, F),
    [
        "You said you'd do the dishes last night and they're still in the sink.",
        "I was exhausted after work. I'll do them now.",
        "It's always 'now'. I'm tired of being the only one who cleans up.",
        "That's not fair. I took out the trash and did the laundry this week.",
        "Laundry once doesn't balance dishes every single day, Sam.",
        "Fine. I get it. I'll set a reminder so you stop having to ask.",
        "I don't want to nag. I just want it to feel equal.",
        "It will. I promise I'll pull my weight on the housework.",
        "whatever.",
        "Don't shut down on me. Can we actually talk about this?",
        "I'm not shutting down. I'm just hurt that it took a fight to be heard.",
        "I'm sorry. I really am. The chores split has been lopsided and that's on me.",
        # DISTRACTOR: same words 'balance' but bank balance, not fairness
        "Separately, did you check the account balance? Rent comes out tomorrow.",
        # near-duplicate emotional but DIFFERENT issue (in-laws, not chores)
        "And honestly your mom calling every day is its own separate thing we need to discuss.",
    ],
)

thread(
    "thread_friction_money",
    "topic_relationship_friction",
    (E, F),
    [
        "We need to talk about the credit card bill. It's higher than we agreed.",
        "I know. The car repair was unexpected, I didn't plan for it.",
        "Twelve hundred dollars unexpected? You didn't think to tell me?",
        "I was going to. I just didn't want you to stress about money again.",
        "Hiding it makes me stress more, not less.",
        "You're right. No more surprise expenses without a conversation first.",
        "I'm not trying to control you. I just want us on the same page financially.",
        "Same page. Got it. Let's set a monthly budget this weekend.",
        "ok.",
        "Thank you for not blowing up about the car this time.",
        "I'm trying. The repair wasn't your fault, I just hate surprises.",
        # NEAR-DUPLICATE distractor: a DIFFERENT bill (medical, not credit card)
        "Reminder the medical bill from the ER visit is also due, separate from the card.",
        # DISTRACTOR: 'interest' different meaning (curiosity vs APR)
        "On a lighter note, I lost interest in that budgeting app, the UI is awful.",
    ],
)

# ===========================================================================
# TOPIC: household_logistics (roommates) — threads: move, repairs
# ===========================================================================

thread(
    "thread_house_move",
    "topic_household_logistics",
    (G, H),
    [
        "The lease is up in March. Are we renewing or finding a new place?",
        "Rent's going up 8%. I think we should look at other apartments.",
        "Agreed. I want a second bathroom this time, the morning queue is brutal.",
        "And a dishwasher. Non-negotiable for me.",
        "Two bed, two bath, dishwasher, under our current budget plus the increase. Doable?",
        "Tight but doable if we go one neighborhood further out.",
        "I can handle the apartment search if you handle the movers.",
        "Deal. I'll get three moving quotes by Friday.",
        "ok",
        ("found one", {
            "description": "Listing photos of a 2-bed 2-bath apartment with in-unit dishwasher and washer-dryer, 1100 sq ft, listed at the top of our budget, 20 minutes further from downtown",
        }),
        # near-duplicate distractor: a DIFFERENT apartment (wrong answer)
        "There's also a 1-bed loft downtown, gorgeous, but only one bathroom and pricey.",
        # DISTRACTOR: 'move' different meaning (a move in a game/chess)
        "Unrelated, that was a brilliant move you made in chess last night, I never saw it coming.",
    ],
)

thread(
    "thread_house_repairs",
    "topic_household_logistics",
    (G, H),
    [
        "The kitchen faucet is leaking again. Puddle under the sink this morning.",
        "I'll call the landlord. That's the third leak this year.",
        "While you're at it, the bathroom fan is loud enough to wake the dead.",
        "Adding the noisy fan to the list. Anything else broken I should mention?",
        "The bedroom window doesn't latch. It's drafty and cold at night.",
        "Leaky faucet, loud fan, drafty window. I'll send the landlord the whole list.",
        "Thanks. If they ignore it again we withhold rent until it's fixed.",
        "Agreed. Documented everything with photos just in case.",
        "good",
        ("proof", {
            "explanation": "Photo of water pooling under the kitchen sink cabinet with visible corrosion on the faucet supply line connector",
        }),
        # DISTRACTOR: 'leak' different meaning (info leak, not water)
        "Off topic, did you hear there was a data leak at our old internet provider?",
        # near-duplicate: a DIFFERENT faucet (bathroom, not kitchen) - subtle wrong answer
        "Oh and the bathroom faucet drips too, slower though, not flooding like the kitchen one.",
    ],
)

# ===========================================================================
# TOPIC: travel_planning (personal) — threads: flights, itinerary
# ===========================================================================

thread(
    "thread_travel_flights",
    "topic_travel_planning",
    (E, F),
    [
        "Should we book the Lisbon flights now or wait for a sale?",
        "Prices are climbing. I'd book the flights this week to be safe.",
        "Direct or one stop? The one-stop is 200 dollars cheaper.",
        "Direct. The layover in the cheap option is six hours, not worth it.",
        "Booked. Two direct seats to Lisbon, leaving the 14th, back on the 23rd.",
        "Window seat for me, you can have the aisle.",
        "Obviously. You'd make me climb over you every twenty minutes otherwise.",
        "rude. but accurate.",
        ("confirmation", {
            "summary": "Flight confirmation: two direct round-trip tickets to Lisbon, departing the 14th at 18:40, returning the 23rd, total fare 1840 dollars, seats 12A and 12C",
        }),
        # near-duplicate distractor: a DIFFERENT destination (wrong answer for Lisbon)
        "Tempting to do Barcelona instead, but we already said Lisbon, let's not flip-flop.",
        # DISTRACTOR: 'book' different meaning (a novel)
        "Random, I finished that book about Lisbon, it made me even more excited to go.",
    ],
)

thread(
    "thread_travel_itinerary",
    "topic_travel_planning",
    (E, F),
    [
        "For Lisbon, do you want a packed itinerary or a relaxed one?",
        "Relaxed. One big thing a day, lots of wandering and cafes.",
        "The tram 28 ride and the Belem tower are my two must-dos.",
        "Add the pasteis de nata place near Belem. Non-negotiable.",
        "Day one tram and old town, day two Belem and pastries, day three beach.",
        "Which beach? Cascais or Costa da Caparica?",
        "Cascais. Easier train ride and prettier town.",
        "Perfect. Loose plan, good food, no 6 AM alarms unlike Alice's hikes.",
        "haha. agreed, this is a real vacation.",
        # DISTRACTOR: 'tram' / 'ride' but about a theme park ride, not Lisbon tram
        "Speaking of rides, that roller coaster at the fair last summer almost killed me.",
        # near-duplicate: a DIFFERENT beach mentioned (the rejected option)
        "I keep thinking about Costa da Caparica though, the surf there is supposed to be better.",
    ],
)

# ===========================================================================
# TOPIC: project_orion (work, third project + extra distractor mass)
# ===========================================================================

thread(
    "thread_orion_design",
    "topic_project_orion",
    (C, D),
    [
        "Orion needs an auth system too. Reuse Nexus OAuth2 or build fresh?",
        "Reuse. No reason to build a second login flow from scratch.",
        "Agreed. Though Orion needs single sign-on, which Nexus never did.",
        "SSO via SAML then. That's the one real difference from the Nexus auth.",
        "What about rate limiting? Orion's traffic will be ten times Nexus.",
        "We'll need a much higher rate limit and probably a distributed token bucket.",
        "Distributed rate limiting it is. Redis-backed, unlike the in-memory one in Nexus.",
        "Right, the Nexus rate limiter is single-node and won't scale to Orion.",
        "noted",
        # near-duplicate distractor: Orion's OWN payment bug, similar wording to Nexus payment bug
        "Heads up, Orion's billing module is double-charging cards on retry. Sound familiar?",
        "Ha, yeah, same shape as the Nexus transaction duplication. Idempotency key again.",
        ("billing trace", {
            "summary": "Orion billing logs showing duplicate charge events fired when the payment callback is retried before the idempotency record is committed",
        }),
    ],
)


thread(
    "thread_orion_rollout",
    "topic_project_orion",
    (C, D),
    [
        "Orion beta rollout is Monday. Are the feature flags ready?",
        "Flags are wired. We can dark-launch to 5% and ramp from there.",
        "Start at 1%. After the billing scare I don't trust a 5% blast radius.",
        "Fair. 1% Monday, 10% Wednesday if metrics hold.",
        "What's our rollback plan if the SSO login breaks?",
        "Flip the flag off, falls back to the old password login instantly.",
        "Good. Document the rollback steps in the runbook before Monday.",
        "Already drafted. Linking it in the channel now.",
        "perfect",
        # near-duplicate distractor: a DIFFERENT rollout (marketing), not the beta
        "Separate thing: marketing wants to roll out the ad campaign the same week, bad idea.",
        # DISTRACTOR: 'flag' different meaning (a literal flag / red flag emotionally)
        "Honestly the fact that we're rushing this is a red flag about the whole timeline.",
    ],
)

# ===========================================================================
# TOPIC: fitness_goals (personal) — threads: running, gym
# ===========================================================================

thread(
    "thread_fitness_running",
    "topic_fitness_goals",
    (E, F),
    [
        "I signed us up for the half marathon in April. No backing out now.",
        "Half marathon? I can barely run a 5K without dying.",
        "That's why we start training now. Twelve week plan, three runs a week.",
        "Fine. But the long runs are on Sundays, mornings are sacred during the week.",
        "Sunday long runs it is. We build from 5K up to 18K before race day.",
        "My knees are already filing a complaint.",
        "We'll get you proper running shoes this weekend. Cheap ones wrecked your knees last time.",
        "Deal. New running shoes, Sunday long runs, no excuses.",
        "ok let's actually do it this time.",
        # DISTRACTOR: 'run' different meaning (a run in production / a software run)
        "Funny, 'run' now means something very different than my work deploys.",
        # near-duplicate: a DIFFERENT race (the rejected 10K), wrong answer
        "There's also a 10K in March if the half feels like too much, just saying.",
    ],
)

thread(
    "thread_fitness_gym",
    "topic_fitness_goals",
    (G, H),
    [
        "Want to split a gym membership? It's cheaper with two.",
        "Only if we actually go. I have three dead memberships to my name.",
        "Accountability buddy then. Skip a session, you buy coffee for a week.",
        "Brutal. But effective. Deal.",
        "Mondays and Thursdays, 6 PM, after work before we lose the will.",
        "6 PM works. Legs Monday, upper body Thursday?",
        "Yeah. And no skipping leg day, that's how you end up looking ridiculous.",
        "I've seen those guys. Massive arms, tiny chicken legs. Never me.",
        "ha, agreed",
        ("membership", {
            "description": "Photo of a gym membership receipt for a shared two-person plan, 64 dollars a month, with unlimited classes and a note about the 6 PM peak hours being crowded",
        }),
        # DISTRACTOR: 'split' different meaning (a relationship split)
        "Off topic, did you hear Sam and Jordan almost split last month? They're fine now.",
    ],
)

# ===========================================================================
# TOPIC: project_nexus (extra thread for mass) — thread: security review
# ===========================================================================

thread(
    "thread_nexus_security",
    "topic_project_nexus",
    (A, B),
    [
        "Security flagged the Nexus login for storing tokens in local storage. We need to fix that.",
        "Move them to httpOnly cookies? That's the standard fix for token theft risk.",
        "Yes. httpOnly secure cookies, and rotate the refresh token on every use.",
        "Rotating refresh tokens adds complexity but it's the right call for security.",
        "Also they want rate limiting on the login endpoint to stop brute force attempts.",
        "We already have rate limiting elsewhere, I'll extend it to the auth login route.",
        "Good. Brute force protection plus httpOnly cookies should close the audit findings.",
        "Pushing the security fixes today. Pen test re-run is scheduled for Friday.",
        "noted",
        ("audit findings", {
            "summary": "Security audit report listing two high findings: auth tokens stored in browser local storage exposing XSS token theft, and no rate limit on the login endpoint enabling credential brute force",
        }),
        # near-duplicate distractor: a DIFFERENT security issue (a physical office badge)
        "Unrelated, building security wants us to stop propping the office door, it's a real risk.",
    ],
)


# ---------------------------------------------------------------------------
# Nav anchor helpers
# ---------------------------------------------------------------------------


def _nav_anchor_positions(n: int) -> tuple[int, int, int]:
    """Return (start_pos, mid_pos, end_pos) for nav anchor insertion.

    Positions are insertion points: the anchor is emitted before the message
    at that index.  Guarantees three distinct positions within [0, n].
    """
    if n < 3:
        # Degenerate thread: emit all anchors after the last real message.
        return (n, n, n)
    start = 1  # after first real message
    mid = max(start + 1, int(n * 0.30))
    end = n - 1  # before last message (penultimate insertion point)
    # Ensure distinct.
    if mid >= end:
        mid = max(start + 1, end - 1)
    if mid <= start:
        mid = start + 1
    return (start, mid, end)


# ---------------------------------------------------------------------------
# Emit corpus
# ---------------------------------------------------------------------------


def build_corpus():
    messages = []
    by_id = {}
    counter = 0
    base = datetime(2025, 11, 1, 8, 0, 0, tzinfo=timezone.utc)
    # global monotonically increasing minute offset to guarantee unique sent_at
    tick = 0
    for th in THREADS:
        s1, s2 = th["dyad"]
        n_msgs = len(th["msgs"])
        start_pos, mid_pos, end_pos = _nav_anchor_positions(n_msgs)
        nav_anchors: dict[int, dict[str, str]] = {
            start_pos: {
                "id": f"nav_{th['thread_id']}_start",
                "content": f"[NAV_ANCHOR: {th['thread_id']} start]",
            },
            mid_pos: {
                "id": f"nav_{th['thread_id']}_mid",
                "content": f"[NAV_ANCHOR: {th['thread_id']} midpoint]",
            },
            end_pos: {
                "id": f"nav_{th['thread_id']}_end",
                "content": f"[NAV_ANCHOR: {th['thread_id']} recent_end]",
            },
        }
        for i, m in enumerate(th["msgs"]):
            # Emit nav anchor before the real message if we're at an anchor
            # position.  Use a descriptive id (nav_*) so real message ids
            # (m001..) are preserved for golden-case expected_ids.
            if i in nav_anchors:
                a = nav_anchors[i]
                # Interpolate tick halfway between the previous real message
                # and this one so the anchor slots chronologically.
                anchor_tick = tick + 0.5
                rec = {
                    "id": a["id"],
                    "thread_id": th["thread_id"],
                    "topic_id": th["topic_id"],
                    "sender": s1,
                    "recipient": s2,
                    "sent_at": (
                        base + timedelta(minutes=anchor_tick)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "content": a["content"],
                }
                messages.append(rec)
                by_id[a["id"]] = rec

            if isinstance(m, tuple):
                content, media = m
            else:
                content, media = m, None
            counter += 1
            mid = f"m{counter:03d}"
            sender = s1 if i % 2 == 0 else s2
            recipient = s2 if i % 2 == 0 else s1
            tick += 1
            sent_at = base + timedelta(minutes=tick)
            rec = {
                "id": mid,
                "thread_id": th["thread_id"],
                "topic_id": th["topic_id"],
                "sender": sender,
                "recipient": recipient,
                "sent_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "content": content,
            }
            if media is not None:
                rec["media_analysis"] = media
            messages.append(rec)
            by_id[mid] = rec
    return messages, by_id


# ---------------------------------------------------------------------------
# Golden set authoring.
#
# Each case is a dict. For paraphrase / cross_thread cases we deliberately give
# the query SOME lexical overlap with at least one target so the ILIKE baseline
# can score nonzero (FAIR). A minority are tagged hard_zero_overlap=True: genuine
# synonym-only cases the baseline cannot touch, kept so we still measure the
# pure-semantic ceiling — but they don't dominate.
#
# 'overlap_hint' documents the contiguous substring shared with a target (for the
# fairness audit). It is informational only.
# ---------------------------------------------------------------------------

CASES: list[dict] = [
    # ===================== VERBATIM_QUOTE (exact substring) =================
    dict(id="GC01", query="I told you so", expected=["m013"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Exact terse reply."),
    dict(id="GC02", query="fine.", expected=["m007"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="One-word reply, exact match."),
    dict(id="GC03", query="osso buco", expected=["m081", "m087", "m090", "m091"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Substring across dinner thread + media menu (m090)."),
    dict(id="GC04", query="Blue Ridge", expected=["m061", "m069", "m073", "m086", "m087", "m077"],
         scope="all", qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Appears in hike+dinner threads; m077 media."),
    dict(id="GC05", query="idempotency key", expected=["m021", "m025"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Near-duplicate Nexus payment-bug pair."),
    dict(id="GC06", query="rate limiting", expected=["m010", "m029"], scope="thread",
         thread="thread_nexus_kickoff",
         note="Thread-scoped: only m010 is in kickoff; m029 in bugs is scoped out.",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         expected_in_scope=["m010"]),
    dict(id="GC07", query="httpOnly cookies", expected=["m213", "m218"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Security thread exact phrase."),
    dict(id="GC08", query="worker pool", expected=["m050", "m052", "m054", "m058"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Atlas incident; m058 in media summary too."),
    dict(id="GC09", query="osso buco", expected=["m090"], scope="thread",
         thread="thread_weekend_dinner",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Scoped to dinner thread; media menu match."),
    dict(id="GC10", query="kitchen faucet", expected=["m133", "m142"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Repairs thread + media photo (m142); bathroom faucet m144 is distractor."),
    dict(id="GC11", query="half marathon", expected=["m190", "m191"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Fitness running thread."),
    dict(id="GC12", query="Lisbon flights", expected=["m145"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Exact bigram only in m145."),
    dict(id="GC13", query="running shoes", expected=["m196", "m197"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Fitness thread exact phrase."),
    dict(id="GC14", query="sure", expected=["m027"], scope="thread",
         thread="thread_nexus_bugs",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="Terse hotfix confirmation, scoped."),

    # ===================== PARAPHRASE (realistic overlap) ===================
    # Queries are short, keyword-style search phrases (the way a user/agent
    # actually drives `search_messages`, which does '%text_contains%'). Each
    # FAIR paraphrase query is a contiguous substring of >=1 expected target so
    # the baseline can score nonzero; the semantic win comes from also pulling
    # restated/synonym targets the baseline ranks poorly or misses, and from
    # precision against same-word distractors. A labeled minority are genuinely
    # zero-overlap synonym-only cases (hard_zero=True).
    dict(id="GC15", query="login integration", expected=["m004"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'login integration' is a substring of m004 ('OAuth2 login integration'). Baseline can hit m004; semantic must also rank it over the security-thread login mentions.",
         overlap_hint="login integration"),
    dict(id="GC16", query="migration scripts blocked", expected=["m006"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'migration scripts' substring of m005/m006/m016; the 'blocked' status answer is m006. Baseline returns the lexical set ordered by recency; semantic prefers the blocked one.",
         overlap_hint="migration scripts blocked"),
    dict(id="GC17", query="don't forget sunscreen", expected=["m067"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'sunscreen' substring of m067. Realistic reminder phrasing.",
         overlap_hint="sunscreen"),
    dict(id="GC18", query="caching layer", expected=["m008"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'caching layer' substring of m008. Query is the user's term; m009 ('benchmark the cache') is a lexical neighbor semantic should also relate.",
         overlap_hint="caching layer"),
    dict(id="GC19", query="duplicating transaction", expected=["m019", "m025"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'duplicating transaction' substring of m019; m025 is the near-duplicate. Orion m176/m177 same-shape billing bug is a different-topic distractor.",
         overlap_hint="duplicating transaction"),
    dict(id="GC20", query="apartment search", expected=["m127", "m128"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'apartment search' substring of m127; m128 (movers) is the paired action needing meaning. Baseline gets m127 only.",
         overlap_hint="apartment search"),
    dict(id="GC21", query="dishes in the sink", expected=["m094", "m096", "m105"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'dishes' phrasing overlaps m094 ('dishes ... in the sink'); the fairness/resolution targets m096/m105 need meaning. Baseline gets m094.",
         overlap_hint="dishes in the sink"),
    dict(id="GC22", query="car repair", expected=["m109"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'car repair' substring of m109. Realistic search term; semantic also relates m117/m118 (same incident) but answer is m109.",
         overlap_hint="car repair"),
    dict(id="GC23", query="query latency", expected=["m036", "m037"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'query latency' substring of m036; m037 has the p95 number. Video-call latency m047 is a same-word distractor semantic should de-rank.",
         overlap_hint="query latency"),
    dict(id="GC24", query="window seat", expected=["m150"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'window seat' substring of m150. Direct keyword search.",
         overlap_hint="window seat"),
    dict(id="GC25", query="rollback plan", expected=["m183", "m185"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'rollback' substring of m183/m185 ('rollback plan'/'rollback steps'). m184 (the actual fallback mechanism) needs meaning.",
         overlap_hint="rollback"),
    dict(id="GC26", query="drafty window", expected=["m137"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'drafty' substring of m137; also m138 summary list. Answer is the report m137.",
         overlap_hint="drafty window"),
    dict(id="GC27", query="rooftop reservation", expected=["m085"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'rooftop' + 'reservation' both in m085. m093 uses 'reservations' = doubts (same-word distractor) semantic should reject.",
         overlap_hint="rooftop / reservation"),
    dict(id="GC28", query="token theft risk", expected=["m212", "m213"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'token theft risk' substring of m213; m212 (the local-storage finding) needs meaning. Many other 'token' mentions are distractors (m018 gift token).",
         overlap_hint="token theft risk"),
    # --- HARD zero-overlap paraphrase (synonym only; baseline must miss) ----
    dict(id="GC29", query="sunburn anecdote", expected=["m068"], scope="all",
         qt="paraphrase", hard_zero=True, difficulty="hard", fairness="adversarial",
         note="HARD: m068 says 'looked like a tomato' — no shared substring. Baseline []."),
    dict(id="GC30", query="NPE fix", expected=["m015"], scope="all",
         qt="paraphrase", hard_zero=True, difficulty="hard", fairness="adversarial",
         note="HARD: m015 says 'null pointer ... fixed it' — acronym, no substring. Baseline []."),
    dict(id="GC31", query="demoralized after the launch", expected=["m059"], scope="all",
         qt="paraphrase", hard_zero=True, difficulty="hard", fairness="adversarial",
         note="HARD: m059 'feeling pretty down' — 'demoralized' shares no substring; 'launch' appears in m059? no. Baseline []."),
    dict(id="GC32", query="UV protection", expected=["m067"], scope="all",
         qt="paraphrase", hard_zero=True, difficulty="hard", fairness="adversarial",
         note="HARD: 'UV protection' vs 'sunscreen' — synonym only, no substring. Baseline []."),
    dict(id="GC33", query="food and drinks to pack", expected=["m063"], scope="all",
         qt="paraphrase", hard_zero=True, difficulty="hard", fairness="adversarial",
         note="HARD: m063 'pack the sandwiches ... water filters' — synonyms, no shared substring. Baseline []."),
    dict(id="GC34", query="hiding money stress", expected=["m111", "m112"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'money' substring of m111 ('stress about money'); 'hiding' substring of m112. Baseline can hit both lexically; tests ranking + recall.",
         overlap_hint="money / hiding"),
    dict(id="GC35", query="brute force", expected=["m216", "m218"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="'brute force' substring of m216/m218. Realistic term; semantic should also surface m212 (the vuln) via meaning though it's not expected here.",
         overlap_hint="brute force"),
    dict(id="GC36", query="partitioning the events table", expected=["m040"], scope="all",
         qt="paraphrase", difficulty="medium", fairness="either",
         note="Substring of m040; m041 ('Daily partitioning') and m038/m039 (index on events table) are strong lexical neighbors — tests precision.",
         overlap_hint="partitioning the events table"),

    # ===================== CROSS_THREAD (topic spans 2 threads) =============
    # Short keyword query that substring-matches at least one target; the
    # CROSS-THREAD challenge is that the full answer set spans BOTH threads of
    # the topic, so a lexical hit in one thread still leaves recall low unless
    # the retriever generalizes by meaning across the topic.
    dict(id="GC37", query="bring food", scope="topic", topic="topic_weekend_plans",
         expected=["m063", "m080", "m081", "m087", "m090", "m091"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'food' substring of m069/m073 (hike) — baseline catches the hike-food mentions but misses the dinner thread (Luigi's m080-m091), which needs meaning to span.",
         overlap_hint="food"),
    dict(id="GC38", query="deploy", scope="topic", topic="topic_project_nexus",
         expected=["m006", "m010", "m019", "m025", "m028", "m219"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'deploy' substring of m019/m025 (bugs). Baseline catches the bugs-thread deploy mentions; the kickoff schema-block m006 and security-fix m219 need meaning to span the topic.",
         overlap_hint="deploy"),
    dict(id="GC39", query="overkill for our scale", scope="topic",
         topic="topic_project_nexus",
         expected=["m008", "m009", "m010", "m012"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'scale' substring of m008. Baseline gets m008; benchmarking m009 and rate-limit m010/m012 (the scaling responses) need meaning.",
         overlap_hint="scale"),
    dict(id="GC40", query="login flow", scope="all",
         expected=["m003", "m004", "m167", "m168", "m212", "m213"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'login flow' substring of m168 (Orion). Baseline gets m168; the Nexus auth (m003/m004/m212/m213) needs meaning to span both projects.",
         overlap_hint="login flow"),
    dict(id="GC41", query="Lisbon", scope="topic", topic="topic_travel_planning",
         expected=["m145", "m149", "m156", "m158", "m160"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'Lisbon' substring of m145/m149 (flights) and m156 (itinerary). Baseline gets the literal Lisbon mentions; tram/Belem itinerary items (m158/m160) need meaning. Barcelona m154 distractor.",
         overlap_hint="Lisbon"),
    dict(id="GC42", query="rate limiting", scope="all",
         expected=["m010", "m012", "m171", "m172", "m216"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'rate limiting' substring across Nexus kickoff (m010), Orion (m171), security (m216). Baseline has a real lexical shot here; m012/m172 (the fixes) partly need meaning. Hardest verbatim-style cross-thread.",
         overlap_hint="rate limiting"),
    dict(id="GC43", query="broken", scope="topic",
         topic="topic_household_logistics",
         expected=["m133", "m135", "m137", "m138"],
         qt="cross_thread", difficulty="hard", fairness="semantic_favored",
         note="'broken' substring of m136 (a neighbor, not expected) — the actual broken-items (faucet m133, fan m135, window m137) are described without the word 'broken', so semantics carry it. Baseline likely 0 here.",
         overlap_hint="broken (weak)"),
    dict(id="GC44", query="training plan", scope="topic", topic="topic_fitness_goals",
         expected=["m192", "m194", "m197", "m205", "m206"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'training' substring of m192/m194 (running). Baseline catches running-side; the gym schedule (m205/m206) is the cross-thread part needing meaning.",
         overlap_hint="training"),
    dict(id="GC45", query="budget", scope="topic",
         topic="topic_relationship_friction",
         expected=["m108", "m110", "m111", "m115"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'budget' substring of m115 (money thread). Baseline gets m115; the bill/spending/stress lead-up (m108/m110/m111) needs meaning. Chores thread excluded by topic+meaning.",
         overlap_hint="budget"),
    dict(id="GC46", query="duplicate charge", scope="all",
         expected=["m019", "m025", "m176", "m177"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'charge' substring of m176 (Orion billing). Baseline gets the Orion side; Nexus transaction-dup m019/m025 spans the other project by meaning. Email-dup m030/m031 same-word distractor.",
         overlap_hint="charge"),
    dict(id="GC47", query="6 AM Saturday", scope="topic",
         topic="topic_weekend_plans",
         expected=["m061", "m065", "m069", "m073", "m075"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'6 AM' substring of m065/m069/m073. Baseline catches the timing lines; trail/forecast logistics (m061/m075) need meaning. Dinner thread excluded by meaning.",
         overlap_hint="6 AM"),
    dict(id="GC48", query="audit findings", scope="topic",
         topic="topic_project_nexus",
         expected=["m212", "m216", "m218", "m219"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'audit findings' substring of m218 (+m221 media, a neighbor). Baseline gets m218; the local-storage finding m212, rate-limit fix m216, push m219 need meaning. Office-door m222 same-word distractor.",
         overlap_hint="audit findings"),
    dict(id="GC49", query="Atlas is down", scope="topic", topic="topic_project_atlas",
         expected=["m049", "m052", "m056", "m058"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'Atlas is down' substring of m049. Baseline gets m049; worker-pool cause m052, recovery m056, postmortem m058 need meaning. m059 'feeling down' same-word distractor.",
         overlap_hint="Atlas is down"),
    dict(id="GC50", query="other apartments", scope="topic",
         topic="topic_household_logistics",
         expected=["m121", "m122", "m125", "m127"],
         qt="cross_thread", difficulty="medium", fairness="either",
         note="'other apartments' substring of m122. Baseline gets m122; lease decision m121, requirements m125, search split m127 need meaning. Chess-move m132 same-word distractor.",
         overlap_hint="other apartments"),

    # ===================== TOPIC_RECALL =====================================
    # Short topical search phrase substring-matching at least one target; full
    # recall of the topical thread needs meaning beyond the literal hit.
    dict(id="GC51", query="authentication module", scope="topic", topic="topic_project_nexus",
         expected=["m003", "m004", "m014"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'authentication module' substring of m003 (+m014 media). Baseline gets those; OAuth2 status m004 needs meaning."),
    dict(id="GC52", query="payment processor", scope="topic", topic="topic_project_nexus",
         expected=["m010", "m019", "m025", "m032"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'payment processor' substring of m019/m025. Baseline gets payment dup; rate-limit m010 and app-crash m032 (other Nexus bugs) need meaning."),
    dict(id="GC53", query="Blue Ridge hike", scope="thread", thread="thread_weekend_hike",
         expected=["m061", "m063", "m069", "m073", "m075"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'Blue Ridge' substring of m061/m069/m073. Baseline catches those; food/forecast logistics m063/m075 need meaning. Whitetail m079 distractor."),
    dict(id="GC54", query="dinner", scope="thread", thread="thread_weekend_dinner",
         expected=["m080", "m081", "m085", "m087"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'dinner' substring of m080/m087. Baseline gets those; Luigi's name m081, reservation m085 need meaning. Marco's m092 distractor."),
    dict(id="GC55", query="latency", scope="all",
         expected=["m008", "m010", "m036", "m037"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'latency' substring of m036/m037 (Atlas). Baseline gets Atlas perf; Nexus cache m008 + rate-limit m010 (cross-topic perf) need meaning. Video-call latency m047 distractor."),
    dict(id="GC56", query="feels equal", scope="topic",
         topic="topic_relationship_friction", expected=["m096", "m098", "m100", "m105"],
         qt="topic_recall", difficulty="medium", fairness="either",
         note="'equal' substring of m100. Baseline gets m100; the fairness conflict m096/m098/m105 needs meaning. Mostly meaning-driven."),
    dict(id="GC57", query="Belem tower", scope="thread", thread="thread_travel_itinerary",
         expected=["m156", "m158", "m159", "m160"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'Belem' substring of m158/m159/m160. Baseline gets Belem mentions; the relaxed-itinerary framing m156 needs meaning. Roller-coaster m165 distractor."),
    dict(id="GC58", query="gym membership", scope="thread", thread="thread_fitness_gym",
         expected=["m201", "m205", "m206", "m210"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'gym membership' substring of m201 (+m210 media). Baseline gets those; schedule m205/m206 needs meaning. Split m211 distractor."),
    dict(id="GC59", query="Atlas launch", scope="topic",
         topic="topic_project_atlas", expected=["m034", "m042", "m044", "m045"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'Atlas launch' substring of m034. Baseline gets m034; deadline/burnout/slip m042/m044/m045 need meaning."),
    dict(id="GC60", query="beta rollout", scope="thread", thread="thread_orion_rollout",
         expected=["m179", "m180", "m182", "m185"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'beta rollout' substring of m179. Baseline gets m179; ramp plan m180/m182 and runbook m185 need meaning. Ad-campaign m188 distractor."),
    dict(id="GC61", query="call the landlord", scope="thread",
         thread="thread_house_repairs", expected=["m133", "m135", "m137", "m138"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'landlord' substring of m134 (a neighbor) and m138. The broken items m133/m135/m137 are described without 'landlord' — need meaning. Data-leak m143 distractor."),
    dict(id="GC62", query="half marathon training", scope="thread",
         thread="thread_fitness_running", expected=["m190", "m192", "m194", "m197"], qt="topic_recall",
         difficulty="medium", fairness="either",
         note="'half marathon' substring of m190/m191; 'training' in m192/m194. Baseline catches those; shoes m197 needs meaning. 10K m200 distractor."),

    # ===================== NEW KEYWORD-PLAUSIBLE (GC63+) ====================
    # Short, terse, context-dependent query shapes that genuinely contain
    # ILIKE-matchable substrings.  These give the baseline fair shots and
    # increase corpus density for the keyword_favoured / either fairness slices.
    dict(id="GC63", query="sprint review", expected=["m002"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="'sprint review' substring of m002 ('latest sprint review'). Terse keyword query."),
    dict(id="GC64", query="first aid kit", expected=["m070"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="'first aid kit' substring of m070. Realistic context-dependent lookup."),
    dict(id="GC65", query="design doc", expected=["m048"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="'design doc' substring of m048. Short terse query shape."),
    dict(id="GC66", query="moving quotes", expected=["m128"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="'moving quotes' substring of m128. Quick move-planning lookup."),
    dict(id="GC67", query="monthly budget", expected=["m115"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="'monthly budget' substring of m115. Personal finance keyword."),
    dict(id="GC68", query="audit trail", expected=["m078"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="'audit trail' substring of m078 (a cross-topic lexical trap — 'trail' is hiking but 'audit trail' is work)."),
    dict(id="GC69", query="dishwasher", expected=["m123", "m130"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="'dishwasher' substring of m123 and m130 (media). Both are in the house_move thread."),
    dict(id="GC70", query="feature flags", expected=["m179", "m180"], scope="all",
         qt="verbatim_quote", difficulty="easy", fairness="keyword_favored",
         note="'feature flags' substring of m179; 'Flags' in m180. Orion rollout thread."),
]


def _expected_in_scope(case, by_id):
    """Return the expected ids that actually fall within the case's scope.

    Recall is computed against the full expected list, but for the fairness
    audit we report whether the baseline *could* match given scope.
    """
    exp = case["expected"]
    scope = case["scope"]
    if scope == "thread":
        return [e for e in exp if by_id[e]["thread_id"] == case.get("thread")]
    if scope == "topic":
        return [e for e in exp if by_id[e]["topic_id"] == case.get("topic")]
    return exp


def build_golden():
    out = []
    for c in CASES:
        rec = {
            "id": c["id"],
            "query": c["query"],
            "expected_message_ids": c["expected"],
            "scope": c["scope"],
            "query_type": c["qt"],
        }
        if c.get("difficulty"):
            rec["difficulty"] = c["difficulty"]
        if c.get("fairness"):
            rec["fairness"] = c["fairness"]
        if c.get("thread"):
            rec["thread_id"] = c["thread"]
        if c.get("topic"):
            rec["topic_id"] = c["topic"]
        note = c.get("note", "")
        if c.get("hard_zero"):
            note = "[HARD zero-overlap] " + note
        if c.get("overlap_hint"):
            note = note + f" (overlap: {c['overlap_hint']})"
        rec["notes"] = note
        out.append(rec)
    return out


def _full_text(rec):
    parts = [rec["content"]]
    ma = rec.get("media_analysis")
    if ma:
        for f in ("explanation", "description", "summary"):
            v = ma.get(f)
            if isinstance(v, str):
                parts.append(v)
    return " ".join(parts).lower()


def audit_overlap(cases, by_id):
    """Report, per query_type, whether the WHOLE query is an ILIKE substring of
    any expected target (baseline whole-string match) and whether ANY query word
    is a substring (partial lexical signal). Validates fairness claims."""
    import re
    from collections import defaultdict

    stats = defaultdict(lambda: {"n": 0, "whole_hit": 0, "word_hit": 0, "zero": 0})
    for c in cases:
        qt = c["qt"]
        stats[qt]["n"] += 1
        q = c["query"].lower()
        exp_texts = [_full_text(by_id[e]) for e in c["expected"]]
        whole = any(q in t for t in exp_texts)
        words = [w for w in re.split(r"[^a-z0-9]+", q) if len(w) > 2]
        word = any(any(w in t for t in exp_texts) for w in words)
        if whole:
            stats[qt]["whole_hit"] += 1
        if word:
            stats[qt]["word_hit"] += 1
        if not word:
            stats[qt]["zero"] += 1
    return stats


# ---------------------------------------------------------------------------
# Non-message source entries (T24)
#
# Each source entry has a deterministic id (mem001.., obs001.., etc.), a
# topic_id linking it to a message topic, and searchable text. These are
# emitted alongside messages so source-aware retrievers can be tested.
# Message-only adapters ignore these entries by design.
# ---------------------------------------------------------------------------

SOURCE_MEMORIES: list[dict] = [
    {"id": "mem001", "topic_id": "topic_project_nexus", "content": "The OAuth2 integration is mostly done but token refresh edge cases need handling.", "visibility": "private"},
    {"id": "mem002", "topic_id": "topic_project_nexus", "content": "Migration scripts are blocked on schema review — Jenkins flagged three compatibility issues.", "visibility": "private"},
    {"id": "mem003", "topic_id": "topic_project_nexus", "content": "Rate limit was bumped from 50 to 75 requests per minute for staging.", "visibility": "private"},
    {"id": "mem004", "topic_id": "topic_project_atlas", "content": "Atlas p95 query latency is around 4 seconds under load; the events table join is the bottleneck.", "visibility": "private"},
    {"id": "mem005", "topic_id": "topic_weekend_plans", "content": "Blue Ridge hike: 6 AM Saturday, I’ll bring food, you bring water filters and first aid.", "visibility": "dyad_shareable"},
    {"id": "mem006", "topic_id": "topic_relationship_friction", "content": "Sam feels the chores split has been lopsided and wants it to feel equal.", "visibility": "private"},
    {"id": "mem007", "topic_id": "topic_travel_planning", "content": "Booked two direct seats to Lisbon, departing the 14th, returning the 23rd. Total fare $1840.", "visibility": "dyad_shareable"},
    {"id": "mem008", "topic_id": "topic_project_orion", "content": "Orion beta rollout starts Monday at 1% with feature flags; rollback plan documented in the runbook.", "visibility": "private"},
]

SOURCE_OBSERVATIONS: list[dict] = [
    {"id": "obs001", "topic_id": "topic_project_nexus", "content": "The null pointer exception at AuthService.java:342 was a missing null guard on session factory injection.", "confidence": "high", "significance": 4},
    {"id": "obs002", "topic_id": "topic_project_nexus", "content": "Payment processor duplication was caused by an idempotency key collision under concurrent requests.", "confidence": "high", "significance": 5},
    {"id": "obs003", "topic_id": "topic_project_atlas", "content": "Atlas ingestion worker pool exhausted DB connections at 09:14, cascading 503s for 22 minutes.", "confidence": "high", "significance": 5},
    {"id": "obs004", "topic_id": "topic_relationship_friction", "content": "The $1200 car repair was unexpected and not communicated before the credit card bill arrived.", "confidence": "medium", "significance": 4},
    {"id": "obs005", "topic_id": "topic_weekend_plans", "content": "Osso buco is a Thursday special at Luigi’s, not available on Saturday — mushroom risotto is the chef’s recommendation.", "confidence": "medium", "significance": 2},
    {"id": "obs006", "topic_id": "topic_household_logistics", "content": "Kitchen faucet is leaking (third leak this year), bathroom fan is loud, bedroom window doesn’t latch.", "confidence": "high", "significance": 3},
]

SOURCE_DISTILLATIONS: list[dict] = [
    {"id": "dst001", "topic_id": "topic_project_nexus", "content": "Nexus authentication: OAuth2 integration nearly complete, security review flagged local-storage token risk requiring httpOnly cookies.", "visibility": "private"},
    {"id": "dst002", "topic_id": "topic_project_atlas", "content": "Atlas performance: query latency bottleneck is the events table join; daily partitioning is the leading candidate solution.", "visibility": "private"},
    {"id": "dst003", "topic_id": "topic_weekend_plans", "content": "Saturday plan: Blue Ridge hike at 6 AM (food+water+first aid), then Luigi’s rooftop dinner at 7:30 PM (mushroom risotto if osso buco unavailable).", "visibility": "dyad_shareable"},
    {"id": "dst004", "topic_id": "topic_relationship_friction", "content": "Relationship friction centers on unequal chores distribution and surprise expenses; both partners agree on setting a monthly budget and reminders.", "visibility": "private"},
]

SOURCE_ARTIFACTS: list[dict] = [
    {"id": "art001", "topic_id": "topic_project_nexus", "title": "Security audit report", "summary": "Two high findings: auth tokens in browser local storage exposing XSS token theft, and no rate limit on login endpoint enabling brute force.", "artifact_type": "review_summary"},
    {"id": "art002", "topic_id": "topic_project_atlas", "title": "Atlas outage postmortem", "summary": "Worker pool exhausted DB connection pool at 09:14 causing 22-minute 503 cascade; pool scaled from 6 to 12 workers to resolve.", "artifact_type": "review_summary"},
    {"id": "art003", "topic_id": "topic_household_logistics", "title": "Apartment listing", "summary": "2-bed 2-bath with dishwasher and washer-dryer, 1100 sq ft, top of budget, 20 min further from downtown.", "artifact_type": "live_debrief"},
]

SOURCE_CONVERSATION_NOTES: list[dict] = [
    {"id": "note001", "topic_id": "topic_project_nexus", "text": "[fact] The CI pipeline is green again after bumping the PostGIS extension version to 3.4."},
    {"id": "note002", "topic_id": "topic_project_nexus", "text": "[decision] httpOnly secure cookies will replace local storage for auth tokens; rotate refresh token on every use."},
    {"id": "note003", "topic_id": "topic_project_atlas", "text": "[open_loop] Daily partitioning prototype for the events table is due this week."},
    {"id": "note004", "topic_id": "topic_relationship_friction", "text": "[decision] Monthly budget to be set this weekend; no more surprise expenses without a conversation first."},
    {"id": "note005", "topic_id": "topic_weekend_plans", "text": "[fact] Blue Ridge trailhead parking fills up by 6:45 AM on Saturdays — arrive early."},
]

SOURCE_THEMES: list[dict] = [
    {"id": "thm001", "topic_id": "topic_relationship_friction", "title": "Unequal household labor", "description": "One partner feels they carry a disproportionate share of chores; dishes and daily cleanup are recurring friction points.", "status": "active"},
    {"id": "thm002", "topic_id": "topic_relationship_friction", "title": "Financial transparency", "description": "Surprise expenses without prior communication cause stress; both partners want shared visibility into spending.", "status": "active"},
    {"id": "thm003", "topic_id": "topic_project_nexus", "title": "Technical debt in auth layer", "description": "Authentication tokens stored in local storage create XSS vulnerability; need migration to httpOnly cookies.", "status": "active"},
    {"id": "thm004", "topic_id": "topic_weekend_plans", "title": "Blue Ridge hiking routine", "description": "The Saturday hiking routine has settled into 6 AM starts with shared food/water responsibility; weather preparedness is improving.", "status": "active"},
]


# ---------------------------------------------------------------------------
# Non-message entry emitter
# ---------------------------------------------------------------------------


def build_sources():
    """Build source-level entries keyed by collection name."""
    return {
        "memories": SOURCE_MEMORIES,
        "observations": SOURCE_OBSERVATIONS,
        "distillations": SOURCE_DISTILLATIONS,
        "artifacts": SOURCE_ARTIFACTS,
        "conversation_notes": SOURCE_CONVERSATION_NOTES,
        "themes": SOURCE_THEMES,
    }




def write_yaml(msgs, golden):
    sources = build_sources()
    corpus_doc = {"messages": msgs, **sources}
    golden_doc = {"cases": golden}

    class _S(str):
        pass

    corpus_path = HERE / "corpus.yaml"
    golden_path = HERE / "golden_set.yaml"
    header_c = (
        "# Synthetic retrieval-eval corpus (FAIR rebuild). Generated by\n"
        "# eval/retrieval/_generate_fixtures.py — DO NOT hand-edit; edit the\n"
        "# generator and re-run. Terse, emotionally-charged dyadic messaging\n"
        "# across multiple topics/threads, with deliberate hard distractors\n"
        "# (near-duplicate incidents, same-word-different-meaning traps).\n"
        "# Determinism: stable ids m001.., strictly unique increasing sent_at.\n\n"
    )
    header_g = (
        "# Golden set (FAIR rebuild). Generated by\n"
        "# eval/retrieval/_generate_fixtures.py — DO NOT hand-edit.\n"
        "# Paraphrase/cross_thread queries share SOME lexical overlap with their\n"
        "# targets so the ILIKE baseline can score nonzero; a labeled minority are\n"
        "# genuine zero-overlap synonym cases ([HARD zero-overlap] in notes).\n\n"
    )
    corpus_path.write_text(
        header_c + yaml.safe_dump(corpus_doc, sort_keys=False, allow_unicode=True, width=200),
        encoding="utf-8",
    )
    golden_path.write_text(
        header_g + yaml.safe_dump(golden_doc, sort_keys=False, allow_unicode=True, width=200),
        encoding="utf-8",
    )
    return corpus_path, golden_path


if __name__ == "__main__":
    msgs, by_id = build_corpus()
    golden = build_golden()
    sources = build_sources()
    print(f"corpus messages: {len(msgs)}")
    for name, entries in sources.items():
        print(f"corpus {name}: {len(entries)}")
    print(f"threads: {len(THREADS)}  topics: {len(set(t['topic_id'] for t in THREADS))}")
    print(f"golden cases: {len(golden)}")
    from collections import Counter
    print("by type:", dict(Counter(c['qt'] for c in CASES)))

    # validate all expected ids exist
    bad = [(c['id'], e) for c in CASES for e in c['expected'] if e not in by_id]
    assert not bad, f"dangling expected ids: {bad}"

    # fairness audit
    stats = audit_overlap(CASES, by_id)
    print("\nFAIRNESS AUDIT (whole-query is the production '%text_contains%' shape):")
    for qt in sorted(stats):
        s = stats[qt]
        print(f"  {qt:14s} n={s['n']:2d}  whole-query-substring-hit={s['whole_hit']:2d}"
              f"  (baseline can score nonzero on these)  zero-substring={s['n']-s['whole_hit']:2d}")

    cp, gp = write_yaml(msgs, golden)
    print(f"\nwrote {cp}")
    print(f"wrote {gp}")
