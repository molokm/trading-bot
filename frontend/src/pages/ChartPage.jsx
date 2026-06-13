import React, { useState, useEffect, useRef } from 'react'
import { createChart, ColorType, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../services/api'
import { BarChart3, Activity, TrendingUp, TrendingDown, Clock } from 'lucide-react'

export default function ChartPage() {
  const [bots, setBots] = useState([])
  const [selected, setSelected] = useState(null)
  const [chartData, setChartData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [tf, setTf] = useState('')
  const chartRef = useRef(null)
  const containerRef = useRef(null)

  useEffect(() => {
    api.listBots().then(r => {
      const list = r.bots || []
      setBots(list)
      if (list.length > 0 && !selected) {
        setSelected(list[0].id)
      }
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!selected) return
    setLoading(true)
    const params = tf ? `?bar=${tf}` : ''
    api.getBotChart(selected, params).then(r => {
      setChartData(r)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [selected, tf])

  useEffect(() => {
    if (!chartData || !chartData.candles || chartData.candles.length === 0) return

    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }

    const container = containerRef.current
    if (!container) return

    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.Solid, color: '#0d1117' },
        textColor: '#8b949e',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#1a2332' },
        horzLines: { color: '#1a2332' },
      },
      crosshair: {
        mode: 0,
        vertLine: { color: '#30363d', width: 1, style: 3, labelBackgroundColor: '#1a2332' },
        horzLine: { color: '#30363d', width: 1, style: 3, labelBackgroundColor: '#1a2332' },
      },
      timeScale: {
        borderColor: '#1a2332',
        timeVisible: true,
        secondsVisible: false,
      },
      rightPriceScale: {
        borderColor: '#1a2332',
        scaleMargins: { top: 0.1, bottom: 0.3 },
        entireTextOnly: true,
      },
      localization: {
        priceFormatter: (p) => '$' + p.toFixed(0),
      },
      width: container.clientWidth,
      height: 500,
    })

    const candlestickSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#00ff88',
      downColor: '#ff4444',
      borderUpColor: '#00ff88',
      borderDownColor: '#ff4444',
      wickUpColor: '#00ff88',
      wickDownColor: '#ff4444',
      priceFormat: { type: 'price', precision: 0, minMove: 1 },
    })

    candlestickSeries.setData(chartData.candles.map(c => ({
      time: c.time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    })))

    if (chartData.markers && chartData.markers.length > 0) {
      createSeriesMarkers(chart, candlestickSeries, chartData.markers)
    }

    if (chartData.indicators?.lines) {
      chartData.indicators.lines.forEach(line => {
        const lineSeries = chart.addSeries(LineSeries, {
          color: line.color,
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: true,
          title: line.name,
        })
        lineSeries.setData(line.data)
      })
    }

    const rsiData = chartData.indicators?.rsi
    if (rsiData && rsiData.length > 0) {
      const rsiSeries = chart.addSeries(LineSeries, {
        color: '#a78bfa',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: true,
        title: 'RSI 14',
        priceFormat: { type: 'custom', formatter: v => v.toFixed(1) },
        scaleMargins: { top: 0.75, bottom: 0.02 },
      })
      rsiSeries.setData(rsiData)

      const overbought = chart.addSeries(LineSeries, {
        color: '#ff444466',
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      })
      overbought.setData(rsiData.map(d => ({ time: d.time, value: 70 })))

      const oversold = chart.addSeries(LineSeries, {
        color: '#00ff8866',
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      })
      oversold.setData(rsiData.map(d => ({ time: d.time, value: 30 })))
    }

    const candles = chartData.candles
    const lastTime = candles[candles.length - 1].time
    const firstTime = candles[0].time
    const range = lastTime - firstTime
    const visibleSecs = Math.max(Math.floor(range * 0.25), 3600)
    chart.timeScale().setVisibleRange({
      from: lastTime - visibleSecs,
      to: lastTime + 60,
    })
    chartRef.current = chart

    const handleResize = () => {
      if (container) {
        chart.applyOptions({ width: container.clientWidth })
      }
    }
    window.addEventListener('resize', handleResize)
    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
    }
  }, [chartData])

  const bot = bots.find(b => b.id === selected)
  const intervals = ['1m', '3m', '5m', '15m', '30m', '1H', '4H']

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white flex items-center gap-2">
            <BarChart3 size={22} className="text-neon-blue" />
            График
          </h2>
          <p className="text-sm text-gray-400 mt-1">Свечной график с индикаторами и сделками</p>
        </div>
      </div>

      <div className="flex items-center gap-4 flex-wrap">
        <select
          className="glass px-4 py-2 text-sm text-white rounded-lg border border-white/10 focus:border-neon-blue/50 outline-none"
          value={selected || ''}
          onChange={e => setSelected(e.target.value)}
        >
          <option value="" disabled>Выберите бота</option>
          {bots.map(b => (
            <option key={b.id} value={b.id}>
              {b.strategy_id} — {b.symbol} ({b.timeframe})
            </option>
          ))}
        </select>

        <div className="flex gap-1">
          {intervals.map(i => (
            <button
              key={i}
              onClick={() => setTf(tf === i ? '' : i)}
              className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-all ${
                tf === i
                  ? 'bg-neon-blue/20 text-neon-blue border border-neon-blue/30'
                  : 'text-gray-400 border border-white/5 hover:border-white/20'
              }`}
            >
              {i}
            </button>
          ))}
        </div>

        {bot && (
          <div className="flex items-center gap-3 text-xs text-gray-400 ml-auto">
            <span className="flex items-center gap-1">
              <Activity size={12} />
              {bot.symbol}
            </span>
            <span className="flex items-center gap-1">
              <Clock size={12} />
              {bot.timeframe}
            </span>
            {bot.position !== 0 && (
              <span className={`flex items-center gap-1 font-medium ${bot.position > 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                {bot.position > 0 ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                {bot.position > 0 ? 'LONG' : 'SHORT'} {Math.abs(bot.position).toFixed(4)}
              </span>
            )}
          </div>
        )}
      </div>

      {loading && (
        <div className="glass p-10 text-center text-gray-500">
          <div className="animate-spin w-6 h-6 border-2 border-neon-blue border-t-transparent rounded-full mx-auto mb-2" />
          Загрузка данных...
        </div>
      )}

      {!loading && chartData && chartData.candles && chartData.candles.length > 0 && (
        <div className="glass p-4">
          <div ref={containerRef} className="w-full" style={{ height: 500 }} />
        </div>
      )}

      {!loading && chartData && chartData.candles && chartData.candles.length === 0 && (
        <div className="glass p-10 text-center text-gray-500">
          <BarChart3 size={40} className="mx-auto mb-3 opacity-30" />
          <p>Нет данных для отображения</p>
        </div>
      )}

      {chartData?.trades && chartData.trades.length > 0 && (
        <div className="glass p-4">
          <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <Activity size={14} className="text-neon-green" />
            Сделки ({chartData.trades.length})
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-white/5">
                  <th className="text-left py-2 px-2 font-medium">Время</th>
                  <th className="text-left py-2 px-2 font-medium">Сторона</th>
                  <th className="text-right py-2 px-2 font-medium">Цена</th>
                  <th className="text-right py-2 px-2 font-medium">Объём</th>
                  <th className="text-right py-2 px-2 font-medium">P&L</th>
                  <th className="text-right py-2 px-2 font-medium">Статус</th>
                </tr>
              </thead>
              <tbody>
                {chartData.trades.map((t, i) => {
                  const pnl = t.pnl != null ? parseFloat(t.pnl) : null
                  return (
                    <tr key={i} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                      <td className="py-2 px-2 text-xs text-gray-400">
                        {t.timestamp ? new Date(t.timestamp).toLocaleString() : '-'}
                      </td>
                      <td className="py-2 px-2">
                        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                          t.side === 'buy' ? 'bg-neon-green/10 text-neon-green' : 'bg-neon-red/10 text-neon-red'
                        }`}>
                          {t.side?.toUpperCase() || '-'}
                        </span>
                      </td>
                      <td className="py-2 px-2 text-right mono">${parseFloat(t.px || 0).toLocaleString()}</td>
                      <td className="py-2 px-2 text-right mono">{t.sz || '-'}</td>
                      <td className="py-2 px-2 text-right">
                        {pnl !== null ? (
                          <span className={`mono text-xs font-bold ${pnl >= 0 ? 'text-neon-green' : 'text-neon-red'}`}>
                            {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)} USDT
                          </span>
                        ) : '-'}
                      </td>
                      <td className="py-2 px-2 text-right">
                        <span className={`text-xs px-2 py-0.5 rounded-full ${
                          t.state === 'closed' ? 'text-neon-yellow bg-neon-yellow/10' : 'text-neon-green bg-neon-green/10'
                        }`}>
                          {t.state || 'filled'}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}