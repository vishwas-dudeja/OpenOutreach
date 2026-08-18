# openoutreach/core/cycle.py
"""The cycle — wake up, do the single most valuable thing available, sleep.

**A queue is a status, not a table.** Work is found by asking the deals what they
need (``Deal.objects.filter(state=...)``), so a deal is available because of its own
row. Nothing is created in advance, which means nothing can drift, be lost, or need
reconciling — and no row's timestamp can gate anything but itself.

What this replaces was a *token loop*. ``core/scheduler.py`` wrote ``Task`` rows that
were permission tokens stamped with a time — "at 14:32 someone may send one email for
campaign A" — without saying to whom; the loop took the earliest-due token, and the
handler then went and found its own target. When no token was due it slept **until
the earliest token's timestamp**. On 2026-08-05 that put a live install to sleep for
34 hours: two BetterContact polls had never terminated, their uncapped backoff had
reached 45h30m, they were the only rows in the table, and 55 ready deals with 70
sends of headroom simply had no token and therefore were not work. The shape was
inherited from the Playwright era, when a token queue was how access to one browser
got serialised. The browser went; the queue outlived it.

Here the sleep is a fixed short interval and capacity is re-read every time.

## The hierarchy

One ordered list. The cycle walks it top to bottom and stops at the first thing it
can do, so priority is just the order these are written in:

    1  FINDING_EMAIL          check the lookup            (not_before elapsed)
    2  EMAILED + reply        answer the reply            (a lead wrote back)
    3  READY_TO_EMAIL         send the first email        (a mailbox is free)
    4  QUALIFIED              score with the campaign's model
    5  READY_TO_FIND_EMAIL    buy the address             (room to send today)
    6  (the campaign itself)  top up the pipeline         (room to send today)

A state that is not listed is terminal, and terminal costs nothing: NO_EMAIL_
BETTERCONTACT, UNSUBSCRIBED, COMPLETED, FAILED — and EMAILED with no unanswered
reply, which is where most deals come to rest, because nobody is ever chased.

Rows 5 and 6 share one condition, and it is the only spend control there is:
**never resolve an address, and never spend an LLM call qualifying, for someone
there is no room to email today.** Idle is the *normal* state here — sends are paced
minutes apart and a cycle is seconds — so "nothing better to do" cannot bound
discovery on its own; that condition is what does.

Campaigns take turns (``_rotate``). There is no share, no weight and no allocation:
with nothing minted in advance there is no budget to split, so the fairness question
collapses to whose turn it is.
"""
from __future__ import annotations

import logging
import time

from django.utils import timezone
from pydantic_ai.exceptions import ModelHTTPError
from termcolor import colored

from openoutreach.core.sending_window import within_sending_window
from openoutreach.crm.models import DealState

logger = logging.getLogger(__name__)

# How long to wait after each action. Short, fixed, and derived from nothing: the
# cycle's whole point is that no data decides when it next wakes up. It bounds how
# precisely a ``not_before`` can be honoured, so keep it well under the send spacing.
CYCLE_SECONDS = 5

# How often an idle cycle says so. Idle is the *normal* state here — sends are paced
# minutes apart and a cycle is seconds — so a line per cycle would bury the lines that
# mean something. One a minute is a pulse: enough to tell a working daemon from a
# wedged one, and it carries the counts that say why there was nothing to do.
IDLE_LOG_INTERVAL_S = 60

# How often the mail pass logs into each box. The daemon's only inbound latency —
# a reply is invisible until the next pass — traded against one IMAP login per box
# per interval. Minutes, not seconds: nobody expects an answer inside a minute, and
# a login a second would be its own kind of rude.
MAIL_PASS_INTERVAL_S = 300

# Errors that must stop the daemon rather than be retried. A misconfigured LLM key
# is not a transient fault: every cycle would raise it, log it and try again in five
# seconds, forever, while the operator sees a daemon that looks alive and does
# nothing. Everything else is logged and skipped — the row is left untouched and the
# next cycle moves on.
HALTING_ERRORS = (ModelHTTPError,)

_mail_read_at = None

# When the last "nothing to do" line went out, so the pulse is one a minute rather
# than one a cycle. Reset by any action, so the first idle after work always prints.
_idle_logged_at: float | None = None

