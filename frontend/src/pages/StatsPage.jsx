import React, { useEffect, useMemo, useState } from 'react'
import { useTranslation } from '../hooks/useTranslation'
import { api } from '../services/api'

const PERIODS = [
  { id: 'today', labelKey: 'stats.period_today' },
  { id: 'week', labelKey: 'stats.period_week' },
  { id: '30d', labelKey: 'stats.period_30d' },
  { id: '90d', labelKey: 'stats.period_90d' },
  { id: 'all', labelKey: 'stats.period_all' },
]

function fmtPnl(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}`
}

function pnlClass(v) {
  const n = Number(v)
  if (n > 0) return 'text-[var(--profit)]'
  if (n < 0) return 'text-[var(--loss)]'
  return 'text-[var(--txt-muted)]'
}

function Metric({ label, value, sub, tone }) {
  const cls =
    tone === 'profit' ? 'text-[var(--profit)]' :
    tone === 'loss' ? 'text-[var(--loss)]' :
    'text-[var(--txt)]'
  return (
    <div className="metric-card">
      <div className="label">{label}</div>
      <div className={`value mono ${cls}`}>{value}</div>
      {sub ? <div className="change text-[var(--txt-muted)]">{sub}</div> : null}
    </div>
  )
}

function EquitySpark({ points }) {
  const data = points || []
  if (data.length < 2) {
    return (
      <div className="h-24 flex items-center justify-center text-sm text-[var(--txt-muted)]">
        Недостаточно точек для кривой
      </div>
    )
  }
  const vals = data.map(p => Number(p.cum) || 0)
  const min = Math.min(...vals, 0)
  const max = Math.max(...vals, 0)
  const span = max - min || 1
  const w = 320
  const h = 96
  const pad = 4
  const coords = vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (w - pad * 2)
    const y = h - pad - ((v - min) / span) * (h - pad * 2)
    return `${x},${y}`
  })
  const last = vals[vals.length - 1]
  const stroke = last >= 0 ? 'var(--profit)' : 'var(--loss)'
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-24" preserveAspectRatio="none">
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth="2"
        points={coords.join(' ')}
      />
    </svg>
  )
}

export default function StatsPage() {
  const { t } = useTranslation()
  const [period, setPeriod] = useState('all')
  const [mode] = useState('demo') // stage 1: DEMO only
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    let cancelled = false
    let first = true

    const load = () => {
      if (first) {
        setLoading(true)
        first = false
      }
      setErr('')
      return api.getStats({ period, mode })
        .then(d => {
          if (!cancelled) {
            setData(d || {})
            setLoading(false)
          }
        })
        .catch(e => {
          if (!cancelled) {
            setErr(e?.message || 'Ошибка загрузки')
            // keep previous data on refresh errors
            setLoading(false)
          }
        })
    }

    load()
    // Auto-refresh so each new closed trade appears without manual reload
    const id = setInterval(load, 30000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [period, mode])

  const byCoin = data?.by_coin || []
  const recent = data?.recent_trades || []
  const curve = data?.equity_curve || []

  const periodLabel = useMemo(() => {
    if (!data?.from) return ''
    return `${data.from} → ${data.to}`
  }, [data])

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6 space-y-5">
      <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-[var(--txt)]">
            {t('stats.title') || 'Статистика'}
          </h1>
          <p className="text-sm text-[var(--txt-muted)] mt-0.5">
            {t('stats.subtitle') || 'Доходность демо-счёта AI · только закрытые сделки'}
            {periodLabel ? ` · ${periodLabel}` : ''}
            {' · '}
            <span className="text-[var(--txt-muted)]">обновляется каждые 30 с</span>
          </p>
        </div>
        <div className="flex items-center gap-1 flex-wrap">
          <span className="text-2xs px-2 py-0.5 rounded-md border border-[var(--border)] text-[var(--txt-muted)] uppercase tracking-wide">
            DEMO
          </span>
          {PERIODS.map(p => (
            <button
              key={p.id}
              type="button"
              onClick={() => setPeriod(p.id)}
              className={`px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${
                period === p.id
                  ? 'bg-[var(--info-dim)] border-[var(--info)] text-[var(--txt)]'
                  : 'border-[var(--border)] text-[var(--txt-muted)] hover:border-[var(--border-hover)]'
              }`}
            >
              {t(p.labelKey) || p.id}
            </button>
          ))}
        </div>
      </div>

      {err ? (
        <div className="rounded-lg border border-[var(--loss)]/40 bg-[var(--loss-dim)] px-3 py-2 text-sm text-[var(--loss)]">
          {err}
        </div>
      ) : null}

      {loading ? (
        <div className="text-sm text-[var(--txt-muted)] py-12 text-center">Загрузка…</div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Metric
              label={t('stats.realized') || 'Realized PnL'}
              value={`${fmtPnl(data?.realized_pnl)} $`}
              tone={Number(data?.realized_pnl) > 0 ? 'profit' : Number(data?.realized_pnl) < 0 ? 'loss' : undefined}
              sub={t('stats.closed_only') || 'только закрытые'}
            />
            <Metric
              label={t('stats.unrealized') || 'Нереализ. PnL'}
              value={`${fmtPnl(data?.unrealized_pnl)} $`}
              tone={Number(data?.unrealized_pnl) > 0 ? 'profit' : Number(data?.unrealized_pnl) < 0 ? 'loss' : undefined}
            />
            <Metric
              label={t('stats.trades') || 'Сделок'}
              value={String(data?.trades ?? 0)}
              sub={`${data?.wins ?? 0}↑ / ${data?.losses ?? 0}↓`}
            />
            <Metric
              label={t('stats.win_rate') || 'Win rate'}
              value={`${data?.win_rate ?? 0}%`}
              sub={data?.profit_factor != null ? `PF ${data.profit_factor}` : undefined}
            />
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Metric
              label={t('stats.avg_win') || 'Средний плюс'}
              value={`${fmtPnl(data?.avg_win)} $`}
              tone="profit"
            />
            <Metric
              label={t('stats.avg_loss') || 'Средний минус'}
              value={`${fmtPnl(data?.avg_loss)} $`}
              tone="loss"
            />
            <Metric
              label={t('stats.sum_wins') || 'Сумма плюсов'}
              value={`${fmtPnl(data?.sum_wins)} $`}
              tone="profit"
            />
            <Metric
              label={t('stats.sum_losses') || 'Сумма минусов'}
              value={`${fmtPnl(data?.sum_losses)} $`}
              tone="loss"
            />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4">
              <div className="text-sm font-medium text-[var(--txt)] mb-2">
                {t('stats.equity') || 'Кривая капитала (кумулятив)'}
              </div>
              <EquitySpark points={curve} />
              <div className="text-2xs text-[var(--txt-muted)] mt-1">
                {curve.length ? `${curve.length} дн. · источник ${data?.source || '—'}` : 'Нет закрытий за период'}
              </div>
            </div>

            <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4">
              <div className="text-sm font-medium text-[var(--txt)] mb-3">
                {t('stats.by_coin') || 'По монетам'}
              </div>
              {byCoin.length === 0 ? (
                <div className="text-sm text-[var(--txt-muted)]">Нет данных</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-[var(--txt-muted)] text-2xs border-b border-[var(--border)]">
                        <th className="pb-2 font-medium">Пара</th>
                        <th className="pb-2 font-medium text-right">Сделок</th>
                        <th className="pb-2 font-medium text-right">Win%</th>
                        <th className="pb-2 font-medium text-right">PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {byCoin.map(row => (
                        <tr key={row.coin} className="border-b border-[var(--border)]/60">
                          <td className="py-2 font-medium text-[var(--txt)]">{row.coin}</td>
                          <td className="py-2 text-right mono text-[var(--txt-secondary)]">{row.trades}</td>
                          <td className="py-2 text-right mono text-[var(--txt-secondary)]">{row.win_rate}%</td>
                          <td className={`py-2 text-right mono font-medium ${pnlClass(row.pnl)}`}>
                            {fmtPnl(row.pnl)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4">
            <div className="text-sm font-medium text-[var(--txt)] mb-3">
              {t('stats.recent') || 'Последние закрытия'}
            </div>
            {recent.length === 0 ? (
              <div className="text-sm text-[var(--txt-muted)]">Нет закрытых сделок</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-[var(--txt-muted)] text-2xs border-b border-[var(--border)]">
                      <th className="pb-2 font-medium">Пара</th>
                      <th className="pb-2 font-medium">Сторона</th>
                      <th className="pb-2 font-medium text-right">PnL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recent.map((tr, i) => (
                      <tr key={`${tr.coin}-${tr.close_ts}-${i}`} className="border-b border-[var(--border)]/60">
                        <td className="py-2 font-medium text-[var(--txt)]">{tr.coin}</td>
                        <td className="py-2 text-[var(--txt-secondary)] uppercase text-2xs">
                          {tr.side || '—'}
                        </td>
                        <td className={`py-2 text-right mono font-medium ${pnlClass(tr.pnl)}`}>
                          {fmtPnl(tr.pnl)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <p className="text-2xs text-[var(--txt-muted)] pb-4">
            Epoch {data?.pnl_epoch || '—'} · TZ {data?.pnl_tz || 'Europe/Moscow'} · {data?.engine || ''}
          </p>
        </>
      )}
    </div>
  )
}
