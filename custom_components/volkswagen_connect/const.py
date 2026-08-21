"""Constants for the Volkswagen Connect integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "volkswagen_connect"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_BRAND = "brand"
CONF_OTP = "otp"
# Persisted website-portal session cookies (enables the optional authproxy
# data source + silent refresh across restarts).
CONF_WEBSITE_COOKIES = "website_cookies"
# User-configurable poll interval, in minutes (config entry option). Falls
# back to DEFAULT_SCAN_INTERVAL when unset.
CONF_SCAN_INTERVAL = "scan_interval"

# Polling cadence. The portal delivers at most one dataset per 15 min, so there
# is no value in polling faster; we add a small offset to avoid hammering on the
# exact slot boundary.
DEFAULT_SCAN_INTERVAL = timedelta(minutes=15)
# The EU Data Act feed itself only refreshes every 15 min regardless, but the
# website-portal channel (charging/warning lights/maintenance) is live - a
# shorter interval still gets fresher portal data. The session-refresh
# throttle in coordinator.py (_MIN_REFRESH_INTERVAL_S = 600s) is the actual
# safety net against polling too aggressively, not this floor.
MIN_SCAN_INTERVAL_MINUTES = 5
MAX_SCAN_INTERVAL_MINUTES = 240

# Vehicle "status" sensor states
STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"
STATUS_NOT_CONFIGURED = "not_configured"
