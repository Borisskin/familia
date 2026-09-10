"""In-memory store of pending approval actions.

When the policy engine returns ``Decision.ASK``, the tool that triggered
the rule parks the action here, keyed by a short random token, and
returns the token to the agent.  A later ``/approve <token>`` command
from an authorized approver looks the action up, checks eligibility,
and either returns it for execution or reports a failure.

The store is process-local and not persisted — approvals that age out
or live across restarts are lost.  This is acceptable for MVP; a later
pass can back it with the session store if we need durability.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobot.bus.events import OutboundMessage


DEFAULT_TTL_SECONDS = 15 * 60


@dataclass
class PendingApproval:
    token: str
    action: str
    outbound: OutboundMessage
    requester_actor: str | None
    approvers: list[str]
    reason: str
    expires_at: float
    # Channel the requester spoke on when they triggered the gate — used so
    # verdict notifications land in the chat they're actually watching,
    # not the actor's first-registered identity.
    requester_channel: str | None = None
    requester_chat_id: str | None = None
    rule_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def allows_approver(self, actor: str | None) -> bool:
        if not actor:
            return False
        # Records without the original request route are ambiguous legacy
        # state. Never turn their stored approver snapshot into authority.
        if not self.requester_channel or not self.requester_chat_id:
            return False

        try:
            from familia.policy.engine import Decision, PolicyContext, get_engine
            from familia.principals import get_registry

            if get_registry().get(actor) is None:
                return False
            decision = get_engine().evaluate(
                PolicyContext(
                    action=self.action,
                    actor=self.requester_actor,
                    channel=self.requester_channel,
                    to_channel=self.outbound.channel,
                    to_chat=self.outbound.chat_id,
                )
            )
            if decision.decision is not Decision.ASK:
                return False
            for pattern in decision.approver:
                if pattern in {"*", actor}:
                    return True
                if pattern == "@principal":
                    return True
                if pattern.startswith("@"):
                    from familia.roles import get_effective_roles

                    if pattern[1:] in get_effective_roles(actor):
                        return True
            return False
        except Exception:  # noqa: BLE001
            # Authorization is a trust boundary; a broken live lookup denies.
            return False


class PendingStore:
    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._by_token: dict[str, PendingApproval] = {}
        self._lock = threading.Lock()

    def park(
        self,
        action: str,
        outbound: OutboundMessage,
        requester_actor: str | None,
        approvers: list[str],
        reason: str = "",
        rule_name: str = "",
        requester_channel: str | None = None,
        requester_chat_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> PendingApproval:
        token = secrets.token_urlsafe(6)
        pending = PendingApproval(
            token=token,
            action=action,
            outbound=outbound,
            requester_actor=requester_actor,
            requester_channel=requester_channel,
            approvers=list(approvers),
            reason=reason,
            expires_at=time.time() + self._ttl,
            rule_name=rule_name,
            requester_chat_id=requester_chat_id,
            extra=dict(extra or {}),
        )
        with self._lock:
            self._gc_locked()
            self._by_token[token] = pending
        return pending

    def take(self, token: str) -> PendingApproval | None:
        """Atomic lookup+remove.  Returns None if missing or expired."""
        with self._lock:
            pending = self._by_token.pop(token, None)
        if pending is None:
            return None
        if pending.is_expired():
            return None
        return pending

    def take_if_authorized(
        self, token: str, actor: str | None
    ) -> tuple[PendingApproval | None, str]:
        """Consume one approval only after a live authorization check.

        The status is ``taken``, ``unauthorized`` or ``missing``.  The check
        and removal share the store lock, so an unauthorized or repeated press
        cannot spend the one-shot token.
        """
        with self._lock:
            pending = self._by_token.get(token)
            if pending is None:
                return None, "missing"
            if pending.is_expired():
                self._by_token.pop(token, None)
                return None, "missing"
            if not pending.allows_approver(actor):
                return None, "unauthorized"
            return self._by_token.pop(token), "taken"

    def cancel(self, token: str) -> PendingApproval | None:
        """Remove without checking expiry — used to discard a parked
        outbound whose approval flow has become undeliverable (e.g. no
        approver was reachable). Returns the removed record or None."""
        with self._lock:
            return self._by_token.pop(token, None)

    def peek(self, token: str) -> PendingApproval | None:
        with self._lock:
            pending = self._by_token.get(token)
        if pending is None or pending.is_expired():
            return None
        return pending

    def _gc_locked(self) -> None:
        now = time.time()
        stale = [t for t, p in self._by_token.items() if p.is_expired(now)]
        for t in stale:
            self._by_token.pop(t, None)


_store: PendingStore | None = None
_lock = threading.Lock()


def get_pending_store() -> PendingStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = PendingStore()
    return _store
