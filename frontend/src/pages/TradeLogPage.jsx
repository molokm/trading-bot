import React, { useState, useEffect } from 'react'
import { ScrollText, Filter, Clock, RefreshCw, ChevronLeft, ChevronRight } from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

const PERIODS = [
  { label: 'Сегодня', days: 0 },
  { label: '7 дней', days: 7 },
  { label: '30 дней', days: 30 },
  { label: 'Всё время', days: null },
]

export default function TradeLogPage() {
  const { t } = useTranslation()
  const [trades, setTrades] = useState([])
  const [loading, setLoading] = useState(true)
  const [period, setPeriod] = useState(0)

  function getDateRange(days) {
    if (days === null) return { begin: '', end: '' }
    const end = new Date().toISOString()
    const begin = days === 0
      ? new Date(new Date().setHours(0,0,0,0)).toISOString()
      : new Date(Date.now() - days * 86400000).toISOString()
    return { begin, end }
  }

  async function loadTrades() {
    setLoading(true)
    try {
      const p = PERIODS[period]
      const { begin, end } = getDateRange(p.days)
      const res = await api.getPairedTrades(15, begin, end)
      setTrades(res.trades || [])
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    loadTrades()
  }, [period])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Журнал сделок</h2>
          <p className="text-sm text-gray-400 mt-1">
            {trades.length > 0
              ? `Показано ${trades.length} закрытых сделок`
              : 'Закрытые сделки с P&L'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            className="btn-neon px-4 py-2 rounded-xl text-sm flex items-center gap-2"
            onClick={loadTrades}
          >
            <RefreshCw size={14} />
            Обновить
          </button>
        </div>
      </div>

      {/* Period Filter */}
      <div className="glass p-4">
        <div className="flex items-center gap-2 flex-wrap">
          <Filter size={14} className="text-gray-400" />
          {PERIODS.map((p, i) => (
            <button
              key={i}
              className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                period === i
                  ? 'bg-neon-green/10 text-neon-green border border-neon-green/20'
                  : 'bg-white/5 text-gray-400 hover:text-white hover:bg-white/10'
              }`}
              onClick={() => setPeriod(i)}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* Trades Table */}
      <div className="glass overflow-hidden">
        {loading ? (
          <div className="text-center py-12 text-gray-500">Загрузка...</div>
        ) : trades.length === 0 ? (
          <div className="text-center py-12">
            <ScrollText size={40} className="mx-auto text-gray-500 mb-3" />
            <p className="text-gray-400">Нет закрытых сделок</p>
            <p className="text-xs text-gray-500 mt-1">Сделки появятся после закрытия позиций</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 border-b border-white/5">
                <th className="text-left py-3 px-4 font-medium">Время входа</th>
                <th className="text-left py-3 px-4 font-medium">Выход</th>
                <th className="text-left py-3 px-4 font-medium">Пара</th>
                <th className="text-center py-3 px-4 font-medium">Направление</th>
                <th className="text-right py-3 px-4 font-medium">Цена входа</th>
                <th className="text-right py-3 px-4 font-medium">Цена выхода</th>
                <th className="text-right py-3 px-4 font-medium">Объём</th>
                <th className="text-right py-3 px-4 font-medium">P&L</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => {
                const pnl = t.pnl != null ? parseFloat(t.pnl) : null
                return (
                  <tr key={t.signal_id || i} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                    <td className="py-3 px-4 text-gray-400 mono text-xs">
                      {t.entry_time ? new Date(t.entry_time).toLocaleString() : '-'}
                    </td>
                    <td className="py-3 px-4 text-gray-400 mono text-xs">
                      {t.exit_time ? new Date(t.exit_time).toLocaleString() : 'открыта'}
                    </td>
                    <td className="py-3 px-4 text-white font-medium">{t.inst_id || '-'}</td>
                    <td className="py-3 px-4 text-center">
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                        t.side === 'buy' ? 'bg-neon-green/10 text-neon-green' : 'bg-neon-red/10 text-neon-red'
                      }`}>
                        {t.side === 'buy' ? 'LONG' : 'SHORT'}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-right mono">
                      {t.entry_px ? `$${parseFloat(t.entry_px).toLocaleString()}` : '-'}
                    </td>
                    <td className="py-3 px-4 text-right mono">
                      {t.exit_px ? `$${parseFloat(t.exit_px).toLocaleString()}` : '-'}
                    </td>
                    <td className="py-3 px-4 text-right mono">{t.entry_sz || '-'}</td>
                    <td className="py-3 px-4 text-right">
                      {pnl !== null ? (
                        <span className={`mono text-xs font-bold ${pnl >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                          {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)} USDT
                        </span>
                      ) : (
                        <span className="text-xs text-gray-500">открыта</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
