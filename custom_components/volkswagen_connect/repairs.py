"""HA Repairs integration for auth failures.

Without this, a failing login only shows up as a log line plus entities
quietly going unavailable, and the generic "Reauthentication required"
notice HA shows for a bare ConfigEntryAuthFailed. This surfaces a persistent
issue under Settings -> System -> Repairs, worded for the actual reason
(``exceptions.classify``'s vocabulary - the same ``reason`` strings already
used as config-flow error keys, see strings.json's ``config.error`` block),
and gives the fixable ones a "Repair" button that jumps straight into the
reauth step instead of making the user find it themselves.
"""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
import homeassistant.helpers.issue_registry as ir

from .const import DOMAIN
from .eu_data_act import BASE_URL

# Every reason we currently classify is resolved by (re)running the reauth
# step - even "not_authorised", whose actual fix is a manual step in a
# browser, still ends with the user re-entering their password here so the
# entry is validated again afterwards.
_FIXABLE_REASONS: dict[str, str] = {
    "invalid_auth": "reauth",
    "account_locked": "reauth",
    "throttled": "reauth",
    "not_authorised": "reauth",
}

# Reasons with a translation entry under "issues" in strings.json. Anything
# else (a reason we haven't classified yet) falls back to the generic
# "auth_failed" translation instead of showing a raw, untranslated key.
_KNOWN_REASONS = (*_FIXABLE_REASONS, "cannot_connect")


def _learn_more_url(reason: str) -> str | None:
    """A deep link worth offering for this specific reason, if any."""
    if reason == "not_authorised":
        return f"{BASE_URL}/"
    return None


def raise_issue_auth_required(hass: HomeAssistant, entry_id: str, reason: str) -> None:
    """Create (or refresh) a Repair issue for an auth failure on this entry.

    Idempotent: HA de-dupes by issue id, so calling this again on every
    failed poll just keeps the existing issue's timestamp fresh.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{entry_id}_{reason}",
        is_fixable=reason in _FIXABLE_REASONS,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=reason if reason in _KNOWN_REASONS else "auth_failed",
        learn_more_url=_learn_more_url(reason),
        data={"entry_id": entry_id, "reason": reason},
    )


def clear_auth_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every possible auth Repair issue for this entry after a success."""
    for reason in (*_KNOWN_REASONS, "auth_failed"):
        ir.async_delete_issue(hass, DOMAIN, f"{entry_id}_{reason}")


class _AuthRepairFlow(RepairsFlow):
    """Delegates to the reauth config-flow step so the user fixes it in place."""

    def __init__(self, entry_id: str, reason: str) -> None:
        self._entry_id = entry_id
        self._reason = reason

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                description_placeholders={"reason": self._reason},
            )
        flow_step = _FIXABLE_REASONS.get(self._reason, "reauth")
        await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": flow_step, "entry_id": self._entry_id}
        )
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """HA RepairsFlow factory, called when the user clicks "Repair"."""
    entry_id = (data or {}).get("entry_id", "")
    reason = (data or {}).get("reason", "auth_failed")
    return _AuthRepairFlow(entry_id, reason)
