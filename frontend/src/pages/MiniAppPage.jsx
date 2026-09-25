import React, { useState, useEffect, useCallback, useMemo } from 'react'
import {
  RefreshCw, Zap, Wifi, WifiOff, Bot, ArrowUpRight, ArrowDownRight,
} from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

window.__MINI_APP__ = true

function fmt(n, digits = 2) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function pnlClass(v) {
  const n = Number(v)
  if (!n) return 'text-[var(--txt-secondary)]'
  return n > 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'
}

function pnlSign(v) {
  const n = Number(v) || 0
  if (!n) return '$0.00'
  return (n > 0 ? '+$' : '-$') + fmt(Math.abs(n))
}

function dualPnl(demoVal, liveVal, liveConnected) {
  const left = pnlSign(demoVal)
  const right = liveConnected ? pnlSign(liveVal) : '—'
  return (
    <span className="inline-flex items-baseline gap-0.5 flex-wrap mono leading-tight">
      <span className={pnlClass(demoVal)} title="Демо">{left}</span>
      <span className="text-[var(--txt-muted)] font-normal text-[0.85em]">/</span>
      <span className={liveConnected ? pnlClass(liveVal) : 'text-[var(--txt-muted)]'} title="Лайф">{right}</span>
    </span>
  )
}

function withTimeout(promise, ms = 15000) {
  return new Promise((resolve, reject) => {
    const id = setTimeout(() => reject(new Error('timeout')), ms)
    promise.then(v => { clearTimeout(id); resolve(v) }, e => { clearTimeout(id); reject(e) })
  })
}

class MiniAppErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { err: null } }
  static getDerivedStateFromError(err) { return { err } }
  render() {
    if (this.state.err) {
      return (
        <div className="min-h-[100dvh] flex flex-col items-center justify-center gap-3 p-6 bg-[var(--bg)] text-[var(--txt)]">
          <div className="text-sm font-semibold">Ошибка</div>
          <div className="text-2xs text-[var(--txt-muted)] text-center max-w-xs break-words">{String(this.state.err?.message || this.state.err)}</div>
          <button type="button" className="px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold" onClick={() => window.location.reload()}>Обновить</button>
        </div>
      )
    }
    return this.props.children
  }
}

function Card({ children, className = '' }) {
  return <div className={`rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-3.5 ${className}`}>{children}</div>
}

function Metric({ label, value, className = '' }) {
  return (
    <div className="flex flex-col gap-0.5 min-w-0">
      <span className="text-[0.65rem] uppercase tracking-wide text-[var(--txt-muted)] font-semibold">{label}</span>
      <span className={`text-sm font-bold mono truncate ${className}`}>{value}</span>
    </div>
  )
}

