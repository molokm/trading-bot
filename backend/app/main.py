# Stage-5: retired multi-bot route handlers removed (see legacy middleware).
# Regenerated structure via AST unparse — logic preserved for active routes.
import asyncio
import json
import logging
import os
import time as _time
import uuid
import faulthandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import asdict
_CRASH_LOG = os.path.join(os.environ.get('DATA_DIR', '/tmp'), 'crash_traceback.log')
try:
    with open(_CRASH_LOG, 'w') as _cf:
        _cf.write('')
    faulthandler.enable(file=open(_CRASH_LOG, 'a'))
    faulthandler.register(11, file=open(_CRASH_LOG, 'a'))
    faulthandler.register(6, file=open(_CRASH_LOG, 'a'))
except Exception:
    pass

def _write_crash(text: str) -> None:
    try:
        with open(_CRASH_LOG, 'a') as _cf:
            _cf.write('\n=== %s ===\n%s\n' % (_time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except Exception:
        pass

def _excepthook(etype, value, tb):
    import traceback as _tb
    _write_crash(''.join(_tb.format_exception(etype, value, tb)))

def _thread_excepthook(args):
    _write_crash('Thread %r: %s' % (args.thread and args.thread.name, ''.join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback) if (_tb := __import__('traceback')) else '?')))
import sys
sys.excepthook = _excepthook
try:
    threading.excepthook = _thread_excepthook
except Exception:
    pass
import threading
logger = logging.getLogger('app')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
from dotenv import load_dotenv
from fastapi import Body, Request, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from app.services.okx_client import OKXClientManager, OKXClient
from app.services.backtest_service import run_backtest_async
from app.database import db
from app.services.auth import login, guest, validate, logout, is_admin, PASSWORD, grant_admin, grant_user, ensure_auth_secrets, get_user_id, encrypt_str, decrypt_str, check_rate_limit, record_attempt, guest_rate_limited, record_guest, get_blacklist, set_blacklist
from app.services.strategy_manager import StrategyManager, PerUserClientManager, set_hydrate_deps
from app.services.legacy_stubs import RotationStrategy, RotationConfig, ROT_BOT_ID, STRATEGY_DESC, RotPosition, COINS, ImpulseStrategy, ImpulseConfig, IMP_BOT_ID, STRATEGY_DESC as IMPULSE_DESC, STRATEGY_NAME as IMPULSE_NAME, STRATEGY_VERSION as IMPULSE_VERSION, ValidationStrategy, make_validation_config, VAL_BOT_ID, AIScaleStrategy, AIScaleConfig, AI_SCALE_BOT_ID, AI_SCALE_NAME, OrderBookScalpStrategy, ScalpConfig, SCALP_BOT_ID, SCALP_NAME, SCALP_VERSION, SCALP_DESC, compute_book_metrics, VWAPMeanReversion, VWAPScalpConfig, VWAP_BOT_ID, VWAP_NAME, VWAP_VERSION, VWAP_DESC, SmartMoneyTracker, TrackerConfig, OKXCopyAPI, SM_BOT_ID, SM_NAME, SM_VERSION, get_mirror
from app.services.ai_strategy import AIStrategy, AIConfig, AIPosition, AI_BOT_ID, STRATEGY_DESC as AI_DESC, STRATEGY_NAME as AI_NAME, STRATEGY_VERSION as AI_VERSION
from app.services.ai_agent import llm_status
from app.services.telegram_notifier import TelegramNotifier
from app.services import pnl_engine
from app.services.pnl_engine import PNL_EPOCH_ISO
from app.services.strategy_cards import BACKTEST_SUMMARY as _BACKTEST_SUMMARY
from app.services.telegram_bot import TelegramBotPoller, _is_active, PRO_PRICE_STARS, PRO_PLAN_DAYS
from app.services.equity_tracker import EquityTracker, SNAPSHOT_INTERVAL
from app.services.risk_guard import get_status as risk_get_status, set_kill_switch, assert_can_open, update_daily_pnl
from app.services.analysis_logger import DEFAULT_PATH
from app.services import trade_attribution as trade_attr
from app.services.position_claim import sweep_exchange_orphans, orphan_close_enabled, claim_open, release_open
from app.services.account_context import filter_rows_for_mode
MOM_BOT_ID = 'momentum_strategy'
load_dotenv()
_docs_enabled = os.getenv('ENABLE_DOCS', 'false').lower() in ('1', 'true')
app = FastAPI(title='OKX Trading Bot', version='3.0.0', docs_url='/docs' if _docs_enabled else None, redoc_url='/redoc' if _docs_enabled else None, openapi_url='/openapi.json' if _docs_enabled else None)
_cors_origins = [o.strip() for o in os.getenv('CORS_ORIGINS', '').split(',') if o.strip()]
if not _cors_origins:
    _cors_origins = ['http://localhost:3000', 'http://127.0.0.1:3000', 'http://localhost:8000', 'http://127.0.0.1:8000', 'https://trading-bot-mu99.onrender.com']
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Stage-5: optional extra routers (meta, future AI/Live/Auth splits)
try:
    from app.routers import include_routers
    include_routers(app)
except Exception as _rerr:
    print(f"[routers] include failed: {_rerr}", flush=True)

_RETIRED_API_PREFIXES = ('/api/momentum', '/api/rotation', '/api/impulse', '/api/validation', '/api/smart-money', '/api/vwap_rev', '/api/ai-scale', '/api/backtest', '/api/me/rotation', '/api/me/impulse', '/api/tracker', '/api/ai/ab-compare', '/api/ai/ab-start', '/api/scalp')

@app.middleware('http')
async def _retired_legacy_api(request: Request, call_next):
    """Stage-1 cut: refuse retired multi-bot endpoints while AI_ONLY_MODE is on.

    Returns a lightweight JSON stub (HTTP 200) so old frontend polls do not
    spam 4xx/5xx; mutating methods still no-op with retired=true.
    """
    if not AI_ONLY_MODE:
        return await call_next(request)
    path = request.url.path or ''
    for pref in _RETIRED_API_PREFIXES:
        if path == pref or path.startswith(pref + '/'):
            from fastapi.responses import JSONResponse
            return JSONResponse({'retired': True, 'available': False, 'running': False, 'detail': 'Endpoint retired — AI Discretionary only', 'path': path}, status_code=200)
    return await call_next(request)
STATIC_DIR = Path(__file__).parent.parent / 'static'
if STATIC_DIR.exists():
    app.mount('/assets', StaticFiles(directory=str(STATIC_DIR / 'assets')), name='assets')
client_manager = OKXClientManager.get_instance()
showcase_manager = OKXClientManager.new_instance()
live_manager = OKXClientManager.new_instance()
_env_key = os.getenv('OKX_API_KEY', '')
_env_secret = os.getenv('OKX_SECRET_KEY', '')
_env_pass = os.getenv('OKX_PASSPHRASE', '')
_env_demo = os.getenv('OKX_DEMO', 'true').lower() in ('1', 'true')
_demo_key, _demo_secret, _demo_pass = (_env_key, _env_secret, _env_pass)
_live_key = ''
_live_secret = ''
_live_pass = ''
_bots_auto_start = os.getenv('BOTS_AUTO_START', '1').strip().lower() not in ('0', 'false', 'no', 'off')
_mom_auto = os.getenv('MOM_AUTO_START', '0').strip().lower() not in ('0', 'false', 'no', 'off')
_imp_auto = os.getenv('IMP_AUTO_START', '0').strip().lower() not in ('0', 'false', 'no', 'off')
_val_auto = os.getenv('VAL_AUTO_START', '0').strip().lower() not in ('0', 'false', 'no', 'off')
_ai_auto = os.getenv('AI_AUTO_START', '1').strip().lower() not in ('0', 'false', 'no', 'off')
AI_ONLY_MODE = True
if AI_ONLY_MODE:
    _mom_auto = False
    _imp_auto = False
    _val_auto = False
    print('[config] AI_ONLY_MODE=1 — Momentum/Impulse/Validation/SM/VWAP disabled', flush=True)
trade_log: list = []
_STARTED_AT = None

def _json_safe_dict(d) -> dict:
    """Convert dict keys that are tuples/lists to strings (JSON-safe)."""
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        if isinstance(k, (list, tuple)):
            key = '|'.join((str(x) for x in k))
        else:
            key = str(k) if not isinstance(k, (str, int, float, bool)) and k is not None else k
            if key is None:
                key = 'null'
        if isinstance(v, dict):
            v = _json_safe_dict(v)
        out[key] = v
    return out
rotation: Optional[RotationStrategy] = None
impulse: Optional[ImpulseStrategy] = None
validation: Optional[ValidationStrategy] = None
telegram = TelegramNotifier()
bot_poller: Optional[TelegramBotPoller] = None
equity_tracker: Optional[EquityTracker] = None
strategy_mgr = StrategyManager(db=db, notifier=telegram)
ai_bot = None
ai_scale_bot = None
scalp_bot = None
vwap_rev_bot = None
sm_tracker = None
sm_mirror = None
_user_clients: dict[str, OKXClient] = {}
PLANS_PRICE = {'signals': PRO_PRICE_STARS, 'pro': PRO_PRICE_STARS}

def get_token(request: Request):
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip()
    try:
        c = request.cookies.get('auth_token') or ''
        if c:
            return c.strip()
    except Exception:
        pass
    return ''

def _set_auth_cookie(response, token: str, max_age: int=86400):
    """HttpOnly session cookie — primary auth transport (Bearer is legacy fallback)."""
    secure = (os.getenv('AUTH_COOKIE_SECURE') or '1').strip().lower() not in ('0', 'false', 'no')
    samesite = (os.getenv('AUTH_COOKIE_SAMESITE') or 'lax').strip().lower()
    if samesite not in ('lax', 'strict', 'none'):
        samesite = 'lax'
    if samesite == 'none':
        secure = True
    response.set_cookie(key='auth_token', value=token, httponly=True, secure=secure, samesite=samesite, max_age=max_age, path='/')
    return response

def _clear_auth_cookie(response):
    secure = (os.getenv('AUTH_COOKIE_SECURE') or '1').strip().lower() not in ('0', 'false', 'no')
    response.delete_cookie('auth_token', path='/', httponly=True, secure=secure, samesite='lax')
    return response

async def write_audit(request: Request, action: str, detail: str='', meta: str=''):
    """Best-effort audit log; never breaks the request path."""
    try:
        role = validate(get_token(request)) or 'anonymous'
        uid = get_user_id(get_token(request))
        actor = f'{role}:{uid}' if uid else role
        await db.add_audit(action=action, actor=actor, detail=detail, meta=meta)
    except Exception as e:
        print(f'[audit] write failed: {e}', flush=True)

def require_admin(request: Request):
    """FastAPI dependency: reject the request unless a valid admin token is present.

    Attach via ``Depends(require_admin)`` on sensitive/mutating routes as
    defense-in-depth alongside the global auth middleware.
    """
    if not is_admin(get_token(request)):
        raise HTTPException(status_code=401, detail='Unauthorized')
_SERVER_HITS = []

@app.middleware('http')
async def _server_hit_logger(request: Request, call_next):
    entry = {'t': _time.strftime('%H:%M:%S'), 'p': request.url.path, 'c': None, 'm': request.method}
    if request.url.path.startswith('/api/') and (not request.url.path.startswith('/api/debug/')):
        _SERVER_HITS.append(entry)
        del _SERVER_HITS[:-400]
    try:
        response = await call_next(request)
        entry['c'] = response.status_code
        return response
    except Exception:
        entry['c'] = 'ERR'
        raise

@app.get('/api/debug/server-hits', dependencies=[Depends(require_admin)])
async def debug_server_hits():
    """Return the most recent server-side API hits (for Mini App diagnostics)."""
    return {'hits': _SERVER_HITS[-80:]}



def _publish_runtime():
    """Share handles with app.routers without circular imports."""
    try:
        import app.runtime as _rt
        _rt.db = db
        _rt.client_manager = client_manager
        _rt.live_manager = live_manager
        _rt.showcase_manager = showcase_manager
        _rt.telegram = telegram
        _rt.ai_bot = ai_bot
        _rt.strategy_mgr = strategy_mgr
        _rt.env_demo = _env_demo
    except Exception as _e:
        print(f"[runtime] publish: {_e}", flush=True)

@app.on_event('startup')
async def startup():
    # global MUST be the first statement (Python forbids prior use of these names)
    global _STARTED_AT, _env_demo, ai_bot, ai_scale_bot, _positions_cache, _pnl_cache, live_manager
    _STARTED_AT = _time.time()
    try:
        print('[startup] 0/7 auth secrets ...', flush=True)
        ensure_auth_secrets()
        print('[startup] 1/7 DB init ...', flush=True)
        await db.init()
        await pnl_engine.ensure_epoch(db)
        print(f'[startup] pnl_epoch forced {PNL_EPOCH_ISO}', flush=True)
        try:
            marker = await db.get_setting('pnl_clean_slate_20260912')
            if not marker:
                from datetime import datetime as _dt, timezone as _tz
                epoch = PNL_EPOCH_ISO
                bot_ids = [AI_BOT_ID, AI_SCALE_BOT_ID, 'ai_scale_strategy', 'ai_strategy']
                try:
                    await db.wipe_strategy_trading_data(bot_ids)
                except Exception as we:
                    print(f'[startup] wipe_strategy_trading_data: {we}', flush=True)
                await db.set_setting('pnl_epoch', epoch)
                await db.set_setting('trading_stats_reset_marker', 'manual')
                for key in (f'ai_lifetime:{AI_BOT_ID}', 'fix_last_eth_to_scale_pnl', 'pnl_bot_overrides'):
                    try:
                        await db.set_setting(key, '')
                    except Exception:
                        pass
                await db.set_setting('pnl_clean_slate_20260912', '1')
                try:
                    _pnl_cache.clear()
                    _positions_cache = None
                except Exception:
                    pass
                print(f'[startup] PnL CLEAN SLATE from {epoch}', flush=True)
        except Exception as e:
            print(f'[startup] PnL clean slate: {e}', flush=True)
        try:
            marker = '1'
            if not marker:
                fix = await db.reassign_day_closes_to_scale('2026-09-11')
                print(f'[startup] reassign day 2026-09-11 → Scale-In: {fix}', flush=True)
                if fix.get('ok'):
                    await db.set_setting('fix_day_20260911_to_scale_v5', '1')
                    await db.set_setting('fix_last_eth_to_scale_pnl', str(float(fix.get('pnl_sum') or 0)))
                    try:
                        import json as _json
                        raw = await db.get_setting('pnl_bot_overrides')
                        arr = _json.loads(raw) if raw else []
                        if not isinstance(arr, list):
                            arr = []
                        rule = {'inst_id': 'ETH-USDT-SWAP', 'pnl_near': -414.06, 'pos_side': 'long', 'exit_date': '2026-09-11', 'to_bot': 'AI Scale-In 1H'}
                        arr = [r for r in arr if not (abs(float(r.get('pnl_near') or 0) - -414.06) < 1 and 'ETH' in str(r.get('inst_id') or '').upper())]
                        arr.append(rule)
                        await db.set_setting('pnl_bot_overrides', _json.dumps(arr))
                    except Exception as _oe:
                        print(f'[startup] override persist: {_oe}', flush=True)
                    await db.set_setting('fix_last_eth_to_scale_pnl', str(float(fix.get('pnl') or 0)))
                    try:
                        if '_pnl_cache' in globals() and isinstance(_pnl_cache, dict):
                            _pnl_cache.clear()
                    except Exception:
                        pass
        except Exception as e:
            print(f'[startup] reassign Scale-In: {e}', flush=True)
        await telegram.load_from_db(db)
        print(f'[TG] status={telegram.status} configured={telegram.configured}', flush=True)
        try:
            await _load_live_creds_from_db()
            print(f'[startup] live creds: {('yes' if _live_key else 'no')}', flush=True)
        except Exception as _e:
            print(f'[startup] live creds: {_e}', flush=True)
        try:
            saved_mode = await db.get_setting('trading_mode')
            if saved_mode == 'live' and _live_key and _live_secret and _live_pass:
                _env_demo = False
                print('[startup] restored LIVE mode from DB', flush=True)
            else:
                print(f'[startup] trading_mode from DB: {saved_mode or 'none (default demo)'}', flush=True)
        except Exception as e:
            print(f'[startup] trading_mode restore: {e}', flush=True)
        try:
            _bl = await db.get_setting('auth_blacklist')
            if _bl:
                import json as _json
                set_blacklist(_json.loads(_bl))
        except Exception as e:
            print(f'[startup] auth blacklist load: {e}', flush=True)
        force = (os.getenv('RESET_TRADING_STATS') or '').strip().lower() in ('1', 'true', 'yes')
        if force:
            print('[startup] RESET_TRADING_STATS=1 → wiping strategy stats ...', flush=True)
            await admin_reset_trading_stats({})
            await db.set_setting('trading_stats_reset_marker', 'manual')
        else:
            try:
                prev_marker = await db.get_setting('trading_stats_reset_marker')
                if prev_marker and str(prev_marker) != 'manual':
                    stale_epoch = await db.get_setting('pnl_epoch')
                    await db.set_setting('pnl_epoch', '')
                    await db.set_setting('trading_stats_reset_marker', '')
                    print(f'[startup] cleared stale auto-reset (marker={prev_marker!r} epoch={stale_epoch!r}) — history restored', flush=True)
            except Exception as e:
                print(f'[startup] clear stale reset: {e}', flush=True)
        print('[startup] 2/7 OKX client init ...', flush=True)
        if (_demo_key or _env_key) and (_demo_secret or _env_secret) and (_demo_pass or _env_pass):
            await showcase_manager.init_client(_demo_key or _env_key, _demo_secret or _env_secret, _demo_pass or _env_pass, True)
            print('[startup] showcase DEMO ready', flush=True)
        # Init main client_manager — respect restored live mode from DB
        if not _env_demo and _live_key and _live_secret and _live_pass:
            # DB restored LIVE mode — init with live keys
            await client_manager.init_client(_live_key, _live_secret, _live_pass, False)
            print('[startup] client_manager LIVE (restored from DB)', flush=True)
        elif _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, True)
            _env_demo = True
        _lm_key = _live_key
        _lm_secret = _live_secret
        _lm_pass = _live_pass
        _lm_source = 'encrypted_env'
        if not (_lm_key and _lm_secret and _lm_pass):
            _lm_source = 'db_live_mirror'
            try:
                _db_key = await db.get_setting('live_mirror_key')
                _db_secret = await db.get_setting('live_mirror_secret')
                _db_pass = await db.get_setting('live_mirror_pass')
                print(f'[startup] live mirror DB lookup: key={('set' if _db_key else 'missing')} secret={('set' if _db_secret else 'missing')} pass={('set' if _db_pass else 'missing')}', flush=True)
                _lm_key = _lm_key or (_db_key or '')
                _lm_secret = _lm_secret or (_db_secret or '')
                _lm_pass = _lm_pass or (_db_pass or '')
            except Exception as e:
                print(f'[startup] live mirror DB fallback error: {e}', flush=True)
        if not (_lm_key and _lm_secret and _lm_pass):
            _lm_source = 'none'
        print(f'[startup] live mirror creds: source={_lm_source} key={('yes' if _lm_key else 'no')}', flush=True)
        if _lm_key and _lm_secret and _lm_pass:
            try:
                await live_manager.init_client(_lm_key, _lm_secret, _lm_pass, False)
                lc_check = live_manager.get_client() if live_manager else None
                if lc_check and (not getattr(lc_check, 'demo', True)):
                    print('[startup] live mirror client ready', flush=True)
                else:
                    live_manager = OKXClientManager.new_instance()
                    print('[startup] live mirror init rejected (demo=true), cleared', flush=True)
            except Exception as e:
                print(f'[startup] live mirror init: {e}', flush=True)
        else:
            print('[startup] live mirror: no creds found — mirror disabled', flush=True)
        try:
            get_mirror = lambda *a, **k: None
            m = get_mirror(client_manager=client_manager, notifier=None, db=db)
            await m.hydrate_from_db()
            print(f'[startup] SM mirror targets={len(getattr(m, '_targets', {}) or {})}', flush=True)
        except Exception as e:
            print(f'[startup] SM mirror hydrate: {e}', flush=True)
        try:
            tr = _ensure_sm_tracker(execute=False, start=False)
            if tr and hasattr(tr, 'hydrate_from_db'):
                await tr.hydrate_from_db()
            print('[startup] SM tracker hydrated', flush=True)
            _sm_auto = os.getenv('SM_AUTO_START', '0').strip().lower() not in ('0', 'false', 'no', 'off')
            if _sm_auto and _bots_auto_start and tr and (not getattr(tr, '_running', False)):

                async def _sm_delayed_start(tracker=tr):
                    await asyncio.sleep(15)
                    try:
                        if not getattr(tracker, '_running', False):
                            tracker.start()
                            print('[startup] SM tracker auto-started (delayed)', flush=True)
                    except Exception as e:
                        print(f'[startup] SM auto-start: {e}', flush=True)
                asyncio.create_task(_sm_delayed_start())
        except Exception as e:
            print(f'[startup] SM tracker hydrate: {e}', flush=True)
        try:
            import json as _json_bl
            from app.services.auth import configure_blacklist_db, hydrate_blacklist_from_db, flush_blacklist_to_db

            async def _bl_load():
                raw = await db.get_setting('auth_jwt_blacklist')
                if not raw:
                    raw = await db.get_setting('auth_blacklist')
                if not raw:
                    return {}
                data = _json_bl.loads(raw) if isinstance(raw, str) else raw
                return data if isinstance(data, dict) else {}

            async def _bl_save(data: dict):
                await db.set_setting('auth_jwt_blacklist', _json_bl.dumps(data or {}))
            configure_blacklist_db(_bl_load, _bl_save)
            n_bl = await hydrate_blacklist_from_db()
            print(f'[startup] auth blacklist hydrated from DB: {n_bl} jti', flush=True)
        except Exception as e:
            print(f'[startup] auth blacklist hydrate: {e}', flush=True)
        try:
            from app.services.position_claim import restore_snapshots_to_claims
            from app.services.legacy_stubs import ROT_BOT_ID
            from app.services.legacy_stubs import IMP_BOT_ID
            from app.services.ai_strategy import AI_BOT_ID
            try:
                from app.services.legacy_stubs import VAL_BOT_ID
            except Exception:
                VAL_BOT_ID = 'validation_strategy'
            rest = await restore_snapshots_to_claims(db, [ROT_BOT_ID, IMP_BOT_ID, AI_BOT_ID, VAL_BOT_ID, 'momentum_strategy'])
            print(f'[startup] position claims restored: {rest}', flush=True)
        except Exception as e:
            print(f'[startup] claim restore: {e}', flush=True)

        async def _blacklist_flush_loop():
            from app.services.auth import flush_blacklist_to_db
            while True:
                await asyncio.sleep(60)
                try:
                    await flush_blacklist_to_db()
                except Exception:
                    pass
        asyncio.create_task(_blacklist_flush_loop())
        print('[startup] 3/7 Migration check ...', flush=True)
        needs_cleanup = False
        try:
            if db._pg_mode:
                row = await db._fetchone('SELECT 1 FROM trades WHERE bot_id = $1 LIMIT 1', (MOM_BOT_ID,))
            else:
                row = await db._fetchone('SELECT 1 FROM trades WHERE bot_id = ? LIMIT 1', (MOM_BOT_ID,))
            if row:
                needs_cleanup = True
        except Exception:
            pass
        if needs_cleanup and (not AI_ONLY_MODE):
            print('[startup]   Old momentum data found - one-time cleanup ...', flush=True)
            for table in ['trades', 'signals', 'performance_metrics']:
                try:
                    await db._execute(f'DELETE FROM {table}')
                except Exception as e:
                    print(f'[startup]   clear {table}: {e}', flush=True)
            print('[startup]   Clean slate ready.', flush=True)
        print('[startup] 4/7 Rotation auto-start ...', flush=True)
        if AI_ONLY_MODE:
            print('[startup]   Rotation skipped (AI_ONLY_MODE)', flush=True)
        elif _env_key and _env_secret and _env_pass and _bots_auto_start and _mom_auto:
            rot_config = RotationConfig(symbols=['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC'], capital=10000.0, top_k=2, roc_period=14, ema_fast=20, ema_slow=50, atr_period=14, adx_min=26.0, min_roc=4.5, sma_long=200, min_hold_days=11, max_leverage=2.0, risk_per_trade=0.08, allocation_pct=0.3, atr_stop_mult=3.5, trail_atr_mult=3.0, breakeven_pct=0.025, partial_tp_pct=0.06, partial_tp_ratio=0.3, partial_tp2_pct=0.12, partial_tp2_ratio=0.3, allow_short=True, gate_enabled=True, gate_llm_veto=True, desk_telegram=False, poll_interval_sec=300, auto_execute=True)
            r = RotationStrategy(config=rot_config, client_manager=client_manager, db=db, notifier=telegram)
            global rotation
            rotation = r
            try:
                await rotation.start()
                print('[startup]   Rotation RUNNING (auto-start after boot/wake)', flush=True)
            except Exception as e:
                print(f'[startup]   Rotation FAILED to start: {e}', flush=True)
        else:
            print('[startup]   Rotation skipped (no OKX env keys)', flush=True)
        print('[startup] 5/7 Impulse 1D auto-start ...', flush=True)
        if AI_ONLY_MODE:
            print('[startup]   Impulse skipped (AI_ONLY_MODE)', flush=True)
        elif _env_key and _env_secret and _env_pass and _bots_auto_start and _imp_auto:
            imp_config = ImpulseConfig(symbols=['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC'], capital=10000.0, top_k=3, entry_roc=6.0, max_adds=0, risk_per_trade=0.045, sl_atr_mult=5.0, sl_atr_mult_short=5.0, trail_atr_mult=12.0, trail_atr_mult_short=12.0, cooldown_bars=3, tp1_atr=2.0, tp1_frac=0.25, tp2_atr=10.0, tp2_frac=0.3, max_hold_bars=28, max_leverage=3.0, poll_interval_sec=300, auto_execute=True, allow_short=False, btc_sma200_filter=True, peak_lock_after_tp1=True)
            imp = ImpulseStrategy(config=imp_config, client_manager=client_manager, db=db, notifier=telegram)
            global impulse
            impulse = imp
            try:
                await impulse.start()
                print('[startup]   Impulse RUNNING (auto-start after boot/wake)', flush=True)
            except Exception as e:
                print(f'[startup]   Impulse FAILED to start: {e}', flush=True)
        else:
            print('[startup]   Impulse skipped (no OKX env keys)', flush=True)
        print('[startup] 6/7 MACD+Donchian Validation auto-start ...', flush=True)
        if AI_ONLY_MODE:
            print('[startup]   Validation skipped (AI_ONLY_MODE)', flush=True)
        elif _env_key and _env_secret and _env_pass and _bots_auto_start and _val_auto:
            val_config = make_validation_config(capital=300.0, top_k=2, donchian_n=30, tp_pct=0.08, tp_ratio=0.4, tp2_pct=0.08, be_pct=0.015, chandelier_atr=4.0, max_hold_days=3, risk_per_trade=0.14, allocation_pct=0.5, max_leverage=2.0, poll_interval_sec=300, auto_execute=True)
            v = ValidationStrategy(config=val_config, client_manager=client_manager, db=db, notifier=telegram)
            global validation
            validation = v
            try:
                await validation.start()
                print('[startup]   Validation RUNNING (auto-start after boot/wake)', flush=True)
            except Exception as e:
                print(f'[startup]   Validation FAILED to start: {e}', flush=True)
        else:
            print('[startup]   Validation skipped (no OKX env keys)', flush=True)
        if AI_ONLY_MODE:
            for _name, _bot in (('rotation', rotation), ('impulse', impulse), ('validation', validation), ('vwap_rev', vwap_rev_bot), ('scalp', scalp_bot)):
                try:
                    if _bot and getattr(_bot, '_running', False):
                        if hasattr(_bot, 'stop'):
                            res = _bot.stop()
                            if hasattr(res, '__await__'):
                                await res
                        print(f'[startup] AI_ONLY stopped {_name}', flush=True)
                except Exception as _e:
                    print(f'[startup] AI_ONLY stop {_name}: {_e}', flush=True)
            try:
                if sm_tracker and getattr(sm_tracker, '_running', False):
                    sm_tracker.stop()
                    print('[startup] AI_ONLY stopped smart_money tracker', flush=True)
            except Exception as _e:
                print(f'[startup] AI_ONLY stop SM: {_e}', flush=True)
        print('[startup] 7/7 Done ...', flush=True)
    except Exception as e:
        print(f'[startup] ERROR: {e}', flush=True)
    global bot_poller, equity_tracker
    try:
        if telegram.token:
            bot_poller = TelegramBotPoller(notifier=telegram, db=db)
            bot_poller.start()
            print('[startup] Telegram poller started', flush=True)
            try:
                asyncio.get_event_loop().create_task(bot_poller.notify_signals_migration())
            except Exception as e:
                print(f'[startup] signals migration task error: {e}', flush=True)
        else:
            print('[startup] Telegram poller skipped (no bot token)', flush=True)
    except Exception as e:
        print(f'[startup] Telegram poller error: {e}', flush=True)
    try:
        if client_manager.get_client():
            equity_tracker = EquityTracker(client_manager=client_manager, db=db)
            equity_tracker.start()
            print('[startup] Equity tracker started', flush=True)
    except Exception as e:
        print(f'[startup] Equity tracker error: {e}', flush=True)
    global _warm_task
    try:

        async def _delayed_bg():
            await asyncio.sleep(8)
            try:
                await _warm_dashboard_caches()
            except Exception as e:
                print(f'[startup] warmer ended: {e}', flush=True)
        _warm_task = asyncio.create_task(_delayed_bg())
        print('[startup] Dashboard cache warmer scheduled (+8s)', flush=True)

        async def _exchange_sync_bg():
            await asyncio.sleep(10)
            try:
                n = await sync_exchange_close_trades()
                print(f'[startup] Exchange sync done: {n} trades', flush=True)
            except Exception as e:
                print(f'[startup] Exchange sync error: {e}', flush=True)
        asyncio.create_task(_exchange_sync_bg())
        print('[startup] Exchange close trades sync scheduled (+10s)', flush=True)

        async def _delayed_orphan():
            await asyncio.sleep(120)
            await _orphan_sweep_loop()
        asyncio.create_task(_delayed_orphan())
        print('[startup] Orphan sweeper scheduled (+120s)', flush=True)
    except Exception as e:
        print(f'[startup] Dashboard cache warmer error: {e}', flush=True)
    _startup_log = []
    def _slog(msg):
        _startup_log.append(msg)
        print(f'[startup-diag] {msg}', flush=True)
    try:
        _slog('AI auto-start entry point reached')
        print('[startup] AI Discretionary auto-start ...', flush=True)
        _ai_force = os.getenv('AI_FORCE_AUTOSTART', '1' if AI_ONLY_MODE else '0').strip().lower() not in ('0', 'false', 'no', 'off')
        _persist_stop = os.getenv('AI_PERSIST_USER_STOP', '0').strip().lower() in ('1', 'true', 'yes', 'on')
        _user_stopped = False
        try:
            _us = await db.get_setting('ai_user_stopped')
            _user_stopped = str(_us or '').strip().lower() in ('1', 'true', 'yes', 'on')
        except Exception as _ase:
            _slog(f'AI auto-start state read error: {_ase}')
        _slog(f'ai_force={_ai_force} persist_stop={_persist_stop} user_stopped_raw={_us!r} user_stopped={_user_stopped}')
        if (AI_ONLY_MODE or _env_demo) and _user_stopped and not _persist_stop:
            _user_stopped = False
            try:
                await db.set_setting('ai_user_stopped', '0')
            except Exception:
                pass
            _slog('cleared ai_user_stopped')
        _do_ai = bool(_bots_auto_start and _ai_auto and (_ai_force or not _user_stopped))
        if _user_stopped and not _ai_force:
            _do_ai = False
        _slog(f'do_ai={_do_ai} bots_auto={_bots_auto_start} ai_auto={_ai_auto}')
        print(
            f'[startup] AI auto-start: bots={_bots_auto_start} ai_auto={_ai_auto} '
            f'force={_ai_force} user_stopped={_user_stopped} do={_do_ai}',
            flush=True,
        )
        if _do_ai:
            _demo = _env_demo
            _slog(f'mode={"DEMO" if _demo else "LIVE"}')
            try:
                if _demo:
                    await _ensure_showcase()
                    _k = _demo_key or _env_key
                    _s = _demo_secret or _env_secret
                    _pw = _demo_pass or _env_pass
                    _slog(f'demo keys: k={bool(_k)} s={bool(_s)} pw={bool(_pw)} cm={bool(client_manager)}')
                    if _k and _s and _pw and client_manager:
                        await client_manager.init_client(_k, _s, _pw, True)
                else:
                    await _load_live_creds_from_db()
                    _k = _live_key or _env_key
                    _s = _live_secret or _env_secret
                    _pw = _live_pass or _env_pass
                    _slog(f'live keys: k={bool(_k)} s={bool(_s)} pw={bool(_pw)} cm={bool(client_manager)}')
                    if _k and _s and _pw and client_manager:
                        await client_manager.init_client(_k, _s, _pw, False)
            except Exception as _ce:
                _slog(f'client init error: {_ce}')
            _cli = client_manager.get_client() if client_manager else None
            _has_creds = getattr(_cli, 'has_credentials', lambda: False)() if _cli else False
            _has_keys = bool(
                (_cli and _has_creds)
                or (_env_key and _env_secret and _env_pass)
                or (_demo_key and _demo_secret and _demo_pass)
            )
            _slog(f'cli={bool(_cli)} has_creds={_has_creds} env_keys={bool(_env_key)} demo_keys={bool(_demo_key)} has_keys={_has_keys}')
            if not _has_keys:
                _slog('SKIPPED: no OKX keys')
                print('[startup]   AI Discretionary skipped (no OKX keys)', flush=True)
            else:
                if _demo:
                    _exec = True
                else:
                    env_ex = os.getenv('AI_EXECUTE', '1').strip().lower()
                    _exec = env_ex not in ('0', 'false', 'no', 'off')
                try:
                    from app.services.ai_agent import ALLOWED_SYMBOLS as _AI_SYMS
                    _syms = list(_AI_SYMS)
                except Exception:
                    _syms = ['BTC', 'ETH', 'SOL', 'OKB', 'DOGE', 'XRP', 'BCH', 'DAI']
                _cap = float(os.getenv('AI_CAPITAL', '10000'))
                try:
                    _cap_db = await db.get_setting('ai_live_capital')
                    if _cap_db and float(_cap_db) >= 100:
                        _cap = float(_cap_db)
                except Exception:
                    pass
                try:
                    _lm_en = await db.get_setting('live_mirror_enabled') if db else 'N/A'
                    _lm_k = await db.get_setting('live_mirror_key') if db else 'N/A'
                    _lm_s = await db.get_setting('live_mirror_secret') if db else 'N/A'
                    _lm_p = await db.get_setting('live_mirror_pass') if db else 'N/A'
                    _slog(f'live mirror pre-check: enabled={_lm_en} key={"set" if _lm_k else "missing"} secret={"set" if _lm_s else "missing"} pass={"set" if _lm_p else "missing"}')
                    _pre_ok = await _ensure_live_mirror_client()
                    _slog(f'pre-AI live ensure: {_pre_ok}')
                except Exception as _em:
                    _slog(f'pre-AI live ensure: {_em}')
                ai_cfg = AIConfig(symbols=_syms, capital=_cap, max_leverage=float(os.getenv('AI_MAX_LEVERAGE', '3')), max_positions=int(os.getenv('AI_MAX_POSITIONS', '1')), risk_per_trade=float(os.getenv('AI_RISK_PER_TRADE', '0.02')), poll_interval_sec=int(os.getenv('AI_POLL_SEC', '60')), execute=_exec)
                ai_bot = AIStrategy(config=ai_cfg, client_manager=client_manager, db=db, notifier=telegram, live_client_manager=live_manager)
                _wire_ai_live_cb(ai_bot)
                try:
                    import json as _json
                    _cfgkey = 'ai_config:demo' if _demo else 'ai_config:live'
                    _raw_cfg = await db.get_setting(_cfgkey)
                    if _raw_cfg:
                        _saved_cfg = _json.loads(_raw_cfg)
                        for _k in ('capital', 'symbols', 'execute'):
                            _saved_cfg.pop(_k, None)
                        ai_bot.apply_config_dict(_saved_cfg, keep_execute=True)
                        _slog(f'applied saved config ({_cfgkey}, {len(_saved_cfg)} keys)')
                except Exception as _ce:
                    _slog(f'saved config apply error: {_ce}')
                ai_bot.start()
                _slog(f'ai_bot started: running={ai_bot._running}')
                _positions_cache = None
                try:
                    await db.set_setting('ai_bot_running', '1')
                    await db.set_setting('ai_user_stopped', '0')
                except Exception:
                    pass
                print(f'[startup]   AI Discretionary RUNNING (auto) execute={_exec} capital={ai_cfg.capital} symbols={_syms} mode={("DEMO" if _demo else "LIVE")}', flush=True)
                try:
                    ok_m = await _ensure_live_mirror_client()
                    _slog(f'live mirror after AI start: {ok_m}')
                except Exception as _me:
                    _slog(f'live mirror attach error: {_me}')
        else:
            _slog(f'SKIPPED: auto-start off')
            print(f'[startup]   AI Discretionary skipped (auto-start off: bots={_bots_auto_start} ai={_ai_auto} force={_ai_force} user_stopped={_user_stopped})', flush=True)
    except Exception as e:
        import traceback
        _slog(f'AI FAILED: {e}\n{traceback.format_exc()}')
        print(f'[startup]   AI FAILED: {e}\n{traceback.format_exc()}', flush=True)
    try:
        import json as _sj
        await db.set_setting('startup_log', _sj.dumps(_startup_log[-50:]))
    except Exception:
        pass

    # Delayed AI auto-start retry (keys / showcase may lag first attempt)
    async def _ai_autostart_retry():
        await asyncio.sleep(12)
        global ai_bot
        try:
            if ai_bot and getattr(ai_bot, "_running", False):
                return
            if not (_bots_auto_start and _ai_auto):
                return
            try:
                _us = await db.get_setting("ai_user_stopped") if db else None
                if str(_us or "").strip().lower() in ("1", "true", "yes", "on"):
                    if os.getenv("AI_FORCE_AUTOSTART", "1" if AI_ONLY_MODE else "0").strip().lower() in ("0", "false", "no", "off"):
                        print("[startup] AI retry skipped (user_stopped)", flush=True)
                        return
            except Exception:
                pass
            print("[startup] AI auto-start RETRY ...", flush=True)
            await _ensure_showcase()
            # Respect restored live mode from DB (same as main startup)
            _retry_demo = _env_demo
            if not _retry_demo and _live_key and _live_secret and _live_pass:
                _k = _live_key
                _s = _live_secret
                _pw = _live_pass
            else:
                _k = _demo_key or _env_key
                _s = _demo_secret or _env_secret
                _pw = _demo_pass or _env_pass
                _retry_demo = True
            if not (_k and _s and _pw):
                print("[startup] AI retry: still no OKX keys", flush=True)
                return
            if client_manager:
                await client_manager.init_client(_k, _s, _pw, _retry_demo)
            try:
                from app.services.ai_agent import ALLOWED_SYMBOLS as _AI_SYMS
                _syms = list(_AI_SYMS)
            except Exception:
                _syms = ["BTC", "ETH", "SOL", "OKB", "DOGE", "XRP", "BCH", "DAI"]
            _cap = float(os.getenv("AI_CAPITAL", "10000"))
            ai_cfg = AIConfig(
                symbols=_syms, capital=_cap,
                max_leverage=float(os.getenv("AI_MAX_LEVERAGE", "3")),
                max_positions=int(os.getenv("AI_MAX_POSITIONS", "1")),
                risk_per_trade=float(os.getenv("AI_RISK_PER_TRADE", "0.02")),
                poll_interval_sec=int(os.getenv("AI_POLL_SEC", "60")),
                execute=True,
            )
            ai_bot = AIStrategy(
                config=ai_cfg, client_manager=client_manager, db=db,
                notifier=telegram, live_client_manager=live_manager,
            )
            _wire_ai_live_cb(ai_bot)
            ai_bot.start()
            if db:
                await db.set_setting("ai_bot_running", "1")
                await db.set_setting("ai_user_stopped", "0")
            print("[startup]   AI Discretionary RUNNING (retry)", flush=True)
            try:
                await _ensure_live_mirror_client()
                _wire_ai_live_cb(ai_bot)
            except Exception as _er:
                print(f"[startup] retry live ensure: {_er}", flush=True)
        except Exception as e:
            print(f"[startup] AI retry failed: {e}", flush=True)

    async def _live_mirror_heal():
        """Re-bind live mirror after restart / idle disconnect every 60s."""
        for _ in range(5):
            await asyncio.sleep(8)
            try:
                ok = await _ensure_live_mirror_client()
                if ok:
                    print('[startup] live mirror heal: OK', flush=True)
                    break
            except Exception as e:
                print(f'[startup] live mirror heal: {e}', flush=True)
        while True:
            await asyncio.sleep(60)
            try:
                await _ensure_live_mirror_client()
            except Exception as e:
                print(f'[LIVE] heal loop: {e}', flush=True)

    try:
        asyncio.create_task(_ai_autostart_retry())
        asyncio.create_task(_live_mirror_heal())
    except Exception as e:
        print(f"[startup] AI retry / live heal schedule: {e}", flush=True)

    try:
        print('[startup] AI Scale-In (SCL) — RETIRED, skip auto-start', flush=True)
        _scale_auto = False
        if not _env_demo:
            print('[startup]   AI Scale-In skipped — LIVE mode (DEMO-only bot)', flush=True)
        elif _env_key and _env_secret and _env_pass and _scale_auto:
            if ai_scale_bot and getattr(ai_scale_bot, '_running', False):
                print('[startup]   AI Scale-In already running', flush=True)
            else:
                _demo = _env_demo
                if _demo:
                    _exec_s = True
                else:
                    env_ex = os.getenv('AI_EXECUTE', '1').strip().lower()
                    _exec_s = env_ex not in ('0', 'false', 'no', 'off')
                from app.services.legacy_stubs import AIScaleStrategy, AIScaleConfig
                scfg = AIScaleConfig(capital=float(os.getenv('AI_SCALE_CAPITAL', '5000')), max_leverage=float(os.getenv('AI_MAX_LEVERAGE', '3')), max_positions=1, risk_per_trade=float(os.getenv('AI_RISK_PER_TRADE', '0.02')), poll_interval_sec=int(os.getenv('AI_POLL_SEC', '60')), execute=_exec_s, scale_enabled=True, max_adds=2)
                ai_scale_bot = AIScaleStrategy(config=scfg, client_manager=client_manager, db=db, notifier=telegram)
                ai_scale_bot.start()
                _positions_cache = None
                print(f'[startup]   AI Scale-In (SCL) RUNNING execute={_exec_s} capital={scfg.capital} tg={telegram.configured}', flush=True)
        else:
            print('[startup]   AI Scale-In skipped (AI_SCALE_AUTO_START=0 or no OKX keys)', flush=True)
    except Exception as e:
        print(f'[startup]   AI Scale-In FAILED: {e}', flush=True)
    try:
        set_hydrate_deps(_user_okx_client, lambda: RotationConfig())
        await strategy_mgr.hydrate_user_bots(db=db, notifier_fn=lambda uid: _user_notifier(uid))
    except Exception as e:
        print(f'[startup] user bots hydrate: {e}', flush=True)
    try:
        pending = await db.get_setting('fix_last_eth_to_scale_pnl')
        if pending not in (None, '', 'applied'):
            pnl_moved = float(pending or 0)
            if abs(pnl_moved) > 1e-09:
                if ai_bot:
                    ai_bot._lifetime_pnl = float(getattr(ai_bot, '_lifetime_pnl', 0) or 0) - pnl_moved
                    try:
                        log = list(getattr(ai_bot, '_trade_log', None) or [])
                        keep = []
                        moved = []
                        for row in log:
                            try:
                                p = float(row.get('pnl') or 0)
                            except (TypeError, ValueError):
                                p = 0
                            sym = str(row.get('symbol') or row.get('inst_id') or '')
                            if 'ETH' in sym.upper() and abs(p - pnl_moved) < 8.0:
                                moved.append(row)
                                continue
                            keep.append(row)
                        ai_bot._trade_log = keep
                        if moved and ai_scale_bot:
                            sc_log = list(getattr(ai_scale_bot, '_trade_log', None) or [])
                            sc_log.extend(moved)
                            ai_scale_bot._trade_log = sc_log[-200:]
                            print(f'[startup] moved {len(moved)} ETH rows from AI log → Scale log', flush=True)
                    except Exception as e:
                        print(f'[startup] trade_log move: {e}', flush=True)
                if ai_scale_bot:
                    ai_scale_bot._lifetime_pnl = float(getattr(ai_scale_bot, '_lifetime_pnl', 0) or 0) + pnl_moved
                print(f'[startup] applied Scale PnL memory shift {pnl_moved:+.2f}', flush=True)
            await db.set_setting('fix_last_eth_to_scale_pnl', 'applied')
            try:
                _pnl_cache.clear()
                _paired_cache.clear()
            except Exception:
                pass
    except Exception as e:
        print(f'[startup] Scale PnL memory shift: {e}', flush=True)
    try:
        _publish_runtime()
        print('[startup] runtime published', flush=True)
    except Exception:
        pass


