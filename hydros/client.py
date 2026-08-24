"""
Client for the HYDROS Public API (https://api.coralvuehydros.com).

Reading state is a two-step flow, not a plain GET. POST /device/state/session
mints a 6-hour poll token and wakes the device; the returned pollUrl is then
polled with that token as a Bearer credential. Stop renewing and the device
stops reporting within 6 hours.

Session starts are limited to 5/hour per device, so renewal is deliberately
conservative.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from . import __version__

log = logging.getLogger("hydros.client")

BASE_URL = "https://api.coralvuehydros.com"
SESSION_PATH = "/api/v1/device/state/session"
DEVICE_PATH = "/api/v1/device"
METADATA_PATH = "/api/v1/device/overrides/metadata"

USER_AGENT = f"hydros-exporter/{__version__}"

# Renew this far ahead of expiresAt. 30 min against a 6 h session is ~4 renewals
# a day, well under the 5/hour limit even with restarts.
RENEW_MARGIN_SECONDS = 1800


class AuthError(Exception):
    """401/403. The keys are wrong or lack permission; retrying will not help."""


class TransientError(Exception):
    """429, 5xx or a network fault. Back off and retry."""


class OfflineError(TransientError):
    """404 from the poll: no cached state, so the device is offline."""


@dataclass(frozen=True)
class Session:
    poll_url: str
    poll_token: str
    poll_interval: int
    expires_at: float

    @property
    def renew_at(self) -> float:
        return self.expires_at - RENEW_MARGIN_SECONDS


def _parse_expiry(value: object, duration: object, now: float) -> float:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            log.warning("unparseable expiresAt %r, falling back to durationSeconds", value)
    return now + float(duration or 21600)


class HydrosClient:
    """
    One device key scopes to one device, but the state document it returns
    covers the whole collective, so a single client is enough.
    """

    def __init__(
        self,
        provider_key: str,
        device_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        bearer_auth: bool = False,
    ) -> None:
        self._credential = f"{provider_key}:{device_key}"
        self._base = base_url.rstrip("/")
        # The spec's prose documents a bare `provider:device` Authorization
        # header while its securityScheme declares bearer. We start with the
        # documented form and fall back once on a 401 rather than making the
        # operator guess.
        self._bearer_auth = bearer_auth
        self._auth_settled = False
        self._http = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _key_header(self) -> dict[str, str]:
        prefix = "Bearer " if self._bearer_auth else ""
        return {"Authorization": f"{prefix}{self._credential}"}

    @staticmethod
    def _raise_for_status(resp: httpx.Response, what: str) -> None:
        if resp.status_code in (401, 403):
            raise AuthError(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")
        if resp.status_code == 404:
            raise OfflineError(f"{what}: HTTP 404 {resp.text[:200]}")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")
        if resp.status_code >= 400:
            raise TransientError(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")

    async def _get_with_key(self, path: str, what: str) -> object:
        try:
            resp = await self._http.get(self._base + path, headers=self._key_header())
        except httpx.HTTPError as exc:
            raise TransientError(f"{what}: {exc}") from exc
        self._raise_for_status(resp, what)
        return resp.json()

    async def start_session(self, now: float) -> Session:
        try:
            payload = await self._post_session()
        except AuthError:
            if self._auth_settled:
                raise
            self._bearer_auth = not self._bearer_auth
            log.warning(
                "session start rejected; retrying with %s Authorization form",
                "Bearer" if self._bearer_auth else "bare",
            )
            payload = await self._post_session()

        self._auth_settled = True
        if not isinstance(payload, dict) or "pollUrl" not in payload:
            raise TransientError(f"session start returned no pollUrl: {payload!r}")

        return Session(
            poll_url=str(payload["pollUrl"]),
            poll_token=str(payload["pollToken"]),
            poll_interval=int(payload.get("pollIntervalSeconds") or 30),
            expires_at=_parse_expiry(payload.get("expiresAt"), payload.get("durationSeconds"), now),
        )

    async def _post_session(self) -> object:
        try:
            resp = await self._http.post(self._base + SESSION_PATH, headers=self._key_header())
        except httpx.HTTPError as exc:
            raise TransientError(f"session start: {exc}") from exc
        self._raise_for_status(resp, "session start")
        return resp.json()

    async def poll_state(self, session: Session) -> dict:
        """The poll uses the session's own token, never the device key."""
        try:
            resp = await self._http.get(
                session.poll_url, headers={"Authorization": f"Bearer {session.poll_token}"}
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"state poll: {exc}") from exc
        self._raise_for_status(resp, "state poll")
        payload = resp.json()
        if not isinstance(payload, dict):
            raise TransientError(f"state poll returned {type(payload).__name__}, expected object")
        return payload

    async def get_device(self) -> dict:
        payload = await self._get_with_key(DEVICE_PATH, "device lookup")
        return payload if isinstance(payload, dict) else {}

    async def get_override_metadata(self) -> list[dict]:
        payload = await self._get_with_key(METADATA_PATH, "override metadata")
        if not isinstance(payload, list):
            return []
        return [entry for entry in payload if isinstance(entry, dict)]