function MiniAppPageInner() {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(false)
  const [data, setData] = useState(null)
  const [mode, setMode] = useState('demo')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const tg = window.Telegram?.WebApp
        try { tg?.ready?.(); tg?.expand?.() } catch {}
        const initData = tg?.initData || ''
        if (initData) {
          try {
            const r = await withTimeout(api.telegramAuth(initData), 20000)
            if (r?.token) localStorage.setItem('auth_token', r.token)
            if (r?.role) localStorage.setItem('auth_role', r.role)
          } catch {}
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const d = await withTimeout(api.meDashboard(), 20000)
      setData(d)
    } catch {}
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [])

  const isLive = mode === 'live' && data?.live?.connected

  const metrics = useMemo(() => {
    const d = data?.demo || {}
    const L = data?.live || {}
    let unreal = Number(d.unrealized ?? 0)
    if (!unreal && Array.isArray(d.positions)) {
      unreal = d.positions.reduce((s, p) => s + Number(p.upl || p.unrealized_pnl || 0), 0)
    }
    return {
      // DEMO
      total: Number(d.pnl ?? 0),
      today: Number(d.session_pnl ?? 0),
      unreal,
      equity: d.equity ?? null,
      tradesN: Number(d.trades ?? 0),
      winRate: d.win_rate,
      // LIVE
      liveConnected: !!L.connected,
      liveTotal: Number(L.total_pnl ?? L.strategy_realized ?? 0),
      liveToday: Number(L.session_pnl ?? 0),
      liveUnreal: Number(L.unrealized ?? 0),
      liveEquity: L.equity != null ? Number(L.equity) : null,
      liveTradesN: Number(L.trades ?? 0),
    }
  }, [data])

  const viewPositions = useMemo(() => {
    const src = isLive ? (data?.live?.positions || []) : (data?.demo?.positions || [])
    const CT = { BTC: 0.01, ETH: 0.1, SOL: 1, XRP: 100, DOGE: 1000, BNB: 0.01, ADA: 10, AVAX: 1, LTC: 0.1, BCH: 0.1, TRX: 1000, OKB: 0.1 }
    return src.map((p, i) => {
      const size = Math.abs(Number(p.size || p.sz || p.size_remaining || p.pos || 0))
      const entry = Number(p.entry_price || p.entry || p.avgPx || p.avg_px || p.px || 0)
      const mark = Number(p.mark_price || p.mark || p.mark_px || p.markPx || 0)
      const upl = Number(p.upl || p.unrealized_pnl || 0)
      const coin = String(p.coin || p.symbol || p.inst_id || '').replace('-USDT-SWAP', '').replace('-USD-SWAP', '').toUpperCase()
      const ct = CT[coin] || 1
      const px = mark || entry
      const notional = size && px ? size * ct * px : 0
      return {
        key: `${coin || i}-${p.side}-${isLive ? 'live' : 'demo'}`,
        coin,
        side: (p.side || p.pos_side || 'long').toLowerCase().includes('short') ? 'short' : 'long',
        size,
        entry,
        mark,
        upl,
        notional,
        lever: Number(p.leverage || p.lever || 0),
        mode: isLive ? 'live' : 'demo',
      }
    }).filter(p => p.size > 0 || Math.abs(p.upl) > 0)
  }, [isLive, data])

  const viewTrades = useMemo(() => {
    const all = data?.trades || []
    const filtered = isLive
      ? all.filter(t => String(t.account_mode || '').toLowerCase() === 'live')
      : all.filter(t => {
          const m = String(t.account_mode || t.mode || '').toLowerCase()
          return !m || m === 'demo'
        })
    // Client-side dedupe (ord_id or time+inst+pnl)
    const seen = new Set()
    const unique = []
    for (const t of filtered) {
      const oid = String(t.ord_id || '').trim()
      let key
      if (oid) key = `o:${oid}`
      else {
        const ts = String(t.time || '')
        const inst = String(t.inst || t.symbol || '').replace(/-USDT-SWAP/i, '')
        const pnl = Number(t.pnl || 0).toFixed(2)
        key = `f:${ts}|${inst}|${pnl}`
      }
      if (seen.has(key)) continue
      seen.add(key)
      unique.push(t)
    }
    return unique.slice(0, 3).map(t => {
      let side = String(t.side || t.pos_side || '').toLowerCase()
      // Normalize buy/sell leftovers → position direction
      if (side === 'buy') side = 'long'
      if (side === 'sell') side = 'short'
      if (side !== 'long' && side !== 'short') side = side || '—'
      return {
        time: t.time || t.timestamp || '',
        inst: (t.inst || t.symbol || '').replace('-USDT-SWAP', ''),
        side,
        sideLabel: side === 'long' ? 'LONG' : side === 'short' ? 'SHORT' : side,
        pnl: Number(t.pnl || 0),
        mode: isLive ? 'live' : 'demo',
      }
    })
  }, [data?.trades, isLive])

  if (loading) {
    return <div className="min-h-[100dvh] flex items-center justify-center bg-[var(--bg)] text-[var(--txt-muted)] text-sm">Загрузка…</div>
  }

  if (!data) {
    return <div className="min-h-[100dvh] flex items-center justify-center bg-[var(--bg)] text-[var(--txt-muted)] text-sm">Настройте Telegram бота</div>
  }

  const demoOn = !!data?.demo?.running

  return (
    <div
      className="h-[100dvh] max-h-[100dvh] flex flex-col bg-[var(--bg)] text-[var(--txt)] overflow-hidden"
      style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}
    >
      {/* Header — compact */}
      <div className="flex-shrink-0 flex items-center justify-between gap-2 px-2.5 py-1.5 bg-[var(--surface)] border-b border-[var(--border)]">
        <div className="flex items-center gap-1.5 min-w-0">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-[var(--info)] to-[#4a3fd1] flex items-center justify-center">
            <Zap size={13} className="text-white" />
          </div>
          <span className="text-sm font-bold tracking-tight">COPIX</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="flex rounded-lg border border-[var(--border)] overflow-hidden text-[0.6rem] font-bold">
            <button type="button" onClick={() => setMode('demo')} className={`px-2 py-1 ${mode === 'demo' ? 'bg-[var(--info)] text-white' : 'text-[var(--txt-muted)]'}`}>DEMO</button>
            <button
              type="button"
              onClick={() => setMode('live')}
              disabled={!data?.live?.connected}
              className={`px-2 py-1 flex items-center gap-0.5 ${mode === 'live' ? 'bg-[var(--profit)] text-white' : 'text-[var(--txt-muted)]'} ${!data?.live?.connected ? 'opacity-40' : ''}`}
            >
              {data?.live?.connected ? <Wifi size={9} /> : <WifiOff size={9} />}LIVE
            </button>
          </div>
          <button type="button" onClick={load} className="p-1.5 rounded-lg text-[var(--txt-muted)] active:bg-[var(--surface-overlay)]" aria-label="Refresh">
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Body — one screen: metrics → AI → positions → 3 trades */}
      <div className="flex-1 min-h-0 flex flex-col gap-1.5 p-2 overflow-hidden">
        {/* 2×2 metrics */}
        <div className="flex-shrink-0 grid grid-cols-2 gap-1.5">
          <div className="rounded-[10px] border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5">
            <div className="text-[0.55rem] uppercase tracking-wide text-[var(--txt-muted)] font-semibold">Нереализ. <span className="normal-case opacity-70">демо/лайф</span></div>
            <div className="text-[0.95rem] font-bold leading-tight">{dualPnl(metrics.unreal, metrics.liveUnreal, metrics.liveConnected)}</div>
          </div>
          <div className="rounded-[10px] border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5">
            <div className="text-[0.55rem] uppercase tracking-wide text-[var(--txt-muted)] font-semibold">Сегодня <span className="normal-case opacity-70">демо/лайф</span></div>
            <div className="text-[0.95rem] font-bold leading-tight">{dualPnl(metrics.today, metrics.liveToday, metrics.liveConnected)}</div>
          </div>
          <div className="rounded-[10px] border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5">
            <div className="text-[0.55rem] uppercase tracking-wide text-[var(--txt-muted)] font-semibold">Сумма <span className="normal-case opacity-70">демо/лайф</span></div>
            <div className="text-[0.95rem] font-bold leading-tight">{dualPnl(metrics.total, metrics.liveTotal, metrics.liveConnected)}</div>
          </div>
          <div className="rounded-[10px] border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5">
            <div className="text-[0.55rem] uppercase tracking-wide text-[var(--txt-muted)] font-semibold">Equity <span className="normal-case opacity-70">демо/лайф</span></div>
            <div className="text-[0.95rem] font-bold mono leading-tight text-[var(--txt)]">
              <span title="Демо">{metrics.equity != null ? `$${fmt(metrics.equity, 0)}` : '—'}</span>
              <span className="text-[var(--txt-muted)] mx-0.5">/</span>
              <span title="Лайф" className={metrics.liveConnected ? '' : 'text-[var(--txt-muted)]'}>
                {metrics.liveConnected && metrics.liveEquity != null ? `$${fmt(metrics.liveEquity, 0)}` : '—'}
              </span>
            </div>
          </div>
        </div>

        {/* AI status + Russian pulse */}
        <div className="flex-shrink-0 flex flex-col gap-1 px-2.5 py-1.5 rounded-[10px] border border-[var(--border)] bg-[var(--surface)]">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${demoOn || isLive ? 'bg-[var(--profit)]' : 'bg-[var(--txt-muted)]'}`} />
              <Bot size={12} className="text-[var(--txt-muted)] flex-shrink-0" />
              <span className="text-[0.7rem] font-semibold truncate">AI Discretionary</span>
              <span className={`text-[0.55rem] font-bold px-1.5 py-0.5 rounded ${demoOn || (isLive && data?.live) ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--surface-overlay)] text-[var(--txt-muted)]'}`}>
                {isLive ? (data?.live?.connected ? 'ЛАЙФ' : 'ВЫКЛ') : (demoOn ? 'ВКЛ' : 'ВЫКЛ')}
              </span>
            </div>
            <span className={`mono text-[0.75rem] font-bold flex-shrink-0 ${pnlClass(metrics.total)}`}>{pnlSign(metrics.total)}</span>
          </div>
          {(data?.demo?.pulse || data?.demo?.description) && !isLive && (
            <p className="text-[0.65rem] leading-snug text-[var(--txt-secondary)] line-clamp-3">
              {data.demo.pulse || data.demo.description}
            </p>
          )}
          {isLive && (data?.demo?.pulse || data?.live?.pulse) && (
            <p className="text-[0.65rem] leading-snug text-[var(--txt-secondary)] line-clamp-3">
              {data?.live?.pulse || data?.demo?.pulse || data?.demo?.description}
            </p>
          )}
        </div>

        {/* Positions */}
        <div className="flex-1 min-h-0 flex flex-col gap-1 overflow-hidden">
          <div className="flex-shrink-0 flex items-center justify-between px-0.5">
            <h2 className="text-[0.6rem] font-bold uppercase tracking-wider text-[var(--txt-muted)]">Позиции</h2>
            <span className="text-[0.6rem] text-[var(--txt-muted)] mono">{viewPositions.length}</span>
          </div>
          {viewPositions.length === 0 ? (
            <div className="rounded-[10px] border border-[var(--border)] bg-[var(--surface)] py-3 text-center text-[0.7rem] text-[var(--txt-muted)]">Нет открытых</div>
          ) : (
            <div className="flex-1 min-h-0 overflow-y-auto overscroll-y-contain space-y-1" style={{ WebkitOverflowScrolling: 'touch' }}>
              {viewPositions.map(p => (
                <div key={p.key} className="rounded-[10px] border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1 min-w-0 flex-wrap">
                      <span className="text-[0.75rem] font-bold">{p.coin}</span>
                      <span className={`text-[0.55rem] font-bold px-1 py-0.5 rounded ${p.side === 'long' ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                        {p.side === 'long' ? 'LONG' : 'SHORT'}
                      </span>
                      {p.mode === 'live' ? (
                        <span className="text-[0.5rem] font-bold px-1 py-0.5 rounded bg-[var(--profit)]/10 text-[var(--profit)] border border-[var(--profit)]/30">LIVE</span>
                      ) : (
                        <span className="text-[0.5rem] font-bold px-1 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/30">DEMO</span>
                      )}
                    </div>
                    <span className={`text-[0.75rem] font-bold mono flex-shrink-0 ${pnlClass(p.upl)}`}>{pnlSign(p.upl)}</span>
                  </div>
                  <div className="mt-0.5 text-[0.6rem] text-[var(--txt-muted)] mono flex flex-wrap gap-x-2">
                    {p.notional > 0 && <span>${fmt(p.notional, 0)}</span>}
                    {p.entry > 0 && <span>вх {fmt(p.entry, p.entry >= 100 ? 1 : 3)}</span>}
                    {p.mark > 0 && <span>мрк {fmt(p.mark, p.mark >= 100 ? 1 : 3)}</span>}
                    {p.lever > 0 && <span>{fmt(p.lever, 1)}x</span>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Last 3 trades */}
        <div className="flex-shrink-0 flex flex-col gap-1">
          <div className="flex items-center justify-between px-0.5">
            <h2 className="text-[0.6rem] font-bold uppercase tracking-wider text-[var(--txt-muted)]">Сделки</h2>
            <span className="text-[0.55rem] text-[var(--txt-muted)]">последние 3</span>
          </div>
          <div className="rounded-[10px] border border-[var(--border)] bg-[var(--surface)] overflow-hidden">
            {viewTrades.length === 0 ? (
              <div className="py-2.5 text-center text-[0.7rem] text-[var(--txt-muted)]">Нет сделок</div>
            ) : (
              <div className="divide-y divide-[var(--border)]">
                {viewTrades.map((tr, i) => (
                  <div key={i} className="flex items-center justify-between gap-2 px-2.5 py-1.5">
                    <div className="flex items-center gap-1.5 min-w-0">
                      {Number(tr.pnl) >= 0
                        ? <ArrowUpRight size={12} className="text-[var(--profit)] flex-shrink-0" />
                        : <ArrowDownRight size={12} className="text-[var(--loss)] flex-shrink-0" />}
                      <div className="text-[0.7rem] font-semibold truncate">
                        {tr.inst || '—'}{' '}
                        <span className={`font-bold text-[0.55rem] ${tr.side === 'long' ? 'text-[var(--profit)]' : tr.side === 'short' ? 'text-[var(--loss)]' : 'text-[var(--txt-muted)]'}`}>
                          {tr.sideLabel || tr.side || '—'}
                        </span>
                        {tr.mode === 'live' ? (
                          <span className="ml-1 text-[0.5rem] font-bold px-1 py-0.5 rounded bg-[var(--profit)]/10 text-[var(--profit)] border border-[var(--profit)]/30">LIVE</span>
                        ) : (
                          <span className="ml-1 text-[0.5rem] font-bold px-1 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/30">DEMO</span>
                        )}
                      </div>
                    </div>
                    <span className={`text-[0.7rem] font-bold mono flex-shrink-0 ${pnlClass(tr.pnl)}`}>{pnlSign(tr.pnl)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}


export default function MiniAppPage(props) {
  return <MiniAppErrorBoundary><MiniAppPageInner {...props} /></MiniAppErrorBoundary>
}