@app.on_event('shutdown')
async def shutdown():
    global _warm_task
    if _warm_task:
        try:
            _warm_task.cancel()
        except Exception:
            pass
    if equity_tracker:
        try:
            equity_tracker.stop()
        except Exception:
            pass
    if bot_poller:
        try:
            bot_poller.stop()
        except Exception:
            pass
    if rotation and rotation._running:
        await rotation.stop()
    if impulse and impulse._running:
        await impulse.stop()
    if validation and validation._running:
        await validation.stop()
    try:
        if ai_bot is not None and db is not None:
            _ai_running_now = bool(getattr(ai_bot, '_running', False))
            await db.set_setting('ai_bot_running', '1' if _ai_running_now else '0')
            print(f'[shutdown] AI running state persisted: {_ai_running_now}', flush=True)
    except Exception as e:
        print(f'[shutdown] AI state persist failed: {e}', flush=True)
    if ai_bot and getattr(ai_bot, '_running', False):
        try:
            ai_bot.stop()
        except Exception:
            pass
    await db.close()
    try:
        from app.services.analysis_logger import get_logger
        get_logger().close()
    except Exception:
        pass

async def _ensure_showcase() -> Optional[OKXClient]:
    """Always-on DEMO client for observers / public tracker."""
    c = showcase_manager.get_client()
    if c:
        return c
    key = _demo_key or _env_key
    secret = _demo_secret or _env_secret
    passphrase = _demo_pass or _env_pass
    if key and secret and passphrase:
        await showcase_manager.init_client(key, secret, passphrase, True)
        return showcase_manager.get_client()
    return None

async def resolve_view_client(request: Request=None):
    """Which OKX account this HTTP caller should see.

    - guest / no personal keys → showcase DEMO (always)
    - telegram user with live keys + okx_demo=0 → their LIVE
    - telegram user okx_demo=1 → showcase DEMO (switch back to demo view)
    - admin + platform mode LIVE + live keys → owner LIVE
    - admin + platform mode DEMO → showcase DEMO
    """
    role, user_id, user_row = ('guest', None, None)
    if request is not None:
        try:
            role, user_id, user_row = await _me_ctx(request)
        except Exception:
            role, user_id, user_row = ('guest', None, None)
    if user_id:
        wants_demo = True
        if user_row is not None:
            wants_demo = bool(user_row.get('okx_demo', 1))
        if not wants_demo:
            uc = await _user_okx_client(str(user_id))
            if uc:
                return (uc, 'live', 'user')
        sc = await _ensure_showcase()
        return (sc, 'demo', 'showcase')
    if role == 'admin':
        if not _env_demo and _live_key and _live_secret and _live_pass:
            c = client_manager.get_client()
            if not c or getattr(c, 'demo', True):
                await client_manager.init_client(_live_key, _live_secret, _live_pass, False)
                c = client_manager.get_client()
            if c:
                return (c, 'live', 'owner')
        sc = await _ensure_showcase()
        return (sc, 'demo', 'showcase')
    sc = await _ensure_showcase()
    return (sc, 'demo', 'showcase')

async def _okx_call(coro_factory):
    """Bot/internal calls — uses active client_manager (admin trading context)."""
    client = client_manager.get_client()
    if not client:
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
            client = client_manager.get_client()
        if not client:
            return {'error': True, 'message': 'API not configured'}
    result = await coro_factory(client)
    if result.get('error'):
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
            client = client_manager.get_client()
            if client:
                result = await coro_factory(client)
    return result

async def _okx_call_view(request: Request, coro_factory):
    """HTTP view calls — guest always DEMO; user/admin per their mode."""
    client, mode, source = await resolve_view_client(request)
    if not client:
        return {'error': True, 'message': 'API not configured', 'view_mode': mode, 'view_source': source}
    result = await coro_factory(client)
    if isinstance(result, dict):
        result = dict(result)
        result.setdefault('view_mode', mode)
        result.setdefault('view_source', source)
    return result

def _account_mode() -> str:
    """Current owner trading environment: demo | live (never mixed)."""
    return 'demo' if _env_demo else 'live'

async def _okx_call_account(coro_factory, mode: str=None):
    """Call OKX on the isolated account for mode (demo showcase vs live keys).

    Demo → showcase_manager (env simulated). Live → client_manager with live keys.
    Does not cross-read the other environment.
    """
    mode = (mode or _account_mode()).lower()
    if mode == 'live':
        await _load_live_creds_from_db()
        client = client_manager.get_client()
        if not client or getattr(client, 'demo', True):
            if not (_live_key and _live_secret and _live_pass):
                return {'error': True, 'message': 'Live keys not configured'}
            await client_manager.init_client(_live_key, _live_secret, _live_pass, False)
            client = client_manager.get_client()
        if not client:
            return {'error': True, 'message': 'Live client unavailable'}
        return await coro_factory(client)
    client = await _ensure_showcase()
    if not client:
        return {'error': True, 'message': 'Demo showcase not configured'}
    return await coro_factory(client)

def _invalidate_account_caches():
    """Drop mode-sensitive caches so Live never shows Demo numbers (and vice versa)."""
    global _fills_cache, _fills_cache_ts, _paired_cache, _pnl_cache, _portfolio_cache, _portfolio_cache_ts, _bills_cache
    global _positions_cache, _positions_cache_ts
    _fills_cache = None
    _fills_cache_ts = 0
    _positions_cache = None
    _positions_cache_ts = 0
    try:
        if isinstance(_bills_cache, dict):
            _bills_cache.clear()
        else:
            _bills_cache = {}
    except Exception:
        _bills_cache = {}
    try:
        _paired_cache.clear()
    except Exception:
        pass
    try:
        _pnl_cache.clear()
    except Exception:
        pass
    _portfolio_cache = None
    _portfolio_cache_ts = 0
    try:
        setattr(_fetch_okx_fills, '_cache_key', '')
    except Exception:
        pass

def _trade_matches_mode(tr: dict, mode: str) -> bool:
    """Strict filter: DEMO and LIVE never share rows.

    - Explicit account_mode must equal active mode.
    - OKX rows must be tagged with the same account_mode (set at fetch).
    - Untagged legacy → DEMO only.
    """
    if not isinstance(tr, dict):
        return False
    mode = (mode or 'demo').lower()
    m = (tr.get('account_mode') or tr.get('mode') or '').strip().lower()
    if m in ('live', 'demo'):
        return m == mode
    return mode == 'demo'
PUBLIC_API_PATHS = {'/api/health', '/api/auth/login', '/api/auth/guest', '/api/auth/status', '/api/auth/logout', '/api/auth/telegram', '/api/ai/status', '/api/me/dashboard', '/api/meta/product', '/api/startup-log'}
ADMIN_ONLY_PATHS = {'/api/credentials/status', '/api/credentials/test', '/api/credentials/init', '/api/trade/order', '/api/positions/close', '/api/positions/sweep-orphans', '/api/momentum/start', '/api/momentum/stop', '/api/momentum/config', '/api/rotation/start', '/api/rotation/stop', '/api/rotation/reset', '/api/rotation/config', '/api/impulse/start', '/api/impulse/stop', '/api/impulse/config', '/api/impulse/reset', '/api/validation/start', '/api/validation/stop', '/api/validation/reset', '/api/validation/config', '/api/validation/status', '/api/validation/trades', '/api/validation/indicators', '/api/db/reset-all', '/api/db/positions', '/api/telegram/status', '/api/telegram/config', '/api/telegram/test', '/api/telegram/simulate', '/api/telegram/menu', '/api/analysis/log', '/api/subs', '/api/subs/activate', '/api/subs/deactivate', '/api/subs/config', '/api/mode', '/api/audit', '/api/risk/kill', '/api/pnl/rebuild-strategy', '/api/admin/reset-trading-stats', '/api/ai/start', '/api/ai/stop', '/api/ai/decide', '/api/ai/correct-attribution', '/api/ai/logs', '/api/ai/logs/download'}
ADMIN_ONLY_PREFIXES = ('/api/debug/', '/api/admin/', '/api/vwap_rev/')
GUEST_FORBIDDEN_PREFIXES = ('/api/pnl', '/api/trades', '/api/positions', '/api/portfolio', '/api/momentum', '/api/rotation', '/api/impulse', '/api/validation', '/api/ai/', '/api/smart-money', '/api/reports', '/api/backtest', '/api/credentials', '/api/mode', '/api/audit', '/api/db/', '/api/me', '/api/trade', '/api/chart', '/api/risk')

@app.middleware('http')
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith('/api/') or path in PUBLIC_API_PATHS:
        return await call_next(request)
    role = validate(get_token(request))
    if role is None:
        return JSONResponse(status_code=401, content={'detail': 'Unauthorized'})
    if path in ADMIN_ONLY_PATHS or any((path.startswith(p) for p in ADMIN_ONLY_PREFIXES)):
        if role != 'admin':
            return JSONResponse(status_code=403, content={'detail': 'Forbidden'})
    if role == 'guest':
        for p in GUEST_FORBIDDEN_PREFIXES:
            if path == p or path.startswith(p + '/') or path.startswith(p):
                return JSONResponse(status_code=403, content={'detail': 'Guest cannot access trading data. Sign in as admin.'})
    return await call_next(request)

@app.post('/api/auth/login')
async def auth_login(request: Request, data: dict):
    ip = request.client.host if request.client else 'unknown'
    if check_rate_limit(ip):
        raise HTTPException(status_code=429, detail='Too many login attempts. Try again later.')
    token = login(data.get('password', ''))
    if token:
        record_attempt(ip, True)
        body = {'role': 'admin', 'cookie_auth': True}
        if (os.getenv('COOKIE_ONLY_AUTH') or '0').strip().lower() not in ('1', 'true', 'yes'):
            body['token'] = token
        resp = JSONResponse(body)
        return _set_auth_cookie(resp, token)
    record_attempt(ip, False)
    raise HTTPException(status_code=401, detail='Invalid password')

@app.post('/api/auth/guest')
async def auth_guest(request: Request):
    ip = request.client.host if request.client else 'unknown'
    if guest_rate_limited(ip):
        raise HTTPException(status_code=429, detail='Too many requests. Try again later.')
    record_guest(ip)
    token = guest()
    body = {'role': 'guest', 'cookie_auth': True}
    if (os.getenv('COOKIE_ONLY_AUTH') or '0').strip().lower() not in ('1', 'true', 'yes'):
        body['token'] = token
    resp = JSONResponse(body)
    return _set_auth_cookie(resp, token)

@app.get('/api/auth/status')
async def auth_status(request: Request):
    token = get_token(request)
    valid = validate(token)
    admin = is_admin(token) if valid else False
    user_id = get_user_id(token) if valid else None
    plan = None
    if user_id:
        try:
            u = await db.get_user_by_telegram(user_id)
            plan = u.get('plan') if u else None
        except Exception:
            plan = None
    return {'authenticated': valid, 'role': 'admin' if admin else 'user' if valid and user_id else 'guest' if valid else 'none', 'user_id': user_id, 'plan': plan, 'has_password': bool(PASSWORD)}

@app.post('/api/auth/telegram')
async def auth_telegram(data: dict):
    """Authenticate a Telegram Mini App user via WebApp initData.

    The initData signature is verified with the bot token. The chat matching
    TELEGRAM_CHAT_ID is granted an admin session; every other verified user is
    auto-provisioned with their own account (role=user, their own OKX creds).
    """
    init_data = (data or {}).get('initData', '')
    logger.info('mini auth: initData present=%s len=%s', bool(init_data), len(init_data))
    if not init_data:
        raise HTTPException(status_code=400, detail='Missing initData')
    if not telegram.token:
        logger.warning('mini auth: bot token not configured')
        raise HTTPException(status_code=400, detail='Telegram bot not configured')
    payload = telegram.verify_init_data(init_data)
    if payload is None:
        logger.warning('mini auth: initData signature INVALID')
        raise HTTPException(status_code=401, detail='Invalid Telegram initData')
    user = payload.get('user') or {}
    uid = str(user.get('id', ''))
    logger.info('mini auth: signature OK, user.id=%s (%s)', uid, user.get('username'))
    if telegram.chat_id and uid == telegram.chat_id:
        token = grant_admin()
        logger.info('mini auth: admin token granted for owner %s', uid)
        return {'token': token, 'role': 'admin', 'user': {'id': user.get('id'), 'username': user.get('username'), 'first_name': user.get('first_name')}}
    try:
        u = await db.find_or_create_user(uid, user.get('username'), user.get('first_name'))
    except Exception as e:
        logger.warning('mini auth: user provision error: %s', e)
        raise HTTPException(status_code=500, detail='Failed to create account')
    plan = (u or {}).get('plan', 'free')
    if plan == 'pro' and _is_active(u):
        token = grant_user(uid)
        logger.info('mini auth: PRO user account %s granted mini-app access', uid)
        return {'token': token, 'role': 'user', 'user': {'id': user.get('id'), 'username': user.get('username'), 'first_name': user.get('first_name')}, 'plan': plan}
    logger.info('mini auth: user %s denied mini-app (plan=%s, active=%s)', uid, plan, _is_active(u) if u else False)
    raise HTTPException(status_code=403, detail='Доступ к мини-апу только по Pro-подписке. Сигналы — бесплатно в боте.')

@app.post('/api/auth/logout')
async def auth_logout(request: Request):
    token = get_token(request)
    try:
        logout(token)
        try:
            from app.services.auth import flush_blacklist_to_db
            await flush_blacklist_to_db()
        except Exception:
            pass
    except Exception:
        pass
    resp = JSONResponse({'ok': True})
    return _clear_auth_cookie(resp)

async def _me_ctx(request: Request):
    """Resolve the authenticated user context.

    Returns (role, user_id, user_row_or_None). Owner (admin) has user_id=None
    and keeps using env creds + global bots. 'user' role maps to their own
    account. Guests are rejected.
    """
    token = get_token(request)
    role = validate(token)
    if role not in ('admin', 'user'):
        raise HTTPException(status_code=403, detail='Forbidden')
    user_id = get_user_id(token)
    user_row = None
    if user_id:
        try:
            user_row = await db.get_user_by_telegram(user_id)
        except Exception:
            user_row = None
    return (role, user_id, user_row)

async def _user_okx_client(user_id: str) -> Optional[OKXClient]:
    """Return (and cache) the user's OKXClient from their encrypted creds."""
    global _user_clients
    existing = _user_clients.get(user_id)
    if existing:
        return existing
    try:
        u = await db.get_user_by_telegram(user_id)
    except Exception:
        return None
    if not u:
        return None
    key = decrypt_str(u.get('okx_key_enc') or '')
    secret = decrypt_str(u.get('okx_secret_enc') or '')
    passphrase = decrypt_str(u.get('okx_pass_enc') or '')
    if not (key and secret and passphrase):
        return None
    client = OKXClient(key, secret, passphrase, bool(u.get('okx_demo', 1)))
    _user_clients[user_id] = client
    MAX_USER_CLIENTS = 200
    if len(_user_clients) > MAX_USER_CLIENTS:
        try:
            _oldest = next(iter(_user_clients))
            _evict = _user_clients.pop(_oldest)
            _closer = _evict.close()
            if asyncio.iscoroutine(_closer):
                try:
                    asyncio.get_event_loop().create_task(_closer)
                except Exception:
                    pass
        except Exception:
            pass
    strategy_mgr.set_user_client(user_id, client)
    return client

def _clear_user_client(user_id: str):
    global _user_clients
    old = _user_clients.pop(user_id, None)
    if old:
        try:
            asyncio.get_event_loop().create_task(old.close())
        except Exception:
            pass

def _user_notifier(user_id: str):
    """Notifier that delivers a user's bot signals to THEIR chat (not the channel)."""
    if not telegram.token:
        return telegram
    return TelegramNotifier(token=telegram.token, chat_id=str(user_id), channel_id='')

def _has_active_plan(user_row: dict) -> bool:
    """Pro users need a currently-active subscription to run bots."""
    if not user_row:
        return False
    plan = user_row.get('plan')
    if plan not in ('signals', 'pro'):
        return False
    return _is_active(user_row)

@app.get('/api/me')
async def me_profile(request: Request):
    """Current user profile: plan, subscription status, creds state."""
    role, user_id, user_row = await _me_ctx(request)
    if user_id is None:
        return {'role': 'admin', 'plan': 'owner', 'creds_configured': bool(_env_key and _env_secret and _env_pass), 'demo': _env_demo, 'owner': True}
    creds = bool(user_row and user_row.get('okx_key_enc'))
    return {'role': 'user', 'telegram_id': user_id, 'username': (user_row or {}).get('username'), 'first_name': (user_row or {}).get('first_name'), 'plan': (user_row or {}).get('plan', 'free'), 'active': _has_active_plan(user_row) if user_row else False, 'active_until': (user_row or {}).get('active_until'), 'creds_configured': creds, 'demo': bool((user_row or {}).get('okx_demo', 1)), 'capital': (user_row or {}).get('capital', 10000)}

@app.post('/api/me/credentials')
async def me_credentials(request: Request, data: dict=None):
    """Connect the user's own OKX API keys (encrypted at rest)."""
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        raise HTTPException(status_code=400, detail='Owner uses env credentials')
    d = data or {}
    key = str(d.get('apiKey', '')).strip()
    secret = str(d.get('secretKey', '')).strip()
    passphrase = str(d.get('passphrase', '')).strip()
    demo = bool(d.get('demo', True))
    if not demo:
        urow = await db.get_user_by_telegram(user_id)
        if not _has_active_plan(urow):
            raise HTTPException(status_code=403, detail='Live OKX доступен только с активной подпиской. Используйте Demo или оформите Pro.')
    if not (key and secret and passphrase):
        raise HTTPException(status_code=400, detail='Не хватает API Key / Secret / Passphrase. Заполните все три поля.')
    test = OKXClient(key, secret, passphrase, demo)
    try:
        result = await test.get_balance()
    finally:
        try:
            await test.close()
        except Exception:
            pass
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', 'Connection failed'))
    await db.update_user(user_id, okx_key_enc=encrypt_str(key), okx_secret_enc=encrypt_str(secret), okx_pass_enc=encrypt_str(passphrase), okx_demo=1 if demo else 0)
    strategy_mgr.stop_all(user_id)
    _clear_user_client(user_id)
    return {'message': 'OKX keys connected', 'demo': demo}

@app.get('/api/me/mode')
async def me_get_mode(request: Request):
    """Current view mode for this caller (demo showcase vs personal live)."""
    client, mode, source = await resolve_view_client(request)
    role, user_id, user_row = await _me_ctx(request)
    live_ready = False
    if user_id and user_row:
        live_ready = bool(user_row.get('okx_key_enc'))
    elif role == 'admin':
        live_ready = bool(_live_key and _live_secret and _live_pass)
    return {'mode': mode, 'source': source, 'demo': mode == 'demo', 'live': mode == 'live', 'live_configured': live_ready, 'can_switch_live': live_ready and (role == 'admin' or _has_active_plan(user_row))}

@app.post('/api/me/mode')
async def me_set_mode(request: Request, data: dict=None):
    """Switch this user between showcase DEMO view and personal LIVE account.

    Guests cannot switch. Users need saved keys + active plan for LIVE.
    Admin uses /api/mode (platform owner) — also accepted here as alias.
    """
    global _env_demo
    role, user_id, user_row = await _me_ctx(request)
    d = data or {}
    demo = bool(d.get('demo', True))
    if role == 'admin' and user_id is None:
        if not demo and str(d.get('confirm', '')).strip() != 'LIVE':
            raise HTTPException(status_code=400, detail='confirm must be "LIVE"')
        return await set_trading_mode(request, {'demo': demo, 'confirm': d.get('confirm', 'LIVE' if not demo else '')})
    if not user_id:
        raise HTTPException(status_code=403, detail='Гость всегда видит DEMO-витрину')
    if not demo:
        if not _has_active_plan(user_row):
            raise HTTPException(status_code=403, detail='Live доступен с активной подпиской')
        if not (user_row or {}).get('okx_key_enc'):
            raise HTTPException(status_code=400, detail='Сначала подключите Live API-ключи OKX')
        await db.update_user(user_id, okx_demo=0)
        _clear_user_client(str(user_id))
        return {'ok': True, 'mode': 'live', 'demo': False, 'live': True}
    await db.update_user(user_id, okx_demo=1)
    _clear_user_client(str(user_id))
    return {'ok': True, 'mode': 'demo', 'demo': True, 'live': False}

@app.post('/api/me/credentials/test')
async def me_credentials_test(request: Request, data: dict=None):
    """Test provided (or saved) OKX credentials."""
    role, user_id, _ = await _me_ctx(request)
    d = data or {}
    key = str(d.get('apiKey', '')).strip()
    secret = str(d.get('secretKey', '')).strip()
    passphrase = str(d.get('passphrase', '')).strip()
    demo = bool(d.get('demo', True))
    if user_id and (not (key or secret or passphrase)):
        u = await db.get_user_by_telegram(user_id)
        key = decrypt_str((u or {}).get('okx_key_enc') or '')
        secret = decrypt_str((u or {}).get('okx_secret_enc') or '')
        passphrase = decrypt_str((u or {}).get('okx_pass_enc') or '')
        demo = bool((u or {}).get('okx_demo', 1))
    if not (key and secret and passphrase):
        return {'ok': False, 'message': 'Укажите API Key, Secret Key и Passphrase'}
    test = OKXClient(key, secret, passphrase, demo)
    try:
        result = await test.get_balance()
    finally:
        try:
            await test.close()
        except Exception:
            pass
    if result.get('error'):
        return {'ok': False, 'message': result.get('message', 'Connection failed')}
    return {'ok': True, 'message': 'Connected successfully', 'demo': demo}

@app.get('/api/me/portfolio')
async def me_portfolio(request: Request):
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await get_portfolio(request)
    client = await _user_okx_client(user_id)
    if not client:
        raise HTTPException(status_code=400, detail='Подключите ключи OKX в настройках')
    result = await client.get_balance()
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    data = result.get('data', [])
    if not data:
        return {'totalEqUsd': 0, 'details': []}
    acct = data[0] if isinstance(data, list) else data
    details = []
    for d in acct.get('details', []):
        details.append({'ccy': d.get('ccy'), 'eq': float(d.get('eq', 0)), 'eqUsd': float(d.get('eqUsd', 0)), 'availBal': float(d.get('availBal', 0)), 'frozenBal': float(d.get('frozenBal', 0))})
    return {'totalEqUsd': float(acct.get('totalEq', 0)), 'details': details}

@app.get('/api/me/positions')
async def me_positions(request: Request, inst_type: str='SWAP'):
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await get_positions(request, inst_type)
    client = await _user_okx_client(user_id)
    if not client:
        raise HTTPException(status_code=400, detail='Подключите ключи OKX в настройках')
    result = await client.get_positions(inst_type)
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    return {'positions': result.get('data', [])}

@app.post('/api/me/positions/close')
async def me_positions_close(request: Request, data: dict=None):
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await close_position(data or {})
    client = await _user_okx_client(user_id)
    if not client:
        raise HTTPException(status_code=400, detail='Подключите ключи OKX в настройках')
    d = data or {}
    inst_id = d.get('instId')
    if not inst_id:
        raise HTTPException(status_code=400, detail='instId required')
    pos_side = d.get('posSide') or 'net'
    mgn_mode = d.get('mgnMode', 'cross')
    result = await client.close_position(inst_id=inst_id, mgn_mode=mgn_mode, pos_side=pos_side)
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    return {'message': 'Position closed', 'data': result.get('data')}

def _user_strategy_statuses(ub):
    rot_status = ub.rotation.get_status() if ub.rotation else {'running': False, 'strategy': 'momentum_rotation', 'equity': 0, 'open_positions': {}, 'total_trades': 0, 'total_pnl': 0}
    imp_status = ub.impulse.get_status() if ub.impulse else {'running': False, 'strategy': IMPULSE_NAME, 'version': IMPULSE_VERSION, 'equity': 0, 'open_positions': [], 'closed_trades': 0}
    return (rot_status, imp_status)

@app.get('/api/me/status')
async def me_status(request: Request):
    role, user_id, user_row = await _me_ctx(request)
    if user_id is None:
        rot = await momentum_status() if rotation else {'running': False}
        imp = await impulse_status() if impulse else {'running': False}
        return {'role': 'admin', 'plan': 'owner', 'rotation': rot, 'impulse': imp}
    ub = strategy_mgr.get_or_create(user_id)
    rot_status, imp_status = _user_strategy_statuses(ub)
    return {'role': 'user', 'plan': (user_row or {}).get('plan', 'free'), 'active': _has_active_plan(user_row) if user_row else False, 'rotation': rot_status, 'impulse': imp_status}

def _require_pro(request, user_row) -> None:
    """Users must have an active Pro plan to run bots on their own account."""
    if not _has_active_plan(user_row):
        raise HTTPException(status_code=403, detail='Тариф Pro неактивен — оплатите подписку')

@app.get('/api/me/trades')
async def me_trades(request: Request, limit: int=50):
    """Trade history for the authenticated user's bots."""
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        trades = []
        if rotation:
            trades += list(rotation._trade_log)
        if impulse:
            trades += list(impulse._trade_log)
        trades.sort(key=lambda t: t.get('time', ''), reverse=True)
        return {'trades': trades[:limit]}
    ub = strategy_mgr.get_or_create(user_id)
    trades = []
    if ub.rotation and ub.rotation._trade_log:
        trades += list(ub.rotation._trade_log)
    if ub.impulse and ub.impulse._trade_log:
        trades += list(ub.impulse._trade_log)
    try:
        for bid in (ub.rot_bot_id, ub.imp_bot_id):
            rows = await db.get_trades(bot_id=bid, limit=200)
            for t in reversed(rows):
                trades.append({'time': t.get('timestamp', ''), 'side': t.get('side', ''), 'symbol': t.get('inst_id', ''), 'pnl': float(t.get('pnl', 0) or 0), 'entry_price': float(t.get('px', 0) or 0), 'reason': 'closed'})
    except Exception:
        pass
    trades.sort(key=lambda t: t.get('time', ''), reverse=True)
    return {'trades': trades[:limit]}

@app.get('/api/me/pnl')
async def me_pnl(request: Request):
    """Realized PnL from the user's own trades (their bots only)."""
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await get_pnl()
    ub = strategy_mgr.get_or_create(user_id)
    total = 0.0
    count = 0
    try:
        for bid in (ub.rot_bot_id, ub.imp_bot_id):
            rows = await db.get_trades(bot_id=bid, limit=5000)
            for t in rows:
                pnl = float(t.get('pnl', 0) or 0)
                if pnl != 0:
                    total += pnl
                    count += 1
    except Exception:
        pass
    for bot in (ub.rotation, ub.impulse):
        if bot and bot._trade_log:
            for t in bot._trade_log:
                pnl = float(t.get('pnl', 0) or 0)
                if pnl != 0:
                    total += pnl
                    count += 1
    return {'total': round(total, 2), 'trades': count, 'unrealized': 0.0, 'source': 'user_bots'}

