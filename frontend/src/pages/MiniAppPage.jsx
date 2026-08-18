import React, { useState, useEffect, useCallback, useMemo } from 'react'
import {
  Wallet, RefreshCw, Bot, ArrowUpRight,
  ArrowDownRight, Shield, Loader2, Zap, Key, Lock, Eye
} from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'
import { fmtTs } from '../utils/time'

/* ═══════ Mini App diagnostics log (survives reload, shown in panel) ═══════ */
const MINI_LOGS = []
try {
  const saved = localStorage.getItem('mini_logs')
  if (saved) MINI_LOGS.push(...JSON.parse(saved))
} catch { /* ignore */ }

function miniLog(tag, ...args) {
  const line = `[${new Date().toISOString().slice(11, 19)}] ${tag}: ${args.map(a => {
    try { return typeof a === 'string' ? a : JSON.stringify(a) } catch { return String(a) }
  }).join(' ')}`
  MINI_LOGS.push(line)
  if (MINI_LOGS.length > 300) MINI_LOGS.splice(0, MINI_LOGS.length - 300)
  try { localStorage.setItem('mini_logs', JSON.stringify(MINI_LOGS.slice(-150))) } catch { /* ignore */ }
  console.log(line)
}

window.__MINI_APP__ = true

/* ═══════ Number formatting ═══════ */
function fmt(n, digits = 2) {
  if (n == null || isNaN(n)) return '—'
  return Number(n).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function fmtTime(ts) {
  return fmtTs(ts, 'ru-RU')
}

function pnlClass(v) {
  return v > 0 ? 'text-[var(--profit)]' : v < 0 ? 'text-[var(--loss)]' : 'text-[var(--txt-secondary)]'
}

function pnlSign(v) {
  if (v == null || v === 0) return '0'
  return (v > 0 ? '+' : '') + fmt(v)
}

/* ═══════ Never let a request hang forever (cold starts, flaky networks) ═══════ */
function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const id = setTimeout(() => reject(new Error('timeout')), ms)
    promise.then(
      v => { clearTimeout(id); resolve(v) },
      e => { clearTimeout(id); reject(e) }
    )
  })
}

/* ═══════ Compact card ═══════ */
function Card({ children, className = '' }) {
  return (
    <div className={`bg-[var(--surface)] border border-[var(--border)] rounded-xl p-3 ${className}`}>
      {children}
    </div>
  )
}

function SectionTitle({ children }) {
  return (
    <h2 className="text-2xs font-bold uppercase tracking-wider text-[var(--txt-muted)] mb-2">
      {children}
    </h2>
  )
}

