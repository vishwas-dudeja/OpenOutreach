# openoutreach/emails/steps/lookup.py
"""The paid email lookup, in two steps: buy the address, then check on it.

``buy_address`` resolves free sources first — an address already on the lead, then
the hub's cross-operator cache — and only pays BetterContact when both miss. A paid
submit returns a ``request_id`` and nothing waits on it: the deal parks at
FINDING_EMAIL carrying the handle, and ``check_lookup`` polls it later.

    already has email  → READY_TO_EMAIL          (no lookup, no credit)
    free hub-cache hit → READY_TO_EMAIL          (no provider job, no credit)
    hub miss           → FINDING_EMAIL           (job submitted, poll from the deal)
    couldn't submit    → stays READY_TO_FIND_EMAIL (no key / API down — try later)

**The handle lives on the deal**, not in an external row: ``lookup_request_id`` and
``lookup_attempt`` are columns, so an in-flight job survives a restart and its
backoff (``not_before``) gates that one lead and nothing else. The retired task
queue held both in a queue row whose due-date doubled as the daemon's sleep horizon
— two stalled polls once put a whole install to sleep for 34 hours with 55 deals
ready to send.

**A running job is never abandoned.** There is no deadline and no attempt limit:
the poll interval doubles on the same ``request_id`` and the deal waits. Abandoning
was tried and is worse — it reverted the deal to READY_TO_FIND_EMAIL, where the buy
step bought a *second* job for the same lead, turning a provider outage into 418
submits and 4,512 polls in a week for ~40 leads, none terminating. Doubling makes
waiting nearly free (a week costs 17 polls), and it refuses to mislabel: a timeout
is evidence about the provider, never about whether this person has a findable
address.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from openoutreach.core.conf import COLLECT_BACKOFF_BASE_S, COLLECT_BACKOFF_MAX_S
from openoutreach.core.logblock import block_header, step_line
from openoutreach.crm.models import DealState

logger = logging.getLogger(__name__)


def buy_address(deal) -> DealState | None:
    """Resolve this deal's work email privately, using BetterContact when needed."""
    logger.info("%s", block_header(
        f"buy_address · {deal.campaign} · {deal.lead.profile_url}", "cyan"))

    # Already resolved locally — no lookup or provider credit required.
    if deal.lead.email:
        logger.info("%s", step_line(
            "known email",
            "already resolved → READY_TO_EMAIL",
            glyph="✓",
            color="green",
        ))
        return DealState.READY_TO_EMAIL

    # Vina deployment: skip the OpenOutreach shared contacts hub entirely.
    return _submit(deal)


def _submit(deal) -> DealState | None:
    """Fire the paid provider job and park the deal on its handle.

    A couldn't-submit (no key, API down) leaves the deal where it is: no credit was
    spent and no handle exists to poll, so the next cycle simply tries again.
    """
    from openoutreach.emails import bettercontact
    from openoutreach.emails.bettercontact import BetterContactQuery, BetterContactUnavailable

    if not bettercontact.is_configured():
        logger.info("%s", step_line(
            "bettercontact", "finder unconfigured — left queued", glyph="⚠", color="yellow"))
        return None

    try:
        request_id = bettercontact.submit(
            BetterContactQuery(linkedin_url=deal.lead.profile_url))
    except BetterContactUnavailable as exc:
        logger.info("%s", step_line(
            "bettercontact", f"submit unavailable ({exc}) — left queued",
            glyph="⚠", color="yellow"))
        return None

    deal.lookup_request_id = request_id
    deal.lookup_attempt = 0
    deal.not_before = timezone.now() + timedelta(seconds=COLLECT_BACKOFF_BASE_S)
    logger.info("%s", step_line(
        "bettercontact", f"submitted · req {request_id[:12]}… → FINDING_EMAIL · polling",
        glyph="✓", color="green"))
    return DealState.FINDING_EMAIL


