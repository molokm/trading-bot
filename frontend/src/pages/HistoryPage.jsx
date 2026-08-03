import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { ScrollText, ChevronLeft, ChevronRight, Download, TrendingUp } from 'lucide-react'
import { api } from '../services/api'
import { EmptyState, Loader, Chip } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

const PAGE_SIZE = 30
const ALL_PAIRS_KEY = '__all__'

export default function HistoryPage() {
  const { t, locale } = useTranslation()

  const REASON_MAP = {
    closed: { label: t('reason.closed'), color: 'text-[var(--profit)]', bg: 'bg-[var(--profit-dim)]' },
    open: { label: t('reason.open'), color: 'text-[var(--info)]', bg: 'bg-[var(--info-dim)]' },
    tp: { label: t('reason.tp'), color: 'text-[var(--profit)]', bg: 'bg-[var(--profit-dim)]' },
    sl: { label: t('reason.sl'), color: 'text-[var(--loss)]', bg: 'bg-[var(--loss-dim)]' },
    trail: { label: t('reason.trail'), color: 'text-[var(--info)]', bg: 'bg-[var(--info-dim)]' },
    breakeven: { label: t('reason.breakeven'), color: 'text-[var(--warn)]', bg: 'bg-[var(--warn-dim)]' },
    manual: { label: t('reason.manual'), color: 'text-[var(--txt-secondary)]', bg: 'bg-[var(--surface-overlay)]' },
    roe_threshold: { label: 'ROE', color: 'text-accent-purple', bg: 'bg-accent-purple/10' },
  }

  const [trades, setTrades] = useState([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(0)
  const [filterResult, setFilterResult] = useState('all')
  const [filterPair, setFilterPair] = useState(ALL_PAIRS_KEY)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  useEffect(() => {
    setLoading(true)
    api.getPairedTrades(500).then(data => {
      setTrades(data.trades || [])
      setLoading(false)
    }).catch(() => {
      setTrades([])
      setLoading(false)
    })
  }, [])

  const currentTrades = trades
  const currentLoading = loading

  const allPairs = useMemo(() => {
    const pairs = new Set(currentTrades.map(t => (t.coin || t.symbol || t.inst_id || '').replace('-USDT-SWAP', '').replace('-USD-SWAP', '')))
    return [ALL_PAIRS_KEY, ...Array.from(pairs).filter(Boolean).sort()]
  }, [currentTrades])

  const filtered = useMemo(() => {
    return currentTrades.filter(t => {
      if (filterResult !== 'all') {
        const pnl = parseFloat(t.pnl || 0)
        if (filterResult === 'win' && pnl < 0) return false
        if (filterResult === 'loss' && pnl >= 0) return false
      }
      if (filterPair !== ALL_PAIRS_KEY) {
        const pair = (t.coin || t.symbol || t.inst_id || '').toUpperCase()
        if (!pair.includes(filterPair.toUpperCase())) return false
      }
      if (dateFrom) {
        const tradeTime = t.time || t.exit_time || t.entry_time ? new Date(t.time || t.exit_time || t.entry_time) : null
        if (tradeTime && tradeTime < new Date(dateFrom)) return false
      }
      if (dateTo) {
        const tradeTime = t.time || t.exit_time ? new Date(t.time || t.exit_time) : null
        if (tradeTime && tradeTime > new Date(dateTo)) return false
      }
      return true
    })
  }, [currentTrades, filterResult, filterPair, dateFrom, dateTo])

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE)
  const pageTrades = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  const fmtTime = (ts) => {
    if (!ts) return '---'
    return new Date(ts).toLocaleString(locale, { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' })
  }

  const totalPnl = filtered.reduce((s, t) => s + (parseFloat(t.pnl) || 0), 0)
  const winCount = filtered.filter(t => (parseFloat(t.pnl) || 0) >= 0).length
  const winRate = filtered.length > 0 ? ((winCount / filtered.length) * 100).toFixed(1) : '0.0'

  const handleExportCSV = useCallback(() => {
    const header = [t('history.time'), t('history.type'), t('history.instrument'), t('history.size'), t('history.entry'), t('history.exit'), 'SL', 'TP', t('history.pnl'), t('history.reason')].join(',')
    const rows = filtered.map(tr => {
      const time = tr.time ? new Date(tr.time).toLocaleString(locale) : ''
      const type = tr.side === 'buy' ? 'BUY' : 'SELL'
      const inst = tr.symbol || tr.inst_id || ''
      const size = tr.size ? tr.size.toFixed(2) : ''
      const entry = tr.entry_price ? parseFloat(tr.entry_price).toFixed(2) : ''
      const exit = tr.exit_price ? parseFloat(tr.exit_price).toFixed(2) : ''
      const sl = tr.stop ? parseFloat(tr.stop).toFixed(2) : ''
      const tp = tr.tp ? parseFloat(tr.tp).toFixed(2) : ''
      const pnl = tr.pnl != null ? (parseFloat(tr.pnl) >= 0 ? '+' : '') + parseFloat(tr.pnl).toFixed(2) : ''
      const reason = REASON_MAP[(tr.reason || '').toLowerCase()]?.label || tr.reason || ''
      return [time, type, inst, size, entry, exit, sl, tp, pnl, reason].map(v => `"${v}"`).join(',')
    })
    const csv = [header, ...rows].join('\n')
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `trades_${new Date().toISOString().slice(0,10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }, [filtered, t, locale])

  return (
    <div className="h-full flex flex-col p-4 gap-3 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <ScrollText size={18} className="text-accent-purple" />
            <h2 className="text-lg font-bold text-[var(--txt)]">{t('history.title')}</h2>
          </div>
          <span className="text-2xs text-[var(--txt-muted)]">{filtered.length} {t('history.records')}</span>
        </div>
        <div className="flex items-center gap-3">
          <span className={`text-2xs font-semibold px-2 py-0.5 rounded-full ${parseFloat(winRate) >= 50 ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
            <TrendingUp size={11} className="inline -mt-px" /> {winRate}% WR
          </span>
          <button
            className="btn btn-ghost btn-sm"
            onClick={handleExportCSV}
            disabled={filtered.length === 0}
            title={t('history.export_csv')}
          >
            <Download size={13} />
            CSV
          </button>
          <span className={`text-xs mono font-semibold ${totalPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
            {t('history.total')} {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} USDT
          </span>
        </div>
      </div>

      {/* Filters */}
      <div className="panel flex-shrink-0">
        <div className="p-3 flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1.5">
            <span className="text-2xs text-[var(--txt-muted)]">{t('history.result')}</span>
            {[{ k: 'all', l: t('history.all') }, { k: 'win', l: t('history.profit') }, { k: 'loss', l: t('history.loss') }].map(f => (
              <Chip key={f.k} active={filterResult === f.k} onClick={() => { setFilterResult(f.k); setPage(0) }} color={f.k === 'win' ? 'green' : f.k === 'loss' ? 'red' : ''}>{f.l}</Chip>
            ))}
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-2xs text-[var(--txt-muted)]">{t('history.pair')}</span>
            <select className="!py-1 !px-2 !text-2xs" value={filterPair} onChange={e => { setFilterPair(e.target.value); setPage(0) }}>
              {allPairs.map(p => <option key={p} value={p}>{p === ALL_PAIRS_KEY ? t('history.all') : p}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-2xs text-[var(--txt-muted)]">{t('history.from')}</span>
            <input type="date" className="!py-1 !px-2 !text-2xs" value={dateFrom} onChange={e => { setDateFrom(e.target.value); setPage(0) }} />
            <span className="text-2xs text-[var(--txt-muted)]">{t('history.to')}</span>
            <input type="date" className="!py-1 !px-2 !text-2xs" value={dateTo} onChange={e => { setDateTo(e.target.value); setPage(0) }} />
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="panel flex-1 flex flex-col min-h-0">
        <div className="flex-1 overflow-auto">
          {currentLoading ? (
            <div className="flex items-center justify-center py-16"><Loader /></div>
          ) : pageTrades.length === 0 ? (
            <EmptyState icon={ScrollText} text={t('history.empty')} sub={t('history.empty_hint')} />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('history.time')}</th>
                  <th>Bot</th>
                  <th>{t('history.type')}</th>
                  <th>{t('history.instrument')}</th>
                  <th className="text-right">{t('history.entry')}</th>
                  <th className="text-right">{t('history.exit')}</th>
                  <th className="text-right">SL</th>
                  <th className="text-right">TP</th>
                  <th className="text-right">{t('history.pnl')}</th>
                  <th className="text-right">{t('history.reason')}</th>
                </tr>
              </thead>
              <tbody>
                {pageTrades.map((tr, i) => {
                  const isLong = tr.side === 'buy' || tr.side === 'long'
                  const pnl = parseFloat(tr.pnl || 0)
                  const reason = (tr.reason || '').toLowerCase()
                  const reasonInfo = REASON_MAP[reason] || { label: tr.reason || '-', color: 'text-[var(--txt-muted)]', bg: 'bg-[var(--surface-overlay)]' }
                  const symbol = tr.coin ? `${tr.coin}-USDT-SWAP` : (tr.symbol || tr.inst_id || '-')
                  const tradeTime = tr.time || tr.exit_time || tr.entry_time
                  return (
                    <tr key={i}>
                      <td className="text-2xs mono text-[var(--txt-muted)]">{fmtTime(tradeTime)}</td>
                      <td className={`text-2xs font-bold ${tr.bot === 'Momentum' ? 'text-[var(--info)]' : 'text-[var(--txt-muted)]'}`}>{tr.bot || '—'}</td>
                      <td>
                        <span className={`text-2xs font-bold px-1.5 py-0.5 rounded ${isLong ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                          {isLong ? 'LONG' : 'SHORT'}
                        </span>
                      </td>
                      <td className="text-[var(--txt)] font-medium text-xs">{symbol}</td>
                      <td className="text-right mono text-xs">{tr.entry_price || tr.entry ? `$${parseFloat(tr.entry_price || tr.entry).toFixed(2)}` : '-'}</td>
                      <td className="text-right mono text-xs">{tr.exit_price || tr.exit ? `$${parseFloat(tr.exit_price || tr.exit).toFixed(2)}` : '-'}</td>
                      <td className="text-right mono text-xs">{tr.stop ? `$${parseFloat(tr.stop).toFixed(2)}` : '-'}</td>
                      <td className="text-right mono text-xs">{tr.tp ? `$${parseFloat(tr.tp).toFixed(2)}` : '-'}</td>
                      <td className={`text-right mono text-xs font-bold ${pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {tr.pnl != null ? `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}` : '-'}
                      </td>
                      <td className="text-right">
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

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-[var(--border)]">
            <span className="text-2xs text-[var(--txt-muted)]">
              {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, filtered.length)} {t('history.of')} {filtered.length}
            </span>
            <div className="flex items-center gap-2">
              <button className="btn btn-ghost btn-sm" onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}>
                <ChevronLeft size={14} />
              </button>
              <span className="text-xs text-[var(--txt-secondary)] mono">{page + 1} / {totalPages}</span>
              <button className="btn btn-ghost btn-sm" onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1}>
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
