"""What the server knows about the health of the Kroger sign-in

Every path that learns something about the sign-in reports it here: the
background refresh, the on-demand renewal when a cart or profile call finds
the token expired, sign-out, and a new sign-in. get_authentication_info and
/health show it. It is kept in memory: the hosted server is one process.
"""

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"            # nothing has run yet (a fresh start)
OK = "ok"                      # the last renewal worked
RETRYING = "retrying"          # the last renewal failed for a reason that may pass
SIGNIN_NEEDED = "signin_needed"  # Kroger rejected the refresh token: the user must sign in again
SIGNED_OUT = "signed_out"      # there is no saved sign-in


def _iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def fingerprint(access_token: Optional[str]) -> Optional[str]:
    """A short digest that tells one saved sign-in from another without keeping the token"""
    if not access_token:
        return None
    return hashlib.sha256(access_token.encode()).hexdigest()[:16]


class SignInStatus:
    """The state of the sign-in, with when it last renewed and why it needs attention

    Never holds a token. Safe to use from the tool threads and the event loop.

    A renewal runs for a while before it reports. If the sign-in changed meanwhile
    (a new sign-in, a sign-out), its report is stale. So a caller that takes the
    `generation` before it starts passes it with its report, and a stale report is
    dropped. A new sign-in and a sign-out start a new generation.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.state = UNKNOWN
            self.last_success: Optional[float] = None
            self.reason: Optional[str] = None
            self.since: Optional[float] = None
            self._rejected: Optional[str] = None  # fingerprint of the sign-in Kroger rejected
            self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def _apply(self, state: str, reason: Optional[str] = None, *, generation: Optional[int] = None,
               rejected: Optional[str] = None, bump: bool = False, keep_signin_needed: bool = False,
               stamp_success: bool = False) -> bool:
        """Set the state in one step; True if the state changed, False if the report was dropped or changed nothing"""
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            if keep_signin_needed and self.state == SIGNIN_NEEDED:
                return False
            changed = state != self.state
            if changed or reason != self.reason:
                self.since = time.time()
            self.state, self.reason = state, reason
            self._rejected = rejected if state == SIGNIN_NEEDED else None
            if bump:
                self._generation += 1
            if stamp_success:
                self.last_success = time.time()
            return changed

    def mark_ok(self, generation: Optional[int] = None) -> None:
        self._apply(OK, generation=generation, stamp_success=True)

    def mark_retrying(self, reason: str, generation: Optional[int] = None) -> None:
        """A renewal failed for a reason that may pass

        A sign-in that Kroger already rejected stays dead: a network error says nothing
        new about it, and only a new sign-in, or a renewal that works, clears it.
        """
        self._apply(RETRYING, reason, generation=generation, keep_signin_needed=True)

    def mark_signin_needed(self, reason: str, token_fingerprint: Optional[str] = None,
                           generation: Optional[int] = None) -> None:
        """Kroger rejected the sign-in. Logged once, when the state changes to this.

        Args:
            token_fingerprint: fingerprint() of the saved access token that was rejected, so that
                a different saved token (a sign-in made by another process) clears this
        """
        if self._apply(SIGNIN_NEEDED, reason, generation=generation, rejected=token_fingerprint):
            logger.error("The Kroger sign-in is dead and the user has to sign in again: %s", reason)

    def mark_signed_out(self, generation: Optional[int] = None) -> None:
        """There is no saved sign-in. Without a generation, this is a sign-out, and starts a new generation."""
        self._apply(SIGNED_OUT, generation=generation, bump=generation is None)

    def sign_in_completed(self) -> None:
        """A new sign-in was saved; the refresh loop checks it at once"""
        self._apply(UNKNOWN, bump=True)

    def note_saved_token(self, saved: Optional[Dict[str, Any]]) -> None:
        """Clear signin_needed if the saved token is not the one Kroger rejected

        Another process that shares the token file may have signed the user in again.
        """
        current = fingerprint(saved.get("access_token")) if isinstance(saved, dict) else None
        with self._lock:
            dead_token_replaced = (
                self.state == SIGNIN_NEEDED and self._rejected is not None
                and current is not None and current != self._rejected
            )
        if dead_token_replaced:
            self.sign_in_completed()

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "last_success": _iso(self.last_success),
                "reason": self.reason,
                "since": _iso(self.since),
            }


status = SignInStatus()
