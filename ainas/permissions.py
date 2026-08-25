"""Small, route-independent authorization helpers for Agent access."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

from flask import g

from .errors import ForbiddenError


READ_SCOPE = "read"
WRITE_SCOPE = "write"

# A conservative default that route code can extend or override.  It is kept
# separate from the routes so permissions remain explicit during rollout.
METHOD_SCOPES = {
    "GET": READ_SCOPE,
    "HEAD": READ_SCOPE,
    "POST": WRITE_SCOPE,
    "PUT": WRITE_SCOPE,
    "PATCH": WRITE_SCOPE,
    "DELETE": WRITE_SCOPE,
}


def scope_for_method(method: str) -> Optional[str]:
    """Return the default scope for an HTTP method, if it changes API state."""
    return METHOD_SCOPES.get(method.upper())


def is_authorized(actor_kind: Optional[str], scopes: Iterable[str], required_scope: Optional[str]) -> bool:
    """Check a granted scope. Administrators intentionally bypass Agent scopes."""
    if actor_kind == "admin":
        return True
    if not required_scope:
        return True
    if actor_kind != "agent":
        return False
    granted = {str(scope).strip().casefold() for scope in scopes}
    return required_scope.casefold() in granted or "*" in granted


def require_scope(required_scope: Optional[str]) -> None:
    """Flask route helper; call after authentication to enforce an Agent scope."""
    agent = getattr(g, "agent", None) or {}
    if not is_authorized(getattr(g, "auth_kind", None), agent.get("scopes", ()), required_scope):
        raise ForbiddenError("Agent is not granted the required scope")