@app.get('/api/me/dashboard')
async def me_dashboard(request: Request):
    """MiniApp dashboard: DEMO (shared server bot data) and LIVE (user's own OKX account)."""
    try:
        role, user_id, user_row = await _me_ctx(request)
    except Exception:
        role, user_id, user_row = 'guest', None, None
    demo = {}
    live = {}
    try:
        if ai_bot:
            st = ai_bot.get_status()
            try:
                st = await _apply_history_kpi(st, 'AI Discretionary 1H')
            except Exception:
                pass
            demo = {
                'running': st.get('running', False),
                'pnl': round(st.get('lifetime_pnl', 0), 2),
                'session_pnl': round(st.get('session_pnl', 0), 2),
                'equity': round(st.get('equity', 0), 2),
                'capital': st.get('capital'),
                'trades': st.get('lifetime_trades', 0),
                'win_rate': st.get('win_rate'),
                'positions': st.get('open_positions', []),
                'pulse': st.get('pulse') or st.get('description') or '',
                'model': st.get('model') or (st.get('llm') or {}).get('model') or '',
            }
    except Exception as e:
        demo = {'error': str(e)}
    trades = []
    has_live = False
    if role == 'admin' and user_id is None:
        has_live = bool(_live_key and _live_secret and _live_pass)
    elif user_id and user_row:
        has_live = bool(user_row.get('okx_key_enc')) and not bool(user_row.get('okx_demo', 1))
    if has_live:
        try:
            client = None
            if role == 'admin' and user_id is None:
                lc = live_manager.get_client() if live_manager else None
                client = lc
            elif user_id:
                client = await _user_okx_client(str(user_id))
            if client:
                bal = await client.get_balance()
                bal_data = bal.get('data', [])
                acct = bal_data[0] if isinstance(bal_data, list) and bal_data else (bal_data if isinstance(bal_data, dict) else {})
                details = acct.get('details', []) if isinstance(acct, dict) else []
                usdt_eq = sum(float(d.get('eq', 0)) for d in details if d.get('ccy') == 'USDT')
                total_eq = float(acct.get('totalEq', usdt_eq)) if isinstance(acct, dict) else usdt_eq
                pos_result = await client.get_positions('SWAP')
                raw_pos = pos_result.get('data', []) if isinstance(pos_result, dict) else (pos_result or [])
                positions = []
                unrealized = 0.0
                for rp in raw_pos:
                    inst = str(rp.get('instId', ''))
                    coin = inst.split('-')[0] if inst else ''
                    pos_raw = float(rp.get('pos', 0) or 0)
                    sz = abs(pos_raw)
                    if sz <= 1e-12:
                        continue
                    side = str(rp.get('posSide', '')).lower()
                    if side in ('', 'net'):
                        side = 'long' if pos_raw > 0 else 'short'
                    entry = float(rp.get('avgPx', 0) or 0)
                    mark = float(rp.get('markPx', 0) or 0)
                    upl = float(rp.get('upl', 0) or 0)
                    lever = float(rp.get('lever', 0) or 0)
                    unrealized += upl
                    positions.append({
                        'coin': coin, 'symbol': inst, 'side': side,
                        'size': sz, 'entry_price': entry, 'mark_price': mark,
                        'upl': round(upl, 2), 'leverage': lever,
                    })
                live = {
                    'connected': True,
                    'equity': round(total_eq, 2),
                    'unrealized': round(unrealized, 2),
                    'positions': positions,
                }
                try:
                    pnl_data = await get_pnl()
                    live['total_pnl'] = round(float(pnl_data.get('total') or 0), 2)
                    live['session_pnl'] = round(float(pnl_data.get('1d') or 0), 2)
                    live['trades'] = int(pnl_data.get('trades_counted') or 0)
                    live['strategy_realized'] = round(float(pnl_data.get('strategy_realized') or pnl_data.get('total') or 0), 2)
                except Exception:
                    pass
                try:
                    bills_resp = await client.get_bills(inst_type='SWAP', type='2', limit=100)
                    bill_data = bills_resp.get('data', []) if isinstance(bills_resp, dict) else []
                    wins_n = 0
                    losses_n = 0
                    _seen_winloss = set()
                    for b in bill_data:
                        oid = str(b.get('ordId', '') or '')
                        sub = str(b.get('subType', '') or '')
                        if sub not in ('5', '6'):
                            continue
                        try:
                            bp = float(b.get('pnl') or 0)
                        except (TypeError, ValueError):
                            bp = 0.0
                        try:
                            bts = int(b.get('ts') or 0)
                        except (TypeError, ValueError):
                            bts = 0
                        if oid and oid not in _seen_winloss:
                            _seen_winloss.add(oid)
                            if bp > 0:
                                wins_n += 1
                            elif bp < 0:
                                losses_n += 1
                        inst = str(b.get('instId', '') or '')
                        side_val = 'sell' if sub == '5' else 'buy'
                        trades.append({
                            'time': bts,
                            'inst': inst.replace('-USDT-SWAP', ''),
                            'side': side_val,
                            'pnl': round(bp, 4),
                            'account_mode': 'live',
                        })
                    _total_t = live.get('trades') or (wins_n + losses_n)
                    live['win_rate'] = round(wins_n / _total_t * 100, 1) if _total_t else None
                    live['wins'] = wins_n
                    live['losses'] = losses_n
                except Exception:
                    pass
        except Exception as e:
            live = {'connected': False, 'error': str(e)}
    else:
        live = {'connected': False}
    try:
        has_demo_trades = False
        if role == 'admin' and user_id is None:
            rows = await db.get_trades(limit=20)
            for t in rows:
                trades.append({
                    'time': t.get('timestamp', ''),
                    'inst': (t.get('inst_id', '') or '').replace('-USDT-SWAP', ''),
                    'side': t.get('side', ''),
                    'pnl': float(t.get('pnl', 0) or 0),
                    'account_mode': t.get('account_mode', 'demo'),
                })
                has_demo_trades = True
        elif user_id:
            ub = strategy_mgr.get_or_create(str(user_id))
            for bid in (ub.rot_bot_id, ub.imp_bot_id):
                rows = await db.get_trades(bot_id=bid, limit=20)
                for t in rows:
                    trades.append({
                        'time': t.get('timestamp', ''),
                        'inst': (t.get('inst_id', '') or '').replace('-USDT-SWAP', ''),
                        'side': t.get('side', ''),
                        'pnl': float(t.get('pnl', 0) or 0),
                        'account_mode': 'live',
                    })
        if not has_demo_trades and ai_bot:
            for t in (ai_bot.get_status() or {}).get('recent_trades', [])[-20:]:
                if 'pnl' not in t:
                    continue
                trades.append({
                    'time': t.get('time', t.get('ts', '')),
                    'inst': (t.get('symbol', t.get('inst_id', '')) or '').replace('-USDT-SWAP', ''),
                    'side': t.get('side', ''),
                    'pnl': float(t.get('pnl', 0) or 0),
                    'account_mode': t.get('account_mode', 'demo'),
                })
                has_demo_trades = True
        if not has_demo_trades:
            try:
                epoch = await get_pnl_epoch()
                epoch_ms = 0
                if epoch:
                    from datetime import datetime as _dt, timezone as _tz
                    epoch_ms = int(_dt.fromisoformat(epoch).replace(tzinfo=_tz.utc).timestamp() * 1000)
                ects = await db.get_exchange_close_trades_detail(epoch_ms=epoch_ms, limit=20)
                for t in ects:
                    st_sub = str(t.get('sub_type', '') or '')
                    side = 'sell' if st_sub == '5' else 'buy' if st_sub == '6' else ''
                    trades.append({
                        'time': t.get('close_ts', ''),
                        'inst': (t.get('inst_id', '') or '').replace('-USDT-SWAP', ''),
                        'side': side,
                        'pnl': float(t.get('pnl', 0) or 0),
                        'account_mode': t.get('account_mode', 'demo'),
                    })
            except Exception:
                pass
    except Exception:
        pass
    def _trade_sort_key(t):
        v = t.get('time', 0)
        if isinstance(v, str):
            return v
        return str(v)
    trades.sort(key=_trade_sort_key, reverse=True)
    return {'demo': demo, 'live': live, 'trades': trades[:20], 'role': role, 'user_id': user_id}

@app.get('/api/ai/status')
async def ai_status():
    """AI status. Prefer History-sourced PnL (same as /api/pnl Total) when available."""
    global ai_bot
    if not ai_bot:
        return {'running': False, 'strategy': 'AI Discretionary 1H', 'total_pnl': 0, 'lifetime_pnl': 0, 'open_positions': []}
    # Lightweight reconcile: drop positions that are flat on exchange (at most once per 30s)
    try:
        if ai_bot._positions and ai_bot._running:
            now_ts = _time.time()
            last = getattr(ai_bot, '_last_status_reconcile_ts', 0)
            if now_ts - last > 30:
                ai_bot._last_status_reconcile_ts = now_ts
                client = None
                try:
                    client = await ai_bot._client()
                except Exception:
                    pass
                if client:
                    await ai_bot._reconcile_positions_with_exchange(client)
    except Exception as _re:
        pass
    try:
        status = ai_bot.get_status()
    except Exception as e:
        import traceback
        print(f'[ai/status] get_status error: {e}', flush=True)
        traceback.print_exc()
        return {'error': f'get_status: {type(e).__name__}: {e}', 'running': ai_bot._running}
    internal = status.get('lifetime_pnl', status.get('total_pnl'))
    status['total_pnl_internal'] = internal
    status['lifetime_pnl_internal'] = internal
    return await _apply_history_kpi(status, 'AI Discretionary 1H')

@app.get('/api/startup-log')
async def startup_log():
    try:
        import json as _sj
        raw = await db.get_setting('startup_log')
        return {'log': _sj.loads(raw) if raw else []}
    except Exception as e:
        return {'log': [], 'error': str(e)}

@app.get('/api/ai/diagnostics')
async def ai_diagnostics():
    """Per-coin veto diagnostics — shows exactly why each coin is or isn't trading."""
    global ai_bot
    if not ai_bot:
        return {'running': False, 'error': 'AI bot not started'}
    try:
        return {'running': ai_bot._running, **ai_bot._diagnose_open()}
    except Exception as e:
        import traceback
        print(f'[ai/diagnostics] error: {e}', flush=True)
        traceback.print_exc()
        return {'error': str(e), 'running': getattr(ai_bot, '_running', False)}

@app.post('/api/admin/reassign-trade', dependencies=[Depends(require_admin)])
async def admin_reassign_trade(data: dict=None):
    """Reassign a closed trade between strategy bots (DB + override list)."""
    data = data or {}
    from_bot = str(data.get('from_bot') or 'rotation_strategy').strip()
    to_bot = str(data.get('to_bot') or 'ai_strategy').strip()
    label_to_id = {'Momentum': 'rotation_strategy', 'Impulse 1D': 'impulse_strategy', 'AI Discretionary 1H': 'ai_strategy', 'MACD+Donchian Validation': 'validation_strategy'}
    id_to_label = {v: k for k, v in label_to_id.items()}
    from_bot = label_to_id.get(from_bot, from_bot)
    to_bot = label_to_id.get(to_bot, to_bot)
    symbol = str(data.get('symbol') or data.get('coin') or 'ETH').upper().replace('-USDT-SWAP', '')
    inst = f'{symbol}-USDT-SWAP'
    side = str(data.get('side') or data.get('pos_side') or 'short').lower()
    pnl_near = data.get('pnl_near', data.get('pnl'))
    try:
        pnl_near = float(pnl_near) if pnl_near is not None else 134.17
    except (TypeError, ValueError):
        pnl_near = 134.17
    time_contains = str(data.get('time') or data.get('exit_date') or '2026-09-01')
    stats = await db.reassign_closed_trade(from_bot, to_bot, inst, side=side, pnl_near=pnl_near, time_contains=time_contains)
    import json as _json
    rule = {'inst_id': inst, 'pos_side': 'short' if side in ('short', 'sell') else 'long', 'pnl_near': pnl_near, 'exit_date': time_contains[:10], 'to_bot': id_to_label.get(to_bot, 'AI Discretionary 1H')}
    try:
        raw = await db.get_setting('pnl_bot_overrides')
        arr = _json.loads(raw) if raw else []
        if not isinstance(arr, list):
            arr = []
        arr = [r for r in arr if not (r.get('inst_id') == rule['inst_id'] and abs(float(r.get('pnl_near') or 0) - pnl_near) < 1)]
        arr.append(rule)
        await db.set_setting('pnl_bot_overrides', _json.dumps(arr))
    except Exception as e:
        stats['override_err'] = str(e)
    global _bot_stats_cache, _paired_cache, _pnl_cache
    _bot_stats_cache = {'ts': 0.0, 'data': {}}
    _paired_cache = {}
    try:
        if ai_bot and abs(float(stats.get('pnl') or 0)) > 0:
            ai_bot._lifetime_pnl = float(getattr(ai_bot, '_lifetime_pnl', 0) or 0) + float(stats['pnl'])
            ai_bot._lifetime_trades = int(getattr(ai_bot, '_lifetime_trades', 0) or 0) + int(stats.get('moved') or 0)
    except Exception:
        pass
    return {'ok': True, 'from_bot': from_bot, 'to_bot': to_bot, 'rule': rule, **stats}

@app.post('/api/ai/correct-attribution', dependencies=[Depends(require_admin)])
async def ai_correct_attribution(data: dict=None):
    """Move mis-attributed trades (default SOL) off AI PnL onto Impulse."""
    global ai_bot
    data = data or {}
    symbol = str(data.get('symbol') or 'SOL').upper()
    to_bot = str(data.get('to_bot') or 'impulse_strategy')
    if ai_bot and hasattr(ai_bot, 'correct_misattributed'):
        return await ai_bot.correct_misattributed(symbol, to_bot)
    from app.services.ai_strategy import AI_BOT_ID
    inst = f'{symbol}-USDT-SWAP'
    stats = await db.reassign_trades_instrument(AI_BOT_ID, to_bot, inst)
    try:
        from app.services.position_claim import release_open
        await release_open(db, AI_BOT_ID, inst, 'long')
        await release_open(db, AI_BOT_ID, inst, 'short')
    except Exception:
        pass
    return {'ok': True, 'offline': True, **stats, 'symbol': symbol, 'to_bot': to_bot}

@app.post('/api/ai/start', dependencies=[Depends(require_admin)])
async def ai_start(data: dict=None):
    try:
        global ai_bot
        data = data or {}
        if ai_bot and getattr(ai_bot, '_running', False):
            if 'execute' in data:
                try:
                    ai_bot.set_execute(bool(data['execute']))
                except Exception as e:
                    print(f'[AI] set_execute while running: {e}', flush=True)
            elif not _env_demo:
                env_ex = os.getenv('AI_EXECUTE', '1').strip().lower()
                if env_ex not in ('0', 'false', 'no', 'off'):
                    try:
                        ai_bot.set_execute(True)
                    except Exception:
                        pass
            if data.get('capital'):
                try:
                    cap = float(data['capital'])
                    if cap > 0 and hasattr(ai_bot, 'config'):
                        ai_bot.config.capital = cap
                        ai_bot._capital = cap
                except Exception:
                    pass
            st = ai_bot.get_status()
            print(f'[AI] already running — execute={st.get('execute')} capital={st.get('capital')}', flush=True)
            return {'message': 'AI already running (execute refreshed)', **st}
        _demo = _env_demo
        client = None
        if _demo:
            await _ensure_showcase()
            k = _demo_key or _env_key
            s = _demo_secret or _env_secret
            pw = _demo_pass or _env_pass
            if k and s and pw:
                await client_manager.init_client(k, s, pw, True)
            client = client_manager.get_client() if client_manager else None
        else:
            await _load_live_creds_from_db()
            if not (_live_key and _live_secret and _live_pass):
                raise HTTPException(status_code=400, detail='Live-ключи не сохранены. Настройки → Сохранить Live-ключ (без галочки Demo).')
            await client_manager.init_client(_live_key, _live_secret, _live_pass, False)
            client = client_manager.get_client() if client_manager else None
            if client and getattr(client, 'demo', True):
                raise HTTPException(status_code=400, detail='Клиент всё ещё в Demo — Live-ключи не применились')
        if not client:
            raise HTTPException(status_code=400, detail='OKX client not configured. Save API keys in Settings first.')
        if not _demo:
            try:
                portfolio = await client.get_balance()
                if portfolio and portfolio.get('data'):
                    total_eq = float(portfolio['data'][0].get('totalEq', '0') or 0)
                    print(f'[AI] Live balance check: totalEq=${total_eq:.2f}', flush=True)
                    if total_eq < 50:
                        raise HTTPException(status_code=400, detail=f'Insufficient balance for live trading: ${total_eq:.2f} (min $50)')
            except HTTPException:
                raise
            except Exception as e:
                print(f'[AI] Balance check failed: {e}', flush=True)
        if 'execute' in data:
            _exec = bool(data['execute'])
        elif _demo:
            _exec = True
        else:
            env_ex = os.getenv('AI_EXECUTE', '1').strip().lower()
            _exec = env_ex not in ('0', 'false', 'no', 'off')
        capital = float(data.get('capital') or os.getenv('AI_CAPITAL', '10000'))
        if not _demo:
            try:
                portfolio = await client.get_balance()
                if portfolio and portfolio.get('data'):
                    total_eq = float(portfolio['data'][0].get('totalEq', '0') or 0)
                    if total_eq > 0 and capital > total_eq:
                        print(f'[AI] Capital ${capital} > balance ${total_eq:.2f}, clamping', flush=True)
                        capital = total_eq
            except Exception:
                pass

        provider = data.get("provider") or (
            "groq" if os.getenv("GROQ_API_KEY", "").strip()
            else ("openrouter" if os.getenv("OPENROUTER_API_KEY", "").strip() else None)
        )

        cfg = AIConfig(
            capital=capital,
            max_leverage=float(data.get("max_leverage") or 3),
            max_positions=int(data.get("max_positions") or 1),
            risk_per_trade=float(data.get("risk_per_trade") or 0.02),
            poll_interval_sec=int(data.get("poll_interval_sec") or 60),
            provider=provider,
            execute=_exec,
        )
        if data.get("symbols"):
            cfg.symbols = list(data["symbols"])

        # Ensure live_manager has creds if it was initialized empty
        if live_manager and not live_manager.get_client():
            try:
                if not (_live_key and _live_secret and _live_pass):
                    await _load_live_creds_from_db()
                _lk, _ls, _lp = _live_key, _live_secret, _live_pass
                if _lk and _ls and _lp:
                    await live_manager.init_client(_lk, _ls, _lp, False)
                    _lc = live_manager.get_client()
                    if _lc and (not getattr(_lc, 'demo', True)):
                        print('[AI/start] live mirror client restored from DB', flush=True)
                    else:
                        print('[AI/start] live mirror init rejected (demo=true), skipping', flush=True)
            except Exception as e:
                print(f'[AI/start] live mirror DB restore: {e}', flush=True)
        ai_bot = AIStrategy(config=cfg, client_manager=client_manager, db=db, notifier=telegram, live_client_manager=live_manager)
        _wire_ai_live_cb(ai_bot)
        try:
            import json as _json
            _cfgkey = 'ai_config:demo' if _demo else 'ai_config:live'
            _raw_cfg = await db.get_setting(_cfgkey)
            if _raw_cfg:
                _saved_cfg = _json.loads(_raw_cfg)
                for _k in ('capital', 'symbols', 'execute', 'provider'):
                    _saved_cfg.pop(_k, None)
                ai_bot.apply_config_dict(_saved_cfg, keep_execute=True)
                # Explicit request-body values win over saved config
                for _k, _cast in (('max_leverage', float), ('max_positions', int),
                                  ('risk_per_trade', float), ('poll_interval_sec', int)):
                    if data.get(_k):
                        setattr(ai_bot.config, _k, _cast(data[_k]))
                print(f'[AI/start] applied saved AI config ({_cfgkey})', flush=True)
        except Exception as _ce:
            print(f'[AI/start] saved config apply: {_ce}', flush=True)
        ai_bot.start()
        global _positions_cache
        _positions_cache = None
        if db:
            try:
                await db.set_setting('ai_bot_running', '1')
                await db.set_setting('ai_user_stopped', '0')
            except Exception:
                pass
        mode_str = 'DEMO' if _demo else 'LIVE'
        print(f'[AI] Started in {mode_str} mode, capital=${capital:.2f}, execute={_exec}, provider={provider}', flush=True)
        return {'message': f'AI Discretionary started ({mode_str})', **ai_bot.get_status()}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_detail = f'Failed to start AI bot: {str(e)}'
        print(f'[AI] Start error: {error_detail}\n{traceback.format_exc()}', flush=True)
        raise HTTPException(status_code=500, detail=error_detail)

@app.post('/api/ai/stop', dependencies=[Depends(require_admin)])
async def ai_stop():
    global ai_bot
    if ai_bot:
        ai_bot.stop()
    if db:
        try:
            await db.set_setting('ai_bot_running', '0')
            await db.set_setting('ai_user_stopped', '1')  # explicit Stop — skip autostart until Start
        except Exception:
            pass
    return {'message': 'AI stopped', 'running': False}

@app.get('/api/live/status')
async def live_status():
    """Public — returns live mirror connection state + stats (no secrets). Cached 10s."""
    global ai_bot, live_manager
    # Short cache to avoid hammering OKX bills on every 30s frontend poll
    now_s = _time.time()
    if _live_status_cache and (now_s - _live_status_cache.get('ts', 0) < 10):
        return dict(_live_status_cache['data'])
    global ai_bot, live_manager
    lc = None
    try:
        lc = live_manager.get_client() if live_manager else None
    except Exception:
        lc = None
    # Do not auto-heal if user explicitly disconnected
    _mirror_on = True
    try:
        en = await db.get_setting('live_mirror_enabled') if db else None
        if str(en or '').strip().lower() in ('0', 'false', 'no', 'off'):
            _mirror_on = False
    except Exception:
        pass
    if _mirror_on and (lc is None or getattr(lc, 'demo', False) or not getattr(lc, 'has_credentials', lambda: False)()):
        try:
            await _ensure_live_mirror_client()
            lc = live_manager.get_client() if live_manager else None
        except Exception as e:
            print(f'[LIVE] status ensure: {e}', flush=True)
    if not _mirror_on:
        lc = None
    connected = lc is not None and not getattr(lc, 'demo', False) and getattr(lc, 'has_credentials', lambda: False)()
    if not _mirror_on:
        return {
            'connected': False,
            'enabled': False,
            'demo': False,
            'equity': 0,
            'capital': 0,
            'total_pnl': 0,
            'unrealized_pnl': 0,
            'session_pnl': 0,
            'lifetime_trades': 0,
            'lifetime_fees': 0,
            'win_rate': None,
            'open_positions': [],
            'pnl_source': 'disabled',
        }
    if lc is not None and getattr(lc, 'demo', False):
        lc = None
    connected = lc is not None and getattr(lc, 'has_credentials', lambda: False)()
    live_data = {}
    if ai_bot and hasattr(ai_bot, 'get_status'):
        try:
            st = ai_bot.get_status()
            live_data = st.get('live') or {}
        except Exception:
            pass
    equity = float(live_data.get('equity') or 0)
    # Always pull LIVE exchange positions so UI/mirror shows them even if bot
    # memory was empty after redeploy (bot adopts on next tick).
    if connected and lc is not None:
        try:
            if ai_bot and hasattr(ai_bot, '_reconcile_live_from_exchange'):
                try:
                    await ai_bot._reconcile_live_from_exchange()
                    st2 = ai_bot.get_status()
                    live_data = (st2 or {}).get('live') or live_data
                except Exception as _re:
                    print(f'[LIVE] status reconcile: {_re}', flush=True)
            _live_positions_resp = await lc.get_positions('SWAP')
            ex_resp = _live_positions_resp
            ex_list = []
            ex_map = {}
            for ep in (ex_resp or {}).get('data') or []:
                try:
                    sz = abs(float(ep.get('pos') or 0))
                except (TypeError, ValueError):
                    sz = 0.0
                if sz <= 0:
                    continue
                inst = ep.get('instId') or ''
                pos_side = str(ep.get('posSide') or 'net').lower()
                raw = float(ep.get('pos') or 0)
                side = 'short' if (pos_side == 'short' or (pos_side == 'net' and raw < 0)) else 'long'
                key = f'{inst}|{pos_side}'
                ex_map[key] = ep
                ex_map[f'{inst}|{side}'] = ep
                try:
                    upl = float(ep.get('upl') or 0)
                except (TypeError, ValueError):
                    upl = 0.0
                try:
                    entry = float(ep.get('avgPx') or 0)
                except (TypeError, ValueError):
                    entry = 0.0
                try:
                    lev = float(ep.get('lever') or 0)
                except (TypeError, ValueError):
                    lev = 0.0
                ex_list.append({
                    'symbol': inst,
                    'coin': inst.replace('-USDT-SWAP', '').replace('-USD-SWAP', ''),
                    'side': side,
                    'size': sz,
                    'entry_price': entry,
                    'upl': upl,
                    'upl_ratio': float(ep.get('uplRatio') or 0) if ep.get('uplRatio') not in (None, '') else 0.0,
                    'mark_px': ep.get('markPx') or '',
                    'liq_px': ep.get('liqPx') or '',
                    'leverage': lev,
                    'account_mode': 'live',
                    'source': 'exchange',
                })
            mem = list(live_data.get('open_positions') or [])
            if not mem and ex_list:
                live_data['open_positions'] = ex_list
            else:
                for p in mem:
                    inst = p.get('symbol') or p.get('inst_id') or ''
                    side = str(p.get('side') or 'long').lower()
                    ep = ex_map.get(f'{inst}|{side}') or ex_map.get(f'{inst}|net') or ex_map.get(f'{inst}|long') or ex_map.get(f'{inst}|short')
                    if not ep:
                        continue
                    try:
                        p['upl'] = float(ep.get('upl') or 0)
                    except (TypeError, ValueError):
                        p['upl'] = 0.0
                    try:
                        p['upl_ratio'] = float(ep.get('uplRatio') or 0)
                    except (TypeError, ValueError):
                        p['upl_ratio'] = 0.0
                    p['mark_px'] = ep.get('markPx') or ''
                    p['liq_px'] = ep.get('liqPx') or ''
                live_data['open_positions'] = mem
            live_data['exchange_position_count'] = len(ex_list)
            live_data['_ex_upl_sum'] = sum(float(x.get('upl') or 0) for x in ex_list)
        except Exception as e:
            print(f'[LIVE] status positions enrich: {e}', flush=True)
    # Cache balance result to avoid duplicate OKX API call
    _live_balance_data = None
    if connected and lc is not None:
        try:
            portfolio = await lc.get_balance()
            data = (portfolio or {}).get('data') or []
            if data:
                acct = data[0] if isinstance(data[0], dict) else {}
                total = 0.0
                for k in ('totalEq', 'adjEq', 'isoEq'):
                    try:
                        total = max(total, float(acct.get(k) or 0))
                    except (TypeError, ValueError):
                        pass
                if total <= 0:
                    for d in acct.get('details') or []:
                        ccy = str(d.get('ccy') or '').upper()
                        if ccy not in ('USDT', 'USD', 'USDC'):
                            continue
                        for k in ('eq', 'cashBal', 'availBal', 'availEq'):
                            try:
                                v = float(d.get(k) or 0)
                                if v > 0:
                                    total += v
                                    break
                            except (TypeError, ValueError):
                                pass
                if total > 0 or equity <= 0:
                    equity = total
                if ai_bot is not None:
                    try:
                        ai_bot._live_equity = float(equity)
                        import time as _t
                        ai_bot._live_equity_ts = _t.time()
                    except Exception:
                        pass
            _live_balance_data = portfolio
        except Exception as e:
            print(f'[LIVE] status equity: {e}', flush=True)
    # ── LIVE metrics from OKX live_manager only ──
    debug = {'balance_code': None, 'positions_n': 0, 'bills_n': 0, 'bills_err': None}
    positions_out = []
    unrealized = 0.0
    realized = 0.0
    lifetime_trades = 0
    wins = 0
    fees = 0.0
    capital = 0.0
    try:
        if db:
            raw_cap = await db.get_setting('live_mirror_capital')
            if raw_cap not in (None, ''):
                capital = float(raw_cap)
    except Exception:
        capital = 0.0
    if ai_bot is not None and capital <= 0:
        try:
            capital = float(getattr(ai_bot, '_live_capital', 0) or 0)
        except Exception:
            pass

    if connected and lc is not None:
        # Balance / equity (reuse cached result)
        try:
            portfolio = _live_balance_data or await lc.get_balance()
            debug['balance_code'] = str((portfolio or {}).get('code') or '')
            data = (portfolio or {}).get('data') or []
            if data:
                acct = data[0] if isinstance(data[0], dict) else {}
                total = 0.0
                for k in ('totalEq', 'adjEq', 'isoEq'):
                    try:
                        total = max(total, float(acct.get(k) or 0))
                    except (TypeError, ValueError):
                        pass
                if total <= 0:
                    for d in acct.get('details') or []:
                        ccy = str(d.get('ccy') or '').upper()
                        if ccy not in ('USDT', 'USD', 'USDC'):
                            continue
                        for k in ('eq', 'cashBal', 'availBal', 'availEq'):
                            try:
                                v = float(d.get(k) or 0)
                                if v > 0:
                                    total += v
                                    break
                            except (TypeError, ValueError):
                                pass
                if total > 0:
                    equity = total
                if ai_bot is not None:
                    try:
                        ai_bot._live_equity = float(equity)
                        import time as _t
                        ai_bot._live_equity_ts = _t.time()
                    except Exception:
                        pass
        except Exception as e:
            debug['balance_err'] = str(e)
            print(f'[LIVE] balance: {e}', flush=True)

        # Open positions + UPL (reuse cached)
        try:
            ex_resp = _live_positions_resp or await lc.get_positions('SWAP')
            for ep in (ex_resp or {}).get('data') or []:
                try:
                    sz = abs(float(ep.get('pos') or 0))
                except (TypeError, ValueError):
                    sz = 0.0
                if sz <= 0:
                    continue
                try:
                    u = float(ep.get('upl') or 0)
                except (TypeError, ValueError):
                    u = 0.0
                unrealized += u
                inst = ep.get('instId') or ''
                pos_side = str(ep.get('posSide') or 'net').lower()
                try:
                    raw = float(ep.get('pos') or 0)
                except (TypeError, ValueError):
                    raw = 0.0
                side = 'short' if (pos_side == 'short' or (pos_side == 'net' and raw < 0)) else 'long'
                try:
                    entry = float(ep.get('avgPx') or 0)
                except (TypeError, ValueError):
                    entry = 0.0
                try:
                    lev = float(ep.get('lever') or 0)
                except (TypeError, ValueError):
                    lev = 0.0
                try:
                    mark = float(ep.get('markPx') or 0)
                except (TypeError, ValueError):
                    mark = 0.0
                positions_out.append({
                    'symbol': inst,
                    'coin': inst.replace('-USDT-SWAP', '').replace('-USD-SWAP', ''),
                    'side': side,
                    'size': sz,
                    'entry_price': entry,
                    'upl': u,
                    'unrealized_pnl': u,
                    'upl_ratio': float(ep.get('uplRatio') or 0) if ep.get('uplRatio') not in (None, '') else 0.0,
                    'mark_px': mark or (ep.get('markPx') or ''),
                    'liq_px': ep.get('liqPx') or '',
                    'leverage': lev,
                    'account_mode': 'live',
                    'source': 'exchange',
                })
            debug['positions_n'] = len(positions_out)
            if ai_bot and hasattr(ai_bot, '_reconcile_live_from_exchange'):
                try:
                    await ai_bot._reconcile_live_from_exchange()
                except Exception as _re:
                    print(f'[LIVE] reconcile: {_re}', flush=True)
        except Exception as e:
            debug['positions_err'] = str(e)
            print(f'[LIVE] positions: {e}', flush=True)

        # Realized from bills (type=2) — tolerant parse
        try:
            after = ''
            seen = set()
            for _page in range(6):
                params_try = [
                    {'instType': 'SWAP', 'type': '2', 'limit': '100'},
                    {'instType': 'SWAP', 'limit': '100'},
                ]
                data = []
                last_err = None
                for pr in params_try:
                    if after:
                        pr = dict(pr)
                        pr['after'] = after
                    try:
                        resp = await lc._request('GET', '/api/v5/account/bills', params=pr)
                    except Exception as e:
                        last_err = str(e)
                        continue
                    if not isinstance(resp, dict):
                        continue
                    if resp.get('error'):
                        last_err = resp.get('message')
                        continue
                    if str(resp.get('code', '0')) not in ('0', ''):
                        last_err = resp.get('msg') or resp.get('message')
                        continue
                    data = resp.get('data') or []
                    break
                if not data:
                    if last_err:
                        debug['bills_err'] = last_err
                    break
                for b in data:
                    bid = str(b.get('billId') or '')
                    if bid and bid in seen:
                        continue
                    if bid:
                        seen.add(bid)
                    btype = str(b.get('type') or '')
                    if btype and btype not in ('2', '1'):
                        continue
                    try:
                        bp = float(b.get('pnl') if b.get('pnl') not in (None, '') else 0)
                    except (TypeError, ValueError):
                        bp = 0.0
                    try:
                        bf = abs(float(b.get('fee') or 0))
                    except (TypeError, ValueError):
                        bf = 0.0
                    fees += bf
                    # only count rows that moved PnL (closes / funding already filtered by type)
                    if abs(bp) < 1e-12:
                        continue
                    realized += bp
                    lifetime_trades += 1
                    if bp > 0:
                        wins += 1
                debug['bills_n'] = len(seen)
                after = str(data[-1].get('billId') or '')
                if len(data) < 100 or not after:
                    break
        except Exception as e:
            debug['bills_err'] = str(e)
            print(f'[LIVE] bills: {e}', flush=True)

        if ai_bot is not None:
            try:
                ai_bot._live_lifetime_pnl = realized
                ai_bot._live_lifetime_trades = lifetime_trades
                ai_bot._live_lifetime_wins = wins
                ai_bot._live_lifetime_fees = fees
                if capital > 0:
                    ai_bot._live_capital = capital
            except Exception:
                pass
        print(
            f'[LIVE] status: connected={connected} equity={equity:.2f} capital={capital:.2f} '
            f'realized={realized:.2f} upl={unrealized:.2f} pos={len(positions_out)} bills={debug.get("bills_n")} '
            f'bal_code={debug.get("balance_code")} err={debug.get("bills_err") or debug.get("positions_err") or ""}',
            flush=True,
        )

    total_pnl = round(float(realized) + float(unrealized), 2)
    win_rate = round(100.0 * wins / lifetime_trades, 1) if lifetime_trades else None
    _result = {
        'connected': connected,
        'demo': False,
        'equity': round(float(equity or 0), 2),
        'capital': round(float(capital or 0), 2),
        'total_pnl': total_pnl,
        'unrealized_pnl': round(float(unrealized), 2),
        'session_pnl': round(float(realized), 2),
        'lifetime_trades': int(lifetime_trades),
        'lifetime_fees': round(float(fees), 2),
        'win_rate': win_rate,
        'open_positions': positions_out,
        'pnl_source': 'okx_live',
        'enabled': True,
        'debug': debug if not connected else {k: debug[k] for k in debug if debug.get(k) not in (None, 0, '')},
    }
    _live_status_cache['ts'] = _time.time()
    _live_status_cache['data'] = dict(_result)
    return _result


@app.post('/api/live/connect', dependencies=[Depends(require_admin)])
async def live_connect(request: Request, data: dict = Body(default=None)):
    """Connect LIVE mirror. Body: {key, secret, passphrase, capital, confirm:'LIVE'}."""
    global ai_bot, live_manager, _live_key, _live_secret, _live_pass
    _live_status_cache.clear()
    # Robust body parse (Form/empty/proxy edge cases)
    if not data or not isinstance(data, dict):
        try:
            data = await request.json()
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    key = (data.get('key') or data.get('apiKey') or data.get('api_key') or '').strip()
    secret = (data.get('secret') or data.get('secretKey') or data.get('secret_key') or '').strip()
    passphrase = (data.get('passphrase') or data.get('passPhrase') or data.get('pass') or '').strip()
    if not key or not secret or not passphrase:
        raise HTTPException(
            status_code=400,
            detail='Нужны key, secret, passphrase. Проверьте поля формы.',
        )
    confirm = str(data.get('confirm') or '').strip().upper()
    if confirm not in ('LIVE', 'YES', '1', 'TRUE'):
        # Accept missing confirm from UI that already is admin-only, but log
        if not confirm:
            confirm = 'LIVE'
            print('[LIVE] connect: confirm missing — accepted for admin UI', flush=True)
        else:
            raise HTTPException(status_code=400, detail='Подтвердите подключение: confirm=LIVE')
    try:
        capital = float(data.get('capital') or data.get('amount') or data.get('budget') or 0)
    except (TypeError, ValueError):
        capital = 0.0
    if capital < 10:
        raise HTTPException(
            status_code=400,
            detail='Укажите капитал зеркала (мин. $10)',
        )
    if live_manager is None:
        live_manager = OKXClientManager.new_instance()
    try:
        await live_manager.init_client(key, secret, passphrase, False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'OKX init failed: {e}')
    lc = live_manager.get_client() if live_manager else None
    if not lc or not getattr(lc, 'has_credentials', lambda: False)():
        raise HTTPException(status_code=400, detail='Live client init failed')
    if getattr(lc, 'demo', True):
        raise HTTPException(status_code=400, detail='Клиент в Demo — проверьте, что ключи LIVE (не Simulated)')
    total_eq = 0.0
    bal_err = None
    try:
        portfolio = await lc.get_balance()
        if isinstance(portfolio, dict) and portfolio.get('error'):
            bal_err = str(portfolio.get('message') or portfolio)
        data_rows = (portfolio or {}).get('data') if isinstance(portfolio, dict) else None
        if data_rows:
            acct = data_rows[0] if isinstance(data_rows[0], dict) else {}
            for k in ('totalEq', 'adjEq', 'isoEq'):
                try:
                    total_eq = max(total_eq, float(acct.get(k) or 0))
                except (TypeError, ValueError):
                    pass
        elif isinstance(portfolio, dict) and str(portfolio.get('code', '0')) not in ('0', ''):
            bal_err = portfolio.get('msg') or portfolio.get('message') or str(portfolio.get('code'))
    except Exception as e:
        bal_err = str(e)
        print(f'[LIVE] balance check: {e}', flush=True)
    if bal_err and total_eq <= 0:
        # Auth likely wrong — fail clearly
        raise HTTPException(
            status_code=400,
            detail=f'Не удалось получить баланс LIVE: {bal_err}. Проверьте ключи (Trade permission, не demo).',
        )
    # Soft warn if balance low — still allow connect if capital requested
    if total_eq > 0 and total_eq < 10:
        print(f'[LIVE] low balance ${total_eq:.2f} — connect allowed with capital={capital}', flush=True)
    if total_eq > 0 and capital > total_eq:
        capital = total_eq
        print(f'[LIVE] capital clamped to equity ${capital:.2f}', flush=True)

    _live_key, _live_secret, _live_pass = key, secret, passphrase
    if db:
        try:
            await db.set_setting('live_mirror_capital', str(round(capital, 2)))
            await db.set_setting('live_mirror_enabled', '1')
            try:
                await _save_live_creds(key, secret, passphrase)
                # Also keep plaintext as fallback for when TOKEN_ENCRYPTION_KEY
                # is not set (ephemeral Fernet key breaks decryption after restart)
                await db.set_setting('live_mirror_key', key)
                await db.set_setting('live_mirror_secret', secret)
                await db.set_setting('live_mirror_pass', passphrase)
            except Exception as e:
                print(f'[LIVE] encrypt save: {e}', flush=True)
            print('[LIVE] creds + enabled=1 persisted', flush=True)
        except Exception as e:
            # Mirror is already live in-memory; log DB issue but do not block connect
            print(f'[LIVE] persist warning (mirror still connected in-memory): {e}', flush=True)

    if ai_bot:
        try:
            ai_bot.live_client_manager = live_manager
            ai_bot._live_capital = float(capital)
            if total_eq > 0:
                ai_bot._live_equity = float(total_eq)
                import time as _t
                ai_bot._live_equity_ts = _t.time()
            print(f'[LIVE] bound to ai_bot capital={capital} equity={total_eq}', flush=True)
        except Exception as e:
            print(f'[LIVE] bind ai_bot: {e}', flush=True)

    # Verify ensure path will accept
    try:
        ok = await _ensure_live_mirror_client()
        print(f'[LIVE] post-connect ensure={ok}', flush=True)
    except Exception as e:
        print(f'[LIVE] post-connect ensure err: {e}', flush=True)

    print(f'[LIVE] mirror CONNECTED capital={capital} equity={total_eq}', flush=True)
    return {
        'message': 'LIVE mirror подключён',
        'connected': True,
        'enabled': True,
        'capital': round(float(capital), 2),
        'equity': round(float(total_eq), 2),
    }


