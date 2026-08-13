import React, { useState, useEffect, useMemo } from 'react'
import {
  Play, Download, BarChart3, Loader2, TrendingUp, TrendingDown,
  ArrowUpRight, ArrowDownRight, GitCompare, CheckCircle, Image,
  AlertTriangle, Calendar, Database, Coins
} from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { api } from '../services/api'
import { MetricCard, Tip, Chip, EmptyState, Loader } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

const PAIRS = [
  { id: 'BTC-USDT-SWAP', label: 'BTC/USDT' },
  { id: 'ETH-USDT-SWAP', label: 'ETH/USDT' },
  { id: 'SOL-USDT-SWAP', label: 'SOL/USDT' },
  { id: 'BNB-USDT-SWAP', label: 'BNB/USDT' },
]
const PERIODS = ['7d', '30d', '90d', '1y']
const TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']
const STRATEGY_IDS = ['momentum']


/* Custom Recharts tooltip for dark theme */
function EquityTooltip({ active, payload, label }) {
  const { t } = useTranslation()
  if (!active || !payload?.length) return null
  return (
    <div style={{
      background: 'var(--surface-raised)',
      border: '1px solid var(--border-hover)',
      borderRadius: 'var(--radius-sm)',
      padding: '8px 12px',
      fontSize: '0.7rem',
      boxShadow: '0 8px 24px rgba(0,0,0,0.4)',
    }}>
      <div style={{ color: 'var(--txt-muted)', marginBottom: 4 }}>{t('backtest.trade_tooltip')}{label}</div>
      <div className="mono" style={{ color: 'var(--txt)', fontWeight: 600 }}>
        {payload[0].value >= 1000 ? `$${(payload[0].value / 1000).toFixed(2)}k` : `$${payload[0].value.toFixed(2)}`}
      </div>
    </div>
  )
}

/* PNG export using canvas */
function exportPng(equityCurve, t) {
  const canvas = document.createElement('canvas')
  const w = 800, h = 300
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d')

  ctx.fillStyle = '#13161d'
  ctx.fillRect(0, 0, w, h)

  const pad = { t: 30, r: 20, b: 40, l: 60 }
  const vals = equityCurve.map(d => d.value)
  const minV = Math.min(...vals) * 0.98
  const maxV = Math.max(...vals) * 1.02
  const range = maxV - minV || 1
  const xScale = (i) => pad.l + (i / (vals.length - 1)) * (w - pad.l - pad.r)
  const yScale = (v) => pad.t + (1 - (v - minV) / range) * (h - pad.t - pad.b)

  const isUp = vals[vals.length - 1] >= vals[0]
  const lineColor = isUp ? '#16c784' : '#ea3943'
  const fillColor = isUp ? 'rgba(0,255,136,0.08)' : 'rgba(255,51,102,0.08)'

  // Grid
  ctx.strokeStyle = 'rgba(255,255,255,0.06)'
  ctx.lineWidth = 0.5
  for (let i = 0; i <= 5; i++) {
    const y = pad.t + (i / 5) * (h - pad.t - pad.b)
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke()
    const v = maxV - (range * i) / 5
    ctx.fillStyle = '#5c6370'
    ctx.font = '10px JetBrains Mono, monospace'
    ctx.textAlign = 'right'
    ctx.fillText(v >= 1000 ? `$${(v / 1000).toFixed(1)}k` : `$${v.toFixed(0)}`, pad.l - 8, y + 3)
  }

  // Area fill
  ctx.beginPath()
  vals.forEach((v, i) => {
    const x = xScale(i), y = yScale(v)
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
  })
  ctx.lineTo(xScale(vals.length - 1), h - pad.b)
  ctx.lineTo(pad.l, h - pad.b)
  ctx.closePath()
  ctx.fillStyle = fillColor
  ctx.fill()

  // Line
  ctx.beginPath()
  vals.forEach((v, i) => {
    const x = xScale(i), y = yScale(v)
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
  })
  ctx.strokeStyle = lineColor
  ctx.lineWidth = 2
  ctx.stroke()

  // End dot
  const lastX = xScale(vals.length - 1), lastY = yScale(vals[vals.length - 1])
  ctx.beginPath()
  ctx.arc(lastX, lastY, 4, 0, Math.PI * 2)
  ctx.fillStyle = lineColor
  ctx.fill()

  // Title
  ctx.fillStyle = '#9aa0a9'
  ctx.font = '11px Inter, sans-serif'
  ctx.textAlign = 'left'
  ctx.fillText(t('backtest.equity_title'), pad.l, 18)
  ctx.textAlign = 'right'
  ctx.fillText(`${vals.length} ${t('backtest.trades_word')}`, w - pad.r, 18)

  const link = document.createElement('a')
  link.download = 'equity-curve.png'
  link.href = canvas.toDataURL('image/png')
  link.click()
}