def reclaim_lookup(deal) -> DealState:
    """Send a FINDING_EMAIL deal that carries no job handle back to be bought.

    ``_submit`` only returns FINDING_EMAIL once it has a ``request_id``, so a deal
    parked here with an empty one has no job to poll and never had a credit spent on
    it. Left alone it is invisible to every query — the poll row skips it and no
    other row claims FINDING_EMAIL — so it waits forever while still counting
    against the day's send headroom. Measured on a live install: two deals stranded
    for 206 hours. Going back to READY_TO_FIND_EMAIL puts it in front of the buy
    step again, under the same spend gate as everyone else.
    """
    logger.info("%s", block_header(
        f"reclaim_lookup · {deal.campaign} · {deal.lead.profile_url}", "yellow"))
    deal.not_before = None
    deal.lookup_attempt = 0
    logger.info("%s", step_line(
        "no job handle", "nothing to poll → READY_TO_FIND_EMAIL",
        glyph="⚠", color="yellow"))
    return DealState.READY_TO_FIND_EMAIL


def check_lookup(deal) -> DealState | None:
    """Poll this deal's in-flight lookup exactly once and act on the outcome.

        hit           → READY_TO_EMAIL (address stored locally)
        miss          → NO_EMAIL_BETTERCONTACT (terminal — a fit positive the ML keeps)
        still running → back off, stay put
        couldn't poll → retry at the same interval (nothing was learned about the job)
    """
    from openoutreach.emails import bettercontact
    from openoutreach.emails.bettercontact import BetterContactUnavailable

    logger.info("%s", block_header(
        f"check_lookup · {deal.campaign} · {deal.lead.profile_url}", "magenta",
        meta=f"attempt {deal.lookup_attempt}"))

    try:
        outcome = bettercontact.poll_once(deal.lookup_request_id)
    except BetterContactUnavailable as exc:
        # Transient — the job is untouched, so wait the same interval and ask again.
        logger.info("%s", step_line(
            "poll", f"unavailable ({exc}) — retrying", glyph="⚠", color="yellow"))
        _back_off(deal, advance=False)
        return None

    if outcome.running:
        _back_off(deal, advance=True)
        logger.info("%s", step_line(
            "running", f"not ready — re-poll in {_human(_delay_for(deal))} "
                       f"(attempt {deal.lookup_attempt})"))
        return None

    deal.not_before = None
    deal.lookup_request_id = ""

    if not outcome.email:
        logger.info("%s", step_line(
            "no email", "terminal miss → NO_EMAIL_BETTERCONTACT", glyph="✗", color="yellow"))
        return DealState.NO_EMAIL_BETTERCONTACT

    deal.lead.email = outcome.email
    deal.lead.save(update_fields=["email"])
    logger.info("%s", step_line(
        "hit", f"{outcome.email} → READY_TO_EMAIL", glyph="✓", color="green"))
    return DealState.READY_TO_EMAIL


# ── Backoff ───────────────────────────────────────────────────────


def _back_off(deal, *, advance: bool) -> None:
    """Push this deal's next poll out. ``advance`` doubles the interval.

    A genuine still-running poll doubles; a transient provider outage retries at the
    same interval, since nothing was learned about the job. The interval rails at
    ``COLLECT_BACKOFF_MAX_S`` (a month) purely so the schedule stays representable —
    the chain itself never ends.
    """
    if advance:
        deal.lookup_attempt += 1
    deal.not_before = timezone.now() + timedelta(seconds=_delay_for(deal))


def _delay_for(deal) -> float:
    return min(COLLECT_BACKOFF_BASE_S * (2 ** min(deal.lookup_attempt, 64)),
               COLLECT_BACKOFF_MAX_S)


def _human(seconds: float) -> str:
    """A backoff delay at a readable scale — these run from seconds to weeks."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.1f}{unit}"
    return f"{seconds:.0f}s"