@app.post('/api/live/disconnect', dependencies=[Depends(require_admin)])
async def live_disconnect():
    """Disconnect LIVE mirror. Stops mirroring until explicit /api/live/connect.

    Credentials stay in DB for easier re-connect, but live_mirror_enabled=0
    prevents heal/status from auto-reconnecting.
    """
    global ai_bot, live_manager, _live_key, _live_secret, _live_pass
    _live_status_cache.clear()
    if db:
        try:
            await db.set_setting('live_mirror_enabled', '0')
        except Exception as e:
            print(f'[LIVE] disable flag: {e}', flush=True)
    try:
        live_manager = OKXClientManager.new_instance()
    except Exception:
        pass
    # Clear in-memory keys so status cannot briefly re-init without flag check
    # (keys remain in DB for next connect form / ensure when enabled=1)
    if ai_bot:
        try:
            # Keep manager object bound (empty) so reconnect can init_client on same ref
            ai_bot.live_client_manager = live_manager
            try:
                ai_bot._live_positions.clear()
            except Exception:
                pass
            ai_bot._live_equity = 0.0
            ai_bot._live_equity_ts = 0.0
            ai_bot._live_capital = 0.0
        except Exception as e:
            print(f'[LIVE] unbind: {e}', flush=True)
    print('[LIVE] mirror DISCONNECTED (enabled=0)', flush=True)
    return {
        'message': 'LIVE mirror отключён — для повторного подключения укажите ключи и капитал',
        'connected': False,
        'enabled': False,
    }

@app.get('/api/live/trades')
async def live_trades():
    """Public — recent LIVE mirror trades."""
    if not db:
        return {'trades': []}
    try:
        live_bid = 'ai_strategy_live'
        rows = await db.get_trades(bot_id=live_bid, limit=40)
        trades = []
        for r in reversed(rows or []):
            trades.append({'time': r.get('timestamp') or r.get('created_at') or '', 'side': r.get('side'), 'symbol': r.get('inst_id'), 'size': r.get('sz') or r.get('size'), 'pnl': r.get('pnl'), 'entry_price': r.get('px'), 'state': r.get('state'), 'reason': r.get('state') or 'db', 'coin': (r.get('inst_id') or '').replace('-USDT-SWAP', ''), 'account_mode': 'live'})
        return {'trades': trades}
    except Exception as e:
        print(f'[LIVE] trades: {e}', flush=True)
        return {'trades': []}

@app.post('/api/ai/execute', dependencies=[Depends(require_admin)])
async def ai_execute(data: dict=None):
    """Toggle AI auto-trading (execute=on/off) at runtime. Optionally resets
    stale lifetime PnL (reset=1) — AI ran in signal mode so old data is bogus."""
    global ai_bot
    d = data or {}
    if not ai_bot:
        return {'ok': False, 'message': 'AI bot not running'}
    enabled = bool(d.get('execute'))
    if d.get('reset'):
        ai_bot.reset_lifetime_pnl()
    ai_bot.set_execute(enabled)
    st = ai_bot.get_status()
    return {'ok': True, 'execute': st.get('execute'), 'total_pnl': st.get('total_pnl'), 'lifetime_pnl': st.get('lifetime_pnl'), 'message': f'AI auto-trade {('ON' if enabled else 'OFF')}' + (' (lifetime PnL reset to 0)' if d.get('reset') else '')}

@app.get('/api/ai/config', dependencies=[Depends(require_admin)])
async def ai_get_config():
    """Admin-only. DEMO is the editable workspace; LIVE is promoted snapshot."""
    import json
    demo_raw, live_raw = await db.get_settings_batch(['ai_config:demo', 'ai_config:live'])
    try:
        demo = json.loads(demo_raw) if demo_raw else None
    except Exception:
        demo = None
    try:
        live = json.loads(live_raw) if live_raw else None
    except Exception:
        live = None
    runtime = {}
    try:
        if ai_bot:
            runtime = ai_bot.export_config_dict()
    except Exception:
        runtime = {}
    if not demo:
        demo = runtime or {}
    return {'editable': bool(_env_demo), 'account_mode': 'demo' if _env_demo else 'live', 'demo': demo, 'live': live, 'runtime': runtime, 'policy': {'edit_only_in_demo': True, 'live_is_promoted_snapshot': True, 'admin_only': True}}

@app.put('/api/ai/config', dependencies=[Depends(require_admin)])
async def ai_put_config(request: Request, data: dict=Body(default=None)):
    """Save AI settings — only allowed in DEMO mode (admin)."""
    import json
    if not _env_demo:
        raise HTTPException(status_code=400, detail='Настройки AI редактируются только в DEMO. Переключитесь в Demo, сохраните, затем «В LIVE».')
    data = data or {}
    cfg = data.get('config') if isinstance(data.get('config'), dict) else data
    if not isinstance(cfg, dict) or not cfg:
        raise HTTPException(status_code=400, detail='Empty config')
    for k in ('editable', 'account_mode', 'demo', 'live', 'runtime', 'policy', 'config'):
        cfg.pop(k, None)
    await db.set_setting('ai_config:demo', json.dumps(cfg, ensure_ascii=False))
    if ai_bot:
        try:
            ai_bot.apply_config_dict(cfg, keep_execute=True)
        except Exception as e:
            print(f'[ai/config] apply demo: {e}', flush=True)
    try:
        await write_audit(request, 'ai.config.save_demo', detail='demo settings updated')
    except Exception:
        pass
    return {'ok': True, 'saved': 'demo', 'account_mode': 'demo'}

@app.post('/api/ai/config/promote', dependencies=[Depends(require_admin)])
async def ai_promote_config(request: Request):
    """Copy DEMO settings → LIVE snapshot. LIVE trading will use this snapshot."""
    import json
    if not _env_demo:
        pass
    demo_raw = await db.get_setting('ai_config:demo')
    if not demo_raw and ai_bot:
        demo_raw = json.dumps(ai_bot.export_config_dict(), ensure_ascii=False)
        await db.set_setting('ai_config:demo', demo_raw)
    if not demo_raw:
        raise HTTPException(status_code=400, detail='Нет DEMO-настроек для трансляции в LIVE')
    await db.set_setting('ai_config:live', demo_raw)
    if not _env_demo and ai_bot:
        try:
            ai_bot.apply_config_dict(json.loads(demo_raw), keep_execute=True)
        except Exception as e:
            print(f'[ai/config] apply live after promote: {e}', flush=True)
    try:
        await write_audit(request, 'ai.config.promote', detail='demo -> live')
    except Exception:
        pass
    return {'ok': True, 'promoted': True, 'message': 'Настройки DEMO скопированы в LIVE. В Live-режиме бот использует этот снимок.'}

@app.get('/api/ai/logs', dependencies=[Depends(require_admin)])
async def ai_logs(limit: int=200, event: str=None):
    """Export AI decision/trade logs for prompt tuning.

    Sources: in-memory decision log (running bot) + analysis.jsonl tail (bot=ai).
    """
    global ai_bot
    limit = max(1, min(int(limit or 200), 2000))
    mem = []
    if ai_bot:
        mem = list(getattr(ai_bot, '_decision_log', []) or [])[-limit:]
        if event:
            mem = [d for d in mem if (d.get('event') or d.get('action')) == event or d.get('action') == event]
    file_rows = []
    try:
        from app.services.analysis_logger import DEFAULT_PATH
        path = Path(DEFAULT_PATH)
        if path.exists():
            lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
            for line in lines[-(limit * 3):]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get('bot') != 'ai':
                    continue
                if event and row.get('event') != event:
                    continue
                file_rows.append(row)
            file_rows = file_rows[-limit:]
    except Exception as e:
        print(f'[ai/logs] file read: {e}', flush=True)
    return {'memory': mem, 'file': file_rows, 'memory_n': len(mem), 'file_n': len(file_rows), 'execute': bool(ai_bot and ai_bot._execute_enabled()) if ai_bot else False, 'running': bool(ai_bot and getattr(ai_bot, '_running', False))}

@app.get('/api/ai/logs/download', dependencies=[Depends(require_admin)])
async def ai_logs_download(limit: int=500):
    """Download AI analysis lines as JSONL attachment."""
    from app.services.analysis_logger import DEFAULT_PATH
    path = Path(DEFAULT_PATH)
    out_lines = []
    if path.exists():
        for line in path.read_text(encoding='utf-8', errors='ignore').splitlines()[-(int(limit) * 5):]:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get('bot') == 'ai':
                out_lines.append(json.dumps(row, ensure_ascii=False))
    body = '\n'.join(out_lines[-int(limit):]) + ('\n' if out_lines else '')
    return Response(content=body, media_type='application/x-ndjson', headers={'Content-Disposition': 'attachment; filename=ai_decisions.jsonl'})

@app.post('/api/ai/decide', dependencies=[Depends(require_admin)])
async def ai_decide_once():
    global ai_bot
    if not ai_bot or not getattr(ai_bot, '_running', False):
        raise HTTPException(status_code=400, detail='AI bot not running — start first')
    client = client_manager.get_client() if client_manager else None
    if not client:
        raise HTTPException(status_code=400, detail='OKX client not ready')
    await ai_bot._fetch_indicators(client)
    try:
        ai_bot._refresh_adaptive()
    except Exception:
        pass
    snap = ai_bot._snapshot()
    from app.services.ai_agent import call_llm
    decision = await call_llm(snap, provider=ai_bot._provider())
    enriched = ai_bot._enrich_decision(decision, snap)
    ai_bot._last_decision = enriched
    return {'snapshot': {'indicators': snap.get('indicators'), 'open_positions': snap.get('open_positions'), 'equity': snap.get('equity')}, 'decision': enriched}

def _ensure_sm_tracker(*, execute: bool | None=None, start: bool=False):
    """Create global Smart Money tracker on first use (browse/copy without manual Start)."""
    global sm_tracker
    from app.services.legacy_stubs import SmartMoneyTracker, TrackerConfig, OKXCopyAPI
    if sm_tracker is None:
        okx = OKXCopyAPI(api_key=_env_key or os.getenv('OKX_API_KEY', ''), secret_key=_env_secret or os.getenv('OKX_SECRET_KEY', '') or os.getenv('OKX_SECRET', ''), passphrase=_env_pass or os.getenv('OKX_PASSPHRASE', ''), demo=_env_demo)
        cfg = TrackerConfig(sort_type='pnl_ratio', execute=bool(execute) if execute is not None else False)
        sm_tracker = SmartMoneyTracker(config=cfg, client_manager=client_manager, db=db, notifier=None, okx_api=okx)
    else:
        if execute is True:
            try:
                sm_tracker.config.execute = True
            except Exception:
                pass
        try:
            if sm_tracker.okx_api and (not getattr(sm_tracker.okx_api, 'api_key', None)):
                sm_tracker.okx_api.api_key = _env_key
                sm_tracker.okx_api.secret_key = _env_secret
                sm_tracker.okx_api.passphrase = _env_pass
        except Exception:
            pass
    if start and (not getattr(sm_tracker, '_running', False)):
        sm_tracker.start()
    return sm_tracker

def _sm_okx_api() -> 'OKXCopyAPI':
    """Build a fresh OKX Copy Trading API client (no tracker thread, no
    background work). Copy trading on OKX is a one-shot REST call, so it
    does NOT need the Smart Money tracker thread that crashed the process."""
    from app.services.legacy_stubs import OKXCopyAPI
    return OKXCopyAPI(api_key=_env_key or os.getenv('OKX_API_KEY', ''), secret_key=_env_secret or os.getenv('OKX_SECRET_KEY', '') or os.getenv('OKX_SECRET', ''), passphrase=_env_pass or os.getenv('OKX_PASSPHRASE', ''), demo=_env_demo)

@app.post('/api/positions/reclaim', dependencies=[Depends(require_admin)])
async def positions_reclaim(data: dict=Body(default=None)):
    """Force-bind an exchange position to a strategy (fixes wrong MAC/AI badge).

    Body: { "symbol": "SOL", "side": "short", "to_bot": "ai_scale_strategy" }
    to_bot: ai_scale_strategy | ai_strategy | impulse_strategy | ...
    """
    global ai_bot, ai_scale_bot, _positions_cache
    data = data or {}
    sym = (data.get('symbol') or data.get('coin') or '').upper().replace('-USDT-SWAP', '')
    side = (data.get('side') or 'long').lower()
    if side not in ('long', 'short'):
        raise HTTPException(status_code=400, detail='side must be long|short')
    to_bot = (data.get('to_bot') or 'ai_scale_strategy').strip()
    if not sym:
        raise HTTPException(status_code=400, detail='symbol required')
    inst = f'{sym}-USDT-SWAP'
    from app.services.position_claim import claim_open, release_open, norm_side
    side_n = norm_side(side) if 'norm_side' in dir() else side
    known = ['ai_strategy', 'ai_scale_strategy', 'impulse_strategy', 'validation_strategy', 'rotation_strategy', 'momentum_strategy', 'smart_money', 'vwap_rev_strategy']
    released = []
    for bid in known:
        if bid == to_bot:
            continue
        try:
            await release_open(db, bid, inst, side)
            released.append(bid)
        except Exception:
            pass
    sz, entry = (float(data.get('size') or 0), float(data.get('entry') or 0))
    client = client_manager.get_client()
    if client and (sz <= 0 or entry <= 0):
        try:
            resp = await client.get_positions('SWAP', inst_id=inst)
            for p in resp.get('data') or []:
                if (p.get('instId') or '') != inst:
                    continue
                ps = (p.get('posSide') or 'net').lower()
                if side == 'short' and ps not in ('short', 'net'):
                    continue
                if side == 'long' and ps not in ('long', 'net'):
                    continue
                sz = abs(float(p.get('pos') or 0))
                entry = float(p.get('avgPx') or 0)
                break
        except Exception as e:
            print(f'[reclaim] positions: {e}', flush=True)
    if sz <= 0 or entry <= 0:
        raise HTTPException(status_code=400, detail='Could not resolve size/entry from exchange')
    ok = await claim_open(db, to_bot, inst, side, sz, entry)
    injected = None
    try:
        if to_bot == 'ai_scale_strategy' and ai_scale_bot:
            from app.services.ai_strategy import AIPosition
            from datetime import datetime, timezone
            stop_pct, take_pct = (0.03, 0.06)
            if side == 'long':
                stop, take = (entry * (1 - stop_pct), entry * (1 + take_pct))
            else:
                stop, take = (entry * (1 + stop_pct), entry * (1 - take_pct))
            if ai_bot and sym in getattr(ai_bot, '_positions', {}):
                ai_bot._positions.pop(sym, None)
            ai_scale_bot._positions[sym] = AIPosition(coin=sym, inst_id=inst, side=side, size=sz, entry_price=entry, stop_price=stop, take_price=take, leverage=float(getattr(ai_scale_bot.config, 'max_leverage', 3) or 3), opened_at=datetime.now(timezone.utc).isoformat(), peak_price=entry, signal_id=0)
            injected = 'ai_scale'
        elif to_bot == 'ai_strategy' and ai_bot:
            from app.services.ai_strategy import AIPosition
            from datetime import datetime, timezone
            stop_pct, take_pct = (0.03, 0.06)
            if side == 'long':
                stop, take = (entry * (1 - stop_pct), entry * (1 + take_pct))
            else:
                stop, take = (entry * (1 + stop_pct), entry * (1 - take_pct))
            if ai_scale_bot and sym in getattr(ai_scale_bot, '_positions', {}):
                ai_scale_bot._positions.pop(sym, None)
            ai_bot._positions[sym] = AIPosition(coin=sym, inst_id=inst, side=side, size=sz, entry_price=entry, stop_price=stop, take_price=take, leverage=float(getattr(ai_bot.config, 'max_leverage', 3) or 3), opened_at=datetime.now(timezone.utc).isoformat(), peak_price=entry, signal_id=0)
            injected = 'ai'
    except Exception as e:
        print(f'[reclaim] inject: {e}', flush=True)
    _positions_cache = None
    return {'ok': bool(ok), 'inst_id': inst, 'side': side, 'to_bot': to_bot, 'size': sz, 'entry': entry, 'released': released, 'injected': injected}

@app.get('/api/health/positions-claims', dependencies=[Depends(require_admin)])
async def health_positions_claims():
    """Compare OKX open SWAP positions vs DB strategy claims."""
    from app.services.position_claim import norm_side
    client = client_manager.get_client()
    exchange = []
    if client:
        try:
            res = await client.get_positions('SWAP')
            for p in res.get('data') or []:
                try:
                    sz = abs(float(p.get('pos') or 0))
                except (TypeError, ValueError):
                    sz = 0
                if sz <= 0:
                    continue
                exchange.append({'inst_id': p.get('instId'), 'side': norm_side(p.get('posSide') or 'net'), 'size': sz, 'upl': float(p.get('upl') or 0)})
        except Exception as e:
            return {'ok': False, 'error': str(e)}
    claims = []
    try:
        rows = await db.get_all_positions() if hasattr(db, 'get_all_positions') else []
        for r in rows or []:
            claims.append({'bot_id': r.get('bot_id'), 'inst_id': r.get('inst_id'), 'side': r.get('side'), 'size': r.get('size')})
    except Exception as e:
        claims = [{'error': str(e)}]
    cl_keys = set()
    for c in claims:
        if c.get('inst_id'):
            cl_keys.add((c.get('inst_id'), norm_side(c.get('side') or 'long')))
            cl_keys.add((c.get('inst_id'), 'net'))
    only_exchange = [x for x in exchange if (x['inst_id'], x['side']) not in cl_keys]
    only_claims = [c for c in claims if c.get('inst_id') and (c.get('inst_id'), norm_side(c.get('side') or 'long')) not in {(e['inst_id'], e['side']) for e in exchange}]
    return {'ok': len(only_exchange) == 0, 'exchange_count': len(exchange), 'claims_count': len([c for c in claims if c.get('inst_id')]), 'only_on_exchange': only_exchange, 'only_in_db_claims': only_claims, 'exchange': exchange, 'claims': claims}

@app.get('/api/health')
async def health(request: Request):
    """Liveness + connection + bot flags. Heavy PnL only with ?diag=1."""
    client = client_manager.get_client()
    connected = client is not None
    uptime = None
    if _STARTED_AT is not None:
        uptime = round(_time.time() - _STARTED_AT, 1)

    def _bot_flag(bot) -> bool:
        return bool(bot is not None and getattr(bot, '_running', False))
    diag = {}
    try:
        import resource
        diag['rss_mb'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        pass
    try:
        diag['sm_running'] = bool(getattr(sm_tracker, '_running', False))
        diag['sm_tracked'] = len(getattr(sm_tracker, '_traders', {}) or {})
        diag['sm_error'] = (getattr(sm_tracker, '_last_error', '') or '')[:120]
    except Exception:
        pass
    _env_okx_demo_default = os.getenv('OKX_DEMO', 'true').lower() in ('1', 'true', 'yes', 'on')
    try:
        _ui_demo = bool(_env_demo)
    except Exception:
        _ui_demo = _env_okx_demo_default
    want_diag = False
    try:
        if request is not None:
            want_diag = str(request.query_params.get('diag') or '') in ('1', 'true', 'yes')
    except Exception:
        want_diag = False
    if want_diag:
        try:
            diag['pnl_epoch'] = await get_pnl_epoch()
            _pr = await get_pnl()
            diag['pnl_total'] = _pr.get('total')
            diag['pnl_1d'] = _pr.get('1d')
            diag['pnl_week'] = _pr.get('week')
            diag['pnl_per_bot'] = _pr.get('per_bot')
            diag['pnl_source'] = _pr.get('source')
        except Exception as e:
            diag['pnl_err'] = str(e)[:200]
        _live_on = False
    _live_en = None
    try:
        _lc = live_manager.get_client() if live_manager else None
        _live_on = bool(_lc and not getattr(_lc, 'demo', True) and getattr(_lc, 'has_credentials', lambda: False)())
        if db:
            _live_en = await db.get_setting('live_mirror_enabled')
    except Exception:
        pass
    return {'status': 'ok', 'connected': connected, 'demo': locals().get('_ui_demo', _env_demo), 'version': os.environ.get('RENDER_GIT_COMMIT', '')[:12], 'uptime_sec': uptime, 'bots': {'rotation': _bot_flag(rotation), 'impulse': _bot_flag(impulse), 'validation': _bot_flag(validation), 'ai': _bot_flag(ai_bot), 'ai_scale': _bot_flag(ai_scale_bot), 'scalp': False, 'vwap_rev': _bot_flag(vwap_rev_bot), 'smart_money': bool(getattr(sm_tracker, '_running', False))}, 'live_mirror': {'connected': _live_on, 'enabled': (str(_live_en or '').strip() not in ('0', 'false', 'no', 'off'))}, 'auth': 'jwt', 'risk': risk_get_status().to_dict(), 'sm_diag': diag}

@app.get('/api/risk/status')
async def risk_status():
    """Public-ish status for UI badges (no secrets)."""
    daily = None
    try:
        from app.services import risk_guard as _rg
    except Exception:
        pass
    st = risk_get_status(daily_pnl=None)
    return st.to_dict()

@app.post('/api/risk/kill', dependencies=[Depends(require_admin)])
async def risk_kill(request: Request, data: dict=None):
    """Enable/disable runtime kill switch (blocks new entries, not closes)."""
    data = data or {}
    enabled = bool(data.get('enabled', True))
    set_kill_switch(enabled)
    await write_audit(request, 'risk.kill_switch', detail=f'enabled={enabled}')
    return {'ok': True, **risk_get_status().to_dict()}

async def _load_live_creds_from_db() -> None:
    """Restore LIVE keys: encrypted first, then plaintext live_mirror_* fallback."""
    global _live_key, _live_secret, _live_pass
    try:
        k, s, p = await db.get_settings_batch(
            ['okx_live_key_enc', 'okx_live_secret_enc', 'okx_live_pass_enc']
        )
        if k and s and p:
            dk, ds, dp = decrypt_str(k) or '', decrypt_str(s) or '', decrypt_str(p) or ''
            if dk and ds and dp:
                _live_key, _live_secret, _live_pass = dk, ds, dp
                return
    except Exception as e:
        print(f'[creds] load live enc: {e}', flush=True)
    # Fallback: keys saved by /api/live/connect as live_mirror_*
    try:
        k2 = await db.get_setting('live_mirror_key')
        s2 = await db.get_setting('live_mirror_secret')
        p2 = await db.get_setting('live_mirror_pass')
        if k2 and s2 and p2:
            _live_key, _live_secret, _live_pass = str(k2), str(s2), str(p2)
            print('[creds] load live: using live_mirror_* plaintext fallback', flush=True)
    except Exception as e:
        print(f'[creds] load live mirror: {e}', flush=True)


def _wire_ai_live_cb(bot) -> None:
    """Attach live-mirror ensure callback so strategy can rebind after restart."""
    if bot is None:
        return
    try:
        bot._live_ensure_cb = _ensure_live_mirror_client
        if live_manager is not None and getattr(bot, 'live_client_manager', None) is None:
            bot.live_client_manager = live_manager
    except Exception as e:
        print(f'[LIVE] wire cb: {e}', flush=True)


async def _ensure_live_mirror_client() -> bool:
    """Init/rebind live_manager from DB/env and attach to ai_bot. Survives restart.

    Respects live_mirror_enabled=0 after explicit Disconnect (do not auto-heal).
    """
    global live_manager, ai_bot, _live_key, _live_secret, _live_pass
    try:
        en = await db.get_setting('live_mirror_enabled') if db else None
        # Explicit disconnect → stay offline until /api/live/connect
        if str(en or '').strip().lower() in ('0', 'false', 'no', 'off'):
            print(f'[LIVE] ensure: disabled by user (enabled={en})', flush=True)
            _startup_log = getattr(_ensure_live_mirror_client, '_slog_ref', None)
            return False
    except Exception as e:
        print(f'[LIVE] ensure enabled flag: {e}', flush=True)
    try:
        await _load_live_creds_from_db()
    except Exception as e:
        print(f'[LIVE] ensure load: {e}', flush=True)
    key = _live_key or ''
    secret = _live_secret or ''
    passphrase = _live_pass or ''
    if not (key and secret and passphrase):
        try:
            key = (await db.get_setting('live_mirror_key')) or key
            secret = (await db.get_setting('live_mirror_secret')) or secret
            passphrase = (await db.get_setting('live_mirror_pass')) or passphrase
        except Exception:
            pass
    if not (key and secret and passphrase):
        print(f'[LIVE] ensure: no credentials (enc_key={bool(_live_key)} db_key={bool(key)})', flush=True)
        return False
    if live_manager is None:
        live_manager = OKXClientManager.new_instance()
        print('[LIVE] ensure: created new live_manager', flush=True)
    try:
        await live_manager.init_client(key, secret, passphrase, False)
    except Exception as e:
        print(f'[LIVE] ensure init_client: {e}', flush=True)
        return False
    lc = live_manager.get_client() if live_manager else None
    if not lc or getattr(lc, 'demo', True):
        print(f'[LIVE] ensure: client missing or demo=true (lc={lc is not None} demo={getattr(lc, "demo", "?")})', flush=True)
        return False
    if not getattr(lc, 'has_credentials', lambda: False)():
        print('[LIVE] ensure: no credentials on client', flush=True)
        return False
    if ai_bot is not None:
        try:
            ai_bot.live_client_manager = live_manager
            try:
                cap = await db.get_setting('live_mirror_capital')
                if cap and float(cap) > 0:
                    ai_bot._live_capital = float(cap)
            except Exception:
                pass
        except Exception as e:
            print(f'[LIVE] ensure attach bot: {e}', flush=True)
    print('[LIVE] ensure: mirror client ready', flush=True)
    return True

async def _save_live_creds(key: str, secret: str, passphrase: str) -> None:
    global _live_key, _live_secret, _live_pass
    _live_key, _live_secret, _live_pass = (key, secret, passphrase)
    await db.set_setting('okx_live_key_enc', encrypt_str(key))
    await db.set_setting('okx_live_secret_enc', encrypt_str(secret))
    await db.set_setting('okx_live_pass_enc', encrypt_str(passphrase))

def _active_owner_creds() -> tuple:
    """Keys for current owner mode: demo showcase or live."""
    if _env_demo:
        return (_demo_key or _env_key, _demo_secret or _env_secret, _demo_pass or _env_pass, True)
    if _live_key and _live_secret and _live_pass:
        return (_live_key, _live_secret, _live_pass, False)
    return (_env_key, _env_secret, _env_pass, False)

@app.get('/api/credentials/status', dependencies=[Depends(require_admin)])
async def credentials_status():
    """Showcase DEMO (env) vs owner LIVE keys status."""
    await _load_live_creds_from_db()
    showcase = bool((_demo_key or _env_key) and (_demo_secret or _env_secret) and (_demo_pass or _env_pass))
    live_ok = bool(_live_key and _live_secret and _live_pass)
    return {'configured': showcase or live_ok, 'showcase_configured': showcase, 'live_configured': live_ok, 'demo': _env_demo, 'mode': 'demo' if _env_demo else 'live', 'note': 'Витрина DEMO всегда из env OKX. Live — ваши ключи, переключение в Настройках.'}

@app.post('/api/credentials/test', dependencies=[Depends(require_admin)])
async def credentials_test(data: dict=Body(default=None)):
    data = data or {}
    key = (data.get('apiKey') or data.get('api_key') or data.get('key') or '').strip()
    secret = (data.get('secretKey') or data.get('secret_key') or data.get('secret') or '').strip()
    passphrase = (data.get('passphrase') or data.get('passPhrase') or data.get('password') or '').strip()
    demo = bool(data.get('demo', True))
    any_filled = bool(key or secret or passphrase)
    if any_filled and (not (key and secret and passphrase)):
        return {'success': False, 'message': 'Заполните все три поля: API Key, Secret Key и Passphrase'}
    if not (key and secret and passphrase):
        k, s, p, is_demo = _active_owner_creds()
        key, secret, passphrase = (k or '', s or '', p or '')
        if data.get('demo') is None:
            demo = is_demo
    if not (key and secret and passphrase):
        return {'success': False, 'message': 'Укажите API Key, Secret и Passphrase'}
    try:
        probe = OKXClientManager.new_instance()
        result = await probe.test_connection(key, secret, passphrase, demo)
        if result.get('error'):
            return {'success': False, 'message': result.get('message', 'Connection failed'), 'demo': demo}
        return {'success': True, 'message': 'Connected successfully', 'demo': demo}
    except Exception as e:
        return {'success': False, 'message': str(e)}

@app.post('/api/credentials/init', dependencies=[Depends(require_admin)])
async def credentials_init(request: Request, data: dict=Body(default=None)):
    """Save owner credentials.

    demo=true  → optional override of showcase keys (defaults stay env).
    demo=false → save LIVE keys (encrypted) and switch platform client to LIVE.
    """
    global _env_key, _env_secret, _env_pass, _env_demo
    global _demo_key, _demo_secret, _demo_pass
    data = data or {}
    key = (data.get('apiKey') or data.get('api_key') or data.get('key') or '').strip()
    secret = (data.get('secretKey') or data.get('secret_key') or data.get('secret') or '').strip()
    passphrase = (data.get('passphrase') or data.get('passPhrase') or data.get('password') or '').strip()
    demo = bool(data.get('demo', True))
    missing = []
    if not key:
        missing.append('API Key')
    if not secret:
        missing.append('Secret Key')
    if not passphrase:
        missing.append('Passphrase')
    if missing:
        raise HTTPException(status_code=400, detail=f'Не хватает: {', '.join(missing)}. Заполните все три поля перед сохранением.')
    probe = OKXClientManager.new_instance()
    result = await probe.test_connection(key, secret, passphrase, demo)
    if result.get('error'):
        msg = result.get('message') or 'Connection failed'
        raise HTTPException(status_code=400, detail=f'OKX: {msg}')
    if demo:
        _demo_key, _demo_secret, _demo_pass = (key, secret, passphrase)
        _env_key, _env_secret, _env_pass = (key, secret, passphrase)
        _env_demo = True
        await client_manager.init_client(key, secret, passphrase, True)
        await write_audit(request, 'credentials.init', detail='demo=showcase')
        return {'message': 'Showcase DEMO keys set', 'demo': True, 'mode': 'demo'}
    await _save_live_creds(key, secret, passphrase)
    try:
        await client_manager.init_client(key, secret, passphrase, False)
        _env_demo = False
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'Live client init failed: {e}')
    await write_audit(request, 'credentials.init', detail='live=owner')
    return {'message': 'Live-ключи сохранены. DEMO-витрина не изменена. Переключитесь в Live в шапке.', 'demo': False, 'mode': 'live', 'live_configured': True}

@app.get('/api/mode', dependencies=[Depends(require_admin)])
async def get_trading_mode():
    await _load_live_creds_from_db()
    return {'demo': _env_demo, 'okx_demo': _env_demo, 'live': not _env_demo, 'showcase_configured': bool((_demo_key or _env_key) and (_demo_secret or _env_secret)), 'live_configured': bool(_live_key and _live_secret and _live_pass), 'mode': 'demo' if _env_demo else 'live'}

@app.post('/api/mode', dependencies=[Depends(require_admin)])
async def set_trading_mode(request: Request, data: dict=Body(default=None)):
    """Switch owner client between showcase DEMO and personal LIVE.

    DEMO uses env/showcase keys (always for observers).
    LIVE requires previously saved live keys; confirm must be "LIVE".
    """
    global _env_demo
    data = data or {}
    raw_demo = data.get('demo', True)
    if isinstance(raw_demo, str):
        demo = raw_demo.strip().lower() in ('1', 'true', 'yes', 'demo')
    else:
        demo = bool(raw_demo)
    await _load_live_creds_from_db()
    if not demo:
        confirm = str(data.get('confirm') or '').strip()
        if confirm != 'LIVE':
            raise HTTPException(status_code=400, detail='Switching to LIVE requires confirm: "LIVE"')
        if not (_live_key and _live_secret and _live_pass):
            raise HTTPException(status_code=400, detail='Сначала сохраните Live API-ключи OKX в настройках')
        key, secret, passphrase = (_live_key, _live_secret, _live_pass)
        probe = OKXClientManager.new_instance()
        check = await probe.test_connection(key, secret, passphrase, False)
        if check.get('error'):
            raise HTTPException(status_code=400, detail=f'OKX Live: {check.get('message') or 'connection failed'}')
    else:
        key = _demo_key or _env_key
        secret = _demo_secret or _env_secret
        passphrase = _demo_pass or _env_pass
        if not (key and secret and passphrase):
            raise HTTPException(status_code=400, detail='Showcase DEMO keys (env OKX) not configured')
    prev = _env_demo
    try:
        await client_manager.init_client(key, secret, passphrase, demo)
        _env_demo = demo
        global ai_scale_bot
        if not demo and ai_scale_bot and getattr(ai_scale_bot, '_running', False):
            try:
                ai_scale_bot.stop()
                print('[mode] AI Scale-In stopped (LIVE — DEMO-only bot)', flush=True)
            except Exception as e:
                print(f'[mode] stop Scale-In: {e}', flush=True)
        if demo:
            await _ensure_showcase()
    except Exception as e:
        _env_demo = prev
        raise HTTPException(status_code=400, detail=f'Reconnect failed: {e}')
    try:
        await db.set_setting('trading_mode', 'demo' if demo else 'live')
    except Exception:
        pass
    try:
        await write_audit(request, 'mode.switch', detail=f'{('DEMO' if prev else 'LIVE')} -> {('DEMO' if demo else 'LIVE')}')
    except Exception:
        pass
    _invalidate_account_caches()
    try:

        async def _warm_mode():
            try:
                await asyncio.sleep(0.05)
                client = client_manager.get_client() if client_manager else None
                if client:
                    try:
                        await client.get_balance()
                    except Exception:
                        pass
                    try:
                        await client.get_positions('SWAP')
                    except Exception:
                        pass
                try:
                    await sync_exchange_close_trades()
                except Exception:
                    pass
            except Exception as e:
                print(f'[mode] warm: {e}', flush=True)
        asyncio.create_task(_warm_mode())
    except Exception:
        pass
    try:
        import json as _json
        if ai_bot:
            key = 'ai_config:demo' if demo else 'ai_config:live'
            raw = await db.get_setting(key)
            if raw:
                ai_bot.apply_config_dict(_json.loads(raw), keep_execute=True)
            elif demo:
                await db.set_setting('ai_config:demo', _json.dumps(ai_bot.export_config_dict(), ensure_ascii=False))
    except Exception as e:
        print(f'[mode.switch] ai config apply: {e}', flush=True)
    try:
        if ai_bot is not None:
            try:
                ai_bot._positions.clear()
            except Exception:
                pass
            client = client_manager.get_client()
            if client and hasattr(ai_bot, '_restore_open_positions'):
                await ai_bot._restore_open_positions(client)
    except Exception as e:
        print(f'[mode.switch] AI position resync: {e}', flush=True)
    return {'ok': True, 'demo': _env_demo, 'live': not _env_demo, 'mode': 'demo' if _env_demo else 'live', 'live_configured': bool(_live_key and _live_secret and _live_pass), 'account_mode': _account_mode()}

@app.get('/api/audit', dependencies=[Depends(require_admin)])
async def get_audit(limit: int=100):
    rows = await db.list_audit(limit=limit)
    return {'items': rows}

@app.get('/api/portfolio')
async def get_portfolio(request: Request):
    global _portfolio_cache, _portfolio_cache_ts
    now_s = _time.time()
    _view_mode = _account_mode()
    try:
        _c, _m, _src = await resolve_view_client(request)
        if _m:
            _view_mode = _m
    except Exception:
        pass
    if isinstance(_portfolio_cache, dict) and _portfolio_cache.get('mode') == _view_mode and (now_s - _portfolio_cache_ts < _POS_CACHE_TTL):
        out = dict(_portfolio_cache.get('data') or _portfolio_cache)
        out['account_mode'] = _view_mode
        return out
    result = await _okx_call_view(request, lambda c: c.get_balance())
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    data = result.get('data', [])
    if not data:
        return {'totalEqUsd': 0, 'details': []}
    acct = data[0] if isinstance(data, list) else data
    total_eq = float(acct.get('totalEq', 0))
    details = []
    for d in acct.get('details', []):
        details.append({'ccy': d.get('ccy'), 'eq': float(d.get('eq', 0)), 'eqUsd': float(d.get('eqUsd', 0)), 'availBal': float(d.get('availBal', 0)), 'frozenBal': float(d.get('frozenBal', 0))})
    out = {'totalEqUsd': total_eq, 'details': details}
    _portfolio_cache = {'mode': _view_mode, 'data': out}
    _portfolio_cache_ts = _time.time()
    if isinstance(out, dict):
        out = dict(out)
        out['account_mode'] = _view_mode
    return out

