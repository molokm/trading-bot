import React, { useState, useEffect } from 'react'
import { TrendingUp, Wallet, Activity, Target, Star, RefreshCw, AlertTriangle, ExternalLink } from 'lucide-react'
import { api } from '../services/api'

function fmt(n, digits = 2) {
  if (n == null || isNaN(n)) return '—'
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}
function pct(n, digits = 1) { return n == null ? '—' : `${fmt(n, digits)}%` }
function pnlCls(v) { return v > 0 ? 'text-[var(--profit)]' : v < 0 ? 'text-[var(--loss)]' : 'text-[var(--txt-secondary)]' }
function pnlSign(v) { if (v == null || v === 0) return '0'; return (v > 0 ? '+' : '') + fmt(v) }

function Card({ children, className = '' }) {
  return <div className={`rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4 ${className}`}>{children}</div>
}
function Metric({ label, value, sub, cls = '' }) {
  return (
    <Card className="min-w-0">
      <div className="text-2xs uppercase tracking-wider text-[var(--txt-muted)]">{label}</div>
      <div className={`text-lg font-bold mono mt-1 truncate ${cls}`}>{value}</div>
      {sub && <div className="text-2xs text-[var(--txt-muted)] mt-0.5">{sub}</div>}
    </Card>
  )
}

