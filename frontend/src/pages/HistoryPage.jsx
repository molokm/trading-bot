import React, { useState, useEffect, useMemo } from 'react'
import { ScrollText, ChevronLeft, ChevronRight, Download } from 'lucide-react'
import { api } from '../services/api'
import { EmptyState, Loader, Chip } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'
import { fmtTs, parseTradeTs } from '../utils/time'

const PAGE_SIZE = 30
const ALL_PAIRS_KEY = '__all__'

function tradeMode(t) {
  const m = String(t.account_mode || t.mode || '').toLowerCase()
  return m === 'live' ? 'live' : 'demo'
}

export default function HistoryPage() {
  const { t, locale } = useTranslation()

  const REASON_MAP = {
    closed: { label: t('reason.closed') || 'Закрыта', color: 'text-[var(--profit)]', bg: 'bg-[var(--profit-dim)]' },
    open: { label: t('reason.open') || 'Открыта', color: 'text-[var(--info)]', bg: 'bg-[var(--info-dim)]' },
    tp: { label: t('reason.tp') || 'TP', color: 'text-[var(--profit)]', bg: 'bg-[var(--profit-dim)]' },
    sl: { label: t('reason.sl') || 'SL', color: 'text-[var(--loss)]', bg: 'bg-[var(--loss-dim)]' },
    trail: { label: t('reason.trail') || 'Trail', color: 'text-[var(--info)]', bg: 'bg-[var(--info-dim)]' },
    breakeven: { label: t('reason.breakeven') || 'BE', color: 'text-[var(--warn)]', bg: 'bg-[var(--warn-dim)]' },
    manual: { label: t('reason.manual') || 'Ручная', color: 'text-[var(--txt-secondary)]', bg: 'bg-[var(--surface-overlay)]' },
    rotation: { label: 'Ротация', color: 'text-[var(--info)]', bg: 'bg-[var(--info-dim)]' },
    roe_threshold: { label: 'ROE', color: 'text-[var(--info)]', bg: 'bg-[var(--info-dim)]' },
  }

  const [trades, setTrades] = useState([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(0)
  const [filterResult, setFilterResult] = useState('all')
  const [filterMode, setFilterMode] = useState('all') // all | demo | live
  const [filterPair, setFilterPair] = useState(ALL_PAIRS_KEY)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [report, setReport] = useState(null)

  useEffect(() => {
    setLoading(true)
    api.getPairedTrades(5000).then(data => {
      setTrades(data.trades || [])
      setLoading(false)
    }).catch(() => {
      setTrades([])
      setLoading(false)
    })
    api.reportSummary?.().then(setReport).catch(() => setReport(null))
  }, [])

  const allPairs = useMemo(() => {
    const pairs = new Set(
      trades.map(tr => (tr.coin || tr.symbol || tr.inst_id || '')
        .replace('-USDT-SWAP', '')
        .replace('-USD-SWAP', ''))
    )
    return [ALL_PAIRS_KEY, ...Array.from(pairs).filter(Boolean).sort()]
  }, [trades])

  const filtered = useMemo(() => {
    return trades.filter(tr => {
      const mode = tradeMode(tr)
      if (filterMode === 'demo' && mode !== 'demo') return false
      if (filterMode === 'live' && mode !== 'live') return false

      if (filterResult !== 'all') {
        const pnl = parseFloat(tr.pnl || 0)
        if (filterResult === 'win' && !(pnl > 0)) return false
        if (filterResult === 'loss' && !(pnl < 0)) return false
      }

      const pair = (tr.coin || tr.symbol || tr.inst_id || '')
        .replace('-USDT-SWAP', '')
        .replace('-USD-SWAP', '')
      if (filterPair !== ALL_PAIRS_KEY && pair !== filterPair) return false

      const ts = parseTradeTs(tr.exit_time || tr.time || tr.entry_time)
      if (dateFrom) {
        const from = new Date(dateFrom)
        if (ts && ts < from) return false
      }
      if (dateTo) {
        const to = new Date(dateTo)
        to.setHours(23, 59, 59, 999)
        if (ts && ts > to) return false
      }
      return true
    })
  }, [trades, filterResult, filterMode, filterPair, dateFrom, dateTo])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageRows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  const totalPnl = useMemo(
    () => filtered.reduce((s, tr) => s + (parseFloat(tr.pnl) || 0), 0),
    [filtered]
  )
  const demoPnl = useMemo(
    () => filtered.filter(tr => tradeMode(tr) === 'demo').reduce((s, tr) => s + (parseFloat(tr.pnl) || 0), 0),
    [filtered]
  )
  const livePnl = useMemo(
    () => filtered.filter(tr => tradeMode(tr) === 'live').reduce((s, tr) => s + (parseFloat(tr.pnl) || 0), 0),
    [filtered]
  )

  useEffect(() => { setPage(0) }, [filterResult, filterMode, filterPair, dateFrom, dateTo])

  function exportCsv() {
    const header = [
      t('history.time'), 'Mode', 'Bot', t('history.type'), t('history.instrument'),
      t('history.entry'), t('history.exit'), t('history.pnl'), t('history.reason'),
    ].join(',')
    const rows = filtered.map(tr => {
      const mode = tradeMode(tr)
      const pair = (tr.coin || tr.symbol || tr.inst_id || '').replace('-USDT-SWAP', '')
      const side = (tr.side || tr.pos_side || '').toLowerCase()
      const dir = side === 'buy' || side === 'long' ? 'LONG' : 'SHORT'
      return [
        tr.exit_time || tr.time || tr.entry_time || '',
        mode.toUpperCase(),
        tr.bot || tr.bot_label || 'AI',
        dir,
        pair,
        tr.entry_price ?? tr.entry ?? '',
        tr.exit_price ?? tr.exit ?? '',
        tr.pnl ?? '',
        tr.reason || '',
      ].join(',')
    })
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `copix-history-${filterMode}-${Date.now()}.csv`
    a.click()
  }

  return (
    <div className="h-full flex flex-col gap-3 p-3 md:p-4 overflow-hidden">
      {/* Header */}
      <div className="flex-shrink-0 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <ScrollText size={18} className="text-[var(--info)] flex-shrink-0" />
          <h2 className="text-base font-bold text-[var(--txt)] truncate">{t('history.title')}</h2>
          <span className="text-2xs text-[var(--txt-muted)] mono">{filtered.length} {t('history.records')}</span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <span className="text-2xs text-[var(--txt-muted)]">
            {t('history.total')}{' '}
            <span className={`mono font-bold ${totalPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
              {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} USDT
            </span>
          </span>
          <span className="text-2xs text-[var(--txt-muted)] hidden sm:inline">
            DEMO{' '}
            <span className={`mono font-semibold ${demoPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
              {demoPnl >= 0 ? '+' : ''}{demoPnl.toFixed(2)}
            </span>
          </span>
          <span className="text-2xs text-[var(--txt-muted)] hidden sm:inline">
            LIVE{' '}
            <span className={`mono font-semibold ${livePnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
              {livePnl >= 0 ? '+' : ''}{livePnl.toFixed(2)}
            </span>
          </span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={exportCsv} title={t('history.export_csv')}>
            <Download size={14} />
            <span className="hidden sm:inline">CSV</span>
          </button>
        </div>
      </div>

      {report?.pnl_source && (
        <div className="flex-shrink-0 text-2xs text-[var(--txt-muted)]">
          {t('history.report_source')}: <span className="text-[var(--txt-secondary)]">{report.pnl_source}</span>
        </div>
      )}

      {/* Filters */}
      <div className="panel flex-shrink-0">
        <div className="p-3 flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <span className="text-2xs text-[var(--txt-muted)] font-semibold uppercase tracking-wide">Счёт</span>
            <div className="flex gap-1">
              {[
                { k: 'all', l: t('history.all') },
                { k: 'demo', l: 'DEMO' },
                { k: 'live', l: 'LIVE' },
              ].map(f => (
                <Chip key={f.k} active={filterMode === f.k} onClick={() => setFilterMode(f.k)}>{f.l}</Chip>
              ))}
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-2xs text-[var(--txt-muted)] font-semibold uppercase tracking-wide">{t('history.result')}</span>
            <div className="flex gap-1">
              {[
                { k: 'all', l: t('history.all') },
                { k: 'win', l: t('history.profit') },
                { k: 'loss', l: t('history.loss') },
              ].map(f => (
                <Chip key={f.k} active={filterResult === f.k} onClick={() => setFilterResult(f.k)}>{f.l}</Chip>
              ))}
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-2xs text-[var(--txt-muted)] font-semibold uppercase tracking-wide">{t('history.pair')}</span>
            <select
              className="text-xs"
              value={filterPair}
              onChange={e => setFilterPair(e.target.value)}
            >
              {allPairs.map(p => (
                <option key={p} value={p}>{p === ALL_PAIRS_KEY ? t('history.all') : p}</option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-2xs text-[var(--txt-muted)] font-semibold uppercase tracking-wide">{t('history.from')}</span>
            <input type="date" className="text-xs" value={dateFrom} onChange={e => setDateFrom(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-2xs text-[var(--txt-muted)] font-semibold uppercase tracking-wide">{t('history.to')}</span>
            <input type="date" className="text-xs" value={dateTo} onChange={e => setDateTo(e.target.value)} />
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="panel flex-1 flex flex-col min-h-0 overflow-hidden">
        <div className="flex-1 overflow-auto">
          {loading ? (
            <div className="flex items-center justify-center py-16"><Loader /></div>
          ) : pageRows.length === 0 ? (
            <EmptyState
              icon={ScrollText}
              text={trades.length === 0 ? t('history.empty') : t('history.empty_filtered')}
              sub={trades.length === 0 ? t('history.empty_hint') : t('history.empty_filtered_hint')}
            />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('history.time')}</th>
                  <th>Счёт</th>
                  <th>{t('history.type')}</th>
                  <th>{t('history.instrument')}</th>
                  <th className="text-right">{t('history.entry')}</th>
                  <th className="text-right">{t('history.exit')}</th>
                  <th className="text-right">{t('history.pnl')}</th>
                  <th>{t('history.reason')}</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map((tr, i) => {
                  const pnl = parseFloat(tr.pnl || 0)
                  const side = (tr.side || tr.pos_side || '').toLowerCase()
                  const isLong = side === 'buy' || side === 'long'
                  const symbol = (tr.coin || tr.symbol || tr.inst_id || '').replace('-USDT-SWAP', '').replace('-USD-SWAP', '')
                  const reasonKey = String(tr.reason || 'closed').toLowerCase()
                  const reasonInfo = REASON_MAP[reasonKey] || REASON_MAP.closed
                  const mode = tradeMode(tr)
                  return (
                    <tr key={`${mode}_${tr.ord_id || i}_${tr.time || i}`}>
                      <td className="text-2xs mono text-[var(--txt-muted)]">
                        {fmtTs(tr.exit_time || tr.time || tr.entry_time, locale)}
                      </td>
                      <td>
                        {mode === 'live' ? (
                          <span className="badge badge-live">LIVE</span>
                        ) : (
                          <span className="badge badge-demo">DEMO</span>
                        )}
                        <span className="badge badge-ai ml-1">AI</span>
                      </td>
                      <td>
                        <span className={`badge ${isLong ? 'badge-long' : 'badge-short'}`}>
                          {isLong ? 'LONG' : 'SHORT'}
                        </span>
                      </td>
                      <td className="text-[var(--txt)] font-medium text-xs">{symbol || '—'}</td>
                      <td className="text-right mono text-xs">
                        {(tr.entry_price ?? tr.entry) != null && (tr.entry_price ?? tr.entry) !== ''
                          ? `$${parseFloat(tr.entry_price ?? tr.entry).toFixed(2)}`
                          : '—'}
                      </td>
                      <td className="text-right mono text-xs">
                        {(tr.exit_price ?? tr.exit) != null && (tr.exit_price ?? tr.exit) !== ''
                          ? `$${parseFloat(tr.exit_price ?? tr.exit).toFixed(2)}`
                          : '—'}
                      </td>
                      <td className={`text-right mono text-xs font-bold ${pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {tr.pnl != null ? `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}` : '—'}
                      </td>
                      <td>
                        <span className={`text-2xs font-semibold px-1.5 py-0.5 rounded ${reasonInfo.bg} ${reasonInfo.color}`}>
                          {reasonInfo.label}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>

        {totalPages > 1 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-[var(--border)] flex-shrink-0">
            <span className="text-2xs text-[var(--txt-muted)]">
              {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, filtered.length)} {t('history.of')} {filtered.length}
            </span>
            <div className="flex items-center gap-2">
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}>
                <ChevronLeft size={14} />
              </button>
              <span className="text-xs text-[var(--txt-secondary)] mono">{page + 1} / {totalPages}</span>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1}>
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