def _tag_position_bot(inst_id: str, pos_side: str, *, db_pos_map: dict | None=None) -> str:
    """Determine which bot owns an OKX position.

    Priority: in-memory _positions → trade logs → DB positions table → empty.
    """
    norm_side = pos_side.lower() if pos_side else ''

    def _match(bot) -> bool:
        if not (bot and getattr(bot, '_positions', None)):
            return False
        for coin, pos in bot._positions.items():
            if pos.inst_id == inst_id and (pos.side == norm_side or norm_side in ('', 'net') or (norm_side in ('long', 'short') and pos.side in ('long', 'short'))):
                if norm_side in ('long', 'short') and pos.side in ('long', 'short'):
                    if pos.side != norm_side:
                        continue
                return True
        return False
    if _match(rotation):
        return 'Momentum'
    if _match(impulse):
        return 'Impulse 1D'
    if _match(validation):
        return 'MACD+Donchian Validation'
    if _match(ai_scale_bot):
        return 'AI Scale-In 1H'
    if _match(ai_bot):
        if _match(ai_scale_bot):
            return 'AI Scale-In 1H'
        return 'AI Discretionary 1H'
    if rotation and rotation._trade_log:
        for t in reversed(rotation._trade_log):
            sym = t.get('symbol', '') or t.get('inst_id', '')
            if sym == inst_id and t.get('reason') == 'open':
                return 'Momentum'
    if impulse and impulse._trade_log:
        for t in reversed(impulse._trade_log):
            sym = t.get('symbol', '') or t.get('inst_id', '')
            if sym == inst_id and t.get('reason') == 'open':
                return 'Impulse 1D'
    if validation and validation._trade_log:
        for t in reversed(validation._trade_log):
            sym = t.get('symbol', '') or t.get('inst_id', '')
            if sym == inst_id and t.get('reason') == 'open':
                return 'MACD+Donchian Validation'
    if ai_bot and ai_bot._trade_log:
        for t in reversed(ai_bot._trade_log):
            sym = t.get('symbol', '') or t.get('inst_id', '')
            if sym == inst_id and t.get('reason') == 'open':
                return 'AI Discretionary 1H'
    if vwap_rev_bot and vwap_rev_bot._trade_log:
        for t in reversed(vwap_rev_bot._trade_log):
            sym = t.get('symbol', '') or t.get('inst_id', '')
            if sym == inst_id and t.get('reason') == 'open':
                return 'VWAP Mean Reversion'
    if db_pos_map is not None:
        bot_id = db_pos_map.get((inst_id, norm_side))
        if not bot_id and norm_side == 'net':
            bot_id = db_pos_map.get((inst_id, 'long')) or db_pos_map.get((inst_id, 'short'))
        if not bot_id:
            for (iid, _), bid in db_pos_map.items():
                if iid == inst_id:
                    bot_id = bid
                    break
        if bot_id:
            name = _db_bot_name(bot_id)
            if name:
                return name
    return ''

def _tag_trade_bot(trade: dict, *, db_pos_map: dict | None=None) -> str:
    """Tag a paired trade with bot name. Works for both open and closed trades."""
    inst_id = trade.get('inst_id', '') or trade.get('symbol', '')
    pos_side = trade.get('pos_side', '')
    explicit = str(trade.get('bot') or trade.get('bot_label') or '').strip()
    if explicit in ('AI Scale-In 1H', 'AI Discretionary 1H', 'Momentum', 'Impulse 1D', 'MACD+Donchian Validation', 'Order Book Scalp', 'Умные деньги', 'VWAP Mean Reversion'):
        return explicit
    cl = str(trade.get('cl_ord_id') or trade.get('clOrdId') or '').lower()
    if cl.startswith('ais'):
        return 'AI Scale-In 1H'
    if cl.startswith('ai') and (not cl.startswith('ais')):
        return 'AI Discretionary 1H'
    try:
        pnl = float(trade.get('pnl') if trade.get('pnl') is not None else 1e+18)
    except (TypeError, ValueError):
        pnl = 1e+18
    if 'ETH' in str(inst_id).upper() and abs(pnl - -414.06) < 8.0:
        return 'AI Scale-In 1H'
    by_id = _db_bot_name(trade.get('bot_id', '') or '')
    if by_id:
        return by_id
    if trade.get('reason') == 'open':
        return _tag_position_bot(inst_id, pos_side, db_pos_map=db_pos_map)
    ord_id = str(trade.get('ord_id') or trade.get('close_ord_id') or '').strip()
    if ord_id:
        for bot_label, log in (('AI Scale-In 1H', getattr(ai_scale_bot, '_trade_log', None) if ai_scale_bot else None), ('Momentum', getattr(rotation, '_trade_log', None) if rotation else None), ('Impulse 1D', getattr(impulse, '_trade_log', None) if impulse else None), ('MACD+Donchian Validation', getattr(validation, '_trade_log', None) if validation else None), ('AI Discretionary 1H', getattr(ai_bot, '_trade_log', None) if ai_bot else None), ('VWAP Mean Reversion', getattr(vwap_rev_bot, '_trade_log', None) if vwap_rev_bot else None)):
            if not log:
                continue
            for tlog in log:
                if str(tlog.get('ord_id', '') or '').strip() == ord_id:
                    return bot_label
    entry_time = trade.get('entry_time', '')
    for bot_label, bot in (('AI Scale-In 1H', ai_scale_bot), ('Momentum', rotation), ('Impulse 1D', impulse), ('MACD+Donchian Validation', validation), ('AI Discretionary 1H', ai_bot), ('VWAP Mean Reversion', vwap_rev_bot)):
        log = getattr(bot, '_trade_log', None) if bot else None
        if not log:
            continue
        for tlog in log:
            if tlog.get('time', '') == entry_time and (tlog.get('symbol', '') or tlog.get('inst_id', '')) == inst_id:
                return bot_label
    return _db_bot_name(trade.get('bot_id', ''))

def _db_bot_name(bot_id: str) -> str:
    """Map DB bot_id -> UI bot name. Handles per-user suffixed ids."""
    if not bot_id:
        return ''
    base = str(bot_id).split(':')[0]
    if base in ('momentum_strategy', 'rotation_strategy', MOM_BOT_ID, ROT_BOT_ID):
        return 'Momentum'
    if base in ('impulse_strategy', IMP_BOT_ID):
        return 'Impulse 1D'
    if base == VAL_BOT_ID:
        return 'MACD+Donchian Validation'
    if base == AI_BOT_ID:
        return 'AI Discretionary 1H'
    if base == AI_SCALE_BOT_ID or base == 'ai_scale_strategy':
        return 'AI Scale-In 1H'
    if base == SCALP_BOT_ID:
        return 'Order Book Scalp'
    if base == VWAP_BOT_ID:
        return 'VWAP Mean Reversion'
    if base in ('smart_money', 'smart_money_mirror', 'sm_mirror'):
        return 'Умные деньги'
    if base in ('Momentum', 'Impulse 1D', 'MACD+Donchian Validation', 'AI Discretionary 1H', 'AI Scale-In 1H', 'Order Book Scalp', 'Умные деньги'):
        return base
    return ''

async def get_pnl_epoch() -> str:
    """Always 2026-09-01 — recount start mandated by product."""
    try:
        await db.set_setting('pnl_epoch', PNL_EPOCH_ISO)
    except Exception:
        pass
    return PNL_EPOCH_ISO

def _trade_after_epoch(tr: dict, epoch: str) -> bool:
    """Filter closed history by epoch; always keep live open rows."""
    if not epoch:
        return True
    reason = (tr.get('reason') or '').lower()
    if reason in ('open', 'add') or tr.get('pnl') is None:
        return True
    ts = tr.get('exit_time') or tr.get('time') or tr.get('entry_time') or tr.get('timestamp') or ''
    if not ts:
        return True
    try:
        return str(ts)[:19] >= str(epoch)[:19]
    except Exception:
        return True

async def _orphan_sweep_loop():
    """Periodically close exchange positions not owned by any strategy."""
    global _positions_cache
    import asyncio as _asyncio
    await _asyncio.sleep(45)
    while True:
        try:
            client = client_manager.get_client()
            if client and orphan_close_enabled():
                mem = set()
                for bot in (rotation, impulse, validation, ai_bot, vwap_rev_bot, sm_tracker):
                    if not bot or not getattr(bot, '_positions', None):
                        continue
                    for pos in bot._positions.values():
                        mem.add((getattr(pos, 'inst_id', None) or '', getattr(pos, 'side', 'long')))
                closed = await sweep_exchange_orphans(client, db, mem)
                if closed:
                    print(f'[orphan-sweep] closed {len(closed)}: {closed}', flush=True)
                    _positions_cache = None
        except Exception as e:
            print(f'[orphan-sweep] error: {e}', flush=True)
        await _asyncio.sleep(120)

@app.post('/api/positions/sweep-orphans', dependencies=[Depends(require_admin)])
async def sweep_orphans():
    """Close exchange positions not claimed by any strategy (anti-orphan)."""
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail='API not configured')
    mem = set()
    for bot in (rotation, impulse, validation, ai_bot, vwap_rev_bot, sm_tracker):
        if not bot or not getattr(bot, '_positions', None):
            continue
        for pos in bot._positions.values():
            mem.add((pos.inst_id, getattr(pos, 'side', 'long')))
    closed = await sweep_exchange_orphans(client, db, mem)
    global _positions_cache
    _positions_cache = None
    return {'closed': closed, 'n': len(closed)}

@app.get('/api/positions')
async def get_positions(request: Request, inst_type: str='SWAP'):
    global _positions_cache, _positions_cache_ts, _POS_RECLAIM_TS
    now_s = _time.time()
    _view_mode = _account_mode()
    try:
        _c, _m, _src = await resolve_view_client(request)
        if _m:
            _view_mode = _m
    except Exception:
        pass
    if isinstance(_positions_cache, dict) and _positions_cache.get('mode') == _view_mode and (now_s - _positions_cache_ts < _POS_CACHE_TTL):
        out = dict(_positions_cache.get('data') or {})
        out['account_mode'] = _view_mode
        return out
    do_heavy_reclaim = now_s - float(_POS_RECLAIM_TS or 0) >= float(_POS_RECLAIM_TTL or 90)
    result = await _okx_call_view(request, lambda c: c.get_positions(inst_type))
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    db_pos_map = {}
    try:
        db_rows = await db.get_all_positions()
        for row in db_rows:
            db_pos_map[row.get('inst_id', ''), row.get('side', '')] = row.get('bot_id', '')
    except Exception:
        pass
    try:
        import json
        _bot_ids = (ROT_BOT_ID, IMP_BOT_ID, VAL_BOT_ID, AI_BOT_ID, AI_SCALE_BOT_ID, 'smart_money')
        _pos_keys = [f'open_positions:{bid}' for bid in _bot_ids]
        _all_settings = await db.get_settings_batch(_pos_keys)
        for bid, raw in zip(_bot_ids, _all_settings):
            if not raw:
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                for row in data or []:
                    iid = row.get('inst_id') or ''
                    side = (row.get('side') or 'long').lower()
                    if iid and (iid, side) not in db_pos_map:
                        db_pos_map[iid, side] = bid
            except Exception:
                continue
    except Exception:
        pass
    tagged = []

    async def _inject_bot_memory(bot_label: str, inst_id: str, side: str, sz: float, entry: float):
        """Rehydrate strategy in-memory book so UI botMap + management work after deploy."""
        global rotation, impulse, validation, ai_bot
        coin = inst_id.replace('-USDT-SWAP', '').replace('-USD-SWAP', '')
        entry = float(entry or 0)
        sz = float(sz or 0)
        if entry <= 0 or sz <= 0 or (not coin):
            return
        stop = entry * 0.985 if side == 'long' else entry * 1.015
        now_iso = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        try:
            if bot_label == 'Momentum' and rotation:
                from app.services.legacy_stubs import RotPosition
                if coin not in (getattr(rotation, '_positions', None) or {}):
                    rotation._positions[coin] = RotPosition(symbol=inst_id, coin=coin, inst_id=inst_id, side=side, size=sz, size_original=sz, entry_price=entry, stop_price=stop, peak_price=entry, opened_at=now_iso, atr=entry * 0.015, atr_hourly=entry * 0.015, leverage=float(getattr(getattr(rotation, 'config', None), 'max_leverage', 3) or 3))
                    print(f'[positions] injected {coin} → Momentum', flush=True)
            elif bot_label == 'Impulse 1D' and impulse:
                pos_map = getattr(impulse, '_positions', None)
                if pos_map is not None and coin not in pos_map:
                    P = getattr(impulse, 'Position', None) or getattr(impulse, 'ImpPosition', None)
                    if P is None:

                        class _P:
                            pass
                        p = _P()
                        p.symbol = inst_id
                        p.coin = coin
                        p.inst_id = inst_id
                        p.side = side
                        p.size = sz
                        p.size_original = sz
                        p.entry_price = entry
                        p.stop_price = stop
                        p.peak_price = entry
                        p.opened_at = now_iso
                        p.atr = entry * 0.015
                        pos_map[coin] = p
                    else:
                        try:
                            pos_map[coin] = P(symbol=inst_id, coin=coin, inst_id=inst_id, side=side, size=sz, entry_price=entry, stop_price=stop)
                        except TypeError:
                            p = P.__new__(P)
                            for k, v in dict(symbol=inst_id, coin=coin, inst_id=inst_id, side=side, size=sz, entry_price=entry, stop_price=stop).items():
                                try:
                                    setattr(p, k, v)
                                except Exception:
                                    pass
                            pos_map[coin] = p
                    print(f'[positions] injected {coin} → Impulse', flush=True)
            elif bot_label.startswith('MACD') and validation:
                pos_map = getattr(validation, '_positions', None)
                if pos_map is not None and coin not in pos_map:
                    from app.services.legacy_stubs import RotPosition
                    pos_map[coin] = RotPosition(symbol=inst_id, coin=coin, inst_id=inst_id, side=side, size=sz, size_original=sz, entry_price=entry, stop_price=stop, peak_price=entry, opened_at=now_iso, atr=entry * 0.015, atr_hourly=entry * 0.015, leverage=3.0)
                    print(f'[positions] injected {coin} → Validation', flush=True)
            elif ('Scale-In' in bot_label or bot_label in ('AI Scale-In 1H', 'SCL')) and ai_scale_bot:
                pos_map = getattr(ai_scale_bot, '_positions', None)
                if pos_map is not None and coin not in pos_map:
                    try:
                        from app.services.ai_strategy import AIPosition
                        pos_map[coin] = AIPosition(coin=coin, inst_id=inst_id, side=side, size=sz, entry_price=entry, stop_price=stop, take_price=entry * (1.06 if side == 'long' else 0.94), leverage=3.0, opened_at=now_iso)
                    except Exception:
                        pass
                    print(f'[positions] injected {coin} → Scale-In', flush=True)
            elif bot_label in ('AI Discretionary 1H', 'AI') and ai_bot:
                pos_map = getattr(ai_bot, '_positions', None)
                if pos_map is not None and coin not in pos_map:
                    try:
                        pos_map[coin] = AIPosition(coin=coin, inst_id=inst_id, side=side, size=sz, entry_price=entry, stop_price=stop, take_price=entry * (1.06 if side == 'long' else 0.94), leverage=3.0, opened_at=now_iso)
                    except Exception:
                        pass
                    print(f'[positions] injected {coin} → AI Discretionary', flush=True)
        except Exception as e:
            print(f'[positions] inject {bot_label}: {e}', flush=True)
    for p in result.get('data', []):
        inst = p.get('instId', '') or ''
        pos_side = (p.get('posSide', 'net') or 'net').lower()
        try:
            pos_raw = float(p.get('pos') or 0)
        except (TypeError, ValueError):
            pos_raw = 0.0
        if pos_side == 'short' or pos_raw < 0:
            side_n = 'short'
        else:
            side_n = 'long'
        sz = abs(pos_raw)
        try:
            entry = float(p.get('avgPx') or 0)
        except (TypeError, ValueError):
            entry = 0.0
        bot_name = ''
        try:
            coin0 = (inst or '').replace('-USDT-SWAP', '').replace('-USD-SWAP', '')
            if ai_bot and coin0 in (getattr(ai_bot, '_positions', None) or {}):
                bot_name = 'AI Discretionary 1H'
            elif ai_scale_bot and coin0 in (getattr(ai_scale_bot, '_positions', None) or {}):
                bot_name = 'AI Discretionary 1H'
        except Exception:
            pass
        if not bot_name:
            bot_name = _tag_position_bot(inst, pos_side, db_pos_map=db_pos_map)
        if not bot_name:
            bot_name = _tag_position_bot(inst, side_n, db_pos_map=db_pos_map)
        _retired = {'MACD+Donchian Validation', 'Validation', 'Momentum', 'Impulse 1D', 'Impulse', 'VWAP Mean Reversion'}
        if AI_ONLY_MODE and bot_name in _retired:
            print(f'[positions] strip retired badge {inst} → was {bot_name}', flush=True)
            bot_name = ''
            try:
                for dead in (IMP_BOT_ID, VAL_BOT_ID, ROT_BOT_ID, 'impulse_strategy', 'validation_strategy', 'rotation_strategy', 'momentum_strategy'):
                    try:
                        await release_open(db, dead, inst, side_n)
                    except Exception:
                        pass
            except Exception:
                pass
        if not bot_name and inst:
            try:
                last_bot = await db.last_bot_for_instrument(inst)
                last_name = _db_bot_name(last_bot) if last_bot else ''
                from app.services.trade_attribution import is_scale_bot, is_discretionary_bot
                allowed = last_bot in (AI_BOT_ID, AI_SCALE_BOT_ID, 'ai_strategy', 'ai_scale_strategy') or is_scale_bot(last_name or last_bot or '') or is_discretionary_bot(last_name or last_bot or '')
                if last_bot and allowed and (sz > 0) and (entry > 0):
                    await claim_open(db, last_bot, inst, side_n, sz, entry)
                    db_pos_map[inst, side_n] = last_bot
                    db_pos_map[inst, 'net'] = last_bot
                    bot_name = last_name or _tag_position_bot(inst, side_n, db_pos_map=db_pos_map)
                    if bot_name:
                        print(f'[positions] reclaimed {inst} {side_n} → {last_bot}', flush=True)
                elif last_bot and (not allowed):
                    print(f'[positions] skip last_bot={last_bot} (retired) for {inst}', flush=True)
            except Exception as e:
                print(f'[positions] reclaim {inst}: {e}', flush=True)
        if not bot_name and inst and do_heavy_reclaim:
            try:
                fills = await _fetch_okx_fills(limit=100)
                prefix_map = {'ais': (AI_SCALE_BOT_ID, 'AI Scale-In 1H'), 'ai': (AI_BOT_ID, 'AI Discretionary 1H'), 'rot': (ROT_BOT_ID, 'Momentum'), 'imp': (IMP_BOT_ID, 'Impulse 1D'), 'val': (VAL_BOT_ID, 'MACD+Donchian Validation')}
                for f in fills or []:
                    if (f.get('instId') or '') != inst:
                        continue
                    cid = str(f.get('clOrdId') or '').lower()
                    ordered = list(prefix_map.items())
                    for pref, (bid, label) in ordered:
                        if not cid.startswith(pref):
                            continue
                        if AI_ONLY_MODE and pref in ('imp', 'val', 'rot'):
                            continue
                        if sz > 0 and entry > 0:
                            await claim_open(db, bid, inst, side_n, sz, entry)
                            db_pos_map[inst, side_n] = bid
                            db_pos_map[inst, 'net'] = bid
                            bot_name = label
                            print(f'[positions] reclaimed via clOrdId {cid[:20]} → {label}', flush=True)
                        break
                    if bot_name:
                        break
            except Exception as e:
                print(f'[positions] fill-tag {inst}: {e}', flush=True)
        if not bot_name and inst and do_heavy_reclaim:
            try:
                client = client_manager.get_client()
                if client and hasattr(client, 'get_order_list'):
                    pass
                if client:
                    for meth in ('get_orders_pending', 'get_order_list', 'orders_pending'):
                        fn = getattr(client, meth, None)
                        if not callable(fn):
                            continue
                        try:
                            ores = await fn(inst_type='SWAP')
                        except TypeError:
                            try:
                                ores = await fn('SWAP')
                            except Exception:
                                continue
                        except Exception:
                            continue
                        for o in ores.get('data') or [] if isinstance(ores, dict) else []:
                            if (o.get('instId') or '') != inst:
                                continue
                            cid = str(o.get('clOrdId') or '').lower()
                            if cid.startswith('rot'):
                                await claim_open(db, ROT_BOT_ID, inst, side_n, sz, entry)
                                bot_name = 'Momentum'
                                print(f'[positions] reclaimed via pending order {cid[:20]} → Momentum', flush=True)
                                break
                            if cid.startswith('imp'):
                                await claim_open(db, IMP_BOT_ID, inst, side_n, sz, entry)
                                bot_name = 'Impulse 1D'
                                break
                        if bot_name:
                            break
            except Exception as e:
                print(f'[positions] pending-tag {inst}: {e}', flush=True)
        if not bot_name and inst and (sz > 0) and (entry > 0):
            try:
                coin = inst.replace('-USDT-SWAP', '').replace('-USD-SWAP', '')
                candidates = []
                try:
                    from app.services.legacy_stubs import COINS as _RC
                except Exception:
                    _RC = ['BTC', 'ETH', 'BNB', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'AVAX', 'LTC']
                if rotation and getattr(rotation, '_running', False):
                    univ = list(getattr(getattr(rotation, 'config', None), 'symbols', None) or _RC)
                    if coin in univ:
                        candidates.append((ROT_BOT_ID, 'Momentum', rotation))
                if not AI_ONLY_MODE and impulse and getattr(impulse, '_running', False):
                    univ = list(getattr(getattr(impulse, 'config', None), 'symbols', None) or _RC)
                    if coin in univ:
                        candidates.append((IMP_BOT_ID, 'Impulse 1D', impulse))
                if not AI_ONLY_MODE and validation and getattr(validation, '_running', False):
                    univ = list(getattr(getattr(validation, 'config', None), 'symbols', None) or _RC)
                    if coin in univ:
                        candidates.append((VAL_BOT_ID, 'MACD+Donchian Validation', validation))
                if ai_bot and getattr(ai_bot, '_running', False):
                    univ = list(getattr(getattr(ai_bot, 'config', None), 'symbols', None) or ['BTC', 'ETH', 'SOL', 'OKB', 'DOGE', 'XRP', 'BCH', 'DAI'])
                    if coin in univ:
                        candidates.append((AI_BOT_ID, 'AI Discretionary 1H', ai_bot))
                if ai_scale_bot and getattr(ai_scale_bot, '_running', False):
                    univ = list(getattr(getattr(ai_scale_bot, 'config', None), 'symbols', None) or ['BTC', 'ETH', 'SOL', 'OKB', 'DOGE', 'XRP', 'BCH', 'DAI'])
                    if coin in univ:
                        candidates.append((AI_SCALE_BOT_ID, 'AI Scale-In 1H', ai_scale_bot))
                if len(candidates) == 1:
                    bid, label, _bot = candidates[0]
                    other = False
                    try:
                        other = await db.other_bot_owns_position_any(bid, inst, side_n)
                    except Exception:
                        other = False
                    if not other:
                        await claim_open(db, bid, inst, side_n, sz, entry)
                        db_pos_map[inst, side_n] = bid
                        bot_name = label
                        print(f'[positions] last-resort claim {inst} → {label} (unique running bot)', flush=True)
            except Exception as e:
                print(f'[positions] last-resort {inst}: {e}', flush=True)
        coin0 = (inst or '').replace('-USDT-SWAP', '').replace('-USD-SWAP', '')
        scale_has = False
        disc_has = False
        try:
            scale_has = bool(ai_scale_bot and coin0 in (getattr(ai_scale_bot, '_positions', None) or {}))
            disc_has = bool(ai_bot and coin0 in (getattr(ai_bot, '_positions', None) or {}))
        except Exception:
            pass
        try:
            if ai_scale_bot and hasattr(ai_scale_bot, '_entry_fill_owner'):
                client = client_manager.get_client()
                if client:
                    owner = await ai_scale_bot._entry_fill_owner(client, inst)
                    if owner == 'ais':
                        scale_has = True
                        bot_name = 'AI Scale-In 1H'
                    elif owner == 'ai' and (not scale_has):
                        bot_name = 'AI Discretionary 1H'
        except Exception:
            pass
        if scale_has or disc_has or bot_name in ('AI Scale-In 1H', 'AI Discretionary 1H', ''):
            if scale_has or disc_has or bot_name.startswith('AI'):
                bot_name = 'AI Discretionary 1H'
                try:
                    if ai_scale_bot and getattr(ai_scale_bot, '_positions', None) and (coin0 in ai_scale_bot._positions):
                        ai_scale_bot._positions.pop(coin0, None)
                except Exception:
                    pass
                try:
                    if sz > 0 and entry > 0:
                        await claim_open(db, AI_BOT_ID, inst, side_n, sz, entry)
                        await release_open(db, AI_SCALE_BOT_ID, inst, side_n)
                except Exception as e:
                    print(f'[positions] AI claim: {e}', flush=True)
        if bot_name and inst and (sz > 0):
            await _inject_bot_memory(bot_name, inst, side_n, sz, entry)
        p['bot'] = bot_name
        p['_side_norm'] = side_n
        tagged.append(p)
    out = {'positions': tagged}
    _positions_cache = {'mode': _view_mode, 'data': out}
    _positions_cache_ts = _time.time()
    if isinstance(out, dict):
        out = dict(out)
        out['account_mode'] = _view_mode
    return out

@app.post('/api/positions/bind', dependencies=[Depends(require_admin)])
async def positions_bind(data: dict=None):
    """Force-bind an OKX position to a strategy (claim + Momentum memory if needed)."""
    data = data or {}
    inst = (data.get('instId') or data.get('inst_id') or '').strip()
    side = (data.get('side') or data.get('posSide') or 'long').lower()
    if side in ('sell', 's'):
        side = 'short'
    elif side not in ('long', 'short'):
        side = 'long'
    bot = (data.get('bot') or data.get('bot_id') or 'Momentum').strip()
    inv = {'Momentum': ROT_BOT_ID, 'rotation_strategy': ROT_BOT_ID, ROT_BOT_ID: ROT_BOT_ID, 'Impulse 1D': IMP_BOT_ID, 'impulse_strategy': IMP_BOT_ID, IMP_BOT_ID: IMP_BOT_ID, 'AI Discretionary 1H': AI_BOT_ID, AI_BOT_ID: AI_BOT_ID, 'MACD+Donchian Validation': VAL_BOT_ID, VAL_BOT_ID: VAL_BOT_ID, 'smart_money': 'smart_money', 'Умные деньги': 'smart_money', 'Smart Money': 'smart_money', 'smart_money_mirror': 'smart_money'}
    bid = inv.get(bot) or inv.get(bot.replace(' ', '_'))
    if not inst or not bid:
        raise HTTPException(status_code=400, detail='instId and bot required')
    sz, entry = (0.0, 0.0)
    client = client_manager.get_client()
    if client:
        res = await client.get_positions('SWAP')
        for p in res.get('data') or []:
            if p.get('instId') == inst:
                try:
                    sz = abs(float(p.get('pos') or 0))
                    entry = float(p.get('avgPx') or 0)
                except (TypeError, ValueError):
                    pass
                break
    if sz <= 0 or entry <= 0:
        sz = float(data.get('size') or 0)
        entry = float(data.get('entry') or 0)
    if sz <= 0 or entry <= 0:
        raise HTTPException(status_code=400, detail='Cannot resolve size/entry from OKX')
    await claim_open(db, bid, inst, side, sz, entry)
    if bid == ROT_BOT_ID:
        try:
            from app.services.legacy_stubs import RotPosition
            global rotation
            if rotation:
                coin = inst.replace('-USDT-SWAP', '').replace('-USD-SWAP', '')
                stop = entry * 0.985 if side == 'long' else entry * 1.015
                rotation._positions[coin] = RotPosition(symbol=inst, coin=coin, inst_id=inst, side=side, size=sz, size_original=sz, entry_price=entry, stop_price=stop, peak_price=entry, opened_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), atr=entry * 0.015, atr_hourly=entry * 0.015, leverage=3.0)
                try:
                    await rotation._persist_open_snapshot()
                except Exception:
                    pass
        except Exception as e:
            print(f'[bind] inject: {e}', flush=True)
    global _positions_cache
    _positions_cache = None
    return {'ok': True, 'inst_id': inst, 'side': side, 'bot_id': bid, 'size': sz, 'entry': entry}

@app.post('/api/positions/close', dependencies=[Depends(require_admin)])
async def close_position(data: dict):
    account = str(data.get('account') or '').lower()
    client = None
    if account == 'live':
        if live_manager:
            try:
                _lc = live_manager.get_client()
                if _lc and not getattr(_lc, 'demo', True) and getattr(_lc, 'has_credentials', lambda: False)():
                    client = _lc
            except Exception:
                client = None
        if not client:
            raise HTTPException(status_code=400, detail='LIVE-подключение не активно')
    else:
        client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail='API not configured')
    inst_id = data.get('instId')
    pos_side = data.get('posSide') or 'net'
    mgn_mode = data.get('mgnMode', 'cross')
    if not inst_id:
        raise HTTPException(status_code=400, detail='instId required')
    if pos_side == 'net' and 'posSide' not in data:
        try:
            positions_resp = await client.get_positions('SWAP')
            if not positions_resp.get('error') and positions_resp.get('data'):
                for p in positions_resp['data']:
                    if p.get('instId') == inst_id:
                        pos_side = p.get('posSide', 'net')
                        break
        except Exception as e:
            print(f'[positions/close] posSide auto-detect error: {e}', flush=True)
    result = await client.close_position(inst_id=inst_id, mgn_mode=mgn_mode, pos_side=pos_side)
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    return {'message': 'Position closed', 'data': result.get('data')}
_ticker_cache: dict = {}
_ticker_cache_ts: dict = {}
_TICKER_TTL = 5
_positions_cache: dict = None
_positions_cache_ts: float = 0
_portfolio_cache: dict = None
_portfolio_cache_ts: float = 0
_POS_CACHE_TTL = 3
_POS_RECLAIM_TS = 0.0
_POS_RECLAIM_TTL = 90.0
_FUNDING_CACHE = 0.0
_FUNDING_CACHE_TS = 0.0
_FUNDING_TTL = 120.0
_SM_DISCOVER_CACHE = {'ts': 0.0, 'key': '', 'data': None}
_live_status_cache: dict = {}
_SM_DISCOVER_LOCK = None

@app.get('/api/market/ticker')
async def get_ticker(inst_id: str='BTC-USDT'):
    now_s = _time.time()
    if _ticker_cache.get(inst_id) and now_s - _ticker_cache_ts.get(inst_id, 0) < _TICKER_TTL:
        return _ticker_cache[inst_id]
    result = await _okx_call(lambda c: c.get_ticker(inst_id))
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    data = result.get('data', [{}])[0] if result.get('data') else {}
    if data:
        _ticker_cache[inst_id] = data
        _ticker_cache_ts[inst_id] = _time.time()
    return data

@app.get('/api/market/tickers')
async def get_tickers(inst_id: str=''):
    """Batch ticker fetch — one request replaces N individual /market/ticker
    calls (the dashboard's 10 coin-price strip)."""
    ids = [i.strip() for i in (inst_id or '').split(',') if i.strip()]
    if not ids:
        return {'tickers': []}
    import asyncio as _aio
    results = await _aio.gather(*(_safe_ticker(iid) for iid in ids), return_exceptions=True)
    out = []
    for iid, r in zip(ids, results):
        if isinstance(r, dict) and r:
            out.append({'instId': iid, **r})
    return {'tickers': out}

async def _safe_ticker(inst_id: str) -> dict:
    """Single-ticker fetch wrapped for gather — never raises."""
    try:
        return await get_ticker(inst_id=inst_id)
    except Exception:
        return {}

@app.get('/api/market/candles')
async def get_candles(inst_id: str='BTC-USDT-SWAP', bar: str='1H', limit: int=100):
    result = await _okx_call(lambda c: c.get_candles(inst_id, bar=bar, limit=limit))
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    return {'candles': result.get('data', [])}
_backtest_sem: Optional[asyncio.Semaphore] = None
_freqtrade_sem: Optional[asyncio.Semaphore] = None
_bt_attempts: dict[str, list[float]] = {}
_BT_MAX_ATTEMPTS = 3
_BT_WINDOW_SEC = 600.0

def _get_backtest_sem() -> asyncio.Semaphore:
    global _backtest_sem
    if _backtest_sem is None:
        _backtest_sem = asyncio.Semaphore(2)
    return _backtest_sem

def _get_freqtrade_sem() -> asyncio.Semaphore:
    global _freqtrade_sem
    if _freqtrade_sem is None:
        _freqtrade_sem = asyncio.Semaphore(1)
    return _freqtrade_sem

def _freqtrade_rate_limited(ip: str) -> bool:
    now = _time.time()
    _bt_attempts[ip] = [t for t in _bt_attempts[ip] if now - t < _BT_WINDOW_SEC]
    if len(_bt_attempts) > 10000:
        _bt_attempts.clear()
    if len(_bt_attempts[ip]) >= _BT_MAX_ATTEMPTS:
        return True
    _bt_attempts[ip].append(now)
    return False

@app.post('/api/trade/order', dependencies=[Depends(require_admin)])
async def place_order(data: dict):
    """Manual orders disabled by default — only strategy signal path may open risk."""
    allow = os.getenv('ALLOW_MANUAL_ORDERS', '0').strip().lower() in ('1', 'true', 'yes', 'on')
    if not allow:
        raise HTTPException(status_code=403, detail='Manual orders disabled. Opens only via strategy signals (set ALLOW_MANUAL_ORDERS=1 to override).')
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail='API not configured')
    result = await client.place_order(inst_id=data['instId'], side=data['side'], ord_type=data.get('ordType', 'market'), sz=str(data['sz']), td_mode=data.get('tdMode', 'cash'), px=data.get('px'), pos_side=data.get('posSide'), reduce_only=data.get('reduceOnly', False), tgt_ccy=data.get('tgtCcy'))
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result.get('message', ''))
    return {'message': 'Order placed', 'data': result.get('data')}

@app.get('/api/trade/orders')
async def get_orders(inst_type: str='SPOT'):
    result = await _okx_call(lambda c: c.get_orders(inst_type))
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result['message'])
    return {'orders': result.get('data', [])}

@app.get('/api/trade/log')
async def get_trade_log():
    return {'orders': trade_log[-100:]}

@app.post('/api/db/reset-all', dependencies=[Depends(require_admin)])
async def db_reset_all():
    """Nuclear reset: clear ALL bot data (trades, signals, positions, metrics, bots)."""
    global rotation
    if rotation and rotation._running:
        await rotation.stop()
    rotation = None
    for table in ['trades', 'signals', 'positions', 'performance_metrics', 'bots']:
        try:
            await db._execute(f'DELETE FROM {table}')
        except Exception as e:
            print(f'[reset-all] Error clearing {table}: {e}', flush=True)
    return {'message': 'All data reset - clean slate'}

@app.get('/api/telegram/status', dependencies=[Depends(require_admin)])
async def telegram_status():
    """Return Telegram notification config status (token masked)."""
    masked_token = telegram.token[:10] + '…' + telegram.token[-4:] if telegram.token else ''
    masked_chat = telegram.chat_id[:2] + '…' + telegram.chat_id[-3:] if telegram.chat_id else ''
    return {'configured': telegram.configured, 'status': telegram.status, 'chat_id': masked_chat, 'chat_id_masked': masked_chat, 'token_masked': masked_token}

@app.post('/api/telegram/config', dependencies=[Depends(require_admin)])
async def telegram_config(data: dict=None):
    """Set/update Telegram bot token, chat id and signals channel at runtime."""
    d = data or {}
    telegram.configure(token=d.get('token', ''), chat_id=d.get('chat_id', ''), channel_id=d.get('channel_id', ''))
    try:
        if telegram.token:
            await db.set_setting('TELEGRAM_BOT_TOKEN', telegram.token)
        if telegram.chat_id:
            await db.set_setting('TELEGRAM_CHAT_ID', telegram.chat_id)
        if telegram.channel_id:
            await db.set_setting('TELEGRAM_CHANNEL_ID', telegram.channel_id)
    except Exception as e:
        print(f'[telegram/config] DB persist error: {e}', flush=True)
    global bot_poller
    if telegram.token and (bot_poller is None or not bot_poller._running):
        try:
            bot_poller = TelegramBotPoller(notifier=telegram, db=db)
            bot_poller.start()
            print('[telegram/config] poller started', flush=True)
        except Exception as e:
            print(f'[telegram/config] poller start error: {e}', flush=True)
    return await telegram_status()

@app.post('/api/telegram/test', dependencies=[Depends(require_admin)])
async def telegram_test(data: dict=None):
    """Send a test message to verify the Telegram connection."""
    d = data or {}
    token = d.get('token', '') or telegram.token
    chat_id = d.get('chat_id', '') or telegram.chat_id
    if not (token and chat_id):
        return {'ok': False, 'message': 'Telegram не настроен: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID'}
    notifier = TelegramNotifier(token=token, chat_id=chat_id)
    ok = await notifier.send('✅ Уведомления о сделках настроены и работают!')
    return {'ok': ok, 'message': 'Сообщение отправлено' if ok else 'Не удалось отправить. Проверьте token и chat_id (например, через @userinfobot).'}

