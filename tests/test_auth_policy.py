"""Access-policy contract for the API (stage 3).

Before stage 3, access control lived in three hand-written structures inside
main.py plus ``Depends(require_admin)`` on 55 routes, and nothing failed when a
new endpoint was added without protection. Now every /api route carries exactly
one ``@policy(...)``; the middleware enforces it and startup refuses to boot if
anything is unmarked.

This module checks three things:

1. Completeness — every /api route declares a policy (what the boot check does).
2. Parity — for every route and every role, the new declarative decision matches
   a frozen copy of the *old* rules (allow/deny only; see the 401→403 note below).
3. Behaviour — real HTTP requests through the middleware: default-deny without a
   token, guest and non-admin rejections, and reachable public routes.

Note on status codes: for role=user the old code answered 401 (raised by the
route-level ``require_admin`` dependency) where the new one answers 403 (the
middleware knows the route is admin-only). Both deny; 403 is the correct one.
"""
import pytest

from app.services.route_policy import ADMIN, AUTH, AUTH_NO_GUEST, PUBLIC, audit_routes, role_allows

# ── Frozen copy of the pre-stage-3 rules (commit c31a0faf) ────────────────────
LEGACY_PUBLIC = {
    '/api/health', '/api/health/guard', '/api/health/circuit-breaker', '/api/auth/login', '/api/auth/guest',
    '/api/auth/status', '/api/auth/logout', '/api/auth/telegram', '/api/ai/status',
    '/api/me/dashboard', '/api/meta/product', '/api/startup-log',
}
LEGACY_ADMIN_PATHS = {
    '/api/credentials/status', '/api/credentials/test', '/api/credentials/init', '/api/trade/order',
    '/api/positions/close', '/api/positions/sweep-orphans', '/api/momentum/start', '/api/momentum/stop',
    '/api/momentum/config', '/api/rotation/start', '/api/rotation/stop', '/api/rotation/reset',
    '/api/rotation/config', '/api/impulse/start', '/api/impulse/stop', '/api/impulse/config',
    '/api/impulse/reset', '/api/validation/start', '/api/validation/stop', '/api/validation/reset',
    '/api/validation/config', '/api/validation/status', '/api/validation/trades',
    '/api/validation/indicators', '/api/db/reset-all', '/api/db/positions', '/api/telegram/status',
    '/api/telegram/config', '/api/telegram/test', '/api/telegram/simulate', '/api/telegram/menu',
    '/api/analysis/log', '/api/subs', '/api/subs/activate', '/api/subs/deactivate', '/api/subs/config',
    '/api/mode', '/api/audit', '/api/risk/kill', '/api/pnl/rebuild-strategy',
    '/api/admin/reset-trading-stats', '/api/ai/start', '/api/ai/stop', '/api/ai/decide',
    '/api/ai/correct-attribution', '/api/ai/logs', '/api/ai/logs/download',
}
LEGACY_ADMIN_PREFIXES = ('/api/debug/', '/api/admin/', '/api/vwap_rev/')
LEGACY_GUEST_FORBIDDEN = (
    '/api/pnl', '/api/trades', '/api/positions', '/api/portfolio', '/api/momentum', '/api/rotation',
    '/api/impulse', '/api/validation', '/api/ai/', '/api/smart-money', '/api/reports', '/api/backtest',
    '/api/credentials', '/api/mode', '/api/audit', '/api/db/', '/api/me', '/api/trade', '/api/chart',
    '/api/risk',
)
# Routes that additionally carried Depends(require_admin) — generated from
# commit c31a0faf (the last commit before this refactor), not hand-written.
LEGACY_ADMIN_DEP_PATHS = {
    '/api/admin/reassign-trade', '/api/admin/reset-trading-stats', '/api/admin/users',
    '/api/admin/users/clear-credentials', '/api/admin/users/mode', '/api/admin/users/plan',
    '/api/ai/config', '/api/ai/config/promote', '/api/ai/correct-attribution', '/api/ai/decide',
    '/api/ai/execute', '/api/ai/logs', '/api/ai/logs/download', '/api/ai/start', '/api/ai/stop',
    '/api/analysis', '/api/analysis/log', '/api/audit', '/api/credentials/init',
    '/api/credentials/status', '/api/credentials/test', '/api/db/positions',
    '/api/db/reset-all', '/api/debug/bills', '/api/debug/client-error',
    '/api/debug/client-errors', '/api/debug/fills', '/api/debug/mini-log',
    '/api/debug/server-hits', '/api/debug/trades-db', '/api/health/positions-claims',
    '/api/live/connect', '/api/live/disconnect', '/api/mode', '/api/pnl/rebuild-exchange',
    '/api/pnl/rebuild-strategy', '/api/pnl/reconcile', '/api/positions/bind',
    '/api/positions/close', '/api/positions/reclaim', '/api/positions/sweep-orphans',
    '/api/risk/kill', '/api/subs', '/api/subs/activate', '/api/subs/config',
    '/api/subs/deactivate', '/api/telegram/config', '/api/telegram/menu',
    '/api/telegram/simulate', '/api/telegram/status', '/api/telegram/test', '/api/trade/order',
}