export default function TrackerPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true); setError('')
    try {
      const d = await api.getTracker()
      setData(d)
    } catch (e) { setError(e.message || 'Ошибка загрузки') }
    setLoading(false)
  }

  useEffect(() => { load(); const id = setInterval(load, 30000); return () => clearInterval(id) }, [])

  if (loading && !data) {
    return <div className="min-h-screen flex items-center justify-center bg-[var(--bg)]"><RefreshCw size={24} className="animate-spin text-[var(--info)]" /></div>
  }
  if (error && !data) {
    return <div className="min-h-screen flex items-center justify-center p-6 bg-[var(--bg)] text-center">
      <div className="text-sm text-[var(--loss)]">{error}</div>
    </div>
  }
  const pnl = data?.pnl || {}
  const stats = data?.stats || {}
  const curve = data?.equity_curve || []
  const backtest = data?.backtest || {}
  const periods = backtest.periods || []

  return (
    <div className="min-h-screen bg-[var(--bg)] text-[var(--txt)]" style={{ paddingTop: 'env(safe-area-inset-top)' }}>
      {/* Header */}
      <div className="sticky top-0 z-10 flex items-center justify-between px-4 py-3 bg-[var(--surface)] border-b border-[var(--border)]">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-[var(--info)] to-[#4a3fd1] flex items-center justify-center">
            <TrendingUp size={14} className="text-white" />
          </div>
          <span className="text-sm font-bold">COPIX · Live Performance</span>
          <span className={`ml-1 px-1.5 py-0.5 rounded-md text-2xs font-bold ${data?.demo ? 'bg-[var(--warn-dim)] text-[var(--warn)]' : 'bg-[var(--profit-dim)] text-[var(--profit)]'}`}>
            {data?.demo ? 'DEMO' : 'LIVE'}
          </span>
        </div>
        <button className="btn-icon" onClick={load} disabled={loading}>
          <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className="p-4 max-w-3xl mx-auto space-y-4">
        {/* Equity */}
        <Card className="bg-gradient-to-br from-[var(--profit-dim)] to-[var(--info-dim)] border-0">
          <div className="text-2xs font-bold uppercase tracking-wider text-[var(--txt-secondary)] flex items-center gap-1.5">
            <Wallet size={13} className="text-[var(--info)]" /> Текущий счёт
          </div>
          <div className="text-3xl font-extrabold mono mt-1">${fmt(data?.equity)}</div>
          <div className="text-2xs text-[var(--txt-muted)] mt-1">
            Обновлено {data?.updated_at ? new Date(data.updated_at).toLocaleString('ru-RU') : '—'}
          </div>
        </Card>

        {/* Metrics */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <Metric label="PnL 24ч" value={pnlSign(pnl['1d'])} cls={pnlCls(pnl['1d'])} />
          <Metric label="PnL 7д" value={pnlSign(pnl['7d'])} cls={pnlCls(pnl['7d'])} />
          <Metric label="PnL 30д" value={pnlSign(pnl['30d'])} cls={pnlCls(pnl['30d'])} />
          <Metric label="Всего PnL" value={pnlSign(pnl['total'])} cls={pnlCls(pnl['total'])} sub={`комиссии ${fmt(pnl['fees'])}`} />
          <Metric label="Сделок" value={fmt(stats.trades, 0)} />
          <Metric label="Win Rate" value={pct(stats.win_rate, 0)} cls="text-[var(--profit)]" />
          <Metric label="Profit Factor" value={fmt(stats.profit_factor)} cls="text-[var(--info)]" />
          <Metric label="Лучшая / худшая" value={`${fmt(stats.best)} / ${fmt(stats.worst)}`} />
        </div>

        {/* Equity curve */}
        <Card>
          <div className="text-xs font-bold uppercase tracking-wider text-[var(--txt-muted)] mb-3">Кривая счёта</div>
          {curve.length < 2 ? (
            <div className="text-2xs text-[var(--txt-muted)] text-center py-8">
              Снапшоты собираются каждые {data?.snapshot_interval_sec ? data.snapshot_interval_sec / 60 : 10} мин — график появится через ~1 час после запуска.
            </div>
          ) : (
            <div className="space-y-1">
              {curve.map((p, i) => {
                const prev = curve[i - 1]?.equity ?? p.equity
                const cls = p.equity >= prev ? 'bg-[var(--profit)]' : 'bg-[var(--loss)]'
                return (
                  <div key={i} className="flex items-center gap-2">
                    <span className="text-2xs text-[var(--txt-muted)] w-16 shrink-0 truncate">
                      {p.t ? p.t.slice(5, 16) : ''}
                    </span>
                    <div className="flex-1 h-2 rounded bg-[var(--surface-overlay)] overflow-hidden">
                      <div className={`h-full rounded ${cls}`} style={{ width: '100%' }} />
                    </div>
                    <span className="text-2xs mono text-[var(--txt-secondary)] w-20 text-right shrink-0">${fmt(p.equity, 0)}</span>
                  </div>
                )
              })}
            </div>
          )}
        </Card>

        {/* Per-bot breakdown */}
        {Object.keys(pnl.per_bot || {}).length > 0 && (
          <Card>
            <div className="text-xs font-bold uppercase tracking-wider text-[var(--txt-muted)] mb-3">По стратегиям</div>
            <div className="space-y-2">
              {Object.entries(pnl.per_bot).map(([name, v]) => (
                <div key={name} className="flex items-center justify-between text-xs">
                  <span className="text-[var(--txt)] font-medium">{name}</span>
                  <span className={`mono font-bold ${pnlCls(v)}`}>{pnlSign(v)}</span>
                </div>
              ))}
            </div>
          </Card>
        )}

        {/* Backtest credibility */}
        <Card>
          <div className="text-xs font-bold uppercase tracking-wider text-[var(--txt-muted)] mb-3 flex items-center gap-1.5">
            <Target size={13} className="text-[var(--info)]" /> Результаты бэктестов (вне выборки)
          </div>
          <div className="space-y-2">
            {periods.map((p, i) => (
              <div key={i} className="flex items-center justify-between text-xs">
                <span className="text-[var(--txt)]">{p.label}</span>
                <span className="flex items-center gap-3">
                  <span className="text-[var(--txt-secondary)]">DD {pct(p.max_dd_pct, 0)}</span>
                  {p.cagr_pct != null && <span className="text-[var(--info)]">CAGR {pct(p.cagr_pct, 0)}</span>}
                  <span className={`mono font-bold ${p.return_pct >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>+{fmt(p.return_pct, 0)}%</span>
                </span>
              </div>
            ))}
          </div>
          <div className="flex items-center gap-1.5 text-2xs text-[var(--txt-muted)] mt-3">
            <Star size={12} className="text-[var(--warn)]" />
            Win rate {backtest.win_rate_backtest_pct}% · Ликвидаций {backtest.liquidations} · {backtest.note}
          </div>
        </Card>

        {/* Recent trades */}
        {(data?.recent_trades || []).length > 0 && (
          <Card>
            <div className="text-xs font-bold uppercase tracking-wider text-[var(--txt-muted)] mb-3">Последние сделки</div>
            <div className="space-y-1.5">
              {data.recent_trades.slice(0, 8).map((t, i) => {
                const p = Number(t.pnl || 0)
                return (
                  <div key={i} className="flex items-center justify-between text-xs">
                    <div className="min-w-0 flex items-center gap-2">
                      <span className="font-bold text-[var(--txt)] truncate">{t.symbol || t.inst_id}</span>
                      <span className="text-2xs text-[var(--txt-muted)] truncate">
                        {t.entry_time ? t.entry_time.slice(0, 16).replace('T', ' ') : ''}
                      </span>
                      {t.bot && <span className="text-2xs px-1.5 py-0.5 rounded bg-[var(--info-dim)] text-[var(--info)]">{t.bot}</span>}
                    </div>
                    <span className={`mono font-bold ${pnlCls(p)}`}>{pnlSign(p)}</span>
                  </div>
                )
              })}
            </div>
          </Card>
        )}

        {/* Disclaimer */}
        <div className="flex items-start gap-2 p-3 rounded-lg bg-[var(--warn-dim)] border border-[var(--warn)]/30 text-2xs text-[var(--txt-secondary)]">
          <AlertTriangle size={13} className="text-[var(--warn)] shrink-0 mt-0.5" />
          <span>
            Результаты прошлых периодов не гарантируют будущей доходности. Торговля фьючерсами с плечом — высокорисковый инструмент, возможны убыточные периоды и просадки. <ExternalLink size={10} className="inline" /> Проверить результаты самостоятельно можно скриптами верификации из репозитория.
          </span>
        </div>
      </div>
    </div>
  )
}
