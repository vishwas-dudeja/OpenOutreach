# Vina Technologies Fork Customizations

This document details the intentional production customizations implemented for the **Vina Technologies** deployment of OpenOutreach on the `vina-production` branch.

## Summary of Customizations

1. **Freemium Promotional Campaign Importer Disabled**:
   - `openoutreach.core.cycle.run_daemon()` no longer invokes `_import_freemium_campaign()` at startup.

2. **Freemium Campaigns Excluded from Operator Rotation**:
   - `openoutreach.core.operator.campaigns()` explicitly filters campaigns with `is_freemium=False` so any legacy or freemium campaign rows never enter the production outreach rotation.

3. **Shared Contact Hub Lookup Disabled**:
   - `openoutreach.emails.steps.lookup.buy_address()` skips calling `openoutreach.contacts.service.resolve()`. Prospects missing a local email proceed directly to BetterContact resolution.

4. **Shared Contact Hub Contribution Disabled**:
   - `openoutreach.emails.steps.lookup.check_lookup()` stores BetterContact-discovered work emails strictly in the local database (`deal.lead.email`) and does NOT call `openoutreach.contacts.service.contribute()`.

5. **OpenOutreach Promotional Attribution Removed**:
   - `openoutreach.emails.sender._build_message()` assembles outbound messages as `_opt_out(_sign(body, mailbox.signature))`.
   - The promotional attribution footer (`"Sent with OpenOutreach"`) and helper definitions (`ATTRIBUTION`, `_attribute()`) have been removed from `sender.py`.
   - Production message flow is strictly: `AI generated message → mailbox signature → visible opt-out → SEND`.

6. **Deliverability & Unsubscribe Safeguards Preserved**:
   - Mailbox signatures, `List-Unsubscribe` headers, visible opt-out lines (`OPT_OUT_LINE`), suppression handling (`suppressed()`), bounce detection, and reply threading remain fully operational.

## Primary Files Modified / Added

- `openoutreach/emails/steps/lookup.py`
- `openoutreach/emails/sender.py`
- `openoutreach/core/operator.py`
- `openoutreach/core/cycle.py`
- `tests/emails/test_lookup.py`
- `tests/emails/test_send.py`
- `tests/emails/test_unsubscribe.py`
- `tests/test_vina_customizations.py`
- `VINA_CUSTOMIZATIONS.md`