/* ═══════ Pro preview (non-subscribers see what the mini-app looks like) ═══════ */
function ProPreview({ t, onBack }) {
  // Demo figures only — no real data is fetched on this screen.
  const demo = {
    portfolio: 18420.53,
    ccy: [['BTC', 8230.12], ['ETH', 3145.88], ['SOL', 690.44], ['BNB', 340.09]],
    bots: [
      { name: 'Momentum', running: true, pnl: 3240.55, equity: 13280.11, trades: 58 },
      { name: 'Impulse 1D', running: true, pnl: 1890.22, equity: 11890.42, trades: 64 },
    ],
    positions: [
      { instId: 'BTC-USDT-SWAP', side: 'long', size: 0.05, lev: 3, px: 81420, upl: 480.15 },
      { instId: 'SOL-USDT-SWAP', side: 'short', size: 120, lev: 2, px: 152.4, upl: -96.3 },
    ],
    trades: [
      { side: 'open', inst: 'ETH-USDT-SWAP', pnl: null, size: 1.2 },
      { side: 'close', inst: 'BTC-USDT-SWAP', pnl: 312.44, size: 0.03 },
      { side: 'close', inst: 'BNB-USDT-SWAP', pnl: -45.1, size: 8 },
    ],
  }
  const demoBotCard = (b, iconColor) => (
    <Card className="flex-1 min-w-0">
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-1.5">
          <Bot size={14} className={iconColor} />
          <span className="text-xs font-bold text-[var(--txt)] truncate">{b.name}</span>
        </div>
        <span className="flex items-center gap-1 text-2xs font-semibold px-1.5 py-0.5 rounded-md bg-[var(--profit-dim)] text-[var(--profit)]">
          <span className="w-1.5 h-1.5 rounded-full bg-[var(--profit)] animate-pulse-dot" />
          {t('mini.running')}
        </span>
      </div>
      <div className="flex items-end justify-between">
        <div>
          <div className="text-xs text-[var(--txt-secondary)]">{t('mini.pnl')}</div>
          <div className={`text-base font-bold mono ${pnlClass(b.pnl)}`}>{pnlSign(b.pnl)}</div>
        </div>
        <div className="text-right">
          <div className="text-2xs text-[var(--txt-muted)]">{t('mini.balance')}</div>
          <div className="text-xs font-semibold mono text-[var(--txt)]">{fmt(b.equity)}</div>
        </div>
      </div>
      <div className="flex gap-3 mt-2 pt-2 border-t border-[var(--border)] text-2xs text-[var(--txt-muted)]">
        <span>Трейды: <b className="text-[var(--txt)]">{b.trades}</b></span>
      </div>
    </Card>
  )

  return (
    <div className="min-h-screen bg-[var(--bg)] text-[var(--txt)]" style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}>
      <div className="p-3 space-y-3">
        {/* Back */}
        <button onClick={onBack} className="flex items-center gap-1.5 text-2xs text-[var(--txt-muted)] active:opacity-70">
          <ArrowUpRight size={14} className="rotate-180" /> {t('mini.back')}
        </button>

        {/* ═══ What you get with Pro ═══ */}
        <Card className="border-[var(--info)]/40">
          <div className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-[var(--info)] mb-2">
            <Zap size={13} /> {t('mini.pro_what_get')}
          </div>
          <ul className="space-y-1.5 text-2xs text-[var(--txt-secondary)]">
            <li>• {t('mini.pro_perk1')}</li>
            <li>• {t('mini.pro_perk2')}</li>
            <li>• {t('mini.pro_perk3')}</li>
            <li>• {t('mini.pro_perk4')}</li>
          </ul>
          <a
            href="https://t.me/RotationTradeBot?start=subscribe_pro"
            className="mt-3 w-full flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold active:opacity-70"
          >
            {t('mini.get_pro')}
          </a>
        </Card>

        {/* ═══ Demo dashboard ═══ */}
        <div className="flex items-center justify-between">
          <SectionTitle>{t('mini.preview_looks_like')}</SectionTitle>
          <span className="text-2xs font-semibold px-1.5 py-0.5 rounded-md bg-[var(--warn-dim)] text-[var(--warn)]">
            {t('mini.demo_preview')}
          </span>
        </div>

        <Card className="bg-gradient-to-br from-[var(--profit-dim)] to-[var(--info-dim)] border-0">
          <div className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-[var(--txt-secondary)] mb-1">
            <Wallet size={13} className="text-[var(--info)]" />
            {t('mini.portfolio')}
          </div>
          <div className="flex items-end justify-between">
            <div className="text-2xl font-extrabold mono text-[var(--txt)]">${fmt(demo.portfolio)}</div>
            <div className="text-right text-2xs text-[var(--txt-secondary)]">
              <div>{t('mini.positions')}: <b className="text-[var(--txt)]">{demo.positions.length}</b></div>
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {demo.ccy.map(([ccy, v]) => (
              <span key={ccy} className="px-1.5 py-0.5 rounded-md bg-[var(--surface-overlay)] text-2xs font-semibold text-[var(--txt-secondary)]">
                {ccy} <b className="text-[var(--txt)]">${fmt(v)}</b>
              </span>
            ))}
          </div>
        </Card>

        <div>
          <SectionTitle>{t('mini.bots')}</SectionTitle>
          <div className="flex gap-2">
            {demoBotCard(demo.bots[0], 'text-[var(--info)]')}
            {demoBotCard(demo.bots[1], 'text-[var(--profit)]')}
          </div>
        </div>

        <div>
          <SectionTitle>{t('mini.positions')}</SectionTitle>
          <div className="space-y-1.5">
            {demo.positions.map((p, i) => {
              const upl = p.upl
              return (
                <Card key={i} className="py-2.5">
                  <div className="flex items-center justify-between mb-1">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="text-xs font-bold text-[var(--txt)] truncate">{p.instId}</span>
                      <span className={`text-2xs font-bold px-1 py-0.5 rounded ${p.side === 'long' ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                        {p.side === 'long' ? 'LONG' : 'SHORT'}
                      </span>
                    </div>
                    <div className={`text-xs font-bold mono ${pnlClass(upl)}`}>{pnlSign(upl)}</div>
                  </div>
                  <div className="flex items-center justify-between text-2xs text-[var(--txt-muted)]">
                    <span>${fmt(p.px)} · {p.lev}x · {p.size}</span>
                  </div>
                </Card>
              )
            })}
          </div>
        </div>

        <div>
          <SectionTitle>{t('mini.last_trades')}</SectionTitle>
          <Card className="p-0 overflow-hidden">
            <div className="divide-y divide-[var(--border)]">
              {demo.trades.map((tr, i) => {
                const isOpen = tr.side === 'open'
                return (
                  <div key={i} className="flex items-center justify-between px-3 py-2">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className={`text-2xs font-bold px-1 py-0.5 rounded ${isOpen ? 'bg-[var(--info-dim)] text-[var(--info)]' : pnlSign(tr.pnl).startsWith('+') ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                        {isOpen ? t('mini.open') : tr.side === 'close' ? t('mini.exit') : ''}
                      </span>
                      <span className="text-xs font-semibold text-[var(--txt)] truncate">{tr.inst}</span>
                      <span className="text-2xs text-[var(--txt-muted)]">{tr.size}</span>
                    </div>
                    <div className={`text-sm font-bold mono ${isOpen ? 'text-[var(--info)]' : pnlClass(tr.pnl)}`}>
                      {isOpen ? '—' : pnlSign(tr.pnl)}
                    </div>
                  </div>
                )
              })}
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}

/* ═══════ Main page ═══════ */
export default function MiniAppPage() {
  const { t } = useTranslation()
  const [authing, setAuthing] = useState(true)
  const [authError, setAuthError] = useState('')
  const [needsPro, setNeedsPro] = useState(false)
  const [preview, setPreview] = useState(false)
  const [loading, setLoading] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [connected, setConnected] = useState(false)
  const [demoMode, setDemoMode] = useState(true)
  const [portfolio, setPortfolio] = useState(null)
  const [rotation, setRotation] = useState(null)
  const [impulse, setImpulse] = useState(null)
  const [positions, setPositions] = useState([])
  const [trades, setTrades] = useState([])

  const [tg, setTg] = useState(null)
  const [tgResolved, setTgResolved] = useState(false)
  const [isAdmin, setIsAdmin] = useState(localStorage.getItem('auth_role') === 'admin')
  const [role, setRole] = useState(localStorage.getItem('auth_role') || '')
  const [me, setMe] = useState(null)
  const [showLogs, setShowLogs] = useState(false)
  const [copied, setCopied] = useState(false)
  const [serverHits, setServerHits] = useState([])
  const [hitsStatus, setHitsStatus] = useState('')
  // User account: connect OKX keys + start/stop bots
  const [creds, setCreds] = useState({ api_key: '', secret_key: '', passphrase: '', demo: true })
  const [credsSaving, setCredsSaving] = useState(false)
  const [credsStatus, setCredsStatus] = useState(null)
  const [botAction, setBotAction] = useState(null)

  // Same source as the web Dashboard: /trades/paired (OKX-backed) with the same
  // phantom-close suppression — a "closed" row is hidden while its instrument
  // is still open on OKX/bots (no real trade ever happened).
  const displayTrades = useMemo(() => {
    const openKeys = new Set()
    const pushOpen = (p) => {
      const inst = p.inst_id || p.instId || p.symbol || ''
      if (!inst) return
      const sideRaw = (p.side || p.posSide || p.pos_side || 'long').toLowerCase()
      const isLong = sideRaw !== 'short' && sideRaw !== 'sell'
      openKeys.add(`${inst}|${isLong ? 'long' : 'short'}`)
    }
    for (const p of (positions || [])) pushOpen(p)
    for (const p of (rotation?.open_positions || [])) pushOpen(p)
    for (const p of (impulse?.open_positions || [])) pushOpen(p)

    const out = []
    for (const tr of (trades || [])) {
      const inst = tr.inst_id || tr.symbol || ''
      const reason = (tr.reason || '').toLowerCase()
      const isOpen = reason === 'open' || reason === 'add'
      if (!isOpen && inst && [...openKeys].some(k => k.startsWith(inst + '|'))) continue
      out.push({
        ...tr,
        coin: tr.coin || (inst || '').replace('-USDT-SWAP', ''),
        symbol: tr.symbol || inst,
        isOpen,
        entry: tr.entry_px ?? tr.entry_price ?? tr.entry ?? 0,
        exit: tr.exit_px ?? tr.exit_price,
        size: tr.size ?? tr.sz ?? '',
        time: tr.time || tr.exit_time || tr.entry_time || '',
      })
    }
    return out
  }, [trades, positions, rotation, impulse])

  useEffect(() => {
    if (!showLogs) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await withTimeout(api.debugServerHits(), 10000)
        if (!cancelled) { setServerHits(res?.hits || []); setHitsStatus('') }
      } catch (e) {
        if (!cancelled) setHitsStatus('ERR ' + (e.message || e))
      }
    })()
    return () => { cancelled = true }
  }, [showLogs])

  const copyLogs = async () => {
    const text = MINI_LOGS.slice(-150).join('\n')
    const done = () => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        done()
        return
      }
      throw new Error('no clipboard API')
    } catch {
      try {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.focus()
        ta.select()
        const ok = document.execCommand('copy')
        document.body.removeChild(ta)
        if (ok) { done(); return }
        throw new Error('execCommand failed')
      } catch {
        miniLog('copy', 'clipboard blocked, use "Отправить" button instead')
        try {
          tg?.showAlert?.('Буфер обмена недоступен. Используйте кнопку «Отправить».')
        } catch { /* ignore */ }
      }
    }
  }

  const [sentLogs, setSentLogs] = useState(false)
  const sendLogs = async () => {
    setSentLogs(false)
    try {
      const res = await withTimeout(api.debugMiniLog(MINI_LOGS.slice(-150)), 15000)
      miniLog('send', res?.saved ? 'logs saved to server' : 'unexpected response')
      setSentLogs(true)
      try { tg?.HapticFeedback?.notificationOccurred?.('success') } catch { /* ignore */ }
    } catch (err) {
      miniLog('send', 'ERROR', err.message || err)
      try { tg?.showAlert?.('Не удалось отправить: ' + (err.message || err)) } catch { /* ignore */ }
    }
  }

  /* ── Wait for Telegram WebApp SDK + initData (up to ~5s) ── */
  useEffect(() => {
    let tries = 0
    miniLog('sdk', 'waiting for window.Telegram.WebApp...', typeof window.Telegram?.WebApp)
    const interval = setInterval(() => {
      const wa = window.Telegram?.WebApp
      if (wa) {
        clearInterval(interval)
        setTg(wa)
        setTgResolved(true)
        miniLog('sdk', 'found after', tries, 'ticks; initData length =', (wa.initData || '').length)
      } else if (++tries > 25) {
        clearInterval(interval)
        setTg(null)
        setTgResolved(true)
        miniLog('sdk', 'NOT found after 5s (SDK script blocked/absent)')
      }
    }, 200)
    return () => clearInterval(interval)
  }, [])

  /* ── Telegram environment setup ── */
  useEffect(() => {
    if (!tg) return
    try {
      tg.ready()
      tg.expand()
      const scheme = tg.colorScheme
      if (scheme === 'light') {
        document.documentElement.classList.add('light')
        localStorage.setItem('theme', 'light')
      } else {
        document.documentElement.classList.remove('light')
        localStorage.setItem('theme', 'dark')
      }
      if (tg.themeParams?.bg_color) {
        document.body.style.background = tg.themeParams.bg_color
      }
      tg.setHeaderColor?.(tg.themeParams?.header_bg_color || tg.themeParams?.bg_color || '#0b0e14')
      tg.BackButton?.hide()
      miniLog('env', 'theme applied, colorScheme =', scheme)
    } catch (e) { miniLog('env', 'error', e.message) }
  }, [tg])

  /* ── Auth via Telegram initData ── */
  useEffect(() => {
    if (!tgResolved) return
    const run = async () => {
      try {
        const initData = tg?.initData
        if (initData) {
          miniLog('auth', 'initData present, verifying signature...')
          const res = await withTimeout(api.telegramAuth(initData), 20000)
          localStorage.setItem('auth_token', res.token)
          localStorage.setItem('auth_role', res.role)
          setIsAdmin(res.role === 'admin')
          setRole(res.role)
          if (res.role === 'user') {
            try {
              const m = await withTimeout(api.me(), 10000)
              setMe(m)
              setDemoMode(m.demo !== false)
            } catch (e) { miniLog('auth', 'me profile ERR', e.message || e) }
          }
          miniLog('auth', 'OK role=' + res.role, 'user=' + (res.user?.username || res.user?.id || '?'))
        } else if (localStorage.getItem('auth_token')) {
          miniLog('auth', 'no initData, using stored session token')
        } else {
          setAuthError('not_in_telegram')
          miniLog('auth', 'no initData AND no stored token -> error screen')
        }
        try {
          const h = await withTimeout(api.debugServerHits(), 10000)
          miniLog('server-hits',
            ((h?.hits || []).slice(-20)
              .map(x => x.t + ' ' + x.m + ' ' + x.p + '→' + x.c)
              .join(' | ')) || 'none')
        } catch (e) {
          miniLog('server-hits', 'ERR', e.message || e)
        }
      } catch (err) {
        console.warn('mini auth error', err)
        if (err.status === 403) {
          setNeedsPro(true)
          miniLog('auth', 'FORBIDDEN (Pro required)', err.message || err)
        } else {
          setAuthError(err.message || 'auth_failed')
          miniLog('auth', 'ERROR', err.message || err)
        }
      }
      setAuthing(false)
    }
    run()
  }, [tgResolved, tg])

  /* ── Load dashboard data (per-request error handling + timeout) ── */
  const load = useCallback(async () => {
    miniLog('load', 'starting, token=' + (localStorage.getItem('auth_token') ? 'set' : 'EMPTY'))
    setLoading(true)
    const isUser = role === 'user'
    const callers = {
      health: () => api.health(),
      portfolio: () => isUser ? api.mePortfolio() : api.getPortfolio(),
      rotation: () => isUser ? api.meStatus().then(s => s.rotation) : api.rotationStatus(),
      impulse: () => isUser ? api.meStatus().then(s => s.impulse) : api.impulseStatus(),
      positions: () => isUser ? api.mePositions() : api.getPositions('SWAP'),
      trades: () => api.getPairedTrades(20),
    }
    const names = Object.keys(callers)
    const results = await Promise.all(names.map(async (name) => {
      try {
        const v = await withTimeout(callers[name](), 12000)
        const len = (JSON.stringify(v) || '').length
        miniLog('load', name, 'OK len=' + len)
        return [name, v]
      } catch (e) {
        miniLog('load', name, 'ERROR', e.message || String(e))
        return [name, null]
      }
    }))
    const map = Object.fromEntries(results)
    if (map.health) { setConnected(map.health.connected); setDemoMode(map.health.demo) }
    if (map.portfolio) setPortfolio(map.portfolio)
    if (map.rotation) setRotation(map.rotation)
    if (map.impulse) setImpulse(map.impulse)
    if (map.positions) setPositions(map.positions.positions || [])
    if (map.trades) setTrades(map.trades.trades || [])
    if (isUser) {
      try {
        const m = await withTimeout(api.me(), 10000)
        setMe(m)
      } catch (e) { /* ignore */ }
    }
    try {
      const h = await withTimeout(api.debugServerHits(), 10000)
      miniLog('server-hits',
        ((h?.hits || []).slice(-12)
          .map(x => x.t + ' ' + x.m + ' ' + x.p + '→' + x.c)
          .join(' | ')) || 'none')
    } catch (e) {
      miniLog('server-hits', 'ERR', e.message || e)
    }
    setLoaded(true)
    setLoading(false)
  }, [role])

  useEffect(() => {
    miniLog('effect', 'load-effect fired, authing=' + authing + ' authError=' + (authError || '""'))
    if (!authing && !authError) load()
  }, [authing, authError, load])

  /* ── Auto-refresh every 30s ── */
  useEffect(() => {
    if (authing || authError) return
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [authing, authError, load])

  /* ── User account: connect own OKX keys ── */
  const saveCreds = async () => {
    setCredsSaving(true); setCredsStatus(null)
    try {
      const r = await withTimeout(api.meCredentials({
        apiKey: creds.api_key, secretKey: creds.secret_key,
        passphrase: creds.passphrase, demo: creds.demo,
      }), 20000)
      setCreds({ api_key: '', secret_key: '', passphrase: '', demo: true })
      setCredsStatus({ ok: true, message: r.message || 'OK' })
      await load()
    } catch (e) {
      setCredsStatus({ ok: false, message: e.message || 'Ошибка' })
    }
    setCredsSaving(false)
  }

  /* ── User account: start / stop a bot on their account ── */
  const toggleBot = async (which) => {
    const running = which === 'rotation' ? (rotation?.running) : (impulse?.running)
    setBotAction(which)
    try {
      if (running) {
        if (which === 'rotation') await withTimeout(api.meRotationStop(), 15000)
        else await withTimeout(api.meImpulseStop(), 15000)
      } else {
        const cfg = { capital: me?.capital || 10000 }
        if (which === 'rotation') await withTimeout(api.meRotationStart(cfg), 15000)
        else await withTimeout(api.meImpulseStart(cfg), 15000)
      }
      await load()
    } catch (e) {
      setCredsStatus({ ok: false, message: e.message || 'Ошибка' })
    }
    setBotAction(null)
  }

  const proActive = role === 'user' && me?.plan === 'pro' && me?.active


  /* ── Bot status card ── */
  const botCard = (name, s, iconColor) => {
    const running = s?.running
    const pnl = s?.total_pnl ?? 0
    return (
      <Card className="flex-1 min-w-0">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-1.5">
            <Bot size={14} className={iconColor} />
            <span className="text-xs font-bold text-[var(--txt)] truncate">{name}</span>
          </div>
          <span className={`flex items-center gap-1 text-2xs font-semibold px-1.5 py-0.5 rounded-md ${
            running ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--surface-overlay)] text-[var(--txt-muted)]'
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${running ? 'bg-[var(--profit)] animate-pulse-dot' : 'bg-[var(--txt-muted)]'}`} />
            {running ? t('mini.running') : t('mini.stopped')}
          </span>
        </div>
        <div className="flex items-end justify-between">
          <div>
            <div className="text-xs text-[var(--txt-secondary)]">{t('mini.pnl')}</div>
            <div className={`text-base font-bold mono ${pnlClass(pnl)}`}>{pnlSign(pnl)}</div>
          </div>
          <div className="text-right">
            <div className="text-2xs text-[var(--txt-muted)]">{t('mini.balance')}</div>
            <div className="text-xs font-semibold mono text-[var(--txt)]">{fmt(s?.equity ?? 0)}</div>
          </div>
        </div>
        <div className="flex gap-3 mt-2 pt-2 border-t border-[var(--border)] text-2xs text-[var(--txt-muted)]">
          <span>Трейды: <b className="text-[var(--txt)]">{s?.total_trades ?? 0}</b></span>
          <span>WinRate: <b className="text-[var(--txt)]">{fmt(s?.win_rate ?? 0, 0)}%</b></span>
          <span>Позиции: <b className="text-[var(--txt)]">{(s?.open_positions || s?.positions || []).length}</b></span>
        </div>
      </Card>
    )
  }

  if (authing) {
    return (
      <div className="h-screen flex flex-col items-center justify-center gap-3 bg-[var(--bg)] text-[var(--txt-secondary)]">
        <Loader2 size={28} className="animate-spin text-[var(--info)]" />
        <span className="text-xs">{t('mini.loading')}</span>
      </div>
    )
  }

  if (needsPro) {
    if (preview) {
      return <ProPreview t={t} onBack={() => setPreview(false)} />
    }
    return (
      <div className="h-screen flex flex-col items-center justify-center gap-4 p-6 bg-[var(--bg)] text-center">
        <Lock size={40} className="text-[var(--warn)]" />
        <div className="text-sm font-semibold text-[var(--txt)]">{t('mini.pro_required')}</div>
        <div className="text-xs text-[var(--txt-secondary)] leading-relaxed">{t('mini.pro_required_sub')}</div>
        <button
          onClick={() => setPreview(true)}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold active:opacity-70"
        >
          <Eye size={15} />
          {t('mini.get_pro')}
        </button>
        <button
          onClick={() => window.location.reload()}
          className="text-2xs text-[var(--txt-muted)] underline"
        >
          {t('mini.reload')}
        </button>
      </div>
    )
  }

  if (authError) {
    return (
      <div className="h-screen flex flex-col items-center justify-center gap-4 p-6 bg-[var(--bg)] text-center">
        <Shield size={40} className="text-[var(--warn)]" />
        <div className="text-sm font-semibold text-[var(--txt)]">{t('mini.auth_error')}</div>
        <button
          onClick={() => window.location.reload()}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold active:opacity-70"
        >
          <RefreshCw size={15} />
          {t('mini.reload')}
        </button>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-[var(--bg)] text-[var(--txt)]" style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}>
      {/* ═══ Header ═══ */}
      <div className="sticky top-0 z-10 flex items-center justify-between px-4 py-3 bg-[var(--surface)] border-b border-[var(--border)]">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-[var(--info)] to-[#4a3fd1] flex items-center justify-center">
            <Zap size={14} className="text-white" />
          </div>
          <span className="text-sm font-bold text-[var(--txt)]">COPIX</span>
          <span className={`ml-1 flex items-center gap-1 px-1.5 py-0.5 rounded-md text-2xs font-bold ${
            connected ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-[var(--profit)]' : 'bg-[var(--loss)]'}`} />
            {connected ? (demoMode ? 'DEMO' : 'LIVE') : 'OFFLINE'}
          </span>
          {role === 'user' && me?.plan && (
            <span className={`ml-1 px-1.5 py-0.5 rounded-md text-2xs font-bold ${
              me?.plan === 'pro' ? 'bg-[var(--info-dim)] text-[var(--info)]' : 'bg-[var(--surface-overlay)] text-[var(--txt-secondary)]'
            }`}>
              {me?.plan === 'pro' ? '💎 PRO' : 'FREE'}
            </span>
          )}
        </div>
        <button
          className="btn-icon"
          onClick={load}
          disabled={loading}
          title={t('mini.reload')}
        >
          <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className="p-3 space-y-3">
        {/* ═══ Data unavailable banner ═══ */}
        {loaded && !portfolio && !rotation && !impulse && (
          <div className="flex items-center justify-between gap-2 px-3 py-2.5 rounded-xl bg-[var(--warn-dim)] border border-[var(--warn)]">
            <span className="text-xs text-[var(--txt)]">{t('mini.data_error')}</span>
            <button
              onClick={load}
              className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-[var(--warn)] text-white text-2xs font-bold active:opacity-70"
            >
              <RefreshCw size={11} />
              {t('mini.reload')}
            </button>
          </div>
        )}

        {/* ═══ User account panel (non-owner) ═══ */}
        {role === 'user' && (
          <>
            {me && !me.creds_configured && (
              <Card className="border-[var(--warn)]/40">
                <div className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-[var(--warn)] mb-2">
                  <Shield size={13} /> {t('mini.connect_title')}
                </div>
                <p className="text-2xs text-[var(--txt-muted)] mb-3">{t('mini.connect_tip')}</p>
                <div className="space-y-2">
                  <input
                    className="w-full input mono text-2xs"
                    placeholder="API Key"
                    value={creds.api_key}
                    onChange={e => setCreds({ ...creds, api_key: e.target.value })}
                  />
                  <input
                    className="w-full input mono text-2xs"
                    type="password"
                    placeholder="Secret Key"
                    value={creds.secret_key}
                    onChange={e => setCreds({ ...creds, secret_key: e.target.value })}
                  />
                  <input
                    className="w-full input mono text-2xs"
                    type="password"
                    placeholder="Passphrase"
                    value={creds.passphrase}
                    onChange={e => setCreds({ ...creds, passphrase: e.target.value })}
                  />
                  <div className="flex items-center gap-2 text-2xs text-[var(--txt-secondary)]">
                    <input
                      type="checkbox"
                      checked={creds.demo}
                      onChange={e => setCreds({ ...creds, demo: e.target.checked })}
                    />
                    {t('mini.demo_mode')}
                  </div>
                  <button
                    className="w-full btn btn-primary"
                    onClick={saveCreds}
                    disabled={credsSaving}
                  >
                    {credsSaving ? <Loader2 size={14} className="animate-spin" /> : <Key size={14} />}
                    {t('mini.connect_btn')}
                  </button>
                  {credsStatus && (
                    <div className={`text-2xs ${credsStatus.ok ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                      {credsStatus.message}
                    </div>
                  )}
                </div>
              </Card>
            )}

            {me && me.creds_configured && (
              <Card>
                <div className="flex items-center justify-between gap-2 mb-1">
                  <div className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-[var(--profit)]">
                    <Key size={13} /> {t('mini.connected_keys')}
                  </div>
                  <span className={`text-2xs font-semibold px-1.5 py-0.5 rounded-md ${
                    proActive ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--surface-overlay)] text-[var(--txt-muted)]'
                  }`}>
                    {proActive ? t('mini.pro_active') : t('mini.no_pro')}
                  </span>
                </div>
                {!proActive && (
                  <p className="text-2xs text-[var(--txt-muted)] mt-1">{t('mini.no_pro_tip')}</p>
                )}
                {proActive && (
                  <div className="flex gap-2 mt-3">
                    <button
                      className={`btn flex-1 ${rotation?.running ? 'btn-ghost' : 'btn-primary'}`}
                      onClick={() => toggleBot('rotation')}
                      disabled={botAction !== null}
                    >
                      {botAction === 'rotation' ? <Loader2 size={14} className="animate-spin" /> : <Bot size={14} />}
                      {rotation?.running ? t('mini.stop_rotation') : t('mini.start_rotation')}
                    </button>
                    <button
                      className={`btn flex-1 ${impulse?.running ? 'btn-ghost' : 'btn-primary'}`}
                      onClick={() => toggleBot('impulse')}
                      disabled={botAction !== null}
                    >
                      {botAction === 'impulse' ? <Loader2 size={14} className="animate-spin" /> : <Bot size={14} />}
                      {impulse?.running ? t('mini.stop_impulse') : t('mini.start_impulse')}
                    </button>
                  </div>
                )}
              </Card>
            )}
          </>
        )}

        {/* ═══ Portfolio ═══ */}
        <Card className="bg-gradient-to-br from-[var(--profit-dim)] to-[var(--info-dim)] border-0">
          <div className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-[var(--txt-secondary)] mb-1">
            <Wallet size={13} className="text-[var(--info)]" />
            {t('mini.portfolio')}
          </div>
          <div className="flex items-end justify-between">
            <div className="text-2xl font-extrabold mono text-[var(--txt)]">
              {portfolio ? `$${fmt(portfolio.totalEqUsd)}` : '—'}
            </div>
            <div className="text-right text-2xs text-[var(--txt-secondary)]">
              <div>{t('mini.positions')}: <b className="text-[var(--txt)]">{positions.length}</b></div>
            </div>
          </div>
          {portfolio?.details?.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {portfolio.details.slice(0, 6).map(d => (
                <span key={d.ccy} className="px-1.5 py-0.5 rounded-md bg-[var(--surface-overlay)] text-2xs font-semibold text-[var(--txt-secondary)]">
                  {d.ccy} <b className="text-[var(--txt)]">${fmt(d.eqUsd)}</b>
                </span>
              ))}
            </div>
          )}
        </Card>

        {/* ═══ Bots ═══ */}
        <div>
          <SectionTitle>{t('mini.bots')}</SectionTitle>
          <div className="flex gap-2">
            {botCard('Momentum', rotation, 'text-[var(--info)]')}
            {botCard('Impulse 1D', impulse, 'text-[var(--profit)]')}
          </div>
        </div>

        {/* ═══ Positions ═══ */}
        <div>
          <SectionTitle>{t('mini.positions')}</SectionTitle>
          {positions.length === 0 ? (
            <Card className="text-center py-4 text-xs text-[var(--txt-muted)]">
              {t('mini.no_positions')}
            </Card>
          ) : (
            <div className="space-y-1.5">
              {positions.map((p, i) => {
                const upl = Number(p.upl || 0)
                const side = (p.posSide || 'net').toLowerCase()
                return (
                  <Card key={i} className="py-2.5">
                    <div className="flex items-center justify-between mb-1">
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span className="text-xs font-bold text-[var(--txt)] truncate">{p.instId}</span>
                        {p.bot && (
                          <span className="px-1 py-0.5 rounded bg-[var(--info-dim)] text-[var(--info)] text-2xs font-semibold">
                            {p.bot}
                          </span>
                        )}
                      </div>
                      <span className={`flex items-center gap-1 text-2xs font-bold ${
                        side === 'long' ? 'text-[var(--profit)]' : 'text-[var(--loss)]'
                      }`}>
                        {side === 'long'
                          ? <ArrowUpRight size={12} />
                          : <ArrowDownRight size={12} />}
                        {side.toUpperCase()}
                      </span>
                    </div>
                    <div className="grid grid-cols-4 gap-1 text-2xs text-[var(--txt-muted)]">
                      <div>
                        <div>{t('mini.entry')}</div>
                        <b className="text-[var(--txt)] mono">{fmt(p.avgPx)}</b>
                      </div>
                      <div>
                        <div>{t('mini.size')}</div>
                        <b className="text-[var(--txt)] mono">{fmt(p.pos)}</b>
                      </div>
                      <div>
                        <div>{t('mini.leverage')}</div>
                        <b className="text-[var(--txt)] mono">{p.lever}x</b>
                      </div>
                      <div className="text-right">
                        <div>{t('mini.unrealized')}</div>
                        <b className={`mono ${pnlClass(upl)}`}>{pnlSign(upl)}</b>
                      </div>
                    </div>
                  </Card>
                )
              })}
            </div>
          )}
        </div>

        {/* ═══ Last trades ═══ */}
        <div>
          <SectionTitle>{t('mini.last_trades')}</SectionTitle>
          {displayTrades.length === 0 ? (
            <Card className="text-center py-4 text-xs text-[var(--txt-muted)]">
              {t('mini.no_trades')}
            </Card>
          ) : (
            <Card className="p-0 overflow-hidden">
              <div className="divide-y divide-[var(--border)]">
                {displayTrades.slice(0, 8).map((tr, i) => {
                  const pnl = Number(tr.pnl || 0)
                  const reason = tr.reason || ''
                  const isOpen = tr.isOpen || reason === 'open'
                  return (
                    <div key={i} className="flex items-center justify-between px-3 py-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className="text-xs font-bold text-[var(--txt)] truncate">
                            {tr.coin || tr.symbol || '—'}
                          </span>
                          <span className={`text-2xs font-semibold ${isOpen ? 'text-[var(--info)]' : pnlClass(pnl)}`}>
                            {isOpen ? t('mini.open') : reason}
                          </span>
                        </div>
                        <div className="text-2xs text-[var(--txt-muted)]">
                          {fmtTime(tr.time)}
                          {tr.exit ? ` • ${t('mini.entry')} ${fmt(tr.entry)} → ${t('mini.exit')} ${fmt(tr.exit)}` : ''}
                        </div>
                      </div>
                      <div className="text-right">
                        <div className={`text-sm font-bold mono ${isOpen ? 'text-[var(--info)]' : pnlClass(pnl)}`}>
                          {isOpen ? fmt(tr.entry) : pnlSign(pnl)}
                        </div>
                        <div className="text-2xs text-[var(--txt-muted)]">{tr.size ? fmt(tr.size) : '—'}</div>
                      </div>
                    </div>
                  )
                })}
              </div>
            </Card>
          )}
        </div>

        {/* ═══ Footer ═══ */}
        <div className="pb-1 pt-1 text-center text-2xs text-[var(--txt-muted)]">
          COPIX • {connected ? (demoMode ? 'DEMO' : 'LIVE') : 'OFFLINE'}
        </div>

        {/* ═══ Diagnostics panel (admin only) ═══ */}
        {isAdmin && (
        <div className="pb-2">
          <button
            onClick={() => setShowLogs(s => !s)}
            className="w-full flex items-center justify-center gap-1 py-2 text-2xs text-[var(--txt-muted)] active:opacity-70"
          >
            {showLogs ? '▾' : '▸'} {t('mini.diagnostics')} ({MINI_LOGS.length})
          </button>
          {showLogs && (
            <Card className="p-0 overflow-hidden">
              <div className="flex items-center justify-between px-3 py-1.5 bg-[var(--surface-overlay)]">
                <span className="text-2xs font-semibold text-[var(--txt-muted)]">{t('mini.diagnostics')}</span>
                <div className="flex gap-1.5">
                  <button onClick={() => { MINI_LOGS.length = 0; setShowLogs(false) }} className="px-2 py-0.5 rounded bg-[var(--loss-dim)] text-[var(--loss)] text-2xs font-bold">
                    {t('mini.clear')}
                  </button>
                  <button onClick={copyLogs} className="px-2 py-0.5 rounded bg-[var(--info-dim)] text-[var(--info)] text-2xs font-bold">
                    {copied ? t('mini.copied') : t('mini.copy')}
                  </button>
                  <button onClick={sendLogs} className="px-2 py-0.5 rounded bg-[var(--success-dim)] text-[var(--success)] text-2xs font-bold">
                    {sentLogs ? t('mini.sent') : t('mini.send')}
                  </button>
                </div>
              </div>
              <pre className="p-3 text-2xs leading-relaxed text-[var(--txt-secondary)] whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
                {MINI_LOGS.slice(-60).join('\n') || t('mini.no_logs')}
              </pre>
              <div className="px-3 py-1.5 bg-[var(--surface-overlay)] border-t border-[var(--border)]">
                <div className="text-2xs font-semibold text-[var(--txt-muted)] mb-1">{t('mini.server')} {hitsStatus}</div>
                <pre className="text-2xs leading-relaxed text-[var(--txt-secondary)] whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
                  {serverHits.length
                    ? serverHits.slice(-25).map(h => `${h.t} ${h.m} ${h.p} → ${h.c}`).join('\n')
                    : t('mini.no_server_hits')}
                </pre>
              </div>
            </Card>
          )}
        </div>
        )}
      </div>
    </div>
  )
}
