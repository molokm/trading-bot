import React, { useState, useEffect, useRef, useCallback } from 'react'
import { createChart, ColorType, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../services/api'
import { BarChart3, Activity, RefreshCw } from 'lucide-react'

const PAIRS = [
  { id: 'BTC-USDT-SWAP', label: 'BTC/USDT' },
  { id: 'ETH-USDT-SWAP', label: 'ETH/USDT' },
  { id: 'SOL-USDT-SWAP', label: 'SOL/USDT' },
  { id: 'BNB-USDT-SWAP', label: 'BNB/USDT' },
]

export default function ChartPage() {
  const [selectedPair, setSelectedPair] = useState('BTC-USDT-SWAP')
  const [chartData, setChartData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [tf, setTf] = useState('1D')
  const [chartTrades, setChartTrades] = useState({ markers: [], open_positions: [] })
  const chartRef = useRef(null)
  const containerRef = useRef(null)
  const markersRef = useRef(null)

  const loadChartData = useCallback(async () => {
    try {
      const data = await api.momentumChartData()
      setChartTrades(data)
    } catch {
      setChartTrades({ markers: [], open_positions: [] })
    }
  }, [])

  useEffect(() => {
    loadCandles()
    loadChartData()
  }, [selectedPair, tf])

  useEffect(() => {
    const interval = setInterval(loadChartData, 30000)
    return () => clearInterval(interval)
  }, [loadChartData])

  async function loadCandles() {
    setLoading(true)
    try {
      const result = await api.getCandles(selectedPair, tf, 200)
      const rawCandles = result.candles || []
      const candles = rawCandles.map(c => ({
        time: Math.floor(parseInt(c[0]) / 1000),
        open: parseFloat(c[1]),
        high: parseFloat(c[2]),
        low: parseFloat(c[3]),
        close: parseFloat(c[4]),
        volume: parseFloat(c[5]),
      })).reverse()
      setChartData(candles)
    } catch {
      setChartData([])
    }
    setLoading(false)
  }

  useEffect(() => {
    if (!chartData || chartData.length === 0) return

    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }
    markersRef.current = null

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
        scaleMargins: { top: 0.1, bottom: 0.1 },
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
      priceFormat: { type: 'price', precision: selectedPair.includes('BTC') ? 0 : 2, minMove: selectedPair.includes('BTC') ? 1 : 0.01 },
    })

    candlestickSeries.setData(chartData)

    const lastClose = chartData[chartData.length - 1].close
    const decimals = selectedPair.includes('BTC') ? 0 : 2
    const step = selectedPair.includes('BTC') ? 500 : 10
    const center = Math.round(lastClose / step) * step
    for (let i = -4; i <= 4; i++) {
      const price = center + i * step
      candlestickSeries.createPriceLine({
        price,
        color: i === 0 ? '#4a9eff44' : '#1a233288',
        lineWidth: 1,
        lineStyle: i === 0 ? 1 : 2,
        axisLabelVisible: true,
        title: '',
      })
    }

    const pairName = selectedPair.replace('-USDT-SWAP', '')

    const filteredMarkers = chartTrades.markers
      .filter(m => m.symbol === selectedPair || m.symbol === pairName || m.symbol?.includes(pairName))
      .map(m => {
        const ts = m.time
        if (m.side === 'buy') {
          return {
            time: ts,
            position: 'belowBar',
            color: '#00ff88',
            shape: 'arrowUp',
            text: `▲ $${m.entry?.toFixed?.(0) || m.entry}`,
          }
        } else {
          const pnl = m.pnl || 0
          const pnlSign = pnl >= 0 ? '+' : ''
          return {
            time: ts,
            position: 'aboveBar',
            color: pnl >= 0 ? '#00ff88' : '#ff4444',
            shape: 'arrowDown',
            text: `▼ $${m.exit_price?.toFixed?.(0) || m.exit_price} ${pnlSign}$${pnl.toFixed(0)}`,
          }
        }
      })
      .sort((a, b) => a.time - b.time)

    if (filteredMarkers.length > 0) {
      markersRef.current = createSeriesMarkers(candlestickSeries, filteredMarkers)
    }

    chartTrades.open_positions
      .filter(p => p.inst_id === selectedPair || p.symbol === pairName)
      .forEach(pos => {
        const entryMarker = chartTrades.markers.find(m => m.side === 'buy' && (m.symbol === selectedPair || m.symbol === pairName || m.symbol?.includes(pairName)))
        const posOpenTime = entryMarker?.time
        if (!posOpenTime || chartData.length === 0) return

        const fromIdx = chartData.reduce((best, c, i) => c.time <= posOpenTime ? i : best, -1)
        if (fromIdx < 0) return

        const stopLineData = [
          { time: chartData[fromIdx].time, value: pos.stop },
          { time: chartData[chartData.length - 1].time, value: pos.stop },
        ]
        const entryLineData = [
          { time: chartData[fromIdx].time, value: pos.entry },
          { time: chartData[chartData.length - 1].time, value: pos.entry },
        ]

        if (stopLineData.length > 0) {
          chart.addSeries(LineSeries, {
            color: '#ff4444',
            lineWidth: 2,
            lineStyle: 2,
            priceScaleId: 'right',
            lastValueVisible: true,
            priceLineVisible: false,
          }).setData(stopLineData)
        }

        if (entryLineData.length > 0) {
          chart.addSeries(LineSeries, {
            color: '#00ff8888',
            lineWidth: 1,
            lineStyle: 1,
            priceScaleId: 'right',
            lastValueVisible: true,
            priceLineVisible: false,
          }).setData(entryLineData)
        }
      })

    const lastTime = chartData[chartData.length - 1].time
    const firstTime = chartData[0].time
    const range = lastTime - firstTime
    const visibleSecs = Math.max(Math.floor(range * 0.25), 3600)
    chart.timeScale().setVisibleRange({ from: lastTime - visibleSecs, to: lastTime + 60 })
    chartRef.current = chart

    const handleResize = () => {
      if (container) chart.applyOptions({ width: container.clientWidth })
    }
    window.addEventListener('resize', handleResize)
    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
      markersRef.current = null
    }
  }, [chartData, selectedPair, chartTrades])

  const intervals = ['5m', '15m', '1H', '4H', '1D']

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white flex items-center gap-2">
            <BarChart3 size={22} className="text-neon-blue" />
            График
          </h2>
          <p className="text-sm text-gray-400 mt-1">Свечной график + сделки momentum</p>
        </div>
        <button
          onClick={() => { loadCandles(); loadChartData() }}
          className="glass px-3 py-2 text-xs text-gray-400 hover:text-white flex items-center gap-2 rounded-lg border border-white/5 hover:border-white/20 transition-all"
        >
          <RefreshCw size={14} />
          Обновить
        </button>
      </div>

      <div className="flex items-center gap-4 flex-wrap">
        <select
          className="glass px-4 py-2 text-sm text-white rounded-lg border border-white/10 focus:border-neon-blue/50 outline-none"
          value={selectedPair}
          onChange={e => setSelectedPair(e.target.value)}
        >
          {PAIRS.map(p => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>

        <div className="flex gap-1">
          {intervals.map(i => (
            <button
              key={i}
              onClick={() => setTf(i)}
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

        {chartTrades.open_positions.length > 0 && (
          <div className="flex items-center gap-2 ml-auto">
            {chartTrades.open_positions
              .filter(p => p.inst_id === selectedPair || p.symbol === selectedPair.replace('-USDT-SWAP', ''))
              .map((p, i) => (
                <div key={i} className="glass px-3 py-1.5 text-xs flex items-center gap-3 rounded-lg border border-neon-green/20">
                  <span className="text-neon-green font-bold">▲ LONG</span>
                  <span className="text-gray-400">Entry: <span className="text-white">${p.entry.toFixed(p.inst_id?.includes('BTC') ? 0 : 2)}</span></span>
                  <span className="text-neon-red">Stop: <span className="text-white">${p.stop.toFixed(p.inst_id?.includes('BTC') ? 0 : 2)}</span></span>
                  <span className="text-neon-yellow">Peak: <span className="text-white">${p.peak.toFixed(p.inst_id?.includes('BTC') ? 0 : 2)}</span></span>
                </div>
              ))}
          </div>
        )}
      </div>

      {loading && (
        <div className="glass p-10 text-center text-gray-500">
          <div className="animate-spin w-6 h-6 border-2 border-neon-blue border-t-transparent rounded-full mx-auto mb-2" />
          Загрузка данных...
        </div>
      )}

      {!loading && chartData && chartData.length > 0 && (
        <div className="glass p-4">
          <div ref={containerRef} className="w-full" style={{ height: 500 }} />
          <div className="flex items-center gap-4 mt-3 text-xs text-gray-500">
            <span className="flex items-center gap-1">
              <span className="inline-block w-2 h-2 rounded-full bg-neon-green" /> Вход (LONG)
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-2 h-2 rounded-full bg-neon-red" /> Выход
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-2 h-2 rounded-full border border-neon-red" style={{borderStyle:'dashed'}} /> Trail Stop
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-2 h-2 rounded-full border border-neon-green" style={{borderStyle:'dotted'}} /> Entry Level
            </span>
            {chartTrades.markers.length > 0 && (
              <span className="ml-auto">{chartTrades.markers.filter(m => m.symbol?.includes(selectedPair.replace('-USDT-SWAP', ''))).length} сделок</span>
            )}
          </div>
        </div>
      )}

      {!loading && chartData && chartData.length === 0 && (
        <div className="glass p-10 text-center text-gray-500">
          <BarChart3 size={40} className="mx-auto mb-3 opacity-30" />
          <p>Нет данных для отображения</p>
        </div>
      )}
    </div>
  )
}