# Per-campaign `(qualified, past-the-gate)` counts as of the last scoring pass, so a
# pool that has not moved is not re-scored. Process-local by design: there is one
# process, and a restart simply scores once more than it had to.
_scored_at: dict[int, tuple[int, int]] = {}

# ── The loop ──────────────────────────────────────────────────────


def run_daemon() -> None:
    """Run the cycle until the process is stopped or a halting error is raised."""
    from openoutreach.core.operator import campaigns

    known = campaigns()
    if not known:
        logger.error("No campaigns found — cannot start daemon")
        return

    logger.info("%s — %d campaign(s), one action per cycle",
                colored("Daemon started", "green", attrs=["bold"]), len(known))

    rotation = _rotate()
    while True:
        try:
            read_mail_if_due()
            refresh_capacities_if_due()
            run_one_action(next(rotation))
        except HALTING_ERRORS as exc:
            logger.error(
                colored("Daemon stopped", "red", attrs=["bold"]) + " — %s\n"
                "Check ai_model (provider:model), llm_api_key and llm_api_base "
                "in Admin → Site Configuration.", exc,
            )
            return
        except Exception:
            logger.exception("Cycle failed — skipping to the next one")
        time.sleep(CYCLE_SECONDS)


def _rotate():
    """Endless round-robin over the operator's campaigns, re-read each lap.

    Re-reading matters on a fresh install or dynamic updates: campaigns can be created
    during onboarding or added while the daemon is running, so a rotation frozen
    at boot would run one campaign forever.
    """
    from openoutreach.core.operator import campaigns

    while True:
        current = campaigns()
        if not current:
            yield None
            continue
        yield from current


def run_one_action(campaign) -> bool:
    """Do the highest-priority thing available for *campaign*. Returns whether it did.

    Each row is a query and a step. The first one that produces work wins and the
    cycle returns — nothing below it runs, which is what makes the order a priority.

    Every row is timed and named, because the hierarchy is also the only account of
    where the daemon's time goes: the steps log what they *did*, but a row that takes
    twenty seconds to decide it has nothing to do says so nowhere else.
    """
    global _idle_logged_at
    if campaign is None:
        return False

    for name, row in ROWS:
        logger.debug("[%s] → %s?", campaign, name)
        started = time.monotonic()
        acted = row(campaign)
        elapsed = time.monotonic() - started
        if acted:
            logger.info("[%s] %s — %.1fs", campaign,
                        colored(name, "cyan", attrs=["bold"]), elapsed)
            _idle_logged_at = None
            return True
        logger.debug("[%s] %s: nothing (%.1fs)", campaign, name, elapsed)
    _log_idle(campaign)
    return False


def _log_idle(campaign) -> None:
    """Say the daemon is alive and why it has nothing to do — at most once a minute.

    The counts are the hierarchy's own queries in summary form, so the line answers
    the question idleness actually raises: is there no work, or is there work behind
    a gate? A pipeline that is empty and a pipeline that is full but out of send
    headroom look identical from outside and are entirely different problems.
    """
    global _idle_logged_at

    now = time.monotonic()
    if _idle_logged_at is not None and now - _idle_logged_at < IDLE_LOG_INTERVAL_S:
        return
    _idle_logged_at = now
    logger.info("[%s] idle — %s", campaign, _pipeline_summary(campaign))


# What each waiting state means to someone reading a log, in pipeline order. The state
# names are the schema's vocabulary and belong in the schema — READY_TO_FIND_EMAIL says
# where a deal sits in a diagram, "waiting for an address to be bought" says what is
# actually happening to that person.
_WAITING_ON = (
    (DealState.QUALIFIED, "waiting to be ranked"),
    (DealState.READY_TO_FIND_EMAIL, "waiting for an address to be bought"),
    (DealState.FINDING_EMAIL, "address on order"),
    (DealState.READY_TO_EMAIL, "waiting to be emailed"),
)


