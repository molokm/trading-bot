import React, { useState, useEffect } from 'react'
import { ScrollText, TrendingUp, Filter, ChevronLeft, ChevronRight, Loader2 } from 'lucide-react'
import { api } from '../services/api'

const PAGE_SIZE = 20

export default function TradeHistoryPage() {
  const [trades, setTrades] = useState([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(0)
  const [filter, setFilter] = useState('all')

  useEffect(() => {
    setLoading(true)
    Promise.all([
      api.momentumTrades(200).catch(() => ({ trades: [] })),
      api.getAllTrades(200).catch(() => ({ trades: [] })),
    ]).then(([mom, all]) => {
      const momTrades = (mom.trades || []).map(t => ({ ...t, source: 'momentum' }))
      const allTrades = (all.trades || []).map(t => ({ ...t, source: 'all' }))
      const merged = [...momTrades, ...allTrades].sort((a, b) => {
        const ta = a.time ? new Date(a.time).getTime() : 0
        const tb = b.time ? new Date(b.time).getTime() : 0
        return tb - ta
      })
      setTrades(merged)
      setLoading(false)
    })
  }, [])

  const filtered = filter === 'all' ? trades : trades.filter(t => t.source === filter)
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE)
  const pageTrades = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  const formatTime = (ts) => {
    if (!ts) return ''
    const d = new Date(ts)
    return d.toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <ScrollText size={22} className="text-neon-purple" />
          <h2 className="text-2xl font-bold text-white">История сделок</h2>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setFilter('all')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              filter === 'all' ? 'bg-white/10 text-white' : 'text-gray-400 hover:text-white hover:bg-white/5'
            }`}
          >
            Все
          </button>
          <button
            onClick={() => setFilter('momentum')}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              filter === 'momentum' ? 'bg-neon-purple/20 text-neon-purple' : 'text-gray-400 hover:text-white hover:bg-white/5'
            }`}
          >
            Momentum
          </button>
        </div>
      </div>

      <div className="glass overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 size={24} className="animate-spin text-gray-400" />
          </div>
        ) : pageTrades.length === 0 ? (
          <div className="text-center py-16">
            <TrendingUp size={40} className="mx-auto text-gray-600 mb-3" />
            <p className="text-sm text-gray-500">Сделок пока нет</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/5 text-xs text-gray-500 uppercase tracking-wider">
                  <th className="text-left px-4 py-3 font-medium">Время</th>
                  <th className="text-left px-4 py-3 font-medium">Тип</th>
                  <th className="text-left px-4 py-3 font-medium">Инструмент</th>
                  <th className="text-right px-4 py-3 font-medium">Размер</th>
                  <th className="text-right px-4 py-3 font-medium">Цена</th>
                  <th className="text-right px-4 py-3 font-medium">PnL</th>
                  <th className="text-right px-4 py-3 font-medium">Причина</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {pageTrades.map((t, i) => {
                  const isBuy = t.side === 'buy'
                  const isPositive = (t.pnl || 0) >= 0
                  return (
                    <tr key={i} className="hover:bg-white/5 transition-colors">
                      <td className="px-4 py-3 text-gray-400 whitespace-nowrap font-mono text-xs">
                        {formatTime(t.time)}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                          isBuy ? 'bg-neon-green/20 text-neon-green' : 'bg-neon-red/20 text-neon-red'
                        }`}>
                          {isBuy ? 'BUY' : 'SELL'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-white font-medium whitespace-nowrap">
                        {t.symbol || t.inst_id || '-'}
                      </td>
                      <td className="px-4 py-3 text-right text-gray-300 font-mono">
                        {t.size ? t.size.toFixed(2) : '-'}
                      </td>
                      <td className="px-4 py-3 text-right text-gray-300 font-mono">
                        {t.entry ? `$${t.entry.toFixed(2)}` : t.exit_price ? `$${t.exit_price.toFixed(2)}` : '-'}
                      </td>
                      <td className={`px-4 py-3 text-right font-mono font-bold ${
                        t.pnl != null ? (isPositive ? 'text-neon-green' : 'text-neon-red') : 'text-gray-500'
                      }`}>
                        {t.pnl != null ? `${isPositive ? '+' : ''}$${t.pnl.toFixed(2)}` : '-'}
                      </td>
                      <td className="px-4 py-3 text-right text-gray-400 text-xs">
                        {t.reason || '-'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <button
            onClick={() => setPage(p => Math.max(0, p - 1))}
            disabled={page === 0}
            className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-white/5 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronLeft size={16} />
          </button>
          <span className="text-xs text-gray-500">
            {page + 1} / {totalPages}
          </span>
          <button
            onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
            className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-white/5 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronRight size={16} />
          </button>
        </div>
      )}
    </div>
  )
}
