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

function MiniAppPageInner() {
  const { t } = useTranslation()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [updatedAt, setUpdatedAt] = useState(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const res = await withTimeout(api.meDashboard(), 15000)
      setData(res || {})
      setUpdatedAt(Date.now())
    } catch (e) {
      setError(e?.message || String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [load])

  const liveConnected = !!(data?.live?.connected)
  const demoOn = !!(data?.demo?.running)

  const metrics = useMemo(() => {
    const d = data?.demo || {}
    const L = data?.live || {}
    let unreal = Number(d.unrealized ?? 0)
    if (!unreal && Array.isArray(d.positions)) {
      unreal = d.positions.reduce((s, p) => s + Number(p.upl || p.unrealized_pnl || 0), 0)
    }
    return {
      total: Number(d.pnl ?? 0),
      today: Number(d.session_pnl ?? 0),
      unreal,
      equity: d.equity ?? null,
      tradesN: Number(d.trades ?? 0),
      liveConnected: !!L.connected,
      liveTotal: Number(L.total_pnl ?? L.strategy_realized ?? 0),
      liveToday: Number(L.session_pnl ?? L.pnl_1d ?? 0),
      liveUnreal: Number(L.unrealized_pnl ?? L.unrealized ?? 0),
      liveEquity: L.equity != null ? Number(L.equity) : null,
      liveWeek: Number(L.week ?? L.pnl_week ?? 0),
    }
  }, [data])

  // DEMO + LIVE positions together (web dashboard style)
  const viewPositions = useMemo(() => {
    const rows = []
    const push = (list, mode) => {
      for (const [i, p] of (list || []).entries()) {
        const coin = (p.coin || p.symbol || p.instId || '').toString().replace('-USDT-SWAP', '').replace('-USD-SWAP', '')
        const side = String(p.side || p.posSide || '').toLowerCase().includes('short') ? 'short' : 'long'
        const upl = Number(p.upl ?? p.unrealized_pnl ?? 0)
        rows.push({
          key: `${mode}-${coin || i}-${side}-${p.size || p.pos || i}`,
          coin: coin || '—',
          side,
          upl,
          mode,
        })
      }
    }
    push(data?.demo?.positions, 'demo')
    if (liveConnected) push(data?.live?.positions, 'live')
    return rows
  }, [data, liveConnected])

  // DEMO + LIVE closed trades, last 3 by time
  const viewTrades = useMemo(() => {
    const all = Array.isArray(data?.trades) ? data.trades : []
    const mapped = all.map((t, i) => {
      const mode = String(t.account_mode || t.mode || 'demo').toLowerCase() === 'live' ? 'live' : 'demo'
      let side = String(t.side || t.pos_side || t.posSide || '').toLowerCase()
      if (side === 'buy' || side === 'long') side = 'long'
      else if (side === 'sell' || side === 'short') side = 'short'
      else side = ''
      const sideLabel = side === 'long' ? 'ЛОНГ' : side === 'short' ? 'ШОРТ' : '—'
      const ts = Number(t.time || t.exit_time || t.ts || 0) || 0
      return {
        key: `${mode}-${t.ord_id || t.entry_ord_id || i}-${ts}`,
        inst: (t.inst || t.inst_id || t.symbol || '—').toString().replace('-USDT-SWAP', ''),
        side,
        sideLabel,
        pnl: Number(t.pnl || 0),
        mode,
        ts,
      }
    })
    mapped.sort((a, b) => (b.ts || 0) - (a.ts || 0))
    return mapped.slice(0, 3)
  }, [data?.trades])

  if (loading && !data) {
    return (
      <div className="min-h-[100dvh] flex items-center justify-center bg-[var(--bg)] text-[var(--txt-muted)] text-sm">
        Загрузка…
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="min-h-[100dvh] flex flex-col items-center justify-center gap-3 p-6 bg-[var(--bg)] text-[var(--txt)]">
        <div className="text-sm font-semibold">Не удалось загрузить</div>
        <div className="text-2xs text-[var(--txt-muted)]">{error}</div>
        <button type="button" onClick={load} className="px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold">Повторить</button>
      </div>
    )
  }

  const pulse = data?.demo?.pulse || data?.demo?.description || ''

  return (
    <div className="h-[100dvh] max-h-[100dvh] overflow-hidden flex flex-col bg-[var(--bg)] text-[var(--txt)]"
      style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}>
      <div className="flex-1 min-h-0 flex flex-col gap-2 p-2.5 overflow-hidden">

        {/* Header: title + LIVE connect badge + refresh */}
        <div className="flex-shrink-0 flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 min-w-0">
            <Zap size={14} className="text-[var(--info)] flex-shrink-0" />
            <span className="text-[0.8rem] font-bold truncate">{t('nav.dashboard') || 'COPIX'}</span>
          </div>
          <div className="flex items-center gap-1.5 flex-shrink-0">
            {/* LIVE connection confirmation only */}
            <span
              className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[0.55rem] font-bold border ${
                liveConnected
                  ? 'border-[var(--profit)]/40 bg-[var(--profit-dim)] text-[var(--profit)]'
                  : 'border-[var(--border)] bg-[var(--surface)] text-[var(--txt-muted)]'
              }`}
              title={liveConnected ? 'Лайф-счёт подключён' : 'Лайф-счёт не подключён'}
            >
              {liveConnected ? <Wifi size={10} /> : <WifiOff size={10} />}
              {liveConnected ? 'ЛАЙФ ОК' : 'ЛАЙФ —'}
            </span>
            <button
              type="button"
              onClick={load}
              className="p-1.5 rounded-lg border border-[var(--border)] bg-[var(--surface)] text-[var(--txt-muted)]"
              aria-label="Обновить"
            >
              <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>

        {/* Dual PnL metrics */}
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

        {/* AI status + pulse */}
        <div className="flex-shrink-0 flex flex-col gap-1 px-2.5 py-1.5 rounded-[10px] border border-[var(--border)] bg-[var(--surface)]">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${demoOn ? 'bg-[var(--profit)]' : 'bg-[var(--txt-muted)]'}`} />
              <Bot size={12} className="text-[var(--txt-muted)] flex-shrink-0" />
              <span className="text-[0.7rem] font-semibold truncate">AI Discretionary</span>
              <span className={`text-[0.55rem] font-bold px-1.5 py-0.5 rounded ${demoOn ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--surface-overlay)] text-[var(--txt-muted)]'}`}>
                {demoOn ? 'ВКЛ' : 'ВЫКЛ'}
              </span>
            </div>
            <span className={`mono text-[0.75rem] font-bold flex-shrink-0 ${pnlClass(metrics.total)}`}>{pnlSign(metrics.total)}</span>
          </div>
          {pulse && (
            <p className="text-[0.65rem] leading-snug text-[var(--txt-secondary)] line-clamp-3">{pulse}</p>
          )}
        </div>

        {/* Positions DEMO + LIVE */}
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
                        {p.side === 'long' ? 'ЛОНГ' : 'ШОРТ'}
                      </span>
                      {p.mode === 'live' ? (
                        <span className="text-[0.5rem] font-bold px-1 py-0.5 rounded bg-[var(--profit)]/10 text-[var(--profit)] border border-[var(--profit)]/30">LIVE</span>
                      ) : (
                        <span className="text-[0.5rem] font-bold px-1 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/30">DEMO</span>
                      )}
                    </div>
                    <span className={`text-[0.75rem] font-bold mono flex-shrink-0 ${pnlClass(p.upl)}`}>{pnlSign(p.upl)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Last 3 trades DEMO + LIVE */}
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
                {viewTrades.map((tr) => (
                  <div key={tr.key} className="flex items-center justify-between gap-2 px-2.5 py-1.5">
                    <div className="flex items-center gap-1.5 min-w-0">
                      {Number(tr.pnl) >= 0
                        ? <ArrowUpRight size={12} className="text-[var(--profit)] flex-shrink-0" />
                        : <ArrowDownRight size={12} className="text-[var(--loss)] flex-shrink-0" />}
                      <div className="text-[0.7rem] font-semibold truncate">
                        {tr.inst || '—'}{' '}
                        <span className={`font-bold text-[0.55rem] ${tr.side === 'long' ? 'text-[var(--profit)]' : tr.side === 'short' ? 'text-[var(--loss)]' : 'text-[var(--txt-muted)]'}`}>
                          {tr.sideLabel}
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