def _pipeline_summary(campaign) -> str:
    """One line of counts: who is waiting on what, against today's send headroom."""
    from django.db.models import Count

    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Mailbox

    counts = dict(
        Deal.objects.filter(campaign=campaign, lead__disqualified=False)
        .values_list("state")
        .annotate(n=Count("state"))
        .values_list("state", "n"),
    )
    waiting = [f"{counts.get(state, 0)} {phrase}" for state, phrase in _WAITING_ON]
    waiting.append(f"{unanswered_replies(campaign).count()} reply(ies) to answer")

    left = Mailbox.objects.remaining_today()
    # The gate, said as its consequence rather than as its name: `room to spend=False`
    # tells you a boolean, "not buying or qualifying" tells you why two rows declined.
    spending = ("buying and qualifying as needed" if room_to_send_today(campaign)
                else "no send headroom left, so not buying or qualifying")
    # Same reasoning for the clock: a stocked queue and free headroom at 22:00 looks
    # like a stuck daemon unless the line says which gate is holding it.
    clock = "" if within_sending_window() else " · outside sending hours, so not emailing"
    return f"{' · '.join(waiting)} · {left} send(s) left today · {spending}{clock}"


# ── 1. Check an in-flight lookup ──────────────────────────────────


def _check_lookups(campaign) -> bool:
    from openoutreach.emails.steps.lookup import check_lookup, reclaim_lookup

    deal = _due(campaign, DealState.FINDING_EMAIL).first()
    if deal is None:
        return False
    # A deal here without a handle has no job to poll — reclaim it rather than skip
    # it, or it sits in a state no other row claims for as long as the install runs.
    step = check_lookup if deal.lookup_request_id else reclaim_lookup
    return _apply(deal, step(deal))


# ── 2. Answer a reply ─────────────────────────────────────────────


def _answer_replies(campaign) -> bool:
    from openoutreach.emails.steps.reply import answer_reply

    deal = unanswered_replies(campaign).first()
    if deal is None:
        return False
    return _apply(deal, answer_reply(deal))


def unanswered_replies(campaign):
    """EMAILED deals whose newest inbound turn is newer than our newest outgoing one.

    This is the entire follow-up trigger. No timer, no flag, no bookkeeping: the
    mail pass writes inbound rows and the comparison between the two newest
    timestamps says whether the ball is in our court. Oldest reply first, so nobody
    waits behind a livelier thread.

    **Turns, not messages.** A bounce and an out-of-office are in the thread and
    are not somebody writing back, so neither can make a deal actionable — the
    loop that apologised twice to a dead address is closed by the same rule that
    keeps NDRs out of the summary.

    **The two timestamps are subqueries, not aggregates.** ``Max(...)`` over the joined
    messages reads better and is what this asked for originally, but an aggregate makes
    the query a GROUP BY over every selected column — and ``select_related`` puts the
    campaign, lead and mailbox rows in that list. Grouping then carries the BLOBs:
    ``Campaign.model_blob`` (the pickled GP, 10.5 MB once a campaign has ~1,350 labels),
    ``Campaign.anchor_embeddings`` and ``Lead.embedding``, repeated per joined row. On a
    live install that single ``.first()`` allocated **6.5 GB** and ran for ten seconds,
    against 326 EMAILED deals — it climbed to ~3 GB on the 3.8 GB production VM, drove
    the box into swap, and is what SIGKILLed the daemon. A campaign whose GP has few
    labels has a small blob and shows nothing, so this scales with the operator's
    success. Row 2 runs it every cycle and the idle pulse ``.count()``s it, so it is
    also the hottest query in the loop. A correlated subquery per timestamp needs no
    grouping at all and reads the same.
    """
    from django.db.models import F, OuterRef, Q, Subquery

    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Direction, Message
    from openoutreach.emails.models.maillog import TURN_KINDS

    def newest(direction: str) -> Subquery:
        return Subquery(
            Message.objects
            .filter(thread=OuterRef("thread_id"), direction=direction, kind__in=TURN_KINDS)
            .order_by("-sent_at")
            .values("sent_at")[:1]
        )

    return (
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.EMAILED,
            outcome="",
            lead__disqualified=False,
            thread__isnull=False,
        )
        .annotate(
            last_in=newest(Direction.INBOUND),
            last_out=newest(Direction.OUTBOUND),
        )
        .filter(last_in__isnull=False)
        .filter(Q(last_out__isnull=True) | Q(last_in__gt=F("last_out")))
        .select_related("lead", "campaign", "mailbox")
        .order_by("last_in")
    )


# ── 3. Send a first email ─────────────────────────────────────────