# Deliberate tightening (stage 3B): owner data that a Pro `user` token could read
# before is now admin-only. The Mini App keeps only the five routes it actually
# calls (see OWNER_DATA_REACHABLE_BY_USER below).
TIGHTENED_IN_3B = {
    '/api/ai/diagnostics', '/api/chart/trades', '/api/live/trades', '/api/portfolio',
    '/api/reports/summary', '/api/risk/status', '/api/stats', '/api/trade/log',
    '/api/trade/orders', '/api/trades',
}


def _legacy_allows(path: str, role: str | None) -> bool:
    """Old effective behaviour (middleware + require_admin dependency)."""
    if path in LEGACY_PUBLIC:
        return True
    if role is None:
        return False
    if path in LEGACY_ADMIN_PATHS or any(path.startswith(p) for p in LEGACY_ADMIN_PREFIXES):
        return role == 'admin'
    if path in LEGACY_ADMIN_DEP_PATHS:
        return role == 'admin'
    if role == 'guest' and any(path == p or path.startswith(p) for p in LEGACY_GUEST_FORBIDDEN):
        return False
    return True


def _expected_allows(path: str, role: str | None) -> bool:
    """Legacy behaviour plus the conscious stage-3B tightening (now ADMIN, so
    both guest and non-admin user lose access)."""
    if path in TIGHTENED_IN_3B and role in ('user', 'guest'):
        return False
    return _legacy_allows(path, role)


ROLES = (None, 'guest', 'user', 'admin')


@pytest.fixture(scope='module')
def app_module():
    import app.main as m

    return m


@pytest.fixture(scope='module')
def matrix(app_module):
    unmarked, table = audit_routes(app_module.app)
    assert not unmarked, f"routes without @policy: {unmarked}"
    return table


@pytest.fixture(scope='module')
def routes(app_module):
    """Every registered /api route as (method, path, policy) — method matters."""
    from starlette.routing import Route

    from app.services.route_policy import policy_of

    out = []
    for route in app_module.app.routes:
        if not isinstance(route, Route):
            continue
        path = getattr(route, 'path', '') or ''
        if not path.startswith('/api/'):
            continue
        pol = policy_of(route.endpoint)
        for method in sorted(route.methods or {'GET'}):
            if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
                out.append((method, path, pol))
    return out


@pytest.fixture(scope='module')
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app, raise_server_exceptions=False)


# ── 1. completeness ──────────────────────────────────────────────────────────

def test_every_api_route_declares_a_policy(app_module):
    unmarked, table = audit_routes(app_module.app)
    assert unmarked == [], f"undeclared access policy: {unmarked}"
    assert len(table) >= 90, "unexpectedly few /api routes — did route registration change?"


def test_policy_values_are_known(matrix):
    assert set(matrix.values()) <= {PUBLIC, AUTH, AUTH_NO_GUEST, ADMIN}


def test_startup_verification_passes(app_module):
    from app.services.route_policy import verify_route_policies

    assert verify_route_policies(app_module.app) == len(audit_routes(app_module.app)[1])


@pytest.mark.parametrize(
    "path,expected",
    [
        # Policies below are the ones the pre-stage-3 code effectively applied
        # (stage 3 preserved behaviour on purpose). See the gap test under it.
        ('/api/health', PUBLIC),
        ('/api/health/guard', PUBLIC),
        ('/api/meta/product', PUBLIC),
        ('/api/auth/login', PUBLIC),
        ('/api/me', AUTH_NO_GUEST),
        ('/api/live/status', AUTH),
        ('/api/admin/users', ADMIN),
        ('/api/debug/server-hits', ADMIN),
        ('/api/risk/kill', ADMIN),
        ('/api/positions/close', ADMIN),
    ],
)
def test_sensitive_routes_have_expected_policy(matrix, path, expected):
    assert matrix.get(path) == expected, f"{path} must be {expected}, got {matrix.get(path)}"


