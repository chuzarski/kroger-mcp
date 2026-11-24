"""Keep the user's Kroger sign-in alive from inside the server

Kroger access tokens last 30 minutes. Without this, a server that sits idle
finds the token expired on the next cart call. The loop renews the saved
token every KROGER_REFRESH_INTERVAL_SECONDS (default 15 minutes).

The renewal itself is kroger-api's: it runs with the token file locked and
uses a token that another thread or process renewed meanwhile, so it never
sends a refresh token that was already used.
"""

import logging
import os
import time
from typing import Callable

import anyio
import anyio.to_thread

from kroger_api.errors import BadRequestError, NotAuthenticatedError
from kroger_api.kroger_api import KrogerAPI
from kroger_api.token_storage import USER_TOKEN_FILE, load_token

from .shared import invalidate_authenticated_client
from .signin_status import SIGNIN_NEEDED, fingerprint, status

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 900

# Seconds to wait before the second and third try when a renewal fails for a reason that may pass
RETRY_BACKOFF_SECONDS = (5, 15)

# While the sign-in is dead, seconds between looks at whether the user signed in again
PARK_POLL_SECONDS = 5

# A saved token issued less than this many seconds ago was renewed just now by someone else
FRESH_SECONDS = 60


class SignInNeeded(Exception):
    """The saved sign-in cannot be renewed because it holds no refresh token"""


def refresh_interval() -> float:
    """Seconds between renewals from KROGER_REFRESH_INTERVAL_SECONDS; 0 turns the refresh off"""
    raw = os.getenv("KROGER_REFRESH_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
        if value < 0:
            raise ValueError(raw)
    except ValueError:
        logger.warning(
            "KROGER_REFRESH_INTERVAL_SECONDS=%r is not a number of seconds (0 or more); using %d",
            raw, DEFAULT_INTERVAL_SECONDS,
        )
        return DEFAULT_INTERVAL_SECONDS
    return value


def refresh_once(now: Callable[[], float] = time.time) -> str:
    """Renew the saved user token once

    Returns:
        "no_token" if nobody is signed in, "reused" if the saved token was issued
        within the last minute (someone just renewed it), or "refreshed"

    Raises:
        SignInNeeded: The saved token has no refresh token
        BadRequestError: Kroger rejected the refresh token
        NotAuthenticatedError: The token file was deleted meanwhile (the user signed out)
        KrogerAPIError: Renewing failed for another reason, such as a network error
    """
    saved = load_token(USER_TOKEN_FILE)
    if not isinstance(saved, dict) or not saved.get("access_token"):
        return "no_token"
    if not saved.get("refresh_token"):
        raise SignInNeeded("The saved sign-in has no refresh token, so it cannot be renewed.")

    try:
        issued_ago = saved["expires_in"] - (float(saved["expires_at"]) - now())
    except (KeyError, TypeError, ValueError):
        issued_ago = None
    if issued_ago is not None and issued_ago < FRESH_SECONDS:
        return "reused"

    client = KrogerAPI().client
    client.token_file = USER_TOKEN_FILE
    # Takes the token file's lock, and uses a token that was renewed since `saved` was read
    client._renew_user_token(USER_TOKEN_FILE, saved)
    invalidate_authenticated_client()
    return "refreshed"


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


def _saved_fingerprint():
    saved = load_token(USER_TOKEN_FILE)
    return fingerprint(saved.get("access_token")) if isinstance(saved, dict) else None


async def _cycle(sleep) -> None:
    """One renewal, with retries; records the outcome in status

    The outcome is reported with the generation taken before the renewal, so that a
    sign-in or sign-out that happens meanwhile is not overwritten by a stale result.
    """
    generation = status.generation
    attempts = len(RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(attempts):
        before = await anyio.to_thread.run_sync(_saved_fingerprint)
        try:
            result = await anyio.to_thread.run_sync(refresh_once)
        except BadRequestError as e:
            status.mark_signin_needed(e.kroger_message or _first_line(e.message), before, generation)
            return
        except SignInNeeded as e:
            status.mark_signin_needed(str(e), before, generation)
            return
        except NotAuthenticatedError:
            status.mark_signed_out(generation)
            return
        except Exception as e:
            # A network error, a Kroger outage, a missing setting: it may pass
            reason = f"{type(e).__name__}: {_first_line(str(e))}"
            status.mark_retrying(reason, generation)
            logger.warning("Renewing the Kroger sign-in failed (try %d of %d): %s", attempt + 1, attempts, reason)
            if attempt < attempts - 1:
                await sleep(RETRY_BACKOFF_SECONDS[attempt])
            continue
        if result == "no_token":
            status.mark_signed_out(generation)
        else:
            status.mark_ok(generation)
            logger.info("The Kroger sign-in is %s", "renewed" if result == "refreshed" else "current")
        return


async def run_refresh_loop(interval: float, sleep=anyio.sleep) -> None:
    """Renew the sign-in now, then every `interval` seconds, until cancelled

    A refresh token that Kroger rejects ends the renewing: the loop only waits,
    looking every few seconds whether the user signed in again (a new sign-in in
    this process changes the state, and one made by another process changes the
    saved token), and then renews at once.
    """
    while True:
        await _cycle(sleep)
        if status.state == SIGNIN_NEEDED:
            while status.state == SIGNIN_NEEDED:
                await sleep(PARK_POLL_SECONDS)
                status.note_saved_token(await anyio.to_thread.run_sync(load_token, USER_TOKEN_FILE))
            continue
        await sleep(interval)
