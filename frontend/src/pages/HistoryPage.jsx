import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { ScrollText, ChevronLeft, ChevronRight, Download, TrendingUp } from 'lucide-react'
import { api } from '../services/api'
import { EmptyState, Loader, Chip } from '../components/ui'

const PAGE_SIZE = 30
const REASON_MAP = {
  closed: { label: 'Закрыта', color: 'text-[var(--profit)]', bg: 'bg-[var(--profit-dim)]' },
  open: { label: 'Открыта', color: 'text-[var(--info)]', bg: 'bg-[var(--info-dim)]' },
  tp: { label: 'TP', color: 'text-[var(--profit)]', bg: 'bg-[var(--profit-dim)]' },
  sl: { label: 'SL', color: 'text-[var(--loss)]', bg: 'bg-[var(--loss-dim)]' },
  trail: { label: 'Трейл', color: 'text-[var(--info)]', bg: 'bg-[var(--info-dim)]' },
  breakeven: { label: 'BE', color: 'text-[var(--warn)]', bg: 'bg-[var(--warn-dim)]' },
  manual: { label: 'Ручной', color: 'text-[var(--txt-secondary)]', bg: 'bg-[var(--surface-overlay)]' },
  roe_threshold: { label: 'ROE', color: 'text-accent-purple', bg: 'bg-accent-purple/10' },
}