# The only owner-scoped data a non-admin `user` token may read: exactly what the
# Mini App calls. Everything else was tightened to ADMIN in stage 3B.
OWNER_DATA_REACHABLE_BY_USER = {
    '/api/live/status',
    '/api/pnl',
    '/api/pnl/summary',
    '/api/positions',
    '/api/positions/close-all',
    '/api/trades/paired',
}


def test_user_reachable_routes_are_a_known_list(matrix):
    """No route may silently become reachable by role=user.

    /api/me/* is the customer's own data (intended), /api/market/* is public
    market data. Everything else in this set is the owner's data and must be a
    conscious decision — update this list when tightening.
    """
    reachable = {
        path for path, pol in matrix.items()
        if pol in (AUTH, AUTH_NO_GUEST)
        and not path.startswith('/api/me')
        and not path.startswith('/api/market')
    }
    assert reachable == OWNER_DATA_REACHABLE_BY_USER, (
        f"new user-reachable routes: {sorted(reachable - OWNER_DATA_REACHABLE_BY_USER)} | "
        f"no longer reachable: {sorted(OWNER_DATA_REACHABLE_BY_USER - reachable)}"
    )


# ── 2. parity with the pre-stage-3 rules ─────────────────────────────────────

def test_parity_with_legacy_rules_for_every_route_and_role(matrix):
    """Only the deliberate stage-3B tightening may differ from the old rules."""
    mismatches = []
    for path, pol in matrix.items():
        for role in ROLES:
            new = role_allows(pol, role)
            old = _expected_allows(path, role)
            if new != old:
                mismatches.append(f"{path} role={role}: expected={'allow' if old else 'deny'} "
                                  f"new={'allow' if new else 'deny'} ({pol})")
    assert not mismatches, "access changed unexpectedly:\n" + "\n".join(mismatches)


def test_tightened_routes_are_admin(matrix):
    """The stage-3B tightening must actually be in force."""
    wrong = {p: matrix.get(p) for p in TIGHTENED_IN_3B if matrix.get(p) != ADMIN}
    assert not wrong, f"these routes must be ADMIN: {wrong}"


# ── 3. behaviour through the real middleware ─────────────────────────────────

def _tokens():
    from app.services.auth import grant_user, guest, login

    admin = login('test-password')
    assert admin, "admin token could not be minted in tests"
    return {None: None, 'guest': guest(), 'user': grant_user('424242'), 'admin': admin}


def test_public_routes_answer_without_a_token(client):
    for path in ('/api/meta/product', '/api/auth/status'):
        r = client.get(path)
        assert r.status_code == 200, f"{path} should be public, got {r.status_code}"


def test_every_protected_route_is_default_deny(client, routes):
    """No token → 401 on every non-public route (middleware only, no handlers)."""
    bad = []
    for method, path, pol in routes:
        if pol == PUBLIC:
            continue
        r = client.request(method, path)
        if r.status_code != 401:
            bad.append(f"{method} {path} ({pol}) -> {r.status_code}")
    assert not bad, "protected routes reachable without a token:\n" + "\n".join(bad)


def test_guest_is_rejected_on_owner_routes(client, routes):
    tokens = _tokens()
    bad = []
    for method, path, pol in routes:
        if pol not in (ADMIN, AUTH_NO_GUEST):
            continue
        r = client.request(method, path, headers={'Authorization': f"Bearer {tokens['guest']}"})
        if r.status_code != 403:
            bad.append(f"{method} {path} ({pol}) -> {r.status_code}")
    assert not bad, "guest reached owner-scoped routes:\n" + "\n".join(bad)


def test_non_admin_user_is_rejected_on_admin_routes(client, routes):
    tokens = _tokens()
    bad = []
    for method, path, pol in routes:
        if pol != ADMIN:
            continue
        r = client.request(method, path, headers={'Authorization': f"Bearer {tokens['user']}"})
        if r.status_code != 403:
            bad.append(f"{method} {path} -> {r.status_code}")
    assert not bad, "role=user reached admin-only routes:\n" + "\n".join(bad)


def test_unknown_api_path_requires_a_token(client, matrix):
    """Unmatched /api paths stay behind auth (default-deny for retired stubs)."""
    assert client.get('/api/does-not-exist').status_code == 401


def test_retired_endpoint_still_needs_a_token(client):
    """A retired stub is not a policy hole: no token → 401, token → retired stub."""
    tokens = _tokens()
    assert client.get('/api/validation/status').status_code == 401
    r = client.get('/api/validation/status', headers={'Authorization': f"Bearer {tokens['admin']}"})
    assert r.status_code == 200
    assert r.json().get('retired') is True
