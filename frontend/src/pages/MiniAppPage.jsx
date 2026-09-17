import React, { useState, useEffect, useCallback, useMemo } from 'react'
import {
  RefreshCw, Zap, Wifi, WifiOff, Bot, ArrowUpRight, ArrowDownRight,
} from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'
import { fmtTs } from '../utils/time'

window.__MINI_APP__ = true

function fmt(n, digits = 2) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  return Number(n).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
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

function withTimeout(promise, ms = 15000) {
  return new Promise((resolve, reject) => {
    const id = setTimeout(() => reject(new Error('timeout')), ms)
    promise.then(
      (v) => { clearTimeout(id); resolve(v) },
      (e) => { clearTimeout(id); reject(e) },
    )
  })
}

class MiniAppErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { err: null }
  }
  static getDerivedStateFromError(err) {
    return { err }
  }
  render() {
    if (this.state.err) {
      return (
        <div className="min-h-[100dvh] flex flex-col items-center justify-center gap-3 p-6 bg-[var(--bg)] text-[var(--txt)]">
          <div className="text-sm font-semibold">Mini App error</div>
          <div className="text-2xs text-[var(--txt-muted)] text-center max-w-xs break-words">
            {String(this.state.err?.message || this.state.err)}
          </div>
          <button
            type="button"
            className="px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

function Card({ children, className = '' }) {
  return (
    <div className={`rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-3.5 ${className}`}>
      {children}
    </div>
  )
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
  const [authing, setAuthing] = useState(true)
  const [authError, setAuthError] = useState('')
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [mode, setMode] = useState('demo') // demo | live — view switch

  const [connected, setConnected] = useState(false)
  const [aiBot, setAiBot] = useState(null)
  const [liveStatus, setLiveStatus] = useState(null)
  const [pnlData, setPnlData] = useState(null)
  const [positions, setPositions] = useState([])
  const [trades, setTrades] = useState([])

  /* ── Telegram auth ── */
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const tg = window.Telegram?.WebApp
        try { tg?.ready?.(); tg?.expand?.() } catch { /* ignore */ }
        const initData = tg?.initData || ''
        if (initData) {
          try {
            const r = await withTimeout(api.authTelegram(initData), 20000)
            if (r?.token) localStorage.setItem('auth_token', r.token)
            if (r?.role) localStorage.setItem('auth_role', r.role)
          } catch (e) {
            // Guest/public mini view still works without token for public endpoints
            if (!localStorage.getItem('auth_token')) {
              console.warn('tg auth', e?.message || e)
            }
          }
        }
      } catch (e) {
        if (!cancelled) setAuthError(String(e?.message || e))
      } finally {
        if (!cancelled) setAuthing(false)
      }
    })()
    return () => { cancelled = true }
  }, [])

  const liveConnected = !!(liveStatus?.connected && liveStatus?.enabled !== false)

  const load = useCallback(async () => {
    setLoading(true)
    const tasks = [
      ['health', () => api.health()],
      ['ai', () => api.aiStatus()],
      ['live', () => api.liveStatus().catch(() => null)],
      ['pnl', () => (api.getPnlSummary ? api.getPnlSummary() : api.getPnl())],
      ['positions', () => api.getPositions('SWAP')],
      ['trades', () => api.getPairedTrades(40)],
    ]
    await Promise.allSettled(tasks.map(async ([name, fn]) => {
      try {
        const v = await withTimeout(fn(), name === 'trades' || name === 'pnl' ? 25000 : 15000)
        if (name === 'health') setConnected(!!v?.connected)
        if (name === 'ai') setAiBot(v)
        if (name === 'live') setLiveStatus(v)
        if (name === 'pnl') setPnlData(v)
        if (name === 'positions') {
          const pos = Array.isArray(v) ? v : (v?.positions || v?.data || [])
          setPositions(Array.isArray(pos) ? pos.filter(Boolean) : [])
        }
        if (name === 'trades') {
          let list = []
          if (Array.isArray(v)) list = v
          else if (Array.isArray(v?.trades)) list = v.trades
          else if (Array.isArray(v?.data)) list = v.data
          setTrades(list || [])
        }
      } catch {
        /* keep previous values */
      }
    }))
    setLoaded(true)
    setLoading(false)
  }, [])

  useEffect(() => {
    if (!authing) load()
  }, [authing, load])

  useEffect(() => {
    if (authing) return undefined
    const id = setInterval(load, 40000)
    return () => clearInterval(id)
  }, [authing, load])

  /* Prefer LIVE view when mirror is on */
  useEffect(() => {
    if (liveConnected) setMode((m) => (m === 'demo' ? 'live' : m))
  }, [liveConnected])

  const metrics = useMemo(() => {
    if (mode === 'live' && liveConnected) {
      const live = liveStatus || {}
      const aiLive = aiBot?.live || {}
      return {
        total: live.total_pnl ?? aiLive.total_pnl ?? 0,
        today: live.session_pnl ?? aiLive.session_pnl ?? 0,
        unreal: live.unrealized_pnl ?? 0,
        equity: live.equity ?? aiLive.equity ?? null,
        capital: live.capital ?? null,
        tradesN: live.lifetime_trades ?? aiLive.lifetime_trades ?? 0,
      }
    }
    const p = pnlData || {}
    return {
      total: p.total_pnl ?? p.total ?? aiBot?.lifetime_pnl ?? aiBot?.total_pnl ?? 0,
      today: p.today_pnl ?? p.today ?? p.day_pnl ?? 0,
      unreal: p.unrealized_pnl ?? p.unrealized ?? 0,
      equity: null,
      capital: aiBot?.capital ?? null,
      tradesN: aiBot?.lifetime_trades ?? aiBot?.total_trades ?? 0,
    }
  }, [mode, liveConnected, liveStatus, aiBot, pnlData])

  const viewPositions = useMemo(() => {
    if (mode === 'live' && liveConnected) {
      const fromLive = liveStatus?.open_positions || aiBot?.live?.open_positions || []
      if (fromLive.length) return fromLive.map((p) => ({
        instId: p.instId || p.inst_id || `${p.coin || ''}-USDT-SWAP`,
        side: (p.side || p.posSide || '').toLowerCase().includes('short') ? 'short' : 'long',
        px: Number(p.avgPx || p.entry || p.entry_price || 0),
        mark: Number(p.markPx || p.mark || 0),
        size: Number(p.pos || p.size || p.sz || 0),
        lev: Number(p.lever || p.leverage || 0),
        upl: Number(p.upl || p.unrealized_pnl || 0),
        bot: 'AI',
      }))
    }
    const fromAi = aiBot?.open_positions || []
    if (fromAi.length) {
      return fromAi.map((p) => ({
        instId: p.inst_id || p.instId || `${p.coin || ''}-USDT-SWAP`,
        side: (p.side || 'long').toLowerCase(),
        px: Number(p.entry_price || p.entry || 0),
        mark: Number(p.mark || 0),
        size: Number(p.size || p.sz || 0),
        lev: Number(p.leverage || 0),
        upl: Number(p.unrealized_pnl || p.upl || 0),
        bot: 'AI',
      }))
    }
    return (positions || []).map((p) => {
      const raw = Number(p.pos || p.size || 0)
      const side = (p.posSide || p.side || (raw < 0 ? 'short' : 'long')).toLowerCase()
      return {
        instId: p.instId || p.inst_id,
        side: side.includes('short') ? 'short' : 'long',
        px: Number(p.avgPx || p.avg_px || 0),
        mark: Number(p.markPx || 0),
        size: Math.abs(raw),
        lev: Number(p.lever || p.leverage || 0),
        upl: Number(p.upl || 0),
        bot: p.bot_label || p.bot || 'AI',
      }
    }).filter((p) => p.size > 0)
  }, [mode, liveConnected, liveStatus, aiBot, positions])

  const viewTrades = useMemo(() => {
    const list = Array.isArray(trades) ? trades : []
    const filtered = mode === 'live'
      ? list.filter((tr) => String(tr.account_mode || tr.mode || '').toLowerCase() === 'live'
        || String(tr.bot_id || '').includes('live'))
      : list.filter((tr) => String(tr.account_mode || tr.mode || 'demo').toLowerCase() !== 'live')
    const src = filtered.length ? filtered : (mode === 'live' ? [] : list)
    return src.slice(0, 8).map((tr) => {
      const pnl = Number(tr.pnl ?? tr.realized_pnl ?? 0)
      const closed = tr.state === 'closed' || tr.closed || tr.exit_price != null || (tr.pnl != null && tr.side !== 'open')
      return {
        time: tr.time || tr.timestamp || tr.closed_at || tr.created_at,
        inst: (tr.inst_id || tr.symbol || tr.inst || '').replace('-USDT-SWAP', ''),
        side: (tr.side || tr.pos_side || '').toLowerCase(),
        pnl,
        closed,
      }
    })
  }, [trades, mode])

  const aiRunning = !!(aiBot?.running)
  const pulse = aiBot?.pulse || aiBot?.description || aiBot?.last_decision?.reason || ''

  if (authing) {
    return (
      <div className="min-h-[100dvh] flex items-center justify-center bg-[var(--bg)] text-[var(--txt-muted)] text-sm">
        {t('mini.loading') || 'Loading…'}
      </div>
    )
  }

  if (authError && !loaded) {
    return (
      <div className="min-h-[100dvh] flex flex-col items-center justify-center gap-3 p-6 bg-[var(--bg)]">
        <div className="text-sm font-semibold text-[var(--txt)]">{t('mini.auth_error') || 'Auth error'}</div>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-[var(--info)] text-white text-sm font-semibold"
        >
          <RefreshCw size={15} />
          {t('mini.reload') || 'Refresh'}
        </button>
      </div>
    )
  }

  return (
    <div
      className="h-[100dvh] max-h-[100dvh] flex flex-col bg-[var(--bg)] text-[var(--txt)] overflow-hidden"
      style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}
    >
      {/* Header */}
      <div className="flex-shrink-0 flex items-center justify-between gap-2 px-3 py-2.5 bg-[var(--surface)]/95 backdrop-blur-md border-b border-[var(--border)]">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-[var(--info)] to-[#4a3fd1] flex items-center justify-center shadow">
            <Zap size={15} className="text-white" />
          </div>
          <div className="min-w-0">
            <div className="text-sm font-bold leading-none">COPIX</div>
            <div className="text-[0.6rem] text-[var(--txt-muted)] mt-0.5 truncate">AI · 1H</div>
          </div>
          <span className={`flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[0.6rem] font-bold ${
            connected ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-[var(--profit)] animate-pulse' : 'bg-[var(--loss)]'}`} />
            {connected ? 'ON' : 'OFF'}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {/* Demo / Live switch */}
          <div className="flex rounded-lg border border-[var(--border)] overflow-hidden text-[0.65rem] font-bold">
            <button
              type="button"
              onClick={() => setMode('demo')}
              className={`px-2 py-1 ${mode === 'demo' ? 'bg-[var(--info)] text-white' : 'text-[var(--txt-muted)]'}`}
            >
              DEMO
            </button>
            <button
              type="button"
              onClick={() => liveConnected && setMode('live')}
              disabled={!liveConnected}
              className={`px-2 py-1 flex items-center gap-0.5 ${
                mode === 'live' ? 'bg-[var(--profit)] text-white' : 'text-[var(--txt-muted)]'
              } ${!liveConnected ? 'opacity-40' : ''}`}
              title={liveConnected ? 'LIVE mirror' : 'LIVE not connected'}
            >
              {liveConnected ? <Wifi size={10} /> : <WifiOff size={10} />}
              LIVE
            </button>
          </div>
          <button
            type="button"
            className="p-1.5 rounded-lg hover:bg-[var(--surface-raised)]"
            onClick={load}
            disabled={loading}
            title={t('mini.reload') || 'Refresh'}
          >
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      <div
        className="flex-1 min-h-0 overflow-y-auto overscroll-y-contain px-3 py-3 space-y-3"
        style={{ WebkitOverflowScrolling: 'touch', touchAction: 'pan-y' }}
      >
        {/* PnL */}
        <Card>
          <div className="flex items-center justify-between mb-2.5">
            <span className="text-[0.65rem] font-bold uppercase tracking-wider text-[var(--txt-muted)]">
              {mode === 'live' ? 'LIVE PnL' : 'DEMO PnL'}
            </span>
            {metrics.equity != null && (
              <span className="text-[0.65rem] text-[var(--txt-muted)] mono">
                Eq ${fmt(metrics.equity)}
                {metrics.capital != null ? ` · Cap $${fmt(metrics.capital, 0)}` : ''}
              </span>
            )}
          </div>
          <div className="grid grid-cols-3 gap-2">
            <Metric label={t('mini.total_pnl') || 'Total'} value={pnlSign(metrics.total)} className={pnlClass(metrics.total)} />
            <Metric label={t('mini.today') || 'Today'} value={pnlSign(metrics.today)} className={pnlClass(metrics.today)} />
            <Metric label={t('mini.unrealized') || 'Open'} value={pnlSign(metrics.unreal)} className={pnlClass(metrics.unreal)} />
          </div>
        </Card>

        {/* AI bot */}
        <Card>
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2 min-w-0">
              <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${
                aiRunning ? 'bg-[var(--profit-dim)]' : 'bg-[var(--surface-overlay)]'
              }`}>
                <Bot size={18} className={aiRunning ? 'text-[var(--profit)]' : 'text-[var(--txt-muted)]'} />
              </div>
              <div className="min-w-0">
                <div className="text-sm font-bold truncate">AI Discretionary 1H</div>
                <div className="text-[0.65rem] text-[var(--txt-muted)]">
                  {aiBot?.model || aiBot?.llm?.model || 'LLM'} · сделок {metrics.tradesN}
                </div>
              </div>
            </div>
            <span className={`flex-shrink-0 px-2 py-1 rounded-lg text-[0.65rem] font-bold ${
              aiRunning
                ? 'bg-[var(--profit-dim)] text-[var(--profit)]'
                : 'bg-[var(--surface-overlay)] text-[var(--txt-muted)]'
            }`}>
              {aiRunning ? (t('mini.running') || 'ON') : (t('mini.stopped') || 'OFF')}
            </span>
          </div>
          {pulse ? (
            <p className="mt-2.5 text-xs leading-snug text-[var(--txt-secondary)] line-clamp-3">
              {pulse}
            </p>
          ) : null}
        </Card>

        {/* Positions */}
        <div>
          <div className="flex items-center justify-between mb-1.5 px-0.5">
            <h2 className="text-[0.65rem] font-bold uppercase tracking-wider text-[var(--txt-muted)]">
              {t('mini.positions') || 'Positions'}
            </h2>
            <span className="text-[0.65rem] text-[var(--txt-muted)]">{viewPositions.length}</span>
          </div>
          {viewPositions.length === 0 ? (
            <Card className="py-4 text-center text-xs text-[var(--txt-muted)]">
              {t('mini.no_positions') || 'No open positions'}
            </Card>
          ) : (
            <div className="space-y-2 max-h-[40vh] overflow-y-auto overscroll-y-contain" style={{ WebkitOverflowScrolling: 'touch' }}>
              {viewPositions.map((p, i) => (
                <Card key={`${p.instId}-${i}`} className="py-2.5">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="text-xs font-bold truncate">{(p.instId || '').replace('-USDT-SWAP', '')}</span>
                      <span className={`text-[0.6rem] font-bold px-1.5 py-0.5 rounded ${
                        p.side === 'long'
                          ? 'bg-[var(--profit-dim)] text-[var(--profit)]'
                          : 'bg-[var(--loss-dim)] text-[var(--loss)]'
                      }`}>
                        {p.side === 'long' ? 'LONG' : 'SHORT'}
                      </span>
                    </div>
                    <span className={`text-xs font-bold mono ${pnlClass(p.upl)}`}>{pnlSign(p.upl)}</span>
                  </div>
                  <div className="text-[0.65rem] text-[var(--txt-muted)] mono">
                    ${fmt(p.px)}{p.lev ? ` · ${fmt(p.lev, 1)}x` : ''}{p.size ? ` · ${fmt(p.size, 3)}` : ''}
                  </div>
                </Card>
              ))}
            </div>
          )}
        </div>

        {/* Trades */}
        <div className="pb-6">
          <h2 className="text-[0.65rem] font-bold uppercase tracking-wider text-[var(--txt-muted)] mb-1.5 px-0.5">
            {t('mini.last_trades') || 'Recent trades'}
          </h2>
          <Card className="p-0 overflow-hidden">
            {viewTrades.length === 0 ? (
              <div className="py-4 text-center text-xs text-[var(--txt-muted)]">
                {t('mini.no_trades') || 'No trades yet'}
              </div>
            ) : (
              <div className="divide-y divide-[var(--border)]">
                {viewTrades.map((tr, i) => (
                  <div key={i} className="flex items-center justify-between gap-2 px-3 py-2.5">
                    <div className="flex items-center gap-1.5 min-w-0">
                      {tr.closed ? (
                        Number(tr.pnl) >= 0
                          ? <ArrowUpRight size={14} className="text-[var(--profit)] flex-shrink-0" />
                          : <ArrowDownRight size={14} className="text-[var(--loss)] flex-shrink-0" />
                      ) : (
                        <span className="w-3.5 h-3.5 rounded-full border-2 border-[var(--info)] flex-shrink-0" />
                      )}
                      <div className="min-w-0">
                        <div className="text-xs font-semibold truncate">
                          {tr.inst || '—'}{' '}
                          <span className="text-[var(--txt-muted)] font-normal">{tr.side}</span>
                        </div>
                        <div className="text-[0.6rem] text-[var(--txt-muted)]">
                          {tr.time ? fmtTs(tr.time, 'ru-RU') : ''}
                        </div>
                      </div>
                    </div>
                    <span className={`text-xs font-bold mono flex-shrink-0 ${tr.closed ? pnlClass(tr.pnl) : 'text-[var(--txt-muted)]'}`}>
                      {tr.closed ? pnlSign(tr.pnl) : '—'}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}

export default function MiniAppPage(props) {
  return (
    <MiniAppErrorBoundary>
      <MiniAppPageInner {...props} />
    </MiniAppErrorBoundary>
  )
}