function fmtDate(iso, locale) {
  try {
    return new Date(iso).toLocaleDateString(locale, { day: '2-digit', month: '2-digit', year: 'numeric' })
  } catch {
    return iso
  }
}

export default function BacktestPage({ connected }) {
  const { t, locale } = useTranslation()
  const [config, setConfig] = useState({
    pairs: ['BTC-USDT-SWAP'],
    period: '1y',
    timeframe: '1d',
    strategy: 'momentum',
  })
  const [engine, setEngine] = useState('own')
  const [ftCfg, setFtCfg] = useState({
    strategy: 'momentum',
    start: '20240225',
    end: '20250809',
  })
  const [ftResults, setFtResults] = useState(null)
  const [ftRunning, setFtRunning] = useState(false)
  const [ftError, setFtError] = useState(null)
  const [running, setRunning] = useState(false)
  const [results, setResults] = useState([])
  const [activeResult, setActiveResult] = useState(null)
  const [compareMode, setCompareMode] = useState(false)
  const [error, setError] = useState(null)

  const runFreqtrade = async () => {
    setFtRunning(true)
    setFtError(null)
    try {
      const r = await api.runFreqtradeBacktest(ftCfg)
      setFtResults(r)
    } catch (e) {
      setFtError(e.message || t('backtest.ft_error'))
    } finally {
      setFtRunning(false)
    }
  }

  const runBacktest = async () => {
    setRunning(true)
    setError(null)
    try {
      const result = await api.runBacktest(config)
      const newResults = [...results, result]
      setResults(newResults)
      setActiveResult(result)
      localStorage.setItem('backtest_history', JSON.stringify(newResults.slice(-20)))
    } catch (e) {
      setError(e.message || t('backtest.run_failed'))
    } finally {
      setRunning(false)
    }
  }

  useEffect(() => {
    let history = []
    try {
      const saved = localStorage.getItem('backtest_history')
      if (saved) history = JSON.parse(saved) || []
    } catch {}
    // Load the last backtest persisted on the server (survives reload / other devices).
    api.getLastBacktest()
      .then(({ result }) => {
        if (result) {
          const exists = history.some(h => h?.config?.runAt === result.config?.runAt)
          if (!exists) history = [...history, result]
          setResults(history)
          setActiveResult(result)
        } else if (history.length) {
          setResults(history)
          setActiveResult(history[history.length - 1])
        }
      })
      .catch(() => {
        if (history.length) {
          setResults(history)
          setActiveResult(history[history.length - 1])
        }
      })
  }, [])

  const togglePair = (pairId) => {
    setConfig(c => ({
      ...c,
      pairs: c.pairs.includes(pairId) ? c.pairs.filter(p => p !== pairId) : [...c.pairs, pairId],
    }))
  }

  const m = activeResult?.metrics

  // Safe formatter for nullable metric values (legacy results may carry nulls).
  const fmt = (v, digits = 0, prefix = '') =>
    v == null || isNaN(v) ? '—' : `${prefix}${Number(v).toFixed(digits)}`

  /* Pre-compute best values per metric column for compare mode */
  const bestMetrics = useMemo(() => {
    if (results.length < 2) return {}
    return {
      totalReturnPct: Math.max(...results.map(r => r.metrics.totalReturnPct)),
      winRate: Math.max(...results.map(r => r.metrics.winRate)),
      profitFactor: Math.max(...results.map(r => r.metrics.profitFactor)),
      sharpe: Math.max(...results.map(r => r.metrics.sharpe)),
      maxDD: Math.min(...results.map(r => r.metrics.maxDD)),
      trades: -1, // no best for trades
    }
  }, [results])

  return (
    <div className="h-full flex flex-col p-4 gap-4 overflow-auto">
      <div className="flex items-center justify-between flex-shrink-0">
        <div>
          <h2 className="text-lg font-bold text-[var(--txt)]">Backtest</h2>
          <p className="text-xs text-[var(--txt-muted)]">{t('backtest.subtitle')}</p>
        </div>
        <div className="flex gap-2">
          <button
            className={`btn btn-ghost btn-sm ${compareMode ? '!border-[var(--info)] !text-[var(--info)]' : ''}`}
            onClick={() => setCompareMode(!compareMode)}
          >
            <GitCompare size={12} /> {t('backtest.compare')}
          </button>
          {results.length > 0 && activeResult && (
            <button className="btn btn-ghost btn-sm" onClick={() => exportPng(activeResult.equityCurve, t)}>
              <Image size={12} /> PNG
            </button>
          )}
          {results.length > 0 && (
            <button className="btn btn-ghost btn-sm" onClick={() => {
              const csv = [t('backtest.csv_header'), ...activeResult.tradeList.map(tr =>
                `${tr.entry_time},${tr.exit_time ?? ''},${tr.pair},${tr.side},${tr.entry_px == null ? '' : tr.entry_px.toFixed(2)},${tr.exit_px == null ? '' : tr.exit_px.toFixed(2)},${tr.pnl == null ? '' : tr.pnl.toFixed(2)},${tr.pnl_pct == null ? '' : tr.pnl_pct.toFixed(2)}%,${tr.reason}`
              )].join('\n')
              const blob = new Blob([csv], { type: 'text/csv' })
              const url = URL.createObjectURL(blob)
              const a = document.createElement('a')
              a.href = url; a.download = 'backtest.csv'; a.click()
              URL.revokeObjectURL(url)
            }}>
              <Download size={12} /> CSV
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="flex items-center gap-2 px-4 py-2.5 text-2xs text-[var(--loss)] bg-[var(--loss-dim)] border border-[var(--loss)]/30 rounded-md flex-shrink-0">
          <AlertTriangle size={13} />
          <span className="font-medium">{t('backtest.run_failed')}:</span>
          <span>{error}</span>
          <button className="ml-auto btn btn-ghost btn-sm !text-[var(--txt-muted)]" onClick={() => setError(null)}>×</button>
        </div>
      )}

      <div className="panel flex-shrink-0">
        <div className="px-4 py-2.5 text-2xs text-[var(--info)] bg-[var(--info-dim)] border-b border-[var(--border)] flex items-center gap-2">
          <Database size={12} /> {t('backtest.real_data_banner')}
        </div>
        <div className="panel-header"><BarChart3 size={13} className="text-[var(--info)]" /> {t('backtest.config')}</div>
        <div className="p-4 flex flex-wrap items-end gap-6">
          <div>
            <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider flex items-center gap-1">
              {t('backtest.engine')} <Tip text={t('backtest.engine_tip')} />
            </label>
            <div className="flex gap-1 mt-1.5">
              <Chip active={engine === 'own'} onClick={() => setEngine('own')}>{t('backtest.engine_own')}</Chip>
              <Chip active={engine === 'freqtrade'} onClick={() => setEngine('freqtrade')}>{t('backtest.engine_freqtrade')}</Chip>
            </div>
          </div>
          {engine === 'freqtrade' ? (
            <>
              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('backtest.ft_strategy')}</label>
                <div className="flex gap-1 mt-1.5">
                  <Chip active={ftCfg.strategy === 'momentum'} onClick={() => setFtCfg(c => ({ ...c, strategy: 'momentum' }))}>{t('backtest.ft_momentum')}</Chip>
                  <Chip active={ftCfg.strategy === 'impulse'} onClick={() => setFtCfg(c => ({ ...c, strategy: 'impulse' }))}>{t('backtest.ft_impulse')}</Chip>
                </div>
              </div>
              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('backtest.ft_start')}</label>
                <input type="date" className="input input-sm mt-1.5 block" value={ftCfg.start.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3')} onChange={e => setFtCfg(c => ({ ...c, start: e.target.value.replaceAll('-', '') }))} />
              </div>
              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('backtest.ft_end')}</label>
                <input type="date" className="input input-sm mt-1.5 block" value={ftCfg.end.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3')} onChange={e => setFtCfg(c => ({ ...c, end: e.target.value.replaceAll('-', '') }))} />
              </div>
              <button className="btn btn-primary" onClick={runFreqtrade} disabled={ftRunning}>
                {ftRunning ? <><Loader /> {t('backtest.ft_running')}</> : <><BarChart3 size={13} /> {t('backtest.ft_run')}</>}
              </button>
            </>
          ) : (
            <>
          <div className="flex-1 min-w-[200px]">
            <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider flex items-center gap-1">
              {t('backtest.instruments')} <Tip text={t('backtest.instruments_tip')} />
            </label>
            <div className="flex flex-wrap gap-1.5 mt-1.5">
              {PAIRS.map(p => (
                <Chip key={p.id} active={config.pairs.includes(p.id)} onClick={() => togglePair(p.id)}>{p.label}</Chip>
              ))}
            </div>
          </div>
          <div>
            <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('backtest.strategy')}</label>
            <div className="flex gap-1 mt-1.5">
              {STRATEGY_IDS.map(id => (
                <Chip key={id} active={config.strategy === id} onClick={() => setConfig(c => ({ ...c, strategy: id }))}>
                  Momentum
                </Chip>
              ))}
            </div>
          </div>
          <div>
            <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('backtest.period')}</label>
            <div className="flex gap-1 mt-1.5">
              {PERIODS.map(p => (
                <Chip key={p} active={config.period === p} onClick={() => setConfig(c => ({ ...c, period: p }))}>{p}</Chip>
              ))}
            </div>
          </div>
          <div>
            <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('backtest.timeframe')}</label>
            <div className="flex gap-1 mt-1.5">
              {TIMEFRAMES.map(tf => (
                <Chip key={tf} active={config.timeframe === tf} onClick={() => setConfig(c => ({ ...c, timeframe: tf }))}>{tf}</Chip>
              ))}
            </div>
          </div>
          <button className="btn btn-primary" onClick={runBacktest} disabled={running || config.pairs.length === 0}>
            {running ? <><Loader /> {t('backtest.running')}</> : <><Play size={13} /> {t('backtest.run')}</>}
          </button>
            </>
          )}
        </div>
      </div>

      {/* Freqtrade Results */}
      {engine === 'freqtrade' && (ftResults || ftError || ftRunning) && (
        <div className="panel flex-shrink-0">
          <div className="panel-header">
            <BarChart3 size={13} className="text-[var(--info)]" />
            {ftResults?.strategy_name || t('backtest.ft_strategy')}
            {ftResults && <span className="ml-auto text-2xs font-normal text-[var(--info)] flex items-center gap-1"><CheckCircle size={12} /> {t('backtest.ft_verified_badge')}</span>}
          </div>
          {ftError && (
            <div className="flex items-center gap-2 px-4 py-2.5 text-2xs text-[var(--loss)] bg-[var(--loss-dim)] border-b border-[var(--loss)]/30">
              <AlertTriangle size={13} />
              <span>{t('backtest.ft_error')}: {ftError}</span>
            </div>
          )}
          {ftResults && (
            <div className="p-4">
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-2xs mb-3">
                <span className="flex items-center gap-1.5 text-[var(--txt-muted)]"><Database size={11} /> freqtrade · {ftResults.strategy_name}</span>
                <span className="flex items-center gap-1.5 text-[var(--txt-muted)]"><Calendar size={11} /> {t('backtest.ft_period')}: {ftResults.period}</span>
                <span className="text-[var(--txt-muted)]">{ftResults.fee_note}</span>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
                <MetricCard label={t('backtest.ft_cagr')} value={`${fmt(ftResults.cagr_pct, 2, ftResults.cagr_pct != null && Number(ftResults.cagr_pct) >= 0 ? '+' : '')}%`} changeType={Number(ftResults.cagr_pct) >= 0 ? 'positive' : 'negative'} mono />
                <MetricCard label={t('backtest.ft_total_return')} value={`${fmt(ftResults.total_return_pct, 2, ftResults.total_return_pct != null && Number(ftResults.total_return_pct) >= 0 ? '+' : '')}%`} changeType={Number(ftResults.total_return_pct) >= 0 ? 'positive' : 'negative'} mono />
                <MetricCard label={t('backtest.ft_profit_usdt')} value={fmt(ftResults.total_profit_usdt, 2, Number(ftResults.total_profit_usdt) >= 0 ? '+' : '')} changeType={Number(ftResults.total_profit_usdt) >= 0 ? 'positive' : 'negative'} mono />
                <MetricCard label={t('backtest.ft_drawdown')} value={`-${fmt(ftResults.max_drawdown_pct, 2)}%`} changeType="negative" mono />
                <MetricCard label={t('backtest.ft_trades')} value={fmt(ftResults.trades, 0)} mono />
                <MetricCard label={t('backtest.ft_win_rate')} value={`${fmt(ftResults.win_rate_pct, 1)}%`} changeType={Number(ftResults.win_rate_pct) >= 50 ? 'positive' : 'negative'} mono />
              </div>
              <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-2xs text-[var(--txt-muted)]">
                <span>{t('backtest.ft_long_short')}: <span className="text-[var(--profit)] mono">{ftResults.longs}</span> / <span className="text-[var(--loss)] mono">{ftResults.shorts}</span></span>
                <span>{t('backtest.ft_wins')}: <span className="mono text-[var(--profit)]">{ftResults.wins}</span> · {t('backtest.ft_losses')}: <span className="mono text-[var(--loss)]">{ftResults.losses}</span></span>
                <span>{t('backtest.avg_profit_pct')}: <span className="mono">{fmt(ftResults.avg_profit_pct, 2)}%</span></span>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Compare Mode */}
      {compareMode && results.length > 1 && (
        <div className="panel flex-shrink-0">
          <div className="panel-header"><GitCompare size={13} /> {t('backtest.comparison')}</div>
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th className="text-right">{t('backtest.return')}</th>
                  <th className="text-right">{t('backtest.trades_pct')}</th>
                  <th className="text-right">{t('backtest.profit_factor')}</th>
                  <th className="text-right">{t('backtest.sharpe')}</th>
                  <th className="text-right">{t('backtest.max_drawdown')}</th>
                  <th className="text-right">{t('backtest.trades')}</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {results.map((r, i) => {
                  const rm = r.metrics
                  return (
                    <tr key={i}>
                      <td className="font-medium text-[var(--txt)]">#{i + 1}</td>
                      <td className={`text-right mono font-semibold ${rm.totalReturnPct >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'} ${rm.totalReturnPct === bestMetrics.totalReturnPct ? '!bg-[var(--profit-dim)]' : ''}`}> {fmt(rm.totalReturnPct, 2, rm.totalReturnPct >= 0 ? '+' : '')}%</td>
                      <td className={`text-right mono ${rm.winRate === bestMetrics.winRate ? '!bg-[var(--profit-dim)]' : ''}`}>{fmt(rm.winRate, 1)}%</td>
                      <td className={`text-right mono ${rm.profitFactor === bestMetrics.profitFactor ? '!bg-[var(--profit-dim)]' : ''}`}>{fmt(rm.profitFactor, 2)}</td>
                      <td className={`text-right mono ${rm.sharpe === bestMetrics.sharpe ? '!bg-[var(--profit-dim)]' : ''}`}>{fmt(rm.sharpe, 2)}</td>
                      <td className={`text-right mono text-[var(--loss)] ${rm.maxDD === bestMetrics.maxDD ? '!bg-[var(--profit-dim)]' : ''}`}>-{fmt(rm.maxDD, 1)}%</td>
                      <td className="text-right mono">{fmt(rm.trades, 0)}</td>
                      <td className="text-right"><button className="btn btn-ghost btn-sm" onClick={() => setActiveResult(r)}>{t('backtest.view')}</button></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Results */}
      {activeResult && m && (
        <>
          {/* Run metadata — real data info */}
          <div className="panel flex-shrink-0">
            <div className="panel-header"><CheckCircle size={13} className="text-[var(--profit)]" /> {t('backtest.run_info')}</div>
            <div className="px-4 py-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-2xs">
              <span className="flex items-center gap-1.5 text-[var(--txt-muted)]">
                <Coins size={11} /> {config.pairs.join(', ')}
              </span>
              <span className="flex items-center gap-1.5 text-[var(--txt-muted)]">
                <BarChart3 size={11} /> {activeResult.config.timeframe} · {activeResult.config.period}
              </span>
              <span className="flex items-center gap-1.5 text-[var(--txt-muted)]">
                <Calendar size={11} /> {t('backtest.range')}: {fmtDate(activeResult.dataRange?.start, locale)} — {fmtDate(activeResult.dataRange?.end, locale)} ({activeResult.dataRange?.bars} {t('backtest.bars')})
              </span>
              <span className="flex items-center gap-1.5 text-[var(--txt-muted)]">
                <Database size={11} /> {activeResult.dataNote}
              </span>
              <span className="ml-auto text-[var(--txt-muted)]">
                {t('backtest.started_at')}: {new Date(activeResult.config.runAt).toLocaleString(locale)}
              </span>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3 flex-shrink-0">
            <MetricCard label={t('backtest.total_return')} value={`${fmt(m.totalReturnPct, 2, m.totalReturnPct >= 0 ? '+' : '')}%`} change={`$${fmt(m.totalReturn, 0, m.totalReturn >= 0 ? '+' : '')}`} changeType={m.totalReturn >= 0 ? 'positive' : 'negative'} mono tip={t('backtest.total_return_tip')} />
            <MetricCard label={t('backtest.trades_pct_full')} value={`${fmt(m.winRate, 1)}%`} changeType={m.winRate >= 50 ? 'positive' : 'negative'} mono tip={t('backtest.trades_pct_tip')} />
            <MetricCard label={t('backtest.profit_factor_full')} value={fmt(m.profitFactor, 2)} changeType={m.profitFactor >= 1 ? 'positive' : 'negative'} mono tip={t('backtest.profit_factor_tip')} />
            <MetricCard label={t('backtest.sharpe_full')} value={fmt(m.sharpe, 2)} changeType={m.sharpe >= 1 ? 'positive' : 'negative'} mono tip={t('backtest.sharpe_tip')} />
            <MetricCard label={t('backtest.max_drawdown_full')} value={`-${fmt(m.maxDD, 1)}%`} changeType="negative" mono tip={t('backtest.max_drawdown_tip')} />
            <MetricCard label={t('backtest.total_trades')} value={fmt(m.trades, 0)} mono tip={t('backtest.total_trades_tip')} />
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-[1fr_320px] gap-3">
            <div className="panel">
              <div className="panel-header"><TrendingUp size={13} className="text-[var(--profit)]" /> {t('backtest.equity_curve')}</div>
              <div className="p-3" id="equity-chart-container">
                <EquityChart data={activeResult.equityCurve} />
              </div>
            </div>

            <div className="panel">
              <div className="panel-header"><BarChart3 size={13} className="text-[var(--warn)]" /> {t('backtest.heatmap')}</div>
              <div className="p-3">
                <HeatmapChart data={activeResult.heatmap} />
              </div>
            </div>
          </div>

          <div className="panel flex-1 flex flex-col min-h-0">
            <div className="panel-header">
              <BarChart3 size={13} className="text-[var(--info)]" />
              {t('backtest.all_trades')} ({activeResult.tradeList.length})
            </div>
            <div className="flex-1 overflow-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t('backtest.entry_col')}</th><th>{t('backtest.exit_col')}</th><th>{t('backtest.pair_col')}</th><th>{t('backtest.side_col')}</th>
                    <th className="text-right">{t('backtest.entry_col')}</th><th className="text-right">{t('backtest.exit_col')}</th>
                    <th className="text-right">PnL ($)</th><th className="text-right">PnL (%)</th><th className="text-right">{t('backtest.reason_col')}</th>
                  </tr>
                </thead>
                <tbody>
                  {activeResult.tradeList.map((tr, i) => {
                    const reasonColors = { tp: 'text-[var(--profit)]', sl: 'text-[var(--loss)]', trail: 'text-[var(--info)]', breakeven: 'text-[var(--warn)]', rotation: 'text-[var(--warn)]', roi: 'text-[var(--profit)]', backtest_end: 'text-[var(--txt-muted)]' }
                    return (
                      <tr key={i}>
                        <td className="text-2xs mono">{new Date(tr.entry_time).toLocaleString(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</td>
                        <td className="text-2xs mono">{new Date(tr.exit_time).toLocaleString(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</td>
                        <td className="text-[var(--txt)] font-medium">{tr.pair}</td>
                        <td><span className={`text-2xs font-bold ${tr.side === 'LONG' ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>{tr.side}</span></td>
                        <td className="text-right mono">${fmt(tr.entry_px, 2)}</td>
                        <td className="text-right mono">${fmt(tr.exit_px, 2)}</td>
                        <td className={`text-right mono font-semibold ${tr.pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>{tr.pnl >= 0 ? '+' : ''}{fmt(tr.pnl, 2)}</td>
                        <td className={`text-right mono ${tr.pnl_pct >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>{tr.pnl_pct >= 0 ? '+' : ''}{fmt(tr.pnl_pct, 2)}%</td>
                        <td className={`text-right text-2xs font-medium uppercase ${reasonColors[tr.reason] || 'text-[var(--txt-muted)]'}`}>{tr.reason}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {!activeResult && !running && !error && (
        <EmptyState icon={BarChart3} text={t('backtest.no_results')} sub={t('backtest.no_results_hint')} />
      )}
    </div>
  )
}

/* Recharts Equity Chart */
function EquityChart({ data }) {
  if (!data || data.length < 2) return null
  const initialV = data[0].value
  const finalV = data[data.length - 1].value
  const isUp = finalV >= initialV
  const strokeColor = isUp ? 'var(--profit)' : 'var(--loss)'
  const gradId = 'equityGrad'

  return (
    <div style={{ width: '100%', height: 220 }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={strokeColor} stopOpacity={0.25} />
              <stop offset="100%" stopColor={strokeColor} stopOpacity={0.01} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="trade"
            tick={{ fill: 'var(--txt-muted)', fontSize: 10 }}
            axisLine={{ stroke: 'var(--border)' }}
            tickLine={false}
          />
          <YAxis
            tick={{ fill: 'var(--txt-muted)', fontSize: 10 }}
            axisLine={{ stroke: 'var(--border)' }}
            tickLine={false}
            tickFormatter={(v) => v >= 1000 ? `$${(v/1000).toFixed(1)}k` : `$${v.toFixed(0)}`}
            width={50}
          />
          <Tooltip content={<EquityTooltip />} />
          <Area
            type="monotone"
            dataKey="value"
            stroke={strokeColor}
            strokeWidth={2}
            fill={`url(#${gradId})`}
            dot={false}
            activeDot={{ r: 4, fill: strokeColor, stroke: 'var(--bg)', strokeWidth: 2 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

/* Heatmap with CSS tooltip */
const DAY_KEYS = ['day.mon', 'day.tue', 'day.wed', 'day.thu', 'day.fri', 'day.sat', 'day.sun']
function HeatmapChart({ data }) {
  const { t } = useTranslation()
  const DAYS = DAY_KEYS.map(k => t(k))
  if (!data || data.length === 0) return null
  const maxAbs = Math.max(...data.map(d => Math.abs(d.value)), 0.1)
  const getColor = (v) => {
    const norm = v / maxAbs
    if (norm > 0) return `rgba(0,255,136,${Math.min(norm * 0.8, 0.8)})`
    return `rgba(255,51,102,${Math.min(Math.abs(norm) * 0.8, 0.8)})`
  }
  return (
    <div className="space-y-0.5">
      <div className="flex gap-px pl-6">
        {[0, 4, 8, 12, 16, 20].map(h => (
          <div key={h} className="flex-1 text-center text-2xs text-[var(--txt-muted)]">{h.toString().padStart(2, '0')}</div>
        ))}
      </div>
      {DAYS.map((day, di) => {
        const dayData = data.filter(d => d.day === di)
        return (
          <div key={di} className="flex items-center gap-px">
            <div className="w-5 text-2xs text-[var(--txt-muted)] text-right pr-1">{day}</div>
            {Array.from({ length: 24 }, (_, hi) => {
              const cell = dayData.find(d => d.hour === hi)
              const val = cell?.value || 0
              return (
                <div
                  key={hi}
                  className="heatmap-cell flex-1 h-4 relative group"
                  style={{ background: getColor(val) }}
                  title={`${DAYS[di]} ${hi}:00 — ${val >= 0 ? '+' : ''}${val.toFixed(2)}`}
                >
                  <div className="heatmap-tip">
                    <div className="mono" style={{ color: val >= 0 ? 'var(--profit)' : 'var(--loss)', fontWeight: 600 }}>{val >= 0 ? '+' : ''}{val.toFixed(3)}</div>
                    <div style={{ color: 'var(--txt-muted)' }}>{DAYS[di]} {hi.toString().padStart(2, '0')}:00</div>
                  </div>
                </div>
              )
            })}
          </div>
        )
      })}
      <div className="flex items-center justify-center gap-3 mt-2">
        <div className="flex items-center gap-1 text-2xs text-[var(--txt-muted)]">
          <div className="w-3 h-3 rounded-sm" style={{ background: 'rgba(255,51,102,0.6)' }} /> {t('backtest.loss_label')}
        </div>
        <div className="flex items-center gap-1 text-2xs text-[var(--txt-muted)]">
          <div className="w-3 h-3 rounded-sm" style={{ background: 'rgba(0,255,136,0.6)' }} /> {t('backtest.profit_label')}
        </div>
      </div>
    </div>
  )
}
