"""Typed classification for the auth failures raised by the two clients.

``EuDataActClient``/``WebsitePortalClient`` stay Home-Assistant-independent and
keep their own lightweight exceptions (``EuDataActAuthError``,
``WebsitePortalAuthError``) with a free-form ``reason`` string — that string
already doubles as a config-flow error key (see ``strings.json``). This module
adds a small classification layer on top of that same vocabulary: a coarse
split between "this needs the user to do something" and "this is transient,
don't nag for a password", which the coordinator (and, later, a Repairs flow)
can act on without hardcoding the reason-string list themselves.
"""

from __future__ import annotations


class VolkswagenConnectError(Exception):
    """Base class for every typed classification in this module."""


class AuthenticationError(VolkswagenConnectError):
    """Terminal: wrong credentials, needs a fresh login."""


class UpstreamUnavailableError(VolkswagenConnectError):
    """Transient: VW's backend is down/erroring - must NOT trigger reauth."""


class RateLimitError(AuthenticationError):
    """Too many login attempts; VW is throttling this account."""


class TwoFactorRequiredError(AuthenticationError):
    """A second factor is required to complete login.

    Not reachable via ``classify()`` yet - the website portal's OTP challenge
    is already handled as its own config-flow step rather than a ``reason``
    string, and EU Data Act has not been observed to require one. Kept here
    so a future login path (or a finer OTP-failure reason) has somewhere to
    plug in without inventing a parallel hierarchy.
    """


class EmailTwoFactorRequiredError(TwoFactorRequiredError):
    """The second factor is an emailed one-time code."""


class PortalInteractionRequiredError(AuthenticationError):
    """The account needs a manual step on VW's own site (consent, vehicle
    linking, enabling a data request, ...) - not a wrong password."""


# Maps the free-form ``reason`` strings already used by EuDataActAuthError /
# WebsitePortalAuthError (which double as config-flow error keys, see
# strings.json) to a typed class above.
REASON_TO_EXCEPTION: dict[str, type[VolkswagenConnectError]] = {
    "invalid_auth": AuthenticationError,
    "account_locked": AuthenticationError,
    "throttled": RateLimitError,
    "not_authorised": PortalInteractionRequiredError,
    "cannot_connect": UpstreamUnavailableError,
}


def classify(reason: str) -> type[VolkswagenConnectError]:
    """Return the typed exception class for a config-flow error-key reason.

    Unknown/empty reasons default to ``AuthenticationError`` - safer to treat
    an unclassified auth failure as terminal (ask the user to log in again)
    than to silently swallow it as transient.
    """
    return REASON_TO_EXCEPTION.get(reason, AuthenticationError)