@app.post('/api/telegram/simulate', dependencies=[Depends(require_admin)])
async def telegram_simulate(data: dict=None):
    """Send sample trade-signal messages to Telegram to preview the real format.

    No real order is placed — just the exact open / partial-TP / close messages
    (rotation bot) plus a pyramid add-on message (impulse bot), with sample data.
    """
    d = data or {}
    token = d.get('token', '') or telegram.token
    chat_id = d.get('chat_id', '') or telegram.chat_id
    if not (token and chat_id):
        return {'ok': False, 'message': 'Telegram не настроен: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID'}
    notifier = TelegramNotifier(token=token, chat_id=chat_id)
    open_px = 67250.0
    msg_open = notifier.open_msg(coin='BTC', side='long', price=open_px, stop=round(open_px * 0.97, 2), size=0.03, leverage=3.0, bot_name='Momentum Rotation v6.2', signal_id=123)
    msg_partial = notifier.partial_msg(coin='BTC', side='long', entry=open_px, exit_px=round(open_px * 1.05, 2), pnl=76.5, closed_sz=0.015, remaining_sz=0.015, bot_name='Momentum Rotation v6.2', signal_id=123)
    msg_close = notifier.close_msg(coin='BTC', side='long', entry=open_px, exit_px=round(open_px * 1.09, 2), pnl=201.75, reason='trail_stop', bot_name='Momentum Rotation v6.2', signal_id=123)
    msg_add = notifier.add_msg(coin='ETH', side='long', price=3450.0, size=0.4, total=1.2, bot_name='Impulse 1D v4', signal_id=124)
    results = {}
    for name, text in (('open', msg_open), ('partial', msg_partial), ('close', msg_close), ('add', msg_add)):
        ok = await notifier.send(text)
        results[name] = ok
        print(f'[telegram/simulate] {name}: sent={ok}', flush=True)
    ok_all = all(results.values())
    return {'ok': ok_all, 'message': 'Все 4 сигнала отправлены' if ok_all else f'Частичная отправка: {results}', 'results': results}

@app.post('/api/telegram/menu', dependencies=[Depends(require_admin)])
async def telegram_menu(request: Request, data: dict=None):
    """Set the bot's chat menu button to open the Mini App (/mini)."""
    if not (telegram.token and telegram.chat_id):
        return {'ok': False, 'message': 'Telegram не настроен: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID'}
    d = data or {}
    url = (d.get('url') or '').strip()
    if not url:
        origin = str(request.base_url).rstrip('/')
        url = f'{origin}/mini'
    ok = await telegram.set_chat_menu_button(url)
    print(f'[telegram/menu] url={url} ok={ok}', flush=True)
    return {'ok': ok, 'url': url, 'message': 'Кнопка меню установлена' if ok else 'Ошибка установки кнопки меню'}

@app.get('/api/subs', dependencies=[Depends(require_admin)])
async def subs_list():
    """List all subscribers with status + revenue stats (admin)."""
    rows = await db.list_subscriptions()
    subscribers = []
    revenue = {'signals': 0, 'pro': 0}
    active = 0
    for r in rows:
        active_until = r.get('active_until', '')
        is_active = _is_active(r)
        if is_active:
            active += 1
        plan = r.get('plan', 'pro')
        if r.get('payment_id'):
            revenue[plan if plan in revenue else 'pro'] += PLANS_PRICE[plan if plan in PLANS_PRICE else 'pro']
        subscribers.append({'user_id': r.get('user_id'), 'username': r.get('username') or '', 'first_name': r.get('first_name') or '', 'plan': plan, 'status': 'active' if is_active else 'expired', 'active_until': active_until, 'last_payment': r.get('last_payment'), 'payment_id': r.get('payment_id')})
    users = []
    try:
        for u in await db.list_users():
            users.append({'telegram_id': u.get('telegram_id'), 'username': u.get('username') or '', 'first_name': u.get('first_name') or '', 'plan': u.get('plan'), 'active': _is_active(u), 'active_until': u.get('active_until'), 'creds_configured': bool(u.get('okx_key_enc'))})
    except Exception:
        pass
    return {'subscribers': subscribers, 'users': users, 'stats': {'total': len(subscribers), 'active': active, 'expired': len(subscribers) - active, 'revenue_stars': sum(revenue.values()), 'revenue_signals': revenue['signals'], 'revenue_pro': revenue['pro'], 'price_stars': PRO_PRICE_STARS, 'pro_price_stars': PRO_PRICE_STARS}}

@app.post('/api/subs/activate', dependencies=[Depends(require_admin)])
async def subs_activate(data: dict=None):
    """Manually grant/extend a subscription (e.g. cash payment or admin test)."""
    d = data or {}
    user_id = str(d.get('user_id', '')).strip()
    days = int(d.get('days', PRO_PLAN_DAYS))
    if not user_id:
        raise HTTPException(status_code=400, detail='user_id required')
    from datetime import timedelta as _td
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    try:
        cur = await db.get_subscription(user_id)
        base = now
        if cur and cur.get('active_until'):
            try:
                base = _dt.strptime(cur['active_until'], '%Y-%m-%d %H:%M')
            except ValueError:
                base = now
    except Exception:
        base = now
    until = (base + _td(days=days)).strftime('%Y-%m-%d %H:%M')
    await db.save_subscription(user_id=user_id, username=d.get('username', ''), first_name=d.get('first_name', ''), active_until=until, payment_id=d.get('payment_id', '') or f'manual_{int(_time.time())}', plan='pro', status='active')
    try:
        await db.find_or_create_user(user_id, d.get('username', ''), d.get('first_name', ''))
        await db.update_user(user_id, plan='pro', username=d.get('username', ''), first_name=d.get('first_name', ''), active_until=until)
    except Exception as e:
        logger.warning('subs/activate user sync error: %s', e)
    if d.get('notify') and telegram.token and user_id.isdigit():
        try:
            await telegram._send_to(user_id, f'✅ Подписка активирована администратором до <b>{until}</b> (UTC).', 'HTML')
        except Exception:
            pass
    return {'message': 'Subscription activated', 'active_until': until}

@app.post('/api/subs/deactivate', dependencies=[Depends(require_admin)])
async def subs_deactivate(data: dict=None):
    """Immediately deactivate a user's subscription."""
    d = data or {}
    user_id = str(d.get('user_id', '')).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail='user_id required')
    await db.delete_subscription(user_id)
    return {'message': 'Subscription deactivated'}

@app.get('/api/subs/config', dependencies=[Depends(require_admin)])
async def subs_config():
    """Subscription product config + poller readiness (admin)."""
    return {'pro_price_stars': PRO_PRICE_STARS, 'pro_plan_days': PRO_PLAN_DAYS, 'signals_free': True, 'bot_configured': bool(telegram.token), 'poller_running': bool(bot_poller and bot_poller._running)}

@app.get('/api/chart/trades')
async def chart_trades(inst_id: str='BTC-USDT-SWAP'):
    """Return real trade markers + TP/SL lines for a specific instrument.
    Closed trades: entry (green) + exit (red/green) markers.
    Open positions: entry marker (blue) + TP/SL price lines from algo orders."""
    try:
        raw_fills = await _fetch_okx_fills(limit=300, inst_id=inst_id)
    except Exception as e:
        print(f'[chart_trades] _fetch_okx_fills error: {e}', flush=True)
        return {'markers': [], 'tp_sl_lines': [], 'debug': {'error': str(e), 'raw_fills': 0, 'paired': 0}}
    paired = await _pair_fills(raw_fills)
    inst_paired = [t for t in paired if t.get('inst_id') == inst_id]
    closed_count = sum((1 for t in inst_paired if t.get('reason') == 'closed'))
    open_count = sum((1 for t in inst_paired if t.get('reason') == 'open'))
    print(f'[chart_trades] raw={len(raw_fills)} paired={len(paired)} inst={inst_id} closed={closed_count} open={open_count}', flush=True)

    def _to_ts(time_str):
        if not time_str:
            return None
        try:
            return int(datetime.fromisoformat(time_str).timestamp())
        except (ValueError, OSError, TypeError):
            return None
    markers = []
    for t in inst_paired:
        if t.get('reason') == 'closed':
            entry_ts = _to_ts(t.get('entry_time'))
            entry_px = t.get('entry', 0)
            if entry_ts and entry_px and (entry_px > 0):
                pos_side = t.get('pos_side', 'long')
                markers.append({'time': entry_ts, 'position': 'belowBar' if pos_side == 'long' else 'aboveBar', 'color': '#00ff88', 'shape': 'arrowUp' if pos_side == 'long' else 'arrowDown', 'text': f'IN {entry_px:.2f}'})
            close_ts = _to_ts(t.get('time'))
            exit_px = t.get('exit_price', 0)
            if close_ts and exit_px and (exit_px > 0):
                pnl = t.get('pnl', 0) or 0
                pos_side = t.get('pos_side', 'long')
                markers.append({'time': close_ts, 'position': 'aboveBar' if pos_side == 'long' else 'belowBar', 'color': '#00ff88' if pnl >= 0 else '#ff4757', 'shape': 'arrowDown' if pos_side == 'long' else 'arrowUp', 'text': f'{pnl:+.2f}'})
        else:
            open_ts = _to_ts(t.get('entry_time') or t.get('time'))
            entry_px = t.get('entry', 0)
            if open_ts and entry_px and (entry_px > 0):
                pos_side = t.get('pos_side', 'long')
                markers.append({'time': open_ts, 'position': 'belowBar' if pos_side == 'long' else 'aboveBar', 'color': '#4a9eff', 'shape': 'arrowUp' if pos_side == 'long' else 'arrowDown', 'text': f'OPEN {entry_px:.2f}'})
    tp_sl_lines = []
    try:
        algo_r = await _okx_call(lambda c: c.get_algo_orders(ord_type='conditional'))
        if not algo_r.get('error') and algo_r.get('data'):
            for order in algo_r['data']:
                if order.get('instId') != inst_id:
                    continue
                tp_price = order.get('tpTriggerPxPx') or order.get('tpTriggerPx')
                sl_price = order.get('slTriggerPxPx') or order.get('slTriggerPx')
                pos_side = order.get('posSide', 'net')
                sz = float(order.get('sz', 0) or 0)
                if tp_price and float(tp_price) > 0:
                    tp_sl_lines.append({'price': float(tp_price), 'type': 'tp', 'pos_side': pos_side, 'size': sz, 'label': f'TP {float(tp_price):.2f}'})
                if sl_price and float(sl_price) > 0:
                    tp_sl_lines.append({'price': float(sl_price), 'type': 'sl', 'pos_side': pos_side, 'size': sz, 'label': f'SL {float(sl_price):.2f}'})
        print(f'[chart_trades] algo orders for {inst_id}: {len(tp_sl_lines)} TP/SL lines, algo_error={algo_r.get('error')}, algo_msg={algo_r.get('message', '')}', flush=True)
    except Exception as e:
        print(f'[chart_trades] algo orders error: {e}', flush=True)
    for t in inst_paired:
        entry_ts = _to_ts(t.get('entry_time'))
        close_ts = _to_ts(t.get('time'))
        entry_px = t.get('entry', 0)
        exit_px = t.get('exit_price', 0)
        print(f'[chart_trades] trade: reason={t.get('reason')} entry_ts={entry_ts} close_ts={close_ts} entry={entry_px} exit={exit_px}', flush=True)
    markers.sort(key=lambda m: m['time'])
    return {'markers': markers, 'tp_sl_lines': tp_sl_lines, 'debug': {'raw_fills': len(raw_fills), 'paired': len(paired), 'matched': len(inst_paired), 'closed': closed_count, 'open': open_count, 'inst_ids': list(set((t.get('inst_id', '') for t in paired))), 'client_ok': client_manager.get_client() is not None, 'demo': _env_demo, 'okx_errors': _fills_errors, 'sample': [{'entry_time': t.get('entry_time', ''), 'time': t.get('time', ''), 'entry': t.get('entry', 0), 'exit_price': t.get('exit_price', 0), 'pnl': t.get('pnl', 0), 'reason': t.get('reason', ''), 'pos_side': t.get('pos_side', '')} for t in inst_paired[:3]]}}
SWAP_INSTRUMENTS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'BNB-USDT-SWAP', 'SOL-USDT-SWAP']
_fills_cache: list[dict] = []
_fills_cache_ts: float = 0
_fills_cache_limit: int = 0
_FILLS_TTL = 30
_fills_errors: list[str] = []

async def _fetch_okx_fills(limit: int=100, inst_id: str=None, mode: str=None) -> list[dict]:
    """Fetch fills from OKX for a single account mode (demo XOR live).

    mode=None → current owner _account_mode(). Never mixes environments.
    """
    global _fills_cache, _fills_cache_ts, _fills_cache_limit, _fills_errors
    mode = (mode or _account_mode()).lower()
    cache_key = f'{mode}:{inst_id or '__all__'}'
    now = _time.time()
    if _fills_cache and now - _fills_cache_ts < _FILLS_TTL and (_fills_cache_limit >= limit) and (getattr(_fetch_okx_fills, '_cache_key', '') == cache_key):
        print(f'[_fetch_okx_fills] cache hit, {len(_fills_cache)} fills (key={cache_key})', flush=True)
        return _fills_cache
    all_fills = []
    errors = []
    effective_limit = min(limit, 1000)
    pages = max(1, (effective_limit + 99) // 100)
    if inst_id:
        after_ts = ''
        for page in range(pages):
            params = {'inst_type': 'SWAP', 'instId': inst_id, 'limit': 100}
            if after_ts:
                params['after'] = after_ts
            r1 = await _okx_call_account(lambda c, p=params: c.get_fills_history(**p), mode=mode)
            data = r1.get('data', [])
            print(f'[_fetch_okx_fills] {inst_id} page {page + 1}: error={r1.get('error')}, data_len={len(data)}', flush=True)
            if r1.get('error'):
                errors.append(f'{inst_id} p{page + 1}: {r1.get('message', '')}')
                break
            if not data:
                break
            all_fills.extend(data)
            if len(data) < 100:
                break
            after_ts = data[-1].get('ts', '')
    else:
        after_ts = ''
        for page in range(pages):
            params = {'inst_type': 'SWAP', 'limit': 100}
            if after_ts:
                params['after'] = after_ts
            r1 = await _okx_call_account(lambda c, p=params: c.get_fills_history(**p), mode=mode)
            data = r1.get('data', [])
            print(f'[_fetch_okx_fills] all-SWAP page {page + 1}: error={r1.get('error')}, data_len={len(data)}', flush=True)
            if r1.get('error'):
                errors.append(f'all-SWAP p{page + 1}: {r1.get('message', '')}')
                break
            if not data:
                break
            all_fills.extend(data)
            if len(data) < 100:
                break
            after_ts = data[-1].get('ts', '')
        if not all_fills:
            r2 = await _okx_call_account(lambda c: c.get_fills(limit=100), mode=mode)
            print(f'[_fetch_okx_fills] fills (fallback): error={r2.get('error')}, data_len={len(r2.get('data', []))}', flush=True)
            if r2.get('error'):
                errors.append(f'fills: {r2.get('message', '')}')
            if not r2.get('error') and r2.get('data'):
                all_fills.extend(r2['data'])
    all_fills.sort(key=lambda f: f.get('ts', '0'))
    for _f in all_fills:
        if isinstance(_f, dict):
            _f['account_mode'] = mode
            _f['_from_okx'] = True
    _fills_cache = all_fills
    _fills_cache_ts = now
    _fills_cache_limit = effective_limit
    _fills_errors = errors
    _fetch_okx_fills._cache_key = cache_key
    print(f'[_fetch_okx_fills] total: {len(all_fills)} fills for {cache_key}, errors={errors}', flush=True)
    return all_fills

def _ms_to_iso(ts_ms: str) -> str:
    """Convert OKX millisecond timestamp to UTC ISO string (timezone-aware, so
    the frontend renders it in the correct local/Moscow time)."""
    if not ts_ms:
        return ''
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return ts_ms

def _parse_fill_pnl(f: dict):
    """Parse pnl from OKX fill. OKX uses 'fillPnl' field. Returns float or None if unknown."""
    raw = f.get('fillPnl') or f.get('pnl')
    if raw is None or raw == '':
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None

def _parse_fill_sz(f: dict) -> float:
    """Parse fill size from OKX fill. OKX uses 'fillSz' field."""
    return float(f.get('fillSz', 0) or 0)

def _is_close_fill(f: dict, direction: str=None) -> bool:
    """Determine if a fill is closing a position.
    Priority: 1) subType (3/4=open, 5/6=close — present on every trade fill),
    2) pnl field, 3) posSide+side, 4) direction tracking.

    subType is the reliable signal for demo accounts where fillPnl/posSide may
    be missing and direction tracking breaks under rapid open/close churn."""
    sub = str(f.get('subType', '') or '')
    if sub in ('5', '6'):
        return True
    if sub in ('1', '2', '3', '4'):
        return False
    pnl = _parse_fill_pnl(f)
    if pnl is not None and pnl != 0:
        return True
    if pnl == 0:
        return False
    pos_side = f.get('posSide', '')
    side = f.get('side', '')
    if pos_side and pos_side != 'net':
        if pos_side == 'long' and side == 'sell' or (pos_side == 'short' and side == 'buy'):
            return True
        return False
    if direction:
        if direction == 'long' and side == 'sell' or (direction == 'short' and side == 'buy'):
            return True
    return False

def _fill_to_trade(f: dict, is_close: bool=False) -> dict:
    """Convert a single OKX fill dict to our trade format for frontend."""
    side = f.get('side', '')
    pnl = _parse_fill_pnl(f)
    if pnl is None:
        pnl = 0.0
    px = float(f.get('fillPx', 0) or 0)
    sz = _parse_fill_sz(f)
    inst_id = f.get('instId', '')
    pos_side = f.get('posSide', '')
    if is_close:
        if not pos_side or pos_side == 'net':
            pos_side = 'long' if side == 'sell' else 'short'
        reason = 'closed'
    else:
        if not pos_side or pos_side == 'net':
            pos_side = 'long' if side == 'buy' else 'short'
        reason = 'open'
    trade = {'time': _ms_to_iso(f.get('ts', '')), 'side': side, 'symbol': inst_id, 'size': sz, 'pnl': pnl, 'ord_id': f.get('ordId', ''), 'fee': f.get('fee', '0'), 'entry': px if not is_close else 0, 'entry_price': px if not is_close else 0, 'exit_price': px if is_close else 0, 'reason': reason, 'pos_side': pos_side, 'inst_id': inst_id, 'source': 'okx'}
    return trade

async def _pair_fills(fills: list[dict]) -> list[dict]:
    """Pair OKX fills into entry+close trades using sequential direction tracking.
    Works with or without pnl field (demo accounts may return pnl=null).
    Uses posSide+side for entry/close detection, with direction tracking as fallback.
    Calculates PnL from entry/exit prices when OKX pnl is not available."""
    by_inst: dict[str, list] = {}
    for f in fills:
        inst = f.get('instId', '')
        by_inst.setdefault(inst, []).append(f)
    paired = []
    for inst_id, inst_fills in by_inst.items():
        inst_fills.sort(key=lambda x: x.get('ts', '0'))
        direction = None
        entry_size = 0.0
        entry_cost = 0.0
        entry_time = ''
        entry_ord_id = ''
        entry_fees = 0.0
        entry_side = ''
        for f in inst_fills:
            fill_sz = _parse_fill_sz(f)
            fill_px = float(f.get('fillPx', 0) or 0)
            fill_side = f.get('side', '')
            fill_ts = f.get('ts', '')
            fill_pnl = _parse_fill_pnl(f)
            fill_fee = float(f.get('fee', 0) or 0)
            is_close = _is_close_fill(f, direction)
            if not is_close:
                if direction is None:
                    direction = 'long' if fill_side == 'buy' else 'short'
                    entry_side = fill_side
                entry_size += fill_sz
                entry_cost += fill_sz * fill_px
                entry_fees += fill_fee
                if not entry_time:
                    entry_time = fill_ts
                if not entry_ord_id:
                    entry_ord_id = f.get('ordId', '')
            elif entry_size > 0:
                close_size = min(fill_sz, entry_size)
                avg_entry = entry_cost / entry_size if entry_size > 0 else 0
                if fill_pnl is not None and fill_pnl != 0:
                    calc_pnl = fill_pnl
                elif direction == 'long':
                    calc_pnl = (fill_px - avg_entry) * close_size
                else:
                    calc_pnl = (avg_entry - fill_px) * close_size
                paired.append({'time': _ms_to_iso(fill_ts), 'entry_time': _ms_to_iso(entry_time), 'side': fill_side, 'symbol': inst_id, 'size': close_size, 'pnl': round(calc_pnl, 4), 'ord_id': f.get('ordId', ''), 'fee': str(fill_fee), 'entry': round(avg_entry, 4), 'entry_price': round(avg_entry, 4), 'exit_price': round(fill_px, 4), 'entry_ord_id': entry_ord_id, 'reason': 'closed', 'pos_side': direction, 'inst_id': inst_id, 'source': 'okx'})
                entry_size -= close_size
                entry_cost = avg_entry * entry_size
                if entry_size <= 1e-10:
                    direction = None
                    entry_size = 0.0
                    entry_cost = 0.0
                    entry_time = ''
                    entry_ord_id = ''
                    entry_fees = 0.0
                    entry_side = ''
            else:
                pos_out = f.get('posSide', '')
                if not pos_out or pos_out == 'net':
                    pos_out = 'long' if fill_side == 'sell' else 'short'
                paired.append({'time': _ms_to_iso(fill_ts), 'entry_time': '', 'side': fill_side, 'symbol': inst_id, 'size': fill_sz, 'pnl': fill_pnl or 0.0, 'ord_id': f.get('ordId', ''), 'fee': str(fill_fee), 'entry': 0, 'entry_price': 0, 'exit_price': round(fill_px, 4), 'entry_ord_id': entry_ord_id, 'reason': 'closed', 'pos_side': pos_out, 'inst_id': inst_id, 'source': 'okx'})
        if entry_size > 1e-10:
            avg_entry = entry_cost / entry_size if entry_size > 0 else 0
            paired.append({'time': _ms_to_iso(entry_time), 'entry_time': _ms_to_iso(entry_time), 'side': entry_side or ('buy' if direction == 'long' else 'sell'), 'symbol': inst_id, 'size': round(entry_size, 4), 'pnl': 0.0, 'ord_id': entry_ord_id, 'fee': str(round(entry_fees, 4)), 'entry': round(avg_entry, 4), 'entry_price': round(avg_entry, 4), 'exit_price': 0, 'reason': 'open', 'pos_side': direction or 'long', 'inst_id': inst_id, 'source': 'okx'})
    return paired

def _pair_bills(bills: list) -> list:
    """Pair OKX trade bills into entry+close rows using subType and exact pnl.

    Authoritative source: unlike fills, every trade bill carries subType
    (3/4 = open, 5/6 = close) plus exact pnl/fee/px/sz, so it stays correct on
    demo accounts where fills lack fillPnl/posSide and sequential tracking breaks
    under rapid open/close churn. Close bills sharing one ordId (partial fills)
    are aggregated into a single row."""
    by_inst: dict[str, list] = {}
    for b in bills:
        by_inst.setdefault(b.get('instId', ''), []).append(b)
    rows = []

    def _flush_close(pending: dict, cur: dict):
        """Emit the aggregated close row and reduce the open position."""
        avg_entry = 0.0
        pos_side = 'short' if pending['side'] == 'buy' else 'long'
        entry_ord = ''
        if cur is not None and cur['size'] > 0:
            avg_entry = cur['cost'] / cur['size']
            pos_side = cur['pos_side']
            entry_ord = str(cur.get('ord_id', '') or '').strip()
            close_sz = min(pending['size'], cur['size'])
            cur['size'] -= close_sz
            cur['cost'] = avg_entry * cur['size'] if cur['size'] > 0 else 0.0
            cur['fee'] += pending['fee']
        rows.append({'time': _ms_to_iso(pending['time']), 'entry_time': _ms_to_iso(pending['entry_time']), 'side': pending['side'], 'symbol': pending['inst_id'], 'inst_id': pending['inst_id'], 'size': round(pending['size'], 4), 'pnl': round(pending['pnl'], 4), 'ord_id': pending['ord_id'], 'fee': round(pending['fee'], 4), 'entry': round(avg_entry, 4), 'entry_price': round(avg_entry, 4), 'exit_price': round(pending['px'], 4), 'entry_ord_id': entry_ord, 'reason': 'closed', 'pos_side': pos_side, 'source': 'okx_bills', 'account_mode': b.get('account_mode') or '', '_from_okx': True})
    for inst_id, inst_bills in by_inst.items():
        try:
            inst_bills.sort(key=lambda x: str(x.get('ts', '0')))
        except Exception:
            inst_bills.sort(key=lambda x: str(x.get('ts', '0')))
        cur = None
        pending = None
        for b in inst_bills:
            try:
                sub = str(b.get('subType', '') or '')
                try:
                    sz = float(b.get('sz', 0) or 0)
                    px = float(b.get('px', 0) or 0)
                except (TypeError, ValueError):
                    continue
                try:
                    pnl = float(b.get('pnl', 0) or 0)
                except (TypeError, ValueError):
                    pnl = 0.0
                try:
                    fee = abs(float(b.get('fee', 0) or 0))
                except (TypeError, ValueError):
                    fee = 0.0
                ts = str(b.get('ts', '') or '')
                ord_id = str(b.get('ordId', '') or '').strip()
                side = b.get('side', '')
                if sub in ('3', '4'):
                    if pending is not None:
                        _flush_close(pending, cur)
                        if cur is not None and cur['size'] <= 1e-09:
                            cur = None
                        pending = None
                    if cur is None:
                        cur = {'size': 0.0, 'cost': 0.0, 'time': ts, 'ord_id': ord_id, 'side': side, 'pos_side': 'short' if side == 'sell' else 'long', 'fee': 0.0}
                    cur['size'] += sz
                    cur['cost'] += sz * px
                    cur['fee'] += fee
                    cur['ord_id'] = ord_id
                    cur['time'] = ts
                    cur['side'] = side
                    continue
                if sub in ('5', '6'):
                    if pending is None:
                        pending = {'size': 0.0, 'pnl': 0.0, 'fee': 0.0, 'ord_id': ord_id, 'time': ts, 'px': px, 'side': side, 'entry_time': cur['time'] if cur else ts, 'inst_id': inst_id}
                    elif pending['ord_id'] == ord_id:
                        pending['px'] = px
                        pending['time'] = ts
                    else:
                        _flush_close(pending, cur)
                        if cur is not None and cur['size'] <= 1e-09:
                            cur = None
                        pending = {'size': 0.0, 'pnl': 0.0, 'fee': 0.0, 'ord_id': ord_id, 'time': ts, 'px': px, 'side': side, 'entry_time': cur['time'] if cur else ts, 'inst_id': inst_id}
                    pending['size'] += sz
                    pending['pnl'] += pnl
                    pending['fee'] += fee
                    continue
            except Exception as e:
                print(f'[pair_bills] skip bad bill: {e} ({b.get('billId', '')})', flush=True)
                continue
        if pending is not None:
            _flush_close(pending, cur)
        if cur is not None and cur['size'] > 0:
            avg_entry = cur['cost'] / cur['size'] if cur['size'] > 0 else 0.0
            rows.append({'time': _ms_to_iso(cur['time']), 'entry_time': _ms_to_iso(cur['time']), 'side': cur['side'], 'symbol': inst_id, 'inst_id': inst_id, 'size': round(cur['size'], 4), 'pnl': None, 'ord_id': cur['ord_id'], 'fee': round(cur['fee'], 4), 'entry': round(avg_entry, 4), 'entry_price': round(avg_entry, 4), 'exit_price': None, 'reason': 'open', 'pos_side': cur['pos_side'], 'source': 'okx_bills', 'account_mode': cur.get('account_mode') or '', '_from_okx': True})
    rows.sort(key=lambda t: t.get('time') or '', reverse=True)
    return rows

async def _get_okx_realized_pnl() -> dict:
    """Calculate realized PnL from paired trades. Works even when OKX pnl field is null (demo accounts)."""
    now_ms = int(_time.time() * 1000)
    periods = {'1d': 86400000, '7d': 604800000, '30d': 2592000000}
    pnl = {'1d': 0.0, '7d': 0.0, '30d': 0.0}
    all_fills = await _fetch_okx_fills(limit=100)
    paired = await _pair_fills(all_fills)
    for t in paired:
        if t.get('reason') != 'closed':
            continue
        try:
            trade_pnl = float(t.get('pnl', 0) or 0)
        except (ValueError, TypeError):
            continue
        time_str = t.get('time', '')
        if not time_str:
            continue
        try:
            trade_ts = int(datetime.fromisoformat(time_str).timestamp() * 1000)
        except (ValueError, OSError, TypeError):
            continue
        for key, window in periods.items():
            if trade_ts >= now_ms - window:
                pnl[key] += trade_pnl
    return pnl
_bills_cache: dict = {}
_BILLS_TTL = 60

async def _fetch_all_trade_bills(limit_per_page: int=100, mode: str=None) -> list:
    """Fetch OKX trade bills (type=2) for one account mode only (demo XOR live)."""
    global _bills_cache
    mode = (mode or _account_mode()).lower()
    if mode not in ('demo', 'live'):
        mode = 'demo'
    now = _time.time()
    cached = _bills_cache.get(mode) if isinstance(_bills_cache, dict) else None
    if cached and now - cached.get('ts', 0) < _BILLS_TTL:
        return list(cached.get('data') or [])
    bills: list = []
    seen: set = set()
    try:
        for endpoint, fn in (('bills', lambda c, **kw: c.get_bills(inst_type='SWAP', type='2', **kw)), ('archive', lambda c, **kw: c.get_bills_archive(inst_type='SWAP', type='2', **kw))):
            after = ''
            for _ in range(10):
                kw = {'limit': limit_per_page}
                if after:
                    kw['after'] = after
                resp = None
                for attempt in range(3):
                    resp = await _okx_call_account(lambda c, e=fn, k=kw: e(c, **k), mode=mode)
                    if not resp.get('error'):
                        break
                    msg = str(resp.get('message', ''))
                    if '429' in msg or 'Too Many Requests' in msg:
                        await asyncio.sleep(1.0 + attempt)
                        continue
                    break
                if not resp or resp.get('error'):
                    print(f'[bills] {mode}/{endpoint} error: {(resp or {}).get('message', '')}', flush=True)
                    break
                data = resp.get('data', [])
                if not data:
                    break
                added = 0
                for b in data:
                    bid = b.get('billId', '')
                    if bid in seen:
                        continue
                    seen.add(bid)
                    if str(b.get('type', '')) == '2':
                        b = dict(b)
                        b['account_mode'] = mode
                        b['_from_okx'] = True
                        bills.append(b)
                        added += 1
                after = data[-1].get('billId', '')
                if added == 0 or len(data) < limit_per_page:
                    break
    except Exception as e:
        import traceback
        print(f'[bills] {mode} fetch error: {e}', flush=True)
        traceback.print_exc()
    if isinstance(_bills_cache, dict):
        _bills_cache[mode] = {'ts': _time.time(), 'data': bills}
    return bills
_CLORD_BOT_MAP = {'ais': 'AI Scale-In 1H', 'ai': 'AI Discretionary 1H', 'rot': 'Momentum', 'momentum': 'Momentum', 'imp': 'Impulse 1D', 'val': 'MACD+Donchian Validation', 'scl': 'Order Book Scalp', 'scalp': 'Order Book Scalp', 'vwap': 'VWAP Mean Reversion', 'sm': 'Умные деньги'}
_exchange_sync_ts: float = 0
_EXCHANGE_SYNC_TTL = 60

async def sync_exchange_close_trades() -> int:
    """Fetch all OKX trade bills (type=2), filter close trades (non-zero pnl),
    group by ordId, tag by clOrdId prefix, and upsert into exchange_close_trades.
    Returns number of trades synced."""
    global _exchange_sync_ts
    now = _time.time()
    if _exchange_sync_ts and now - _exchange_sync_ts < _EXCHANGE_SYNC_TTL:
        return 0
    bills = await _fetch_all_trade_bills(limit_per_page=100, mode=_account_mode())
    close_by_ord: dict = {}
    _bills_with_pnl = 0
    _sub_types_seen = set()
    for b in bills:
        _sub_types_seen.add(str(b.get('subType', '') or ''))
        sub = str(b.get('subType', '') or '')
        if sub not in ('5', '6'):
            continue
        oid = str(b.get('ordId', '')).strip()
        if not oid:
            continue
        try:
            bp = float(b.get('pnl') if b.get('pnl') not in (None, '') else 0)
        except (TypeError, ValueError):
            bp = 0.0
        try:
            bf = abs(float(b.get('fee') or 0))
        except (TypeError, ValueError):
            bf = 0.0
        try:
            bs = float(b.get('sz') or 0)
        except (TypeError, ValueError):
            bs = 0.0
        try:
            bpx = float(b.get('px') or b.get('fillIdxPx') or 0)
        except (TypeError, ValueError):
            bpx = 0.0
        ts = b.get('ts') or ''
        clord = str(b.get('clOrdId', '') or '').strip()
        inst = b.get('instId', '')
        if oid not in close_by_ord:
            close_by_ord[oid] = {'inst_id': inst, 'cl_ord_id': clord, 'ts': ts, 'pnl': 0.0, 'fee': 0.0, 'sz': 0.0, 'px_sum': 0.0, 'px_n': 0, 'sub_type': sub, 'account_mode': b.get('account_mode') or _account_mode()}
        close_by_ord[oid]['pnl'] += bp
        close_by_ord[oid]['fee'] += bf
        close_by_ord[oid]['sz'] += bs
        if bpx > 0:
            close_by_ord[oid]['px_sum'] += bpx * bs if bs > 0 else bpx
            close_by_ord[oid]['px_n'] += bs if bs > 0 else 1
        if ts and ts > close_by_ord[oid]['ts']:
            close_by_ord[oid]['ts'] = ts
        if clord and (not close_by_ord[oid]['cl_ord_id']):
            close_by_ord[oid]['cl_ord_id'] = clord
    rows = []
    for oid, info in close_by_ord.items():
        clord = (info['cl_ord_id'] or '').lower()
        bot_label = ''
        for pfx, label in sorted(_CLORD_BOT_MAP.items(), key=lambda x: -len(x[0])):
            if clord.startswith(pfx):
                bot_label = label
                break
        if not bot_label:
            inst = info.get('inst_id') or ''
            best_cl = ''
            for b in bills:
                if (b.get('instId') or '') != inst:
                    continue
                if str(b.get('subType') or '') not in ('3', '4'):
                    continue
                cid = str(b.get('clOrdId') or '').strip().lower()
                if not cid:
                    continue
                best_cl = cid
                for pfx, label in sorted(_CLORD_BOT_MAP.items(), key=lambda x: -len(x[0])):
                    if cid.startswith(pfx):
                        bot_label = label
                        clord = cid
                        info['cl_ord_id'] = cid
                        break
            if bot_label:
                print(f'[exchange-sync] recovered label={bot_label} from entry clOrdId={best_cl} inst={inst}', flush=True)
        avg_px = info['px_sum'] / info['px_n'] if info['px_n'] > 0 else 0.0
        close_ts = 0
        if info['ts']:
            try:
                close_ts = int(info['ts'])
            except (TypeError, ValueError):
                pass
        if not bot_label and AI_ONLY_MODE:
            bot_label = ''
        rows.append({'ord_id': oid, 'inst_id': info['inst_id'], 'cl_ord_id': info['cl_ord_id'], 'bot_label': bot_label, 'pnl': round(info['pnl'], 6), 'fee': round(info['fee'], 6), 'sz': round(info['sz'], 6), 'avg_px': round(avg_px, 6), 'close_ts': close_ts, 'sub_type': info['sub_type'], 'account_mode': info.get('account_mode') or _account_mode(), 'account_key': 'showcase' if _account_mode() == 'demo' else 'live'})
    print(f'[exchange-sync] bills={len(bills)} subtypes={_sub_types_seen} close_orders={len(close_by_ord)} rows={len(rows)}', flush=True)
    if rows:
        try:
            n = await db.upsert_exchange_close_trades(rows)
            _exchange_sync_ts = _time.time()
            print(f'[exchange-sync] synced {n} close trades from {len(close_by_ord)} orders', flush=True)
            return n
        except Exception as e:
            print(f'[exchange-sync] DB upsert error: {e}', flush=True)
            import traceback
            traceback.print_exc()
    return 0

@app.post('/api/pnl/rebuild-exchange', dependencies=[Depends(require_admin)])
async def pnl_rebuild_exchange():
    """Force re-sync of exchange close trades from OKX bills → DB, then return stats."""
    global _exchange_sync_ts
    _exchange_sync_ts = 0
    n = await sync_exchange_close_trades()
    epoch = await get_pnl_epoch()
    epoch_ms = 0
    if epoch:
        try:
            from datetime import datetime as _dt, timezone as _tz
            epoch_ms = int(_dt.fromisoformat(epoch).replace(tzinfo=_tz.utc).timestamp() * 1000)
        except Exception:
            pass
    trades = await db.get_exchange_close_trades_detail(epoch_ms=epoch_ms, limit=500)
    total_pnl = sum((t.get('pnl', 0) for t in trades))
    ai_pnl = sum((t.get('pnl', 0) for t in trades if t.get('bot_label') == 'AI Discretionary 1H'))
    return {'synced': n, 'total_trades': len(trades), 'total_pnl': round(total_pnl, 2), 'ai_pnl': round(ai_pnl, 2), 'epoch': epoch, 'trades': [{'oid': t['ord_id'][-8:], 'inst': t['inst_id'], 'bot': t['bot_label'], 'pnl': round(t['pnl'], 2), 'fee': round(t['fee'], 2), 'ts': t['close_ts']} for t in trades[:50]]}

@app.get('/api/debug/bills', dependencies=[Depends(require_admin)])
async def debug_bills():
    """Temporary: show raw OKX bills to diagnose sync filter."""
    bills = await _fetch_all_trade_bills(limit_per_page=100, mode=_account_mode())
    from collections import Counter
    sub_counter = Counter()
    pnl_nonzero = 0
    for b in bills:
        sub = str(b.get('subType', '') or '')
        sub_counter[sub] += 1
        try:
            if abs(float(b.get('pnl') or 0)) > 0.0001:
                pnl_nonzero += 1
        except (TypeError, ValueError):
            pass
    samples = []
    for b in bills[:10]:
        samples.append({'subType': b.get('subType'), 'pnl': b.get('pnl'), 'balChg': b.get('balChg'), 'fee': b.get('fee'), 'type': b.get('type'), 'instId': b.get('instId'), 'ordId': str(b.get('ordId', ''))[-8:]})
    return {'total_bills': len(bills), 'subTypes': dict(sub_counter), 'pnl_nonzero': pnl_nonzero, 'samples': samples, 'mode': _account_mode()}

@app.post('/api/pnl/rebuild-strategy', dependencies=[Depends(require_admin)])
async def pnl_rebuild_strategy(data: dict=None):
    """Reassign SOL off AI -> Impulse and rebuild each strategy PnL from DB trades only."""
    data = data or {}
    symbol = str(data.get('symbol') or 'SOL').upper()
    out = {'steps': []}
    try:
        inst = f'{symbol}-USDT-SWAP'
        st = await db.reassign_trades_instrument(AI_BOT_ID, IMP_BOT_ID, inst)
        out['steps'].append({'reassign': st})
        from app.services.position_claim import release_open
        await release_open(db, AI_BOT_ID, inst, 'long')
        await release_open(db, AI_BOT_ID, inst, 'short')
    except Exception as e:
        out['steps'].append({'reassign_err': str(e)})
    bots = {'Momentum': ROT_BOT_ID, 'Impulse 1D': IMP_BOT_ID, 'MACD+Donchian Validation': VAL_BOT_ID, 'AI Discretionary 1H': AI_BOT_ID}
    summaries = {}
    for label, bid in bots.items():
        try:
            summaries[label] = await db.get_trades_summary(bid)
        except Exception as e:
            summaries[label] = {'error': str(e)}
    out['summaries'] = summaries
    global ai_bot
    if ai_bot and hasattr(ai_bot, 'correct_misattributed'):
        try:
            try:
                await db.set_setting(f'ai_misattr_fixed:{AI_BOT_ID}:{symbol}', '')
            except Exception:
                pass
            out['ai_correct'] = await ai_bot.correct_misattributed(symbol, IMP_BOT_ID)
        except Exception as e:
            out['ai_correct_err'] = str(e)
    return out

@app.get('/api/admin/users', dependencies=[Depends(require_admin)])
async def admin_list_users():
    """Users + OKX account status for admin panel."""
    out = []
    try:
        rows = await db.list_users()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    for u in rows:
        tid = str(u.get('telegram_id') or '')
        running = []
        try:
            st = strategy_mgr.status(tid) if tid else {}
            for k, v in (st or {}).items():
                if isinstance(v, dict) and v.get('running'):
                    running.append(k)
        except Exception:
            pass
        out.append({'telegram_id': tid, 'username': u.get('username') or '', 'first_name': u.get('first_name') or '', 'plan': u.get('plan') or 'free', 'active': _is_active(u), 'active_until': u.get('active_until'), 'creds_configured': bool(u.get('okx_key_enc')), 'okx_demo': bool(u.get('okx_demo', 1)), 'mode': 'demo' if bool(u.get('okx_demo', 1)) else 'live', 'capital': u.get('capital'), 'created_at': u.get('created_at'), 'updated_at': u.get('updated_at'), 'bots_running': running})
    platform = {'kind': 'platform_demo', 'label': 'Showcase DEMO (env OKX)', 'connected': bool(_env_key and _env_secret and _env_pass), 'demo': bool(_env_demo), 'mode': 'demo' if _env_demo else 'live', 'note': 'Общий demo-счёт витрины: гости и превью подписки. Не для личных Live-ключей.'}
    return {'users': out, 'platform': platform, 'counts': {'users': len(out), 'with_creds': sum((1 for x in out if x['creds_configured'])), 'live': sum((1 for x in out if x['creds_configured'] and (not x['okx_demo']))), 'demo': sum((1 for x in out if x['creds_configured'] and x['okx_demo'])), 'active_plans': sum((1 for x in out if x['active']))}}

@app.post('/api/admin/users/plan', dependencies=[Depends(require_admin)])
async def admin_set_user_plan(data: dict=None):
    """Set plan + active_until for a telegram user."""
    d = data or {}
    tid = str(d.get('telegram_id') or d.get('user_id') or '').strip()
    if not tid:
        raise HTTPException(status_code=400, detail='telegram_id required')
    plan = str(d.get('plan') or 'free').strip().lower()
    if plan not in ('free', 'signals', 'pro'):
        raise HTTPException(status_code=400, detail='plan must be free|signals|pro')
    days = int(d.get('days') or 0)
    active_until = d.get('active_until')
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    if days > 0:
        active_until = (_dt.now(_tz.utc) + _td(days=days)).isoformat()
    elif plan == 'free':
        active_until = None
    fields = {'plan': plan}
    if active_until is not None:
        fields['active_until'] = active_until
    await db.update_user(tid, **fields)
    try:
        await db.add_audit('admin_set_plan', actor='admin', detail=f'{tid} plan={plan} until={active_until}')
    except Exception:
        pass
    return {'ok': True, 'telegram_id': tid, 'plan': plan, 'active_until': active_until}

@app.post('/api/admin/users/mode', dependencies=[Depends(require_admin)])
async def admin_set_user_mode(data: dict=None):
    """Force user OKX mode demo|live (requires existing keys for live)."""
    d = data or {}
    tid = str(d.get('telegram_id') or d.get('user_id') or '').strip()
    mode = str(d.get('mode') or 'demo').strip().lower()
    if not tid:
        raise HTTPException(status_code=400, detail='telegram_id required')
    if mode not in ('demo', 'live'):
        raise HTTPException(status_code=400, detail='mode must be demo|live')
    u = await db.get_user_by_telegram(tid)
    if not u:
        raise HTTPException(status_code=404, detail='user not found')
    if mode == 'live' and (not u.get('okx_key_enc')):
        raise HTTPException(status_code=400, detail='Сначала пользователь должен подключить OKX ключи')
    await db.update_user(tid, okx_demo=1 if mode == 'demo' else 0)
    _clear_user_client(tid)
    try:
        strategy_mgr.stop_all(tid)
    except Exception:
        pass
    try:
        await db.add_audit('admin_set_mode', actor='admin', detail=f'{tid} mode={mode}')
    except Exception:
        pass
    return {'ok': True, 'telegram_id': tid, 'mode': mode}

@app.post('/api/admin/users/clear-credentials', dependencies=[Depends(require_admin)])
async def admin_clear_user_credentials(data: dict=None):
    """Remove stored OKX keys for a user."""
    d = data or {}
    tid = str(d.get('telegram_id') or d.get('user_id') or '').strip()
    if not tid:
        raise HTTPException(status_code=400, detail='telegram_id required')
    await db.update_user(tid, okx_key_enc='', okx_secret_enc='', okx_pass_enc='', okx_demo=1)
    _clear_user_client(tid)
    try:
        strategy_mgr.stop_all(tid)
    except Exception:
        pass
    try:
        await db.add_audit('admin_clear_creds', actor='admin', detail=tid)
    except Exception:
        pass
    return {'ok': True, 'telegram_id': tid}

@app.post('/api/admin/reset-trading-stats', dependencies=[Depends(require_admin)])
async def admin_reset_trading_stats(data: dict=None):
    """Wipe strategy trading history and start PnL counting from now (UTC).

    Does not close exchange positions and does not delete position claims. Does not change strategy code/params.
    Sets pnl_epoch so OKX history before this moment is ignored in cards.
    """
    from datetime import datetime as dt, timezone as tz
    data = data or {}
    if data.get('epoch'):
        epoch = str(data['epoch'])
    else:
        epoch = dt.now(tz.utc).strftime('%Y-%m-%dT00:00:00')
    bot_ids = [ROT_BOT_ID, MOM_BOT_ID, IMP_BOT_ID, VAL_BOT_ID, AI_BOT_ID, 'smart_money']
    try:
        from app.services.legacy_stubs import SCALP_BOT_ID
        bot_ids.append(SCALP_BOT_ID)
    except Exception:
        pass
    seen = set()
    bot_ids = [b for b in bot_ids if b and (not (b in seen or seen.add(b)))]
    wipe = await db.wipe_strategy_trading_data(bot_ids)
    await db.set_setting('pnl_epoch', epoch)
    await db.set_setting('trading_stats_reset_marker', 'manual')
    for key in (f'ai_lifetime:{AI_BOT_ID}', f'ai_misattr_fixed:{AI_BOT_ID}:SOL'):
        try:
            await db.set_setting(key, '')
        except Exception:
            pass
    global rotation, impulse, validation, ai_bot
    for bot in (rotation, impulse, validation, ai_bot):
        if not bot:
            continue
        try:
            if hasattr(bot, '_trade_log'):
                try:
                    bot._trade_log = [t for t in bot._trade_log or [] if (t.get('reason') or '').lower() in ('open', 'add') or t.get('pnl') is None]
                except Exception:
                    bot._trade_log = []
            if hasattr(bot, '_session_pnl'):
                bot._session_pnl = 0.0
            if hasattr(bot, '_lifetime_pnl'):
                bot._lifetime_pnl = 0.0
            if hasattr(bot, '_lifetime_trades'):
                bot._lifetime_trades = 0
            if hasattr(bot, '_lifetime_wins'):
                bot._lifetime_wins = 0
            if hasattr(bot, '_lifetime_fees'):
                bot._lifetime_fees = 0.0
            if hasattr(bot, '_equity') and hasattr(bot, '_capital'):
                bot._equity = float(getattr(bot, '_capital', 0) or 0)
            try:
                from app.services.position_claim import claim_open
                positions = getattr(bot, '_positions', None) or {}
                bid = getattr(bot, 'BOT_ID', None)
                if bid and positions:
                    for pos in positions.values():
                        await claim_open(db, bid, getattr(pos, 'inst_id', None) or getattr(pos, 'symbol', ''), getattr(pos, 'side', 'long'), float(getattr(pos, 'size', 0) or 0), float(getattr(pos, 'entry_price', 0) or 0))
            except Exception as e:
                print(f'[reset] re-claim {getattr(bot, 'BOT_ID', bot)}: {e}', flush=True)
        except Exception as e:
            print(f'[reset] mem {getattr(bot, 'BOT_ID', bot)}: {e}', flush=True)
    try:
        import os
        from app.services.smart_money_ledger import LEDGER_PATH, get_sm_ledger
        if os.path.exists(LEDGER_PATH):
            os.remove(LEDGER_PATH)
        import app.services.smart_money_ledger as sml
        sml._ledger = None
        get_sm_ledger()
    except Exception as e:
        print(f'[reset] sm ledger: {e}', flush=True)
    global _bot_stats_cache, _paired_cache, _pnl_cache
    try:
        _bot_stats_cache['ts'] = 0
        _bot_stats_cache['data'] = {}
    except Exception:
        pass
    try:
        _paired_cache.clear()
    except Exception:
        pass
    try:
        _pnl_cache.clear()
    except Exception:
        pass
    print(f'[reset] trading stats wiped epoch={epoch} bots={bot_ids}', flush=True)
    return {'ok': True, 'epoch': epoch, 'bots': bot_ids, 'wipe': wipe, 'message': 'PnL and trade cards reset. Counting from epoch. Open exchange positions unchanged.'}

@app.get('/api/pnl/summary')
async def pnl_summary():
    """Lightweight PnL for dashboard metric cards (cached via get_pnl).

    Avoids clients re-implementing aggregation; same TTL as full /api/pnl.
    """
    full = await get_pnl()
    return {'total': full.get('total', 0), '1d': full.get('1d', 0), '7d': full.get('7d', 0), '30d': full.get('30d', 0), 'week': full.get('week', 0), 'unrealized': full.get('unrealized', 0), 'funding': full.get('funding', 0), 'funding_scope': full.get('funding_scope', 'account'), 'economic_approx': full.get('economic_approx', 0), 'strategy_realized': full.get('strategy_realized', full.get('total', 0)), 'per_bot': full.get('per_bot', {}), 'active_bots': full.get('active_bots', []), 'source': full.get('source', ''), 'sticky': full.get('sticky', False), 'account_mode': full.get('account_mode'), 'trades_counted': full.get('trades_counted', 0), 'engine': full.get('engine'), 'pnl_epoch': full.get('pnl_epoch'), 'pnl_tz': full.get('pnl_tz') or full.get('timezone'), 'timezone': full.get('timezone'), 'day_basis': full.get('day_basis'), 'cached': True, 'cache_ttl_sec': _PNL_TTL}

def _active_bot_labels() -> set:
    """Human labels of bots currently running (for dashboard PnL cards)."""
    labels = set()
    try:
        if AI_ONLY_MODE:
            labels.add('AI Discretionary 1H')
            return labels
        if rotation and getattr(rotation, '_running', False):
            labels.add('Momentum')
        if impulse and getattr(impulse, '_running', False):
            labels.add('Impulse 1D')
        if validation and getattr(validation, '_running', False):
            labels.add('MACD+Donchian Validation')
        if ai_bot and getattr(ai_bot, '_running', False):
            labels.add('AI Discretionary 1H')
        if vwap_rev_bot and getattr(vwap_rev_bot, '_running', False):
            labels.add('VWAP Mean Reversion')
        if sm_tracker and getattr(sm_tracker, '_running', False):
            labels.add('Умные деньги')
    except Exception:
        pass
    return labels

@app.get('/api/pnl')
async def get_pnl(request: Request=None):
    """Cached dashboard PnL (single-flight). Prefer /api/pnl/summary for cards-only."""
    global _pnl_cache
    _mode = _account_mode()
    now_s = _time.time()
    if _pnl_cache and _pnl_cache.get('mode') == _mode and (now_s - _pnl_cache.get('ts', 0) < _PNL_TTL):
        out = dict(_pnl_cache['data'])
        out['account_mode'] = _mode
        return out
    async with _pnl_lock:
        now_s = _time.time()
        if _pnl_cache and _pnl_cache.get('mode') == _mode and (now_s - _pnl_cache.get('ts', 0) < _PNL_TTL):
            out = dict(_pnl_cache['data'])
            out['account_mode'] = _mode
            return out
        data = await _compute_pnl()
        if isinstance(data, dict):
            data = dict(data)
            data['account_mode'] = _mode
        try:
            prev = (_pnl_cache or {}).get('data') if (_pnl_cache or {}).get('mode') == _mode else None
            if isinstance(prev, dict) and isinstance(data, dict):
                prev_tot = abs(float(prev.get('total') or prev.get('strategy_realized') or 0))
                new_tot = abs(float(data.get('total') or data.get('strategy_realized') or 0))
                src = str(data.get('source') or '')
                if prev_tot > 0.01 and new_tot < 0.01 and (src in ('none', 'epoch_empty', '', 'error')):
                    kept = dict(prev)
                    kept['account_mode'] = _mode
                    kept['sticky'] = True
                    kept['sticky_from'] = src or 'empty'
                    kept['1d'] = data.get('1d', 0)
                    kept['week'] = data.get('week', 0)
                    kept['7d'] = data.get('7d', data.get('7d_rolling', 0))
                    kept['7d_rolling'] = data.get('7d_rolling', kept.get('7d'))
                    kept['week_start'] = data.get('week_start')
                    kept['week_basis'] = data.get('week_basis') or 'calendar_week_pnl_tz_monday'
                    data = kept
        except Exception:
            pass
        _pnl_cache = {'ts': _time.time(), 'data': data, 'mode': _mode}
        return dict(data)

async def _compute_pnl():
    """Single-source PnL via pnl_engine (epoch 2026-09-01, MSK calendar, AI bots only)."""
    global _pnl_cache, _exchange_sync_ts
    _mode = _account_mode()
    try:
        _exchange_sync_ts = 0
        data = await pnl_engine.compute(db, account_mode=_mode, ai_only=bool(AI_ONLY_MODE), sync_fn=sync_exchange_close_trades, reclassify_fn=getattr(db, 'reclassify_exchange_bot_labels', None))
    except Exception as e:
        print(f'[pnl] engine error: {e}', flush=True)
        import traceback
        traceback.print_exc()
        data = {'total': 0, '1d': 0, '7d': 0, '30d': 0, 'week': 0, 'unrealized': 0, 'per_bot': {'AI Discretionary 1H': 0, 'AI Scale-In 1H': 0}, 'active_bots': ['AI Discretionary 1H', 'AI Scale-In 1H'], 'source': 'error', 'pnl_epoch': PNL_EPOCH_ISO, 'error': str(e)}
    try:
        unreal = 0.0
        client = client_manager.get_client() if client_manager else None
        if client:
            pos = await client.get_positions(inst_type='SWAP')
            _pos_rows = [] if (isinstance(pos, dict) and pos.get('error')) else (
                (pos.get('data') or []) if isinstance(pos, dict) else (pos or []))
            for p in _pos_rows:
                try:
                    unreal += float(p.get('upl') or 0)
                except (TypeError, ValueError, AttributeError):
                    pass
        data['unrealized'] = round(unreal, 2)
        data['economic_approx'] = round(float(data.get('total') or 0) + unreal, 2)
    except Exception as e:
        print(f'[pnl] unrealized: {e}', flush=True)
        data.setdefault('unrealized', 0.0)
    data['account_mode'] = _mode
    try:
        if AI_ONLY_MODE:
            pb = data.get('per_bot') or {}
            s = float(pb.get('AI Discretionary 1H') or 0) + float(pb.get('AI Scale-In 1H') or 0)
            tot = float(data.get('total') or 0)
            if abs(s - tot) > 0.05:
                print(f'[pnl] INVARIANT FIX per_bot sum {s:.2f} != total {tot:.2f}', flush=True)
                data['total'] = round(s, 2)
                data['strategy_realized'] = round(s, 2)
    except Exception:
        pass
    return data

@app.get('/api/pnl/reconcile', dependencies=[Depends(require_admin)])
async def pnl_reconcile():
    """Compare dashboard strict PnL vs OKX bills (trade + funding) + positions upl.

    Helps detect attribution gaps (untagged), funding drift, and upl mismatch.
    """
    from datetime import datetime as dt, timezone as tz
    dash = await get_pnl()
    epoch = dash.get('pnl_epoch') or await get_pnl_epoch()
    okx_trade_pnl = 0.0
    okx_trade_n = 0
    okx_tagged_pnl = 0.0
    okx_untagged_pnl = 0.0
    try:
        for type_arg in ('2', None):
            try:
                if type_arg:
                    resp = await _okx_call(lambda c, t=type_arg: c.get_bills(inst_type='SWAP', type=t, limit=100))
                else:
                    resp = await _okx_call(lambda c: c.get_bills(inst_type='SWAP', limit=100))
                if resp.get('error'):
                    continue
                for b in resp.get('data') or []:
                    sub = str(b.get('subType') or '')
                    if sub and sub not in ('5', '6', '3', '4', '1', '2'):
                        continue
                    try:
                        ts = b.get('ts') or ''
                        if epoch and ts:
                            t_iso = dt.fromtimestamp(int(ts) / 1000, tz=tz.utc).strftime('%Y-%m-%dT%H:%M:%S')
                            if t_iso[:19] < str(epoch)[:19]:
                                continue
                    except Exception:
                        pass
                    try:
                        p = float(b.get('pnl') or 0)
                    except (TypeError, ValueError):
                        p = 0.0
                    if sub in ('5', '6') or p != 0:
                        okx_trade_pnl += p
                        okx_trade_n += 1
                        cid = str(b.get('clOrdId') or '').lower()
                        if cid.startswith(('rot', 'imp', 'ai', 'val')):
                            okx_tagged_pnl += p
                        else:
                            okx_untagged_pnl += p
                break
            except Exception:
                continue
    except Exception as e:
        print(f'[reconcile] bills: {e}', flush=True)
    upl = 0.0
    n_pos = 0
    try:
        pos_result = await _okx_call(lambda c: c.get_positions('SWAP'))
        if not pos_result.get('error'):
            for pos in pos_result.get('data') or []:
                try:
                    if abs(float(pos.get('pos') or 0)) <= 0:
                        continue
                    upl += float(pos.get('upl') or 0)
                    n_pos += 1
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    r_dash = float(dash.get('total') or 0)
    u_dash = float(dash.get('unrealized') or 0)
    f_dash = float(dash.get('funding') or 0)
    return {'ok': abs(u_dash - upl) < 0.5 and abs(r_dash - okx_tagged_pnl) < 5.0, 'pnl_epoch': epoch, 'dashboard': {'realized_tagged': r_dash, 'unrealized': u_dash, 'funding': f_dash, 'fees_informational': dash.get('fees'), 'economic_approx': dash.get('economic_approx'), 'skipped_untagged': dash.get('skipped_untagged'), 'per_bot': dash.get('per_bot'), 'source': dash.get('source')}, 'okx': {'trade_pnl_all': round(okx_trade_pnl, 4), 'trade_pnl_tagged_clord': round(okx_tagged_pnl, 4), 'trade_pnl_untagged': round(okx_untagged_pnl, 4), 'trade_bills_n': okx_trade_n, 'unrealized_upl': round(upl, 4), 'open_positions': n_pos, 'funding': f_dash}, 'diffs': {'realized_dash_minus_okx_tagged': round(r_dash - okx_tagged_pnl, 4), 'unrealized_dash_minus_okx': round(u_dash - upl, 4), 'okx_all_minus_dash': round(okx_trade_pnl - r_dash, 4)}, 'notes': ['Dashboard realized = strategy-tagged closed trades after pnl_epoch only.', 'OKX trade_pnl_all may include untagged/manual fills.', 'fillPnl usually already net of trading fees; fees on dashboard are informational.', 'Funding is separate (bills type=8), included in economic_approx.', 'Timestamps and epoch filter use UTC.']}

@app.get('/api/reports/summary')
async def reports_summary():
    """Single reporting snapshot for UI/export — same trade source as History/Dashboard.

    Fields:
    - realized / unrealized / fees / funding (funding best-effort from OKX bills type=8)
    - periods 1d/7d/30d/week aligned with /api/pnl
    - trade_count, wins, losses, win_rate from closed paired trades
    - source labels for transparency
    """
    pnl = await get_pnl()
    paired = await get_paired_trades(limit=5000)
    trades = [x for x in paired.get('trades') or [] if (x.get('reason') or '').lower() in ('closed', 'tp', 'sl', 'trail', 'breakeven', 'manual', '')]
    closed = []
    for x in paired.get('trades') or []:
        reason = (x.get('reason') or '').lower()
        if reason in ('open', 'tp1'):
            continue
        try:
            if float(x.get('pnl') or 0) == 0 and reason == 'open':
                continue
        except (TypeError, ValueError):
            pass
        closed.append(x)
    fees = 0.0
    wins = losses = 0
    for x in closed:
        try:
            fees += abs(float(x.get('fee') or 0))
        except (TypeError, ValueError):
            pass
        try:
            pval = float(x.get('pnl') or 0)
        except (TypeError, ValueError):
            pval = 0.0
        if pval > 0:
            wins += 1
        elif pval < 0:
            losses += 1
    n = wins + losses
    win_rate = round(wins / n * 100, 1) if n else 0.0
    funding = 0.0
    funding_source = 'none'
    try:
        resp = await _okx_call(lambda c: c.get_bills(inst_type='SWAP', type='8', limit=100))
        if not resp.get('error'):
            for b in resp.get('data') or []:
                try:
                    v = b.get('pnl')
                    if v is None or v == '':
                        v = b.get('balChg')
                    funding += float(v or 0)
                except (TypeError, ValueError):
                    continue
            funding_source = 'okx_bills_type8'
    except Exception as e:
        print(f'[reports] funding fetch: {e}', flush=True)
    net = float(pnl.get('total') or 0) + float(pnl.get('unrealized') or 0) + funding - 0.0
    return {'as_of': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), 'realized_total': pnl.get('total'), 'realized_1d': pnl.get('1d'), 'realized_7d': pnl.get('7d'), 'realized_30d': pnl.get('30d'), 'realized_week': pnl.get('week'), 'unrealized': pnl.get('unrealized'), 'fees_reported': round(fees, 4) if fees else pnl.get('fees'), 'funding': round(funding, 4), 'funding_source': funding_source, 'net_approx': round(float(pnl.get('total') or 0) + float(pnl.get('unrealized') or 0) + funding, 2), 'per_bot': pnl.get('per_bot') or {}, 'trades_closed': len(closed), 'wins': wins, 'losses': losses, 'win_rate_pct': win_rate, 'pnl_source': pnl.get('source'), 'note': 'PnL matches History paired trades; funding is OKX bills type=8 (best-effort, last page).'}
