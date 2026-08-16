# Deploy verification

## 2026-08-16 — Stage 1: sessions + keep-alive

### JWT sessions (survive Render restart/sleep)

Auth tokens are **signed JWT** (`backend/app/services/auth.py`), not in-memory maps.
After a free-tier spin-down and wake, existing tokens remain valid until `exp` (24h),
as long as the signing secret is stable.

**Required env (production):**

| Variable | Purpose |
|----------|---------|
| `TOKEN_ENCRYPTION_KEY` | Fernet key for OKX secrets at rest; also used as JWT secret if `JWT_SECRET` unset |
| `JWT_SECRET` | Optional dedicated JWT HMAC secret (recommended) |
| `DASHBOARD_PASSWORD` | Admin password |

Generate Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

If neither `JWT_SECRET` nor `TOKEN_ENCRYPTION_KEY` is set, a random secret is used
**per process** and all sessions die on every restart (old behaviour).

### Keep-alive on Render free tier

Free web services sleep after idle traffic. Trading loops stop until the next HTTP request.

**Do this:**

1. Create a free monitor (UptimeRobot, Better Stack, cron-job.org, etc.).
2. Ping **every 5 minutes**:
   - `GET https://<your-service>.onrender.com/api/health`
   - or `HEAD https://<your-service>.onrender.com/api/health`
3. Expect HTTP 200 and JSON `{"status":"ok",...}`.

Keep-alive is **external only** — do not couple health pings with strategy logic.

### Guest mode

- `POST /api/auth/guest` issues a JWT with `role=guest`.
- UI shows amber badge **Guest · view only**.
- Mutating routes still require admin (`Depends(require_admin)`).
- On 401 the SPA redirects to `/login?reason=session`.

### Health payload (stage-1b)

`GET /api/health` includes:

- `uptime_sec` — seconds since process start (resets after sleep/wake)
- `bots.rotation|impulse|validation` — `true` if loop is running after auto-start
- `auth: "jwt"`

On every boot (including Render wake) strategies auto-start when OKX env keys are set.
Failures are isolated: one bot error does not block the others.

### Smoke checklist after deploy

1. Open site → Guest login → dashboard loads, badge visible.
2. Restart/redeploy service → refresh page **without** re-login → still authenticated (JWT).
3. Uptime monitor green for `/api/health`.
4. Admin login → Settings visible; guest cannot open Settings.

---

## 2026-08-12

Verify closed trades persist across redeploys.
