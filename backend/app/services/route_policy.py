"""Declarative HTTP access policy for API routes — the single source of truth.

Motivation
----------
Access control used to live in three hand-maintained structures inside main.py
(``PUBLIC_API_PATHS``, ``ADMIN_ONLY_PATHS``/``ADMIN_ONLY_PREFIXES``,
``GUEST_FORBIDDEN_PREFIXES``) plus ``Depends(require_admin)`` on 55 routes. Adding
one endpoint meant remembering up to three places, and nothing failed loudly when
someone forgot.

Now every ``/api`` route carries exactly one policy decorator; the middleware
enforces it, and ``verify_route_policies()`` runs at startup and refuses to boot
if any route is unmarked (same "fail loud" approach as the LIVE mirror guards).

Policies
--------
- ``PUBLIC``         no token required (health, login, guest login, showcase data)
- ``AUTH``           any valid token: admin | user | guest
- ``AUTH_NO_GUEST``  any valid token except guest (trading/owner-scoped data)
- ``ADMIN``          role must be admin (owner-only data and all mutations)

Usage
-----
    @app.get('/api/positions')
    @policy(AUTH_NO_GUEST)
    async def get_positions(): ...

The decorator only tags the function — it does not wrap it, so FastAPI keeps
seeing the original signature and dependency injection keeps working. The
route decorator must stay above ``@policy(...)``.
"""
from __future__ import annotations

from typing import Callable

PUBLIC = "public"
AUTH = "auth"
AUTH_NO_GUEST = "auth_no_guest"
ADMIN = "admin"

VALID_POLICIES = (PUBLIC, AUTH, AUTH_NO_GUEST, ADMIN)

# Attribute the decorator writes and the middleware reads.
POLICY_ATTR = "__dsh_policy__"

# Paths that are never subject to the policy table (non-API: SPA, assets, docs).
_API_PREFIX = "/api/"


def policy(value: str) -> Callable:
    """Tag an endpoint with the access policy required to call it."""
    if value not in VALID_POLICIES:
        raise ValueError(f"unknown policy {value!r}; expected one of {VALID_POLICIES}")

    def decorate(fn: Callable) -> Callable:
        setattr(fn, POLICY_ATTR, value)
        return fn

    return decorate


def policy_of(endpoint) -> str | None:
    """Return the policy declared on an endpoint, or None if unmarked."""
    return getattr(endpoint, POLICY_ATTR, None)


def role_allows(pol: str, role: str | None) -> bool:
    """Decide access for an already-authenticated role (None = no/invalid token)."""
    if pol == PUBLIC:
        return True
    if role is None:
        return False
    if pol == ADMIN:
        return role == "admin"
    if pol == AUTH_NO_GUEST:
        return role != "guest"
    return True  # AUTH


class RoutePolicyTable:
    """Precompiled path(+method) → policy lookup used by the middleware.

    Exact paths go into a dict; templated paths (``/api/x/{id}``) are matched via
    the route's own regex, so the table always mirrors the real route table.
    """

    def __init__(self) -> None:
        self._exact: dict[tuple[str, str], tuple[str, str]] = {}
        self._patterns: list[tuple[object, str, str, str]] = []

    def build(self, app) -> None:
        from starlette.routing import Route

        self._exact.clear()
        self._patterns.clear()
        for route in app.routes:
            if not isinstance(route, Route):
                continue
            path = getattr(route, "path", "") or ""
            pol = policy_of(getattr(route, "endpoint", None))
            if not path.startswith(_API_PREFIX):
                continue
            methods = getattr(route, "methods", None) or ["GET"]
            methods = [m for m in methods if m in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")]
            if "{" in path:
                self._patterns.append((route.path_regex, path, pol, methods))
            else:
                for m in methods:
                    self._exact[(path, m)] = (pol, path)

    def lookup(self, method: str, path: str) -> tuple[str | None, str | None]:
        """Return (policy, route_path) or (None, None) when no API route matches."""
        hit = self._exact.get((path, method))
        if hit:
            return hit
        for regex, route_path, pol, methods in self._patterns:
            if method not in methods:
                continue
            if regex.match(path):
                return pol, route_path
        return None, None

    @property
    def size(self) -> int:
        return len(self._exact) + len(self._patterns)


def audit_routes(app) -> tuple[list[str], dict[str, str]]:
    """Return (routes_without_policy, {path: policy}) for all /api routes."""
    from starlette.routing import Route

    unmarked: list[str] = []
    matrix: dict[str, str] = {}
    for route in app.routes:
        if not isinstance(route, Route):
            continue
        path = getattr(route, "path", "") or ""
        if not path.startswith(_API_PREFIX):
            continue
        pol = policy_of(getattr(route, "endpoint", None))
        methods = ",".join(sorted(m for m in (getattr(route, "methods", None) or ["GET"])))
        if pol is None:
            unmarked.append(f"{methods} {path}")
        else:
            matrix[path] = pol
    return unmarked, matrix


def verify_route_policies(app) -> int:
    """Fail loudly at boot if any /api route has no declared policy.

    Mirrors the LIVE-mirror guard philosophy: an unmarked route means nobody
    decided who may call it, so we refuse to serve rather than guess.
    """
    unmarked, matrix = audit_routes(app)
    if unmarked:
        raise RuntimeError(
            "AUTH POLICY MISSING — every /api route must declare @policy(...). "
            f"Unmarked ({len(unmarked)}): {', '.join(sorted(unmarked))}"
        )
    return len(matrix)