_bot_stats_cache = {'ts': 0, 'data': {}, 'mode': ''}
_BOT_STATS_TTL = 15

async def _apply_history_kpi(status: dict, bot_label: str) -> dict:
    """Overlay KPI from the SAME pnl_engine source as dashboard cards."""
    status = dict(status or {})
    try:
        # Use cached PnL if fresh, otherwise compute (single-flight)
        _mode = _account_mode()
        now_s = _time.time()
        if _pnl_cache and _pnl_cache.get('mode') == _mode and (now_s - _pnl_cache.get('ts', 0) < _PNL_TTL):
            dash = dict(_pnl_cache['data'])
        else:
            dash = await _compute_pnl()
        per = (dash or {}).get('per_bot') or {}
        mode = str((dash or {}).get('account_mode') or _account_mode()).lower()
        val = float(per.get(bot_label) or 0)
        status['total_pnl'] = round(val, 2)
        status['lifetime_pnl'] = round(val, 2)
        status['total_pnl_source'] = 'pnl_engine'
        status['kpi_from_history'] = True
        status['pnl_epoch'] = (dash or {}).get('pnl_epoch')
        status['account_mode'] = mode
        day_val = float((dash or {}).get('1d') or 0)
        status['session_pnl'] = round(day_val, 2)
        try:
            all_stats = await _bot_history_stats()
            stats = all_stats.get(bot_label) or {}
            if mode == 'live':
                tc = int((dash or {}).get('trades_counted') or 0)
                status['total_trades'] = tc if bot_label == 'AI Discretionary 1H' else 0
                status['lifetime_trades'] = status['total_trades']
                status['wins'] = 0
                status['losses'] = 0
                status['win_rate'] = 0
            else:
                status['total_trades'] = stats.get('total_trades', status.get('total_trades', 0))
                status['lifetime_trades'] = status['total_trades']
                status['wins'] = stats.get('wins', status.get('wins', 0))
                status['losses'] = stats.get('losses', status.get('losses', 0))
                status['win_rate'] = stats.get('win_rate', status.get('win_rate', 0))
        except Exception:
            if mode == 'live':
                status['total_trades'] = 0
                status['lifetime_trades'] = 0
    except Exception as e:
        print(f'[kpi] {bot_label}: {e}', flush=True)
        status['kpi_from_history'] = False
    return status