def _send_first_emails(campaign) -> bool:
    from openoutreach.emails.models import Mailbox
    from openoutreach.emails.steps.send import send_first_email

    mailbox = Mailbox.objects.free_for_first_email()
    if mailbox is None:
        return False
    deal = (
        _due(campaign, DealState.READY_TO_EMAIL)
        .filter(lead__disqualified=False)
        .exclude(lead__email__isnull=True)
        .exclude(lead__email="")
        .first()
    )
    if deal is None:
        return False
    return _apply(deal, send_first_email(deal, mailbox))


# ── 4. Score the qualified pool ───────────────────────────────────


def _score_qualified(campaign) -> bool:
    """Promote every QUALIFIED deal the campaign's model is confident about.

    The one step that is per-campaign rather than per-deal: building the model
    dominates the cost of using it, so once it is in hand it scores the whole pool
    in a single pass and is then dropped.

    Skipped entirely while nothing has changed since the last pass. Scoring is a
    pure function of two things — the labels the GP fits on and the pool it scores —
    so re-running it against the same counts cannot promote anybody, and a campaign
    whose pool sits below the gate would otherwise refit a GP every few seconds
    forever (measured: ~1.1s at 300 labels, against a 5s cycle). The counts are two
    indexed `COUNT`s, and being wrong costs one cycle's delay, never a wrong answer.
    """
    from openoutreach.core.ml.qualifier import qualifier_for
    from openoutreach.core.pipeline.ready_pool import promote_to_ready

    before = _pool_signature(campaign)
    if before[0] == 0 or _scored_at.get(campaign.pk) == before:
        return False

    qualifier = qualifier_for(campaign)
    if qualifier is None:
        return False
    promoted = promote_to_ready(campaign, qualifier)
    _scored_at[campaign.pk] = _pool_signature(campaign)
    return promoted > 0


def _pool_signature(campaign) -> tuple[int, int]:
    """`(deals awaiting the gate, deals already past it)` — what scoring depends on.

    The second count stands in for the label set: every state but QUALIFIED is a
    verdict the GP fits on, and the anchors thin out in step with acceptances, so
    any change to the model's inputs moves one of these two numbers.
    """
    from openoutreach.crm.models import Deal

    of_campaign = Deal.objects.filter(campaign=campaign)
    return (
        of_campaign.filter(state=DealState.QUALIFIED).count(),
        of_campaign.exclude(state=DealState.QUALIFIED).count(),
    )


# ── 5. Buy an address ─────────────────────────────────────────────


def _buy_addresses(campaign) -> bool:
    from openoutreach.emails.models import has_mailbox
    from openoutreach.emails.steps.lookup import buy_address

    if not has_mailbox() or not room_to_send_today(campaign):
        return False
    deal = _due(campaign, DealState.READY_TO_FIND_EMAIL).filter(
        lead__disqualified=False).first()
    if deal is None:
        return False
    return _apply(deal, buy_address(deal))


# ── 6. Top up the pipeline ────────────────────────────────────────


def _top_up(campaign) -> bool:
    from openoutreach.core.pipeline.top_up import top_up
    from openoutreach.emails.models import has_mailbox

    if not has_mailbox() or not room_to_send_today(campaign):
        return False
    return top_up(campaign)


def room_to_send_today(campaign) -> bool:
    """True when this campaign has fewer people heading for a send than sends left today.

    The gate on both spending money and spending LLM calls: never resolve an address,
    and never qualify a lead, for someone there is no room to email today. Paid spend
    rides on send capacity — there is no separate budget and no cap to tune.

    Deliberately same-day and deliberately simple. It empties the pipeline at the end
    of a day, so the first send after midnight waits on one qualification and one
    provider lookup; sends are minutes apart anyway, and the alternative is a
    stockpile of addresses bought against capacity that may never arrive.

    "Heading for a send" is READY_TO_EMAIL plus the in-flight lookups that could
    still land today. A lookup whose backoff has pushed its next poll past
    ``COLLECT_TODAY_HORIZON_S`` cannot produce a send today, and counting it would
    let a handful of stalled jobs wedge the pipeline shut for as long as they stay
    stalled — the backoff is uncapped by design, so that is weeks.
    """
    from datetime import timedelta

    from django.db.models import Q

    from openoutreach.core.conf import COLLECT_TODAY_HORIZON_S
    from openoutreach.crm.models import Deal
    from openoutreach.emails.models import Mailbox

    horizon = timezone.now() + timedelta(seconds=COLLECT_TODAY_HORIZON_S)
    heading_for_a_send = (
        Deal.objects.filter(campaign=campaign, lead__disqualified=False)
        .filter(
            Q(state=DealState.READY_TO_EMAIL)
            | Q(state=DealState.FINDING_EMAIL, not_before__lte=horizon),
        )
        .count()
    )
    return heading_for_a_send < Mailbox.objects.remaining_today()


