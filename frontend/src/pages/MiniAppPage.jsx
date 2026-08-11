import React, { useState, useEffect, useCallback } from 'react'
import {
  Wallet, RefreshCw, Bot, ArrowUpRight,
  ArrowDownRight, Shield, Loader2, Zap
} from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

/* ═══════ Number formatting ═══════ */
function fmt(n, digits = 2) {
  if (n == null || isNaN(n)) return '—'
  return Number(n).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(typeof ts === 'string' ? ts : ts * 1000)
  if (isNaN(d)) return String(ts)
  return d.toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  })
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

/* ═══════ Main page ═══════ */
export default function MiniAppPage() {
  const { t } = useTranslation()
  const [authing, setAuthing] = useState(true)
  const [authError, setAuthError] = useState('')
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

  /* ── Wait for Telegram WebApp SDK + initData (up to ~5s) ── */
  useEffect(() => {
    let tries = 0
    const interval = setInterval(() => {
      const wa = window.Telegram?.WebApp
      if (wa) {
        clearInterval(interval)
        setTg(wa)
        setTgResolved(true)
      } else if (++tries > 25) {
        clearInterval(interval)
        setTg(null)
        setTgResolved(true)
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
    } catch { /* ignore SDK errors outside Telegram */ }
  }, [tg])

  /* ── Auth via Telegram initData ── */
  useEffect(() => {
    if (!tgResolved) return
    const run = async () => {
      try {
        const initData = tg?.initData
        if (initData) {
          const res = await withTimeout(api.telegramAuth(initData), 20000)
          localStorage.setItem('auth_token', res.token)
          localStorage.setItem('auth_role', res.role)
        } else if (!localStorage.getItem('auth_token')) {
          setAuthError('not_in_telegram')
        }
      } catch (err) {
        console.warn('mini auth error', err)
        setAuthError(err.message || 'auth_failed')
      }
      setAuthing(false)
    }
    run()
  }, [tgResolved, tg])

  /* ── Load dashboard data (per-request error handling + timeout) ── */
  const load = useCallback(async () => {
    setLoading(true)
    const results = await withTimeout(
      Promise.allSettled([
        api.health(),
        api.getPortfolio(),
        api.rotationStatus(),
        api.impulseStatus(),
        api.getPositions('SWAP'),
        api.getAllTrades(20),
      ]),
      12000
    ).catch(() => null)
    if (results) {
      const [h, pf, rot, imp, pos, tr] = results.map(r => (r.status === 'fulfilled' ? r.value : null))
      if (h) { setConnected(h.connected); setDemoMode(h.demo) }
      if (pf) setPortfolio(pf)
      if (rot) setRotation(rot)
      if (imp) setImpulse(imp)
      if (pos) setPositions(pos?.positions || [])
      if (tr) setTrades(tr?.trades || [])
    }
    setLoaded(true)
    setLoading(false)
  }, [])

  useEffect(() => {
    if (!authing && !authError) load()
  }, [authing, authError, load])

  /* ── Auto-refresh every 30s ── */
  useEffect(() => {
    if (authing || authError) return
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [authing, authError, load])


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
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-[var(--profit)] to-[var(--info)] flex items-center justify-center">
            <Zap size={14} className="text-white" />
          </div>
          <span className="text-sm font-bold text-[var(--txt)]">OKX Terminal</span>
          <span className={`ml-1 flex items-center gap-1 px-1.5 py-0.5 rounded-md text-2xs font-bold ${
            connected ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-[var(--profit)]' : 'bg-[var(--loss)]'}`} />
            {connected ? (demoMode ? 'DEMO' : 'LIVE') : 'OFFLINE'}
          </span>
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
          {trades.length === 0 ? (
            <Card className="text-center py-4 text-xs text-[var(--txt-muted)]">
              {t('mini.no_trades')}
            </Card>
          ) : (
            <Card className="p-0 overflow-hidden">
              <div className="divide-y divide-[var(--border)]">
                {trades.slice(0, 8).map((tr, i) => {
                  const pnl = Number(tr.pnl || 0)
                  const reason = tr.reason || ''
                  const isOpen = reason === 'open'
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
                          {tr.exit_price ? ` • ${t('mini.entry')} ${fmt(tr.entry_price ?? tr.entry)} → ${t('mini.exit')} ${fmt(tr.exit_price)}` : ''}
                        </div>
                      </div>
                      <div className="text-right">
                        <div className={`text-sm font-bold mono ${isOpen ? 'text-[var(--info)]' : pnlClass(pnl)}`}>
                          {isOpen ? fmt(tr.entry_price ?? tr.entry) : pnlSign(pnl)}
                        </div>
                        <div className="text-2xs text-[var(--txt-muted)]">{fmt(tr.size)}</div>
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
          OKX Terminal • {connected ? (demoMode ? 'DEMO' : 'LIVE') : 'OFFLINE'}
        </div>
      </div>
    </div>
  )
}