export default function HistoryPage() {
  const [trades, setTrades] = useState([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(0)
  const [filterResult, setFilterResult] = useState('all')
  const [filterPair, setFilterPair] = useState('Все')
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

  const allPairs = useMemo(() => {
    const pairs = new Set(trades.map(t => (t.symbol || t.inst_id || '').replace('-USDT-SWAP', '').replace('-USD-SWAP', '')))
    return ['Все', ...Array.from(pairs).filter(Boolean).sort()]
  }, [trades])

  const filtered = useMemo(() => {
    return trades.filter(t => {
      if (filterResult !== 'all') {
        const pnl = parseFloat(t.pnl || 0)
        if (filterResult === 'win' && pnl < 0) return false
        if (filterResult === 'loss' && pnl >= 0) return false
      }
      if (filterPair !== 'Все') {
        const pair = (t.symbol || t.inst_id || '').toUpperCase()
        if (!pair.includes(filterPair.toUpperCase())) return false
      }
      if (dateFrom) {
        const tradeTime = t.time ? new Date(t.time) : (t.entry_time ? new Date(t.entry_time) : null)
        if (tradeTime && tradeTime < new Date(dateFrom)) return false
      }
      if (dateTo) {
        const tradeTime = t.time ? new Date(t.time) : (t.exit_time ? new Date(t.exit_time) : null)
        if (tradeTime && tradeTime > new Date(dateTo)) return false
      }
      return true
    })
  }, [trades, filterResult, filterPair, dateFrom, dateTo])

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE)
  const pageTrades = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  const fmtTime = (ts) => {
    if (!ts) return '---'
    return new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' })
  }

  const totalPnl = filtered.reduce((s, t) => s + (parseFloat(t.pnl) || 0), 0)
  const winCount = filtered.filter(t => (parseFloat(t.pnl) || 0) >= 0).length
  const winRate = filtered.length > 0 ? ((winCount / filtered.length) * 100).toFixed(1) : '0.0'

  const handleExportCSV = useCallback(() => {
    const header = 'Время,Тип,Инструмент,Размер,Вход,Выход,PnL,Причина'
    const rows = filtered.map(t => {
      const time = t.time ? new Date(t.time).toLocaleString('ru-RU') : ''
      const type = t.side === 'buy' ? 'BUY' : 'SELL'
      const inst = t.symbol || t.inst_id || ''
      const size = t.size ? t.size.toFixed(2) : ''
      const entry = t.entry_price ? t.entry_price.toFixed(2) : ''
      const exit = t.exit_price ? t.exit_price.toFixed(2) : ''
      const pnl = t.pnl != null ? (parseFloat(t.pnl) >= 0 ? '+' : '') + parseFloat(t.pnl).toFixed(2) : ''
      const reason = REASON_MAP[(t.reason || '').toLowerCase()]?.label || t.reason || ''
      return [time, type, inst, size, entry, exit, pnl, reason].map(v => `"${v}"`).join(',')
    })
    const csv = [header, ...rows].join('\n')
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `trades_${new Date().toISOString().slice(0,10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }, [filtered])

  return (
    <div className="h-full flex flex-col p-4 gap-3 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <ScrollText size={18} className="text-accent-purple" />
          <h2 className="text-lg font-bold text-[var(--txt)]">История сделок</h2>
          <span className="text-2xs text-[var(--txt-muted)]">{filtered.length} записей</span>
        </div>
        <div className="flex items-center gap-3">
          <span className={`text-2xs font-semibold px-2 py-0.5 rounded-full ${parseFloat(winRate) >= 50 ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
            <TrendingUp size={11} className="inline -mt-px" /> {winRate}% WR
          </span>
          <button
            className="btn btn-ghost btn-sm"
            onClick={handleExportCSV}
            disabled={filtered.length === 0}
            title="Экспорт CSV"
          >
            <Download size={13} />
            CSV
          </button>
          <span className={`text-xs mono font-semibold ${totalPnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
            Итого: {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} USDT
          </span>
        </div>
      </div>

      {/* Filters */}
      <div className="panel flex-shrink-0">
        <div className="p-3 flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-1.5">
            <span className="text-2xs text-[var(--txt-muted)]">Результат:</span>
            {[{ k: 'all', l: 'Все' }, { k: 'win', l: 'Прибыль' }, { k: 'loss', l: 'Убыток' }].map(f => (
              <Chip key={f.k} active={filterResult === f.k} onClick={() => { setFilterResult(f.k); setPage(0) }} color={f.k === 'win' ? 'green' : f.k === 'loss' ? 'red' : ''}>{f.l}</Chip>
            ))}
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-2xs text-[var(--txt-muted)]">Пара:</span>
            <select className="!py-1 !px-2 !text-2xs" value={filterPair} onChange={e => { setFilterPair(e.target.value); setPage(0) }}>
              {allPairs.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-2xs text-[var(--txt-muted)]">С:</span>
            <input type="date" className="!py-1 !px-2 !text-2xs" value={dateFrom} onChange={e => { setDateFrom(e.target.value); setPage(0) }} />
            <span className="text-2xs text-[var(--txt-muted)]">По:</span>
            <input type="date" className="!py-1 !px-2 !text-2xs" value={dateTo} onChange={e => { setDateTo(e.target.value); setPage(0) }} />
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="panel flex-1 flex flex-col min-h-0">
        <div className="flex-1 overflow-auto">
          {loading ? (
            <div className="flex items-center justify-center py-16"><Loader /></div>
          ) : pageTrades.length === 0 ? (
            <EmptyState icon={ScrollText} text="Сделок не найдено" sub="Попробуйте изменить фильтры" />
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Время</th>
                  <th>Тип</th>
                  <th>Инструмент</th>
                  <th className="text-right">Размер</th>
                  <th className="text-right">Вход</th>
                  <th className="text-right">Выход</th>
                  <th className="text-right">PnL</th>
                  <th className="text-right">Причина</th>
                </tr>
              </thead>
              <tbody>
                {pageTrades.map((t, i) => {
                  const isBuy = t.side === 'buy'
                  const pnl = parseFloat(t.pnl || 0)
                  const reason = (t.reason || '').toLowerCase()
                  const reasonInfo = REASON_MAP[reason] || { label: t.reason || '-', color: 'text-[var(--txt-muted)]', bg: 'bg-[var(--surface-overlay)]' }
                  return (
                    <tr key={i}>
                      <td className="text-2xs mono text-[var(--txt-muted)]">{fmtTime(t.time)}</td>
                      <td>
                        <span className={`text-2xs font-bold px-1.5 py-0.5 rounded ${isBuy ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                          {isBuy ? 'BUY' : 'SELL'}
                        </span>
                      </td>
                      <td className="text-[var(--txt)] font-medium text-xs">{t.symbol || t.inst_id || '-'}</td>
                      <td className="text-right mono text-xs">{t.size ? t.size.toFixed(2) : '-'}</td>
                      <td className="text-right mono text-xs">{t.entry_price ? `$${t.entry_price.toFixed(2)}` : '-'}</td>
                      <td className="text-right mono text-xs">{t.exit_price ? `$${t.exit_price.toFixed(2)}` : '-'}</td>
                      <td className={`text-right mono text-xs font-bold ${pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {t.pnl != null ? `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}` : '-'}
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
              {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, filtered.length)} из {filtered.length}
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