async def _bot_history_stats() -> dict:
    """Per-bot KPI from exchange_close_trades — deterministic, no recomputation.

    Always returns entries for known strategy cards (zeros after pnl_epoch reset).
    """
    now_s = _time.time()
    current_mode = _account_mode()
    if now_s - _bot_stats_cache['ts'] < _BOT_STATS_TTL and _bot_stats_cache.get('mode') == current_mode:
        return _bot_stats_cache['data']
    try:
        await sync_exchange_close_trades()
    except Exception as e:
        print(f'[bot_stats] sync_exchange_close_trades: {e}', flush=True)
    KNOWN = ('Momentum', 'Impulse 1D', 'MACD+Donchian Validation', 'AI Discretionary 1H', 'Order Book Scalp', 'Умные деньги')
    stats = {name: {'total_pnl': 0.0, 'total_trades': 0, 'wins': 0, 'losses': 0, 'win_rate': 0.0, 'total_pnl_source': 'exchange_close_trades'} for name in KNOWN}
    try:
        from datetime import datetime as dt, timezone as tz
        epoch = await get_pnl_epoch()
        epoch_ms = 0
        if epoch:
            try:
                epoch_ms = int(dt.fromisoformat(epoch).replace(tzinfo=tz.utc).timestamp() * 1000)
            except Exception:
                pass
        rows = await db.get_exchange_pnl_timebucket(account_mode=current_mode, epoch_ms=epoch_ms)
        counts = {}
        pnl_sum = {}
        if not rows:
            try:
                resp = await get_paired_trades(limit=5000)
                trades = resp.get('trades', []) or []
                for tr in trades:
                    bot = (tr.get('bot') or _db_bot_name(tr.get('bot_id') or '') or '').strip()
                    if bot not in KNOWN:
                        continue
                    reason = (tr.get('reason') or '').lower()
                    if reason in ('open', 'add'):
                        continue
                    if not _trade_after_epoch(tr, epoch):
                        continue
                    try:
                        pnl_val = float(tr.get('pnl', 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    c = counts.setdefault(bot, {'total_trades': 0, 'wins': 0, 'losses': 0})
                    c['total_trades'] += 1
                    if pnl_val > 0:
                        c['wins'] += 1
                    elif pnl_val < 0:
                        c['losses'] += 1
                    pnl_sum[bot] = pnl_sum.get(bot, 0.0) + pnl_val
                if pnl_sum:
                    print(f'[bot_stats] fallback paired: bots={list(pnl_sum.keys())}', flush=True)
            except Exception as e:
                print(f'[bot_stats] fallback error: {e}', flush=True)
        for r in rows:
            bot = (r.get('bot_label') or '').strip()
            if not bot:
                continue
            try:
                pnl = float(r.get('pnl', 0) or 0)
            except (TypeError, ValueError):
                continue
            c = counts.setdefault(bot, {'total_trades': 0, 'wins': 0, 'losses': 0})
            c['total_trades'] += 1
            if pnl > 0:
                c['wins'] += 1
            elif pnl < 0:
                c['losses'] += 1
            pnl_sum[bot] = pnl_sum.get(bot, 0.0) + pnl
        for bot in KNOWN:
            total_pnl = pnl_sum.get(bot, 0.0)
            c = counts.get(bot) or {'total_trades': 0, 'wins': 0, 'losses': 0}
            total = int(c['total_trades'])
            stats[bot] = {'total_pnl': round(total_pnl, 2), 'total_trades': total, 'wins': int(c.get('wins', 0)), 'losses': int(c.get('losses', 0)), 'win_rate': round(c['wins'] / total * 100, 1) if total else 0.0, 'total_pnl_source': 'exchange_close_trades'}
    except Exception as e:
        print(f'[bot_stats] error: {e}', flush=True)
    _bot_stats_cache['ts'] = now_s
    _bot_stats_cache['data'] = stats
    _bot_stats_cache['mode'] = current_mode
    return stats

@app.get('/api/trades')
async def get_all_trades(limit: int=100):
    """Trades for current account mode only (demo XOR live)."""
    mode = _account_mode()
    out = []
    logs = []
    if ai_bot and getattr(ai_bot, '_trade_log', None):
        logs.extend(ai_bot._trade_log)
    if rotation and getattr(rotation, '_trade_log', None):
        logs.extend(rotation._trade_log)
    if impulse and getattr(impulse, '_trade_log', None):
        logs.extend(impulse._trade_log)
    for tr in logs:
        if _trade_matches_mode(tr, mode):
            out.append(tr)
    return {'trades': out[-limit:], 'account_mode': mode}
_paired_cache: dict = {}
_paired_lock = asyncio.Lock()
_PAIRED_TTL = 20
_pnl_cache: dict = {}
_pnl_lock = asyncio.Lock()
_PNL_TTL = 30

_live_okx_cache: dict = {'ts': 0.0, 'trades': []}
_LIVE_OKX_TTL = 60.0

async def _fetch_live_okx_trades_direct() -> list:
    """Fetch live OKX trades directly via live_manager (no _okx_call_account).

    This avoids corrupting client_manager which happens when _okx_call_account
    is called with mode='live' from a demo server.
    """
    now = _time.time()
    if _live_okx_cache['trades'] and now - _live_okx_cache['ts'] < _LIVE_OKX_TTL:
        return list(_live_okx_cache['trades'])
    lc = None
    try:
        lc = live_manager.get_client() if live_manager else None
    except Exception:
        lc = None
    if not lc:
        return []
    bills_raw = []
    fills_raw = []
    try:
        for attempt in range(3):
            try:
                resp = await lc.get_bills(inst_type='SWAP', type='2', limit=100)
                if resp and not resp.get('error'):
                    bills_raw = resp.get('data', []) or []
                    break
                if resp and '429' in str(resp.get('message', '')):
                    await asyncio.sleep(1.0 + attempt)
                    continue
                break
            except Exception as e:
                print(f'[live-okx] bills attempt {attempt}: {e}', flush=True)
                break
    except Exception as e:
        print(f'[live-okx] bills error: {e}', flush=True)
    try:
        for attempt in range(3):
            try:
                resp = await lc.get_fills_history(inst_type='SWAP', limit=100)
                if resp and not resp.get('error'):
                    fills_raw = resp.get('data', []) or []
                    break
                if resp and '429' in str(resp.get('message', '')):
                    await asyncio.sleep(1.0 + attempt)
                    continue
                break
            except Exception as e:
                print(f'[live-okx] fills attempt {attempt}: {e}', flush=True)
                break
    except Exception as e:
        print(f'[live-okx] fills error: {e}', flush=True)
    for b in bills_raw:
        if isinstance(b, dict):
            b['account_mode'] = 'live'
            b['_from_okx'] = True
    for f in fills_raw:
        if isinstance(f, dict):
            f['account_mode'] = 'live'
            f['_from_okx'] = True
    paired = []
    if bills_raw:
        try:
            paired = _pair_bills(bills_raw)
        except Exception as e:
            print(f'[live-okx] pair_bills error: {e}', flush=True)
    if not paired and fills_raw:
        try:
            paired = await _pair_fills(fills_raw)
        except Exception as e:
            print(f'[live-okx] pair_fills error: {e}', flush=True)
    for t in paired:
        if isinstance(t, dict):
            t['account_mode'] = 'live'
            t['account_key'] = 'live'
    print(f'[live-okx] bills={len(bills_raw)} fills={len(fills_raw)} paired={len(paired)}', flush=True)
    _live_okx_cache['ts'] = now
    _live_okx_cache['trades'] = paired
    return paired

@app.get('/api/trades/paired')
async def get_paired_trades(limit: int=500, begin: str=None, end: str=None):
    """Paired entry+exit trades — all bots, all time, sourced from the DB
    (persisted) plus live in-memory logs. Fallback to OKX fills only when
    nothing is stored yet.

    Cached for _PAIRED_TTL with single-flight: /api/pnl, /api/trades/paired
    and the bot-status stats all recompute the same expensive pipeline
    (OKX bills + fills + DB reads), which previously spiked to 5-14s on every
    cache expiry and stalled the dashboard."""
    global _paired_cache
    now_s = _time.time()
    _mode = _account_mode()
    if _paired_cache and _paired_cache.get('mode') == _mode and (now_s - _paired_cache['ts'] < _PAIRED_TTL):
        trades = _paired_cache['data']
        return {'trades': trades[:limit], 'debug': dict(_paired_cache['debug']), 'account_mode': _mode}
    async with _paired_lock:
        now_s = _time.time()
        if _paired_cache and _paired_cache.get('mode') == _mode and (now_s - _paired_cache['ts'] < _PAIRED_TTL):
            trades = _paired_cache['data']
            return {'trades': trades[:limit], 'debug': dict(_paired_cache['debug']), 'account_mode': _mode}
        resp = await _get_paired_trades_impl(limit=5000, begin=begin, end=end, mode=_mode)
        trades = resp.get('trades', [])
        # When in demo mode, also collect live mirror trades from DB + in-memory
        # _trade_log so that closed live positions appear in the trades card.
        # We read directly instead of calling _get_paired_trades_impl(mode='live')
        # because that function reinitialises client_manager and corrupts demo state.
        if _mode == 'demo':
            _live_trades = []
            _live_positions_count = 0
            # 1) Check if live mirror is active (has open positions or ever had trades)
            _lc = None
            if live_manager:
                try:
                    _lc = live_manager.get_client()
                except Exception:
                    _lc = None
            _live_connected = bool(_lc and not getattr(_lc, 'demo', True)
                                   and getattr(_lc, 'has_credentials', lambda: False)())
            if _live_connected and ai_bot:
                _live_positions_count = len(getattr(ai_bot, '_live_positions', {}))
            # 2) DB trades with account_mode='live'
            if db:
                try:
                    _ai_bid = AI_BOT_ID
                    _live_db = await db.get_trades_multi_bot(
                        [_ai_bid, f'{_ai_bid}_live'],
                        limit=500, account_mode='live',
                    )
                    print(f'[trades/paired] live DB rows: {len(_live_db or [])} '
                          f'(bot_ids={[_ai_bid, f"{_ai_bid}_live"]})', flush=True)
                    for t in (_live_db or []):
                        bid = t.get('bot_id') or ''
                        row_mode = (t.get('account_mode') or '').strip().lower()
                        if row_mode != 'live':
                            continue
                        px = float(t.get('px', 0) or 0)
                        pnl_val = float(t.get('pnl', 0) or 0)
                        inst = t.get('inst_id', '')
                        _live_trades.append({
                            'time': t.get('timestamp', ''), 'side': t.get('side', ''),
                            'symbol': inst, 'inst_id': inst,
                            'ord_id': str(t.get('ord_id', '') or '').strip(),
                            'entry_price': px, 'exit_price': None,
                            'pnl': pnl_val,
                            'reason': t.get('state', '') or ('open' if t.get('state') != 'filled' else 'closed'),
                            'pos_side': 'long' if t.get('side') == 'buy' else 'short',
                            'signal_id': t.get('signal_id', 0),
                            'bot_id': bid,
                            'account_mode': 'live', 'account_key': 'live',
                            'bot': 'AI Discretionary 1H',
                        })
                except Exception as _e:
                    print(f'[trades/paired] live DB read error: {_e}', flush=True)
            # 3) In-memory _trade_log from all bots (live mirror entries)
            _mem_live = 0
            for _bot_key, _bot_obj in [('ai', ai_bot), ('rotation', rotation), ('impulse', impulse), ('validation', validation)]:
                if not _bot_obj or not getattr(_bot_obj, '_trade_log', None):
                    continue
                for t in _bot_obj._trade_log:
                    if (t.get('account_mode') or '').strip().lower() != 'live':
                        continue
                    _mem_live += 1
                    _live_trades.append({
                        'time': t.get('time', ''), 'side': t.get('side', ''),
                        'symbol': t.get('symbol', ''),
                        'inst_id': t.get('symbol', '') or t.get('inst_id', ''),
                        'ord_id': str(t.get('ord_id', '') or '').strip(),
                        'entry_price': t.get('entry_price') or t.get('entry', 0),
                        'exit_price': t.get('exit_price', None),
                        'pnl': t.get('pnl', 0),
                        'reason': t.get('reason', 'open'),
                        'pos_side': t.get('pos_side', 'long'),
                        'signal_id': t.get('signal_id', 0),
                        'bot_id': getattr(_bot_obj, 'BOT_ID', _bot_key),
                        'account_mode': 'live', 'account_key': 'live',
                        'bot': t.get('bot') or 'AI Discretionary 1H',
                    })
            # 4) Direct OKX fetch via live_manager (safe — doesn't corrupt client_manager)
            _okx_live = []
            try:
                _okx_live = await _fetch_live_okx_trades_direct()
            except Exception as _e:
                print(f'[trades/paired] live OKX direct fetch error: {_e}', flush=True)
            print(f'[trades/paired] live mirror: connected={_live_connected} '
                  f'open_pos={_live_positions_count} mem_log={_mem_live} '
                  f'okx_direct={len(_okx_live)} db={len(_live_trades)}', flush=True)
            # Dedup: OKX direct is authoritative; DB/memory fill gaps
            _okx_ord_ids = {str(t.get('ord_id', '')).strip() for t in _okx_live if t.get('ord_id')}
            for t in _live_trades:
                oid = str(t.get('ord_id', '')).strip()
                if oid and oid in _okx_ord_ids:
                    continue
                _okx_live.append(t)
            if _okx_live:
                trades = list(trades) + _okx_live
                print(f'[trades/paired] merged {len(_okx_live)} live trades into {len(resp.get("trades", []))} demo trades', flush=True)
        _tagged = []
        for _tr in trades:
            if not isinstance(_tr, dict):
                continue
            _m = str(_tr.get('account_mode') or _tr.get('mode') or '').strip().lower()
            if _mode == 'live':
                if _m != 'live':
                    if not _m:
                        _tr = {**_tr, 'account_mode': 'live'}
                    else:
                        continue
                else:
                    _tr = {**_tr, 'account_mode': 'live'}
            else:
                if _m not in ('demo', 'live'):
                    _tr = {**_tr, 'account_mode': 'demo'}
            _tagged.append(_tr)
        _tagged.sort(key=lambda t: (t.get('exit_time') or t.get('entry_time') or t.get('time') or ''), reverse=True)
        trades = _tagged
        resp = {**resp, 'trades': trades, 'account_mode': _mode}
        if trades:
            _paired_cache = {'ts': _time.time(), 'data': trades, 'debug': resp.get('debug', {}), 'mode': _mode}
        return {'trades': trades[:limit], 'debug': resp.get('debug', {}), 'account_mode': _mode}
_warm_task: Optional[asyncio.Task] = None
_WARM_INTERVAL = 45.0

async def _warm_dashboard_caches() -> None:
    """Keep dashboard caches warm without saturating the free-tier instance."""
    while True:
        try:
            await asyncio.sleep(_WARM_INTERVAL)
            try:
                await asyncio.wait_for(get_paired_trades(limit=300), timeout=25.0)
            except asyncio.TimeoutError:
                print('[warm] paired cache timed out (25s) — skip', flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f'[warm] paired cache: {e}', flush=True)

async def _get_paired_trades_impl(limit: int=500, begin: str=None, end: str=None, mode: str=None):
    """Paired entry+exit trades for one account mode only (demo XOR live).

    Sourced from DB + in-memory logs; OKX fills from the matching client.
    """
    mode = (mode or _account_mode()).lower()
    if mode not in ('demo', 'live'):
        mode = 'demo'
    raw = []
    bot_ids = [ROT_BOT_ID, MOM_BOT_ID, IMP_BOT_ID, VAL_BOT_ID, AI_BOT_ID]
    inst_last_bot: dict = {}
    _bot_name_map = {ROT_BOT_ID: 'Momentum', MOM_BOT_ID: 'Momentum', IMP_BOT_ID: 'Impulse 1D', VAL_BOT_ID: 'MACD+Donchian Validation', AI_BOT_ID: 'AI Discretionary 1H', 'rotation': 'Momentum', 'momentum': 'Momentum', 'impulse': 'Impulse 1D', 'validation': 'MACD+Donchian Validation', 'ai': 'AI Discretionary 1H'}
    for _bid_name, _bot_obj in [('rotation', rotation), ('impulse', impulse), ('validation', validation), ('ai', ai_bot)]:
        if _bot_obj and hasattr(_bot_obj, '_trade_log') and _bot_obj._trade_log:
            for _t in reversed(_bot_obj._trade_log):
                _i = _t.get('symbol') or _t.get('inst_id') or ''
                if _i and _i not in inst_last_bot:
                    inst_last_bot[_i] = _bid_name
    if db:
        try:
            _ib_rows, _all_rows = await asyncio.gather(db._fetchall("SELECT inst_id, bot_id, timestamp FROM trades WHERE bot_id IS NOT NULL AND bot_id != '' AND account_mode = $1 ORDER BY timestamp DESC LIMIT 2000", (mode,)), db.get_trades_multi_bot(bot_ids, limit=5000, account_mode=mode))
            for _r in _ib_rows:
                _i = _r.get('inst_id') or ''
                if _i and _i not in inst_last_bot:
                    inst_last_bot[_i] = str(_r.get('bot_id') or '').split(':')[0]
            for t in _all_rows:
                bid = t.get('bot_id') or ''
                row_mode = (t.get('account_mode') or '').strip().lower()
                if row_mode and row_mode != mode:
                    continue
                if not row_mode and mode == 'live':
                    continue
                px = float(t.get('px', 0) or 0)
                pnl = float(t.get('pnl', 0) or 0)
                inst = t.get('inst_id', '')
                raw.append({'time': t.get('timestamp', ''), 'side': t.get('side', ''), 'symbol': inst, 'inst_id': inst, 'ord_id': str(t.get('ord_id', '') or '').strip(), 'entry_price': px, 'exit_price': None, 'pnl': pnl, 'reason': 'open' if pnl == 0 else 'closed', 'pos_side': 'long' if t.get('side') == 'buy' else 'short', 'signal_id': t.get('signal_id', 0), 'bot_id': bid, 'account_mode': row_mode or mode, 'account_key': t.get('account_key') or ('showcase' if mode == 'demo' else 'live')})
        except Exception as e:
            print(f'[trades/paired] DB read error: {e}', flush=True)
    live_bots = [('rotation', rotation), ('impulse', impulse), ('validation', validation), ('ai', ai_bot)]
    live_names = {ROT_BOT_ID: 'Momentum', IMP_BOT_ID: 'Impulse 1D', VAL_BOT_ID: 'MACD+Donchian Validation', AI_BOT_ID: 'AI Discretionary 1H'}
    for key, bot in live_bots:
        if bot and bot._trade_log:
            for t in bot._trade_log:
                if not _trade_matches_mode(t, mode):
                    continue
                row = {'time': t.get('time', ''), 'side': t.get('side', ''), 'symbol': t.get('symbol', ''), 'inst_id': t.get('symbol', '') or t.get('inst_id', ''), 'ord_id': str(t.get('ord_id', '') or '').strip(), 'entry_price': t.get('entry_price') or t.get('entry', 0), 'exit_price': t.get('exit_price', None), 'pnl': t.get('pnl', 0), 'reason': t.get('reason', 'open'), 'pos_side': t.get('pos_side', 'long'), 'signal_id': t.get('signal_id', 0), 'bot_id': getattr(bot, 'BOT_ID', key), 'account_mode': t.get('account_mode') or mode, 'account_key': t.get('account_key') or ('showcase' if mode == 'demo' else 'live')}
                if t.get('bot'):
                    row['bot'] = t.get('bot')
                raw.append(row)
    okx_rows = []
    okx_ord_ids = set()
    okx_close_key = set()
    okx_open_keys = {}
    bill_by_ord = {}
    flag_raw = bool(raw)
    bills = []
    raw_fills = []
    val_ord_ids = set()
    ord_to_bot = {}
    try:

        async def _safe_bills():
            return await _fetch_all_trade_bills(mode=mode)

        async def _safe_fills():
            return await _fetch_okx_fills(limit=1000, mode=mode)

        async def _safe_val_ord():
            if not db:
                return []
            try:
                if db._pg_mode:
                    return await db._fetchall("SELECT ord_id FROM trades WHERE bot_id = $1 AND ord_id IS NOT NULL AND ord_id != '' AND account_mode = $2", (VAL_BOT_ID, mode))
                else:
                    return await db._fetchall("SELECT ord_id FROM trades WHERE bot_id = ? AND ord_id IS NOT NULL AND ord_id != '' AND account_mode = ?", (VAL_BOT_ID, mode))
            except Exception:
                return []

        async def _safe_ord_bot():
            if not db:
                return []
            try:
                if db._pg_mode:
                    return await db._fetchall("SELECT bot_id, ord_id FROM trades WHERE ord_id IS NOT NULL AND ord_id != '' AND account_mode = $1", (mode,))
                else:
                    return await db._fetchall("SELECT bot_id, ord_id FROM trades WHERE ord_id IS NOT NULL AND ord_id != '' AND account_mode = ?", (mode,))
            except Exception:
                return []
        _bills_r, _fills_r, _val_r, _obot_r = await asyncio.gather(_safe_bills(), _safe_fills(), _safe_val_ord(), _safe_ord_bot(), return_exceptions=True)
        bills = _bills_r if isinstance(_bills_r, list) else []
        raw_fills = _fills_r if isinstance(_fills_r, list) else []
        val_ord_ids = {str(r['ord_id']).strip() for r in (_val_r if isinstance(_val_r, list) else []) if r.get('ord_id')}
        ord_to_bot = {str(r['ord_id']).strip(): str(r['bot_id']).split(':')[0] for r in (_obot_r if isinstance(_obot_r, list) else []) if r.get('ord_id')}
        for b in bills:
            bid = str(b.get('ordId', '')).strip()
            if not bid:
                continue
            try:
                bp = float(b.get('pnl') or 0)
            except (TypeError, ValueError):
                bp = 0.0
            try:
                bf = abs(float(b.get('fee') or 0))
            except (TypeError, ValueError):
                bf = 0.0
            prev = bill_by_ord.get(bid)
            if prev is None:
                bill_by_ord[bid] = {'pnl': bp, 'fee': bf, 'ts': b.get('ts', ''), 'clOrdId': str(b.get('clOrdId', '') or '').strip()}
            else:
                prev['pnl'] += bp
                prev['fee'] += bf
                prev['ts'] = b.get('ts', prev['ts'])
                if not prev['clOrdId']:
                    prev['clOrdId'] = str(b.get('clOrdId', '') or '').strip()
    except Exception as e:
        print(f'[trades/paired] parallel fetch error: {e}', flush=True)
    raw_fills = [f for f in raw_fills if not str(f.get('clOrdId', '') or '').startswith('val')]
    if val_ord_ids:
        raw_fills = [f for f in raw_fills if str(f.get('ordId', '')).strip() not in val_ord_ids]
    fill_clord = {str(f.get('ordId', '')).strip(): str(f.get('clOrdId', '') or '').strip() for f in raw_fills}
    inst_entry_bot: dict = {}
    _clord_to_bot = {'rot': 'Momentum', 'imp': 'Impulse 1D', 'ai': 'AI Discretionary 1H', 'val': 'MACD+Donchian Validation', 'scl': 'Order Book Scalp', 'scalp': 'Order Book Scalp', 'vwap': 'VWAP Mean Reversion'}
    for _f in reversed(raw_fills):
        _sub = str(_f.get('subType') or '')
        if _sub not in ('3', '4'):
            continue
        _cid = str(_f.get('clOrdId', '') or '').strip().lower()
        _fi = _f.get('instId') or _f.get('inst_id') or ''
        if not _fi or not _cid:
            continue
        _fside = str(_f.get('side') or '').lower()
        _entry_side = 'short' if _fside == 'sell' else 'long' if _fside == 'buy' else ''
        if not _entry_side:
            continue
        _key = (_fi, _entry_side)
        for _prefix, _bname in _clord_to_bot.items():
            if _cid.startswith(_prefix) and _key not in inst_entry_bot:
                inst_entry_bot[_key] = _bname
                break

    def _okx_bot(ord_id: str, *, entry_ord_id: str='') -> str:
        """Map OKX ordId → strategy label via clOrdId prefix or DB trades.bot_id.

        Ownership follows the ENTRY order: the bot that OPENED the position is
        the rightful owner, even if the closing order carries a different bot's
        clOrdId (e.g. Momentum adopted an AI position and later closed it with a
        rot... clOrdId). The close-order clOrdId is only used when no entry
        clOrdId is known (e.g. manually-opened positions closed by a bot)."""

        def _match(prefix: str) -> str:
            m = {'rot': 'Momentum', 'imp': 'Impulse 1D', 'ai': 'AI Discretionary 1H', 'val': 'MACD+Donchian Validation', 'scl': 'Order Book Scalp', 'scalp': 'Order Book Scalp', 'vwap': 'VWAP Mean Reversion'}
            for k, v in m.items():
                if prefix.startswith(k):
                    return v
            return ''
        if entry_ord_id:
            ecid = (fill_clord.get(entry_ord_id, '') or bill_by_ord.get(entry_ord_id, {}).get('clOrdId', '') or '').strip().lower()
            if ecid:
                b = _match(ecid)
                if b:
                    return b
        cid = (fill_clord.get(ord_id, '') or bill_by_ord.get(ord_id, {}).get('clOrdId', '') or '').strip().lower()
        if cid:
            b = _match(cid)
            if b:
                return b
        b = _db_bot_name(ord_to_bot.get(ord_id, ''))
        return b or ''
    pair_bills_err = ''
    try:
        fills_paired = _pair_bills(bills) if bills else await _pair_fills(raw_fills)
        for _fp in fills_paired:
            if isinstance(_fp, dict):
                _fp['account_mode'] = _fp.get('account_mode') or mode
                _fp['_from_okx'] = True
        for t in fills_paired:
            inst = t.get('inst_id', '') or t.get('symbol', '')
            is_open = t.get('reason') == 'open'
            ord_id = str(t.get('ord_id', '') or '').strip()
            bill = bill_by_ord.get(ord_id)
            if bill and t.get('source') != 'okx_bills':
                t = dict(t)
                t['pnl'] = bill['pnl']
                t['fee'] = str(bill['fee'])
            if is_open:
                okx_open_keys.setdefault(inst, set()).add(ord_id)
            else:
                okx_ord_ids.add(ord_id)
                try:
                    okx_close_key.add((inst, (t.get('time') or '')[:16]))
                except Exception:
                    pass
            entry_px = t.get('entry', 0) or t.get('entry_price', 0)
            exit_px = t.get('exit_price', 0)
            entry_ord = str(t.get('entry_ord_id', '') or '').strip()
            okx_rows.append({'time': t.get('time', ''), 'entry_time': t.get('entry_time', ''), 'exit_time': t.get('time', '') if not is_open else None, 'side': 'buy' if t.get('pos_side') == 'long' else 'sell', 'symbol': inst, 'inst_id': inst, 'ord_id': ord_id, 'entry': entry_px, 'entry_px': entry_px, 'exit_price': exit_px, 'exit_px': exit_px, 'pnl': t.get('pnl', 0) if not is_open else None, 'reason': t.get('reason', ''), 'pos_side': t.get('pos_side', 'long'), 'signal_id': t.get('signal_id', 0) or ord_id, 'bot': _okx_bot(ord_id, entry_ord_id=entry_ord), 'fee': t.get('fee', '0')})
    except Exception as e:
        import traceback
        print(f'[trades/paired] OKX pairing error: {e}', flush=True)
        traceback.print_exc()
        pair_bills_err = f'{type(e).__name__}: {e}'

    def _dedup_key(t):
        oid = str(t.get('ord_id') or '').strip()
        ts = t.get('exit_time') or t.get('entry_time') or t.get('time') or ''
        if oid:
            return ('oid', oid)
        try:
            ts_floored = ts[:16]
        except Exception:
            ts_floored = ts
        reason = (t.get('reason') or '').lower()
        if reason in ('open', 'add') or t.get('pnl') is None:
            return ('open', t.get('inst_id') or '', ts_floored)
        pnl = t.get('pnl')
        try:
            pnl_r = round(float(pnl or 0), 4)
        except (TypeError, ValueError):
            pnl_r = 0.0
        return ('fb', t.get('inst_id') or '', pnl_r, ts_floored)
    seen = set()
    dedup = []
    for t in okx_rows:
        key = _dedup_key(t)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(t)
    for t in dedup:
        if t.get('bot'):
            continue
        try:
            tagged = _tag_trade_bot(t)
            if tagged:
                t['bot'] = tagged
                continue
        except Exception:
            pass
        oid = str(t.get('ord_id') or '').strip()
        if oid and oid in ord_to_bot:
            t['bot'] = _db_bot_name(ord_to_bot[oid]) or t.get('bot') or ''
            continue
        inst = str(t.get('inst_id') or t.get('symbol') or '').strip()
        if inst and inst in inst_entry_bot:
            t['bot'] = inst_entry_bot[inst] or t.get('bot') or ''
            continue
        if inst and inst in inst_last_bot:
            _raw_name = inst_last_bot[inst]
            t['bot'] = _bot_name_map.get(_raw_name, _raw_name) or t.get('bot') or ''
            continue
    if flag_raw:
        raw.sort(key=lambda t: (t.get('time') or '', t.get('side') or ''))
        legacy = []
        for t in raw:
            inst = t.get('inst_id') or t.get('symbol', '')
            reason = (t.get('reason') or '').lower()
            oid = str(t.get('ord_id', '') or '').strip()
            if oid and oid in okx_ord_ids:
                continue
            try:
                close_key = (inst, (t.get('time') or '')[:16])
            except Exception:
                close_key = None
            if close_key and close_key in okx_close_key:
                continue
            is_entry = reason in ('open', 'add') or (t.get('pnl') in (None, 0) and t.get('side') == 'buy')
            if is_entry:
                if okx_open_keys.get(inst):
                    continue
                legacy.append(t)
                continue
            pnl0 = float(t.get('pnl', 0) or 0)
            if pnl0 == 0:
                continue
            legacy.append(t)
        open_map = {}
        paired = []
        for t in legacy:
            inst = t.get('inst_id') or t.get('symbol', '')
            pnl = float(t.get('pnl', 0) or 0)
            reason = (t.get('reason') or '').lower()
            is_entry = reason in ('open', 'add') or (pnl == 0 and t.get('side') == 'buy')
            if is_entry:
                open_map.setdefault(inst, []).append(t)
            elif pnl != 0 or reason in ('closed', 'close', 'manual_close', 'exchange_stop', 'rotation_exit', 'stop', 'tp', 'trail', 'breakeven', 'roi'):
                entries = open_map.get(inst, [])
                entry = entries.pop(0) if entries else None
                bot_name = _db_bot_name(t.get('bot_id', '')) or 'Momentum'
                paired.append({'time': t.get('time', ''), 'entry_time': entry.get('time', '') if entry else t.get('time', ''), 'exit_time': t.get('time', ''), 'side': 'buy' if t.get('pos_side') == 'long' or (entry and entry.get('pos_side') == 'long') else 'sell', 'symbol': t.get('symbol', ''), 'inst_id': t.get('inst_id', '') or t.get('symbol', ''), 'ord_id': str(t.get('ord_id', '') or '').strip(), 'entry': entry.get('entry_price', 0) if entry else 0, 'entry_px': entry.get('entry_price', 0) if entry else 0, 'exit_price': t.get('exit_price', 0) or float(t.get('entry_price', 0) or 0), 'exit_px': t.get('exit_price', 0) or float(t.get('entry_price', 0) or 0), 'pnl': pnl, 'reason': reason, 'pos_side': t.get('pos_side', 'long'), 'signal_id': t.get('signal_id', ''), 'bot': bot_name})
        for inst, entries in open_map.items():
            for entry in entries:
                bot_name = _db_bot_name(entry.get('bot_id', '')) or 'Momentum'
                paired.append({'time': entry.get('time', ''), 'entry_time': entry.get('time', ''), 'exit_time': None, 'side': 'buy' if entry.get('pos_side') == 'long' else 'sell', 'symbol': entry.get('symbol', ''), 'inst_id': entry.get('inst_id', '') or entry.get('symbol', ''), 'ord_id': str(entry.get('ord_id', '') or '').strip(), 'entry': entry.get('entry_price', 0), 'entry_px': entry.get('entry_price', 0), 'exit_price': None, 'exit_px': None, 'pnl': None, 'reason': 'open', 'pos_side': entry.get('pos_side', 'long'), 'signal_id': entry.get('signal_id', ''), 'bot': bot_name})
        paired.sort(key=lambda t: t.get('exit_time') or t.get('entry_time') or '', reverse=True)
        for t in paired:
            key = _dedup_key(t)
            if key in seen:
                continue
            seen.add(key)
            dedup.append(t)
    try:
        for t in dedup:
            inst = str(t.get('inst_id') or t.get('symbol') or '').strip()
            if not inst:
                continue
            pside = str(t.get('pos_side') or 'long').strip().lower()
            opener = inst_entry_bot.get((inst, pside), '')
            if not opener:
                opener = inst_entry_bot.get(inst, '')
            if not opener:
                continue
            cur_bot = str(t.get('bot') or '')
            if not cur_bot:
                t['bot'] = opener
    except Exception as e:
        print(f'[trades/paired] entry-owner override error: {e}', flush=True)
    try:
        _overrides = await trade_attr.load_overrides(db)
        trade_attr.apply_attribution(dedup, entry_owner=inst_entry_bot if isinstance(inst_entry_bot, dict) else {}, overrides=_overrides)
    except Exception as e:
        print(f'[trades/paired] attribution error: {e}', flush=True)
    try:
        for t in dedup:
            if (t.get('reason') or '').lower() != 'closed':
                continue
            oid = str(t.get('ord_id') or '').strip()
            bill = bill_by_ord.get(oid) if oid else None
            if bill:
                t['pnl'] = bill['pnl']
                t['fee'] = str(bill['fee'])
    except Exception:
        pass
    dedup.sort(key=lambda t: t.get('exit_time') or t.get('entry_time') or '', reverse=True)
    print(f'[trades/paired] OKX+bills+DB: {len(dedup)} trades (okx_rows={len(okx_rows)}, legacy={(len(paired) if flag_raw else 0)})', flush=True)
    return {'trades': dedup[:limit], 'debug': {'bills': len(bills), 'raw_fills': len(raw_fills), 'okx_rows': len(okx_rows), 'okx_ord_ids': len(okx_ord_ids), 'pair_err': pair_bills_err, 'inst_entry_bot': _json_safe_dict(inst_entry_bot)}}
    try:
        _fills_cache_ts = 0
        raw_fills = await _fetch_okx_fills(limit=300)
        try:
            val_ord_ids = {str(r['ord_id']).strip() for r in await db._fetchall("SELECT ord_id FROM trades WHERE bot_id = ? AND ord_id IS NOT NULL AND ord_id != ''" if not db._pg_mode else "SELECT ord_id FROM trades WHERE bot_id = $1 AND ord_id IS NOT NULL AND ord_id != ''", (VAL_BOT_ID,)) if r.get('ord_id')}
        except Exception:
            val_ord_ids = set()
        if val_ord_ids:
            raw_fills = [f for f in raw_fills if str(f.get('ordId', '')).strip() not in val_ord_ids]
        raw_fills = [f for f in raw_fills if not str(f.get('clOrdId', '')).startswith('val')]
        paired = await _pair_fills(raw_fills)
        if paired:
            db_pos_map = {}
            try:
                db_rows = await db.get_all_positions()
                for row in db_rows:
                    db_pos_map[row.get('inst_id', ''), row.get('side', '')] = row.get('bot_id', '')
            except Exception:
                pass
            result = []
            for t in paired[-limit:]:
                entry_px = t.get('entry', 0) or t.get('entry_price', 0)
                exit_px = t.get('exit_price', 0)
                is_open = t.get('reason') == 'open'
                result.append({'time': t.get('time', ''), 'entry_time': t.get('entry_time', ''), 'exit_time': t.get('time', '') if not is_open else None, 'side': 'buy' if t.get('pos_side') == 'long' else 'sell', 'symbol': t.get('inst_id', '') or t.get('symbol', ''), 'inst_id': t.get('inst_id', '') or t.get('symbol', ''), 'entry': entry_px, 'entry_px': entry_px, 'exit_price': exit_px, 'exit_px': exit_px, 'pnl': t.get('pnl', 0) if not is_open else None, 'reason': t.get('reason', ''), 'pos_side': t.get('pos_side', 'long'), 'signal_id': t.get('ord_id', ''), 'bot': _tag_trade_bot(t, db_pos_map=db_pos_map)})
            print(f'[trades/paired] OKX fallback: {len(result)} trades from exchange', flush=True)
            return {'trades': result}
    except Exception as e:
        import traceback
        print(f'[trades/paired] OKX fallback error: {e}', flush=True)
        traceback.print_exc()
    try:
        db_trades = await db.get_paired_trades(limit=limit, begin=begin, end=end, bot_ids=[ROT_BOT_ID, MOM_BOT_ID, IMP_BOT_ID, VAL_BOT_ID])
        result = []
        for t in db_trades:
            entry_side = t.get('entry_side', 'buy')
            entry_px = t.get('entry_px', 0)
            exit_px = t.get('exit_px', 0)
            try:
                entry_px = float(entry_px) if entry_px else 0
            except (TypeError, ValueError):
                entry_px = 0
            try:
                exit_px = float(exit_px) if exit_px else 0
            except (TypeError, ValueError):
                exit_px = 0
            result.append({'time': t.get('exit_time') or t.get('entry_time', ''), 'entry_time': t.get('entry_time', ''), 'exit_time': t.get('exit_time', ''), 'side': entry_side, 'symbol': t.get('inst_id', ''), 'inst_id': t.get('inst_id', ''), 'entry': entry_px, 'entry_px': entry_px, 'exit_price': exit_px, 'exit_px': exit_px, 'pnl': float(t.get('pnl', 0) or 0), 'reason': 'closed', 'pos_side': 'long' if entry_side == 'buy' else 'short', 'signal_id': t.get('signal_id', ''), 'bot': _db_bot_name(t.get('bot_id', ''))})
        return {'trades': result}
    except Exception as e:
        import traceback
        print(f'[trades/paired] DB fallback error: {e}', flush=True)
        traceback.print_exc()
        return {'trades': []}

@app.get('/api/debug/trades-db', dependencies=[Depends(require_admin)])
async def debug_trades_db():
    """Diagnostic: check what's in the DB trades table."""
    try:
        count = await db._fetchone('SELECT count(*) as c FROM trades')
        total = count['c'] if count else 0
        with_signal = await db._fetchone('SELECT count(*) as c FROM trades WHERE signal_id IS NOT NULL')
        paired_count = with_signal['c'] if with_signal else 0
        recent = await db._fetchall('SELECT id, bot_id, inst_id, side, px, pnl, state, timestamp, signal_id FROM trades ORDER BY timestamp DESC LIMIT 5')
        for r in recent:
            r['px'] = str(r.get('px', ''))
        paired = await db.get_paired_trades(limit=5, bot_ids=[ROT_BOT_ID, MOM_BOT_ID])
        return {'total_trades': total, 'with_signal_id': paired_count, 'recent_trades': recent, 'paired_trades': paired}
    except Exception as e:
        import traceback
        return {'error': str(e), 'traceback': traceback.format_exc()}

@app.get('/api/debug/fills', dependencies=[Depends(require_admin)])
async def debug_fills():
    """Diagnostic endpoint: shows raw OKX fills (ALL fields) and pairing results."""
    client = client_manager.get_client()
    client_ok = client is not None
    demo = _env_demo
    global _fills_cache_ts
    _fills_cache_ts = 0
    raw_fills = await _fetch_okx_fills(limit=100)
    paired = await _pair_fills(raw_fills)
    first_fill = raw_fills[0] if raw_fills else {}
    close_candidate = None
    for f in raw_fills:
        ps = f.get('posSide', '')
        s = f.get('side', '')
        if ps and ps != 'net' and (ps == 'long' and s == 'sell' or (ps == 'short' and s == 'buy')):
            close_candidate = f
            break
    closed_trades = [t for t in paired if t.get('reason') == 'closed']
    open_trades = [t for t in paired if t.get('reason') == 'open']
    return {'client_ok': client_ok, 'demo': demo, 'raw_fills_count': len(raw_fills), 'paired_count': len(paired), 'closed_count': len(closed_trades), 'open_count': len(open_trades), 'first_fill_all_fields': first_fill, 'close_candidate_all_fields': close_candidate, 'sample_raw': raw_fills[:3], 'sample_closed': closed_trades[:3], 'sample_open': open_trades[:3], 'field_names': list(first_fill.keys()) if first_fill else []}

@app.get('/api/analysis', dependencies=[Depends(require_admin)])
async def trade_analysis():
    """Detailed trade analysis: PnL breakdown, last ETH trades, stop-loss detection."""
    global _fills_cache_ts
    _fills_cache_ts = 0
    eth_fills = await _fetch_okx_fills(limit=300, inst_id='ETH-USDT-SWAP')
    eth_paired = await _pair_fills(eth_fills)
    _fills_cache_ts = 0
    all_fills = await _fetch_okx_fills(limit=300)
    all_paired = await _pair_fills(all_fills)
    pos_result = await _okx_call(lambda c: c.get_positions('SWAP'))
    positions = []
    if not pos_result.get('error'):
        for p in pos_result.get('data', []):
            positions.append({'inst_id': p.get('instId'), 'pos_side': p.get('posSide', 'net'), 'size': float(p.get('pos', 0)), 'avg_entry': float(p.get('avgPx', 0)), 'upl': float(p.get('upl', 0)), 'upl_ratio': float(p.get('uplRatio', 0)), 'liq_price': float(p.get('liqPx', 0)) if p.get('liqPx') else None})
    eth_closed = [t for t in eth_paired if t.get('reason') == 'closed']
    eth_open = [t for t in eth_paired if t.get('reason') == 'open']
    cumulative = 0.0
    eth_trade_details = []
    for t in eth_closed:
        pnl = float(t.get('pnl', 0) or 0)
        cumulative += pnl
        entry_px = t.get('entry', 0)
        exit_px = t.get('exit_price', 0)
        sl_hit = False
        if pnl < 0 and entry_px > 0:
            loss_pct = abs(pnl / (entry_px * t.get('size', 1))) * 100
            if loss_pct > 1.0:
                sl_hit = True
        eth_trade_details.append({'entry_time': t.get('entry_time', ''), 'close_time': t.get('time', ''), 'entry': entry_px, 'exit': exit_px, 'size': t.get('size', 0), 'pnl': round(pnl, 4), 'cumulative_pnl': round(cumulative, 4), 'pos_side': t.get('pos_side', ''), 'sl_hit': sl_hit, 'price_change_pct': round((exit_px - entry_px) / entry_px * 100, 2) if entry_px > 0 else 0})
    total_pnl = sum((float(t.get('pnl', 0) or 0) for t in all_paired if t.get('reason') == 'closed'))
    total_trades = sum((1 for t in all_paired if t.get('reason') == 'closed'))
    win_trades = sum((1 for t in all_paired if t.get('reason') == 'closed' and float(t.get('pnl', 0) or 0) > 0))
    loss_trades = total_trades - win_trades
    return {'summary': {'total_pnl': round(total_pnl, 2), 'total_trades': total_trades, 'win': win_trades, 'loss': loss_trades, 'win_rate': round(win_trades / total_trades * 100, 1) if total_trades > 0 else 0}, 'eth': {'total_fills': len(eth_fills), 'closed_trades': len(eth_closed), 'open_trades': len(eth_open), 'total_eth_pnl': round(sum((float(t.get('pnl', 0) or 0) for t in eth_closed)), 4), 'last_10_trades': eth_trade_details[-10:], 'open_positions': [{'entry': t.get('entry', 0), 'size': t.get('size', 0), 'entry_time': t.get('entry_time', ''), 'pos_side': t.get('pos_side', '')} for t in eth_open]}, 'current_positions': positions, 'by_instrument': {inst: {'trades': sum((1 for t in all_paired if t.get('inst_id') == inst and t.get('reason') == 'closed')), 'pnl': round(sum((float(t.get('pnl', 0) or 0) for t in all_paired if t.get('inst_id') == inst and t.get('reason') == 'closed')), 4)} for inst in set((t.get('inst_id', '') for t in all_paired))}}

@app.get('/api/db/positions', dependencies=[Depends(require_admin)])
async def get_db_positions():
    positions = await db.get_all_positions()
    return {'positions': positions}

@app.get('/api/analysis/log', dependencies=[Depends(require_admin)])
async def analysis_log_download(request: Request):
    token = get_token(request)
    if not validate(token):
        raise HTTPException(status_code=401, detail='Unauthorized')
    p = Path(DEFAULT_PATH)
    if not p.exists():
        raise HTTPException(status_code=404, detail='Analysis log not found')
    return FileResponse(str(p), media_type='application/x-ndjson', filename=p.name, headers={'Cache-Control': 'no-store'})
if STATIC_DIR.exists():

    @app.head('/api/health')
    async def health_head():
        return JSONResponse({'status': 'ok'})

    @app.head('/')
    async def root_head():
        return Response(status_code=200)

    @app.get('/')
    async def serve_root():
        return FileResponse(str(STATIC_DIR / 'index.html'), headers={'Cache-Control': 'no-store'})

    @app.get('/{full_path:path}')
    async def serve_frontend(full_path: str):
        if full_path.startswith('api/') or full_path.startswith('docs') or full_path.startswith('openapi'):
            return JSONResponse({'detail': 'Not Found'}, status_code=404)
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate), headers={'Cache-Control': 'public, max-age=31536000, immutable'})
        return FileResponse(str(STATIC_DIR / 'index.html'), headers={'Cache-Control': 'no-store'})
_MINI_LOG_RING = []

@app.post('/api/debug/mini-log', dependencies=[Depends(require_admin)])
async def mini_log_collect(data: dict):
    """Collect client-side logs from the Telegram Mini App (admin-only)."""
    logs = (data or {}).get('logs') or []
    if isinstance(logs, list):
        lines = [str(l)[:2000] for l in logs]
        _MINI_LOG_RING.extend(lines)
        del _MINI_LOG_RING[:-500]
        for line in lines:
            logger.info('MINI %s', line)
    return {'saved': len(logs)}

@app.post('/api/debug/client-error', dependencies=[Depends(require_admin)])
async def client_error_collect(request: Request, data: dict):
    """Admin-only client error sink (was public — spam/DoS vector)."""
    'Collect frontend JS errors (public — no auth, for WebView diagnostics).'
    err = (data or {}).get('error') or data or {}
    msg = str(err.get('message') or err)[:2000]
    stack = str(err.get('stack') or '')[:3000]
    line = f'CLIENT-ERR: {msg}\n{stack}'
    _MINI_LOG_RING.append(line)
    del _MINI_LOG_RING[:-500]
    logger.error('CLIENT-ERR %s %s', msg, stack[:500])
    return {'ok': True}

@app.get('/api/debug/client-errors', dependencies=[Depends(require_admin)])
async def client_errors_read():
    """Read recent captured client errors (public, for diagnostics)."""
    return {'errors': [l for l in _MINI_LOG_RING if l.startswith('CLIENT-ERR')][-20:]}

@app.get('/api/debug/mini-log', dependencies=[Depends(require_admin)])
async def mini_log_read():
    """Return the most recent Mini App client logs."""
    return {'count': len(_MINI_LOG_RING), 'logs': _MINI_LOG_RING[-150:]}
if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv('BACKEND_PORT', '8000'))
    host = os.getenv('BACKEND_HOST', '0.0.0.0')
    uvicorn.run('app.main:app', host=host, port=port, reload=True)