# ── Periodic side-effects ─────────────────────────────────────────


def read_mail_if_due() -> None:
    """Walk every mailbox's new mail — replies and opt-outs in one pass per box.

    A periodic side-effect rather than a state transition: no entity is moving, the
    world is simply being read. Best-effort per box, so one unreachable mailbox does
    not stop the others being read.
    """
    global _mail_read_at
    from openoutreach.emails.mail_pass import run_mail_pass

    now = timezone.now()
    if _mail_read_at and (now - _mail_read_at).total_seconds() < MAIL_PASS_INTERVAL_S:
        return
    run_mail_pass()
    _mail_read_at = now


def refresh_capacities_if_due() -> None:
    """Re-measure every mailbox's warm capacity, once a day.

    Costs an IMAP round trip per box and moves on the scale of days, so it is stamped
    daily however it went — an unreachable box costs one timeout a day, not one a
    cycle.
    """
    from openoutreach.emails.models import Mailbox
    from openoutreach.emails.warmth import mark_measured, measurement_due, refresh_capacity

    if not measurement_due():
        return
    boxes = list(Mailbox.objects.all())
    logger.info("warmth: re-measuring %d mailbox(es)", len(boxes))
    for box in boxes:
        refresh_capacity(box)
    mark_measured()


# ── The hierarchy ─────────────────────────────────────────────────

# The ordered list from this module's docstring, made data so the walk can name the
# row it fired. Defined here rather than at the top because it holds the functions
# themselves — the order of this tuple *is* the priority, and there is nowhere else
# it is written down.
#
# The names are what the operator reads in the log, so they say what happens to a
# lead, not which function ran. "top up" named the function and explained nothing;
# "find & qualify new leads" is what that row actually does.
ROWS = (
    ("check for the email address we ordered", _check_lookups),
    ("answer a reply", _answer_replies),
    ("send a first email", _send_first_emails),
    ("rank the qualified leads", _score_qualified),
    ("buy an email address", _buy_addresses),
    ("find & qualify new leads", _top_up),
)


# ── Plumbing ──────────────────────────────────────────────────────


def _due(campaign, state):
    """This campaign's deals in *state* that are not waiting on a ``not_before``."""
    from django.db.models import Q

    from openoutreach.crm.models import Deal

    return (
        Deal.objects.filter(campaign=campaign, state=state)
        .filter(Q(not_before__isnull=True) | Q(not_before__lte=timezone.now()))
        .select_related("lead", "campaign", "mailbox")
        .order_by("creation_date")
    )


def _apply(deal, next_state) -> bool:
    """Persist whatever the step did to *deal*, including its new state.

    One save for the transition and the fields that justify it, so a deal can never
    be recorded as sent without its audit fields, or moved out of READY_TO_EMAIL
    without the send being recorded. A step that returns ``None`` has decided to stay
    put — it has usually written ``not_before`` — and that is saved just the same.
    """
    if next_state is not None:
        deal.state = next_state
    deal.save()
    return True


def _import_freemium_campaign() -> None:
    """Pull the published kit once at startup and mirror it into a local campaign.

    Config import, not model loading: the kit's *model* is fetched on demand by
    ``qualifier_for``. Seeds are imported here too, though the published seed list is
    empty in practice — the freemium campaign feeds on leads other campaigns have
    already discovered.
    """
    from openoutreach.core.ml.hub import fetch_kit
    from openoutreach.core.setup.freemium import import_freemium_campaign, seed_profiles

    kit = fetch_kit()
    if not kit:
        return
    campaign = import_freemium_campaign(kit["config"])
    if campaign:
        seed_profiles(campaign, kit["config"])
