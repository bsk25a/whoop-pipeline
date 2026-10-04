"""
WHOOP v2 API client.

Two things make this API awkward and both are handled here:

1. Refresh tokens ROTATE. Every call to the token endpoint invalidates the
   refresh token you just used and hands back a new one. If the process dies
   between "refresh succeeded" and "new token persisted", the account is
   locked out and needs a manual re-authorisation. So the new token is written
   to durable storage via a callback BEFORE the access token is returned to
   the caller.

2. Collections are cursor paginated with a `next_token` that must be echoed
   back as `nextToken`, and `limit` is capped server-side (25).

Rate limits are 100 req/min and 10,000 req/day. Pagination at 25 records per
page means a 6 month backfill is a few hundred requests — well inside both,
but a 429 is still retried with backoff because the limit is shared across
every app using the same client ID.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator

import requests

log = logging.getLogger(__name__)

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"

# `offline` is required or the token endpoint returns no refresh token at all
# and the whole thing becomes a one-shot script.
SCOPES = [
    "offline",
    "read:profile",
    "read:body_measurement",
    "read:cycles",
    "read:recovery",
    "read:sleep",
    "read:workout",
]

MAX_PAGE_LIMIT = 25
MAX_RETRIES = 5


class WhoopAuthError(RuntimeError):
    """Raised when the refresh token is dead and manual reauthorisation is needed."""


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float  # POSIX seconds

    @classmethod
    def from_response(cls, payload: dict) -> "Token":
        refresh = payload.get("refresh_token")
        if not refresh:
            raise WhoopAuthError(
                "Token response contained no refresh_token. The 'offline' scope "
                "was almost certainly not granted — re-run scripts/authorize.py."
            )
        # 60s safety margin so we never present a token that expires in flight.
        return cls(
            access_token=payload["access_token"],
            refresh_token=refresh,
            expires_at=time.time() + float(payload.get("expires_in", 3600)) - 60,
        )

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Token":
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_at=float(d.get("expires_at", 0)),
        )


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> Token:
    """One-time exchange of an authorisation code for the first token pair."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if not resp.ok:
        raise WhoopAuthError(f"Code exchange failed [{resp.status_code}]: {resp.text}")
    return Token.from_response(resp.json())


class WhoopClient:
    """
    Thin authenticated wrapper over the v2 API.

    `on_token_refresh` is called with the new Token the instant a refresh
    succeeds and must durably persist it. If it raises, the refresh is treated
    as failed, because an unpersisted rotated token is worse than no token.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token: Token,
        on_token_refresh: Callable[[Token], None],
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = token
        self._on_refresh = on_token_refresh
        self._session = requests.Session()

    # ---------------------------------------------------------------- auth

    def _refresh(self) -> None:
        log.info("Refreshing WHOOP access token")
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._token.refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "offline",
            },
            timeout=30,
        )
        if resp.status_code in (400, 401):
            raise WhoopAuthError(
                f"Refresh token rejected [{resp.status_code}]: {resp.text}\n"
                "The stored refresh token is stale or was already consumed. "
                "Re-run scripts/authorize.py to mint a new one."
            )
        resp.raise_for_status()

        new_token = Token.from_response(resp.json())
        # Persist BEFORE adopting. A crash here leaves the old token in storage,
        # which is already spent — but a crash after adopting without persisting
        # would lose the only valid token entirely.
        self._on_refresh(new_token)
        self._token = new_token

    def _auth_header(self) -> dict:
        if self._token.expired:
            self._refresh()
        return {"Authorization": f"Bearer {self._token.access_token}"}

    # ---------------------------------------------------------------- http

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API_BASE}/{path.lstrip('/')}"
        for attempt in range(MAX_RETRIES):
            resp = self._session.get(
                url, headers=self._auth_header(), params=params, timeout=45
            )

            if resp.status_code == 401 and attempt == 0:
                # Access token rejected earlier than its stated expiry.
                self._refresh()
                continue

            if resp.status_code == 429:
                wait = float(resp.headers.get("X-RateLimit-Reset", 2 ** attempt))
                wait = min(wait, 60)
                log.warning("Rate limited on %s, sleeping %.0fs", path, wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("WHOOP %s on %s, retrying in %ds", resp.status_code, path, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"GET {path} failed after {MAX_RETRIES} attempts")

    def _paginate(self, path: str, start: datetime, end: datetime) -> Iterator[dict]:
        """Yield every record in [start, end), following next_token to exhaustion."""
        params = {
            "start": _iso(start),
            "end": _iso(end),
            "limit": MAX_PAGE_LIMIT,
        }
        pages = 0
        while True:
            payload = self._get(path, params)
            records = payload.get("records") or []
            yield from records

            pages += 1
            token = payload.get("next_token")
            if not token:
                break
            params = dict(params, nextToken=token)
            if pages > 2000:  # cursor loop guard
                log.error("Pagination guard tripped on %s", path)
                break

    # ------------------------------------------------------------ resources

    def cycles(self, start: datetime, end: datetime) -> list[dict]:
        return list(self._paginate("v2/cycle", start, end))

    def recoveries(self, start: datetime, end: datetime) -> list[dict]:
        return list(self._paginate("v2/recovery", start, end))

    def sleeps(self, start: datetime, end: datetime) -> list[dict]:
        return list(self._paginate("v2/activity/sleep", start, end))

    def workouts(self, start: datetime, end: datetime) -> list[dict]:
        return list(self._paginate("v2/activity/workout", start, end))

    def profile(self) -> dict:
        return self._get("v2/user/profile/basic")

    def body_measurement(self) -> dict:
        return self._get("v2/user/measurement/body")


def _iso(dt: datetime) -> str:
    """WHOOP wants RFC3339 in UTC with milliseconds, e.g. 2026-01-01T00:00:00.000Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
