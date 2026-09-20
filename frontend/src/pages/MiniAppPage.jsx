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
    if (isLive && data?.live) {
      return {total: data.live.total_pnl ?? data.live.unrealized ?? 0, today: data.live.session_pnl ?? 0, unreal: data.live.unrealized ?? 0, equity: data.live.equity ?? null, tradesN: data.live.trades ?? 0}
    }
    const d = data?.demo || {}
    return {total: d.pnl ?? 0, today: d.session_pnl ?? 0, unreal: 0, equity: d.equity ?? null, capital: d.capital ?? null, tradesN: d.trades ?? 0}
  }, [isLive, data])

  const viewPositions = useMemo(() => {
    const src = isLive ? (data?.live?.positions || []) : (data?.demo?.positions || [])
    return src.map((p, i) => ({
      key: `${p.coin || p.symbol || i}-${p.side}`,
      coin: (p.coin || p.symbol || '').replace('-USDT-SWAP', ''),
      side: (p.side || 'long').toLowerCase(),
      size: Number(p.size || p.sz || 0),
      entry: Number(p.entry_price || p.entry || p.px || 0),
      mark: Number(p.mark_price || p.mark || 0),
      upl: Number(p.upl || p.unrealized_pnl || 0),
      lever: Number(p.leverage || p.lever || 0),
    })).filter(p => p.size > 0)
  }, [isLive, data])

  const viewTrades = useMemo(() => {
    const all = data?.trades || []
    const filtered = isLive ? all.filter(t => t.account_mode === 'live') : all.filter(t => t.account_mode !== 'live')
    return (filtered.length ? filtered : all).slice(0, 10).map(t => ({
      time: t.time || t.timestamp || '',
      inst: (t.inst || t.symbol || '').replace('-USDT-SWAP', ''),
      side: (t.side || '').toLowerCase(),
      pnl: Number(t.pnl || 0),
    }))
  }, [data?.trades, isLive])

  if (loading) {
    return <div className="min-h-[100dvh] flex items-center justify-center bg-[var(--bg)] text-[var(--txt-muted)] text-sm">Загрузка…</div>
  }

  if (!data) {
    return <div className="min-h-[100dvh] flex items-center justify-center bg-[var(--bg)] text-[var(--txt-muted)] text-sm">Настройте Telegram бота</div>
  }

  return (
    <div className="h-[100dvh] max-h-[100dvh] flex flex-col bg-[var(--bg)] text-[var(--txt)] overflow-hidden" style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}>
      <div className="flex-shrink-0 flex items-center justify-between gap-2 px-3 py-2.5 bg-[var(--surface)]/95 backdrop-blur-md border-b border-[var(--border)]">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-[var(--info)] to-[#4a3fd1] flex items-center justify-center shadow">
            <Zap size={15} className="text-white" />
          </div>
          <div className="min-w-0">
            <div className="text-sm font-bold leading-none">COPIX</div>
            <div className="text-[0.6rem] text-[var(--txt-muted)] mt-0.5 truncate">AI · 1H</div>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="flex rounded-lg border border-[var(--border)] overflow-hidden text-[0.65rem] font-bold">
            <button type="button" onClick={() => setMode('demo')} className={`px-2 py-1 ${mode === 'demo' ? 'bg-[var(--info)] text-white' : 'text-[var(--txt-muted)]'}`}>DEMO</button>
            <button type="button" onClick={() => setMode('live')} disabled={!data?.live?.connected} className={`px-2 py-1 flex items-center gap-0.5 ${mode === 'live' ? 'bg-[var(--profit)] text-white' : 'text-[var(--txt-muted)'} ${!data?.live?.connected ? 'opacity-40' : ''}`}>
              {data?.live?.connected ? <Wifi size={10} /> : <WifiOff size={10} />}LIVE
            </button>
          </div>
          <button type="button" className="p-1.5 rounded-lg hover:bg-[var(--surface-raised)]" onClick={load} disabled={loading}>
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto overscroll-y-contain px-3 py-3 space-y-3" style={{ WebkitOverflowScrolling: 'touch', touchAction: 'pan-y' }}>
        <Card>
          <div className="flex items-center justify-between mb-2.5">
            <span className="text-[0.65rem] font-bold uppercase tracking-wider text-[var(--txt-muted)]">{isLive ? 'LIVE PnL' : 'DEMO PnL'}</span>
            {metrics.equity != null && (
              <span className="text-[0.65rem] text-[var(--txt-muted)] mono">Eq ${fmt(metrics.equity)}{metrics.capital != null ? ` · Cap $${fmt(metrics.capital, 0)}` : ''}</span>
            )}
          </div>
          <div className="grid grid-cols-3 gap-2">
            <Metric label="Всего" value={pnlSign(metrics.total)} className={pnlClass(metrics.total)} />
            <Metric label="Сегодня" value={pnlSign(metrics.today)} className={pnlClass(metrics.today)} />
            <Metric label="Открыто" value={pnlSign(metrics.unreal)} className={pnlClass(metrics.unreal)} />
          </div>
        </Card>

        {!isLive && (
          <Card>
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${data?.demo?.running ? 'bg-[var(--profit-dim)]' : 'bg-[var(--surface-overlay)]'}`}>
                  <Bot size={18} className={data?.demo?.running ? 'text-[var(--profit)]' : 'text-[var(--txt-muted)]'} />
                </div>
                <div className="min-w-0">
                  <div className="text-sm font-bold truncate">AI Discretionary 1H</div>
                  <div className="text-[0.65rem] text-[var(--txt-muted)]">{data?.demo?.model || 'LLM'} · сделок {metrics.tradesN}{data?.demo?.win_rate ? ` · WR ${data?.demo?.win_rate}%` : ''}</div>
                </div>
              </div>
              <span className={`flex-shrink-0 px-2 py-1 rounded-lg text-[0.65rem] font-bold ${data?.demo?.running ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--surface-overlay)] text-[var(--txt-muted)]'}`}>
                {data?.demo?.running ? 'ON' : 'OFF'}
              </span>
            </div>
            {data?.demo?.pulse && <p className="mt-2.5 text-xs leading-snug text-[var(--txt-secondary)] line-clamp-3">{data?.demo?.pulse}</p>}
          </Card>
        )}

        {isLive && (
          <Card>
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <div className="w-9 h-9 rounded-xl flex items-center justify-center bg-[var(--profit-dim)]">
                  <Bot size={18} className="text-[var(--profit)]" />
                </div>
                <div className="min-w-0">
                  <div className="text-sm font-bold truncate">LIVE Mirror</div>
                  <div className="text-[0.65rem] text-[var(--txt-muted)]">сделок {metrics.tradesN}{data?.live?.win_rate != null ? ` · WR ${data?.live?.win_rate}%` : ''}</div>
                </div>
              </div>
              <span className="flex-shrink-0 px-2 py-1 rounded-lg text-[0.65rem] font-bold bg-[var(--profit-dim)] text-[var(--profit)]">LIVE</span>
            </div>
          </Card>
        )}

        {isLive && !data?.live?.connected && (
          <Card className="py-4 text-center text-xs text-[var(--txt-muted)]">Подключите LIVE ключи OKX в настройках бота</Card>
        )}

        <div>
          <div className="flex items-center justify-between mb-1.5 px-0.5">
            <h2 className="text-[0.65rem] font-bold uppercase tracking-wider text-[var(--txt-muted)]">Позиции</h2>
            <span className="text-[0.65rem] text-[var(--txt-muted)]">{viewPositions.length}</span>
          </div>
          {viewPositions.length === 0 ? (
            <Card className="py-4 text-center text-xs text-[var(--txt-muted)]">Нет открытых позиций</Card>
          ) : (
            <div className="space-y-2 max-h-[40vh] overflow-y-auto overscroll-y-contain" style={{ WebkitOverflowScrolling: 'touch' }}>
              {viewPositions.map(p => (
                <Card key={p.key} className="py-2.5">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="text-xs font-bold truncate">{p.coin}</span>
                      <span className={`text-[0.6rem] font-bold px-1.5 py-0.5 rounded ${p.side === 'long' ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                        {p.side === 'long' ? 'LONG' : 'SHORT'}
                      </span>
                    </div>
                    <span className={`text-xs font-bold mono ${pnlClass(p.upl)}`}>{pnlSign(p.upl)}</span>
                  </div>
                  <div className="text-[0.65rem] text-[var(--txt-muted)] mono">
                    ${fmt(p.entry)}{p.lever ? ` · ${fmt(p.lever, 1)}x` : ''}{p.size ? ` · ${fmt(p.size, 3)}` : ''}
                  </div>
                </Card>
              ))}
            </div>
          )}
        </div>

        <div className="pb-6">
          <h2 className="text-[0.65rem] font-bold uppercase tracking-wider text-[var(--txt-muted)] mb-1.5 px-0.5">Сделки</h2>
          <Card className="p-0 overflow-hidden">
            {viewTrades.length === 0 ? (
              <div className="py-4 text-center text-xs text-[var(--txt-muted)]">Нет сделок</div>
            ) : (
              <div className="divide-y divide-[var(--border)]">
                {viewTrades.map((tr, i) => (
                  <div key={i} className="flex items-center justify-between gap-2 px-3 py-2.5">
                    <div className="flex items-center gap-1.5 min-w-0">
                      {Number(tr.pnl) >= 0 ? <ArrowUpRight size={14} className="text-[var(--profit)] flex-shrink-0" /> : <ArrowDownRight size={14} className="text-[var(--loss)] flex-shrink-0" />}
                      <div className="min-w-0">
                        <div className="text-xs font-semibold truncate">{tr.inst || '—'} <span className="text-[var(--txt-muted)] font-normal">{tr.side}</span></div>
                      </div>
                    </div>
                    <span className={`text-xs font-bold mono flex-shrink-0 ${pnlClass(tr.pnl)}`}>{pnlSign(tr.pnl)}</span>
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
  return <MiniAppErrorBoundary><MiniAppPageInner {...props} /></MiniAppErrorBoundary>
}