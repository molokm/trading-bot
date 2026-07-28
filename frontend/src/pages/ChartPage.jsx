import React, { useState, useEffect, useRef, useCallback } from 'react'
import { createChart, ColorType, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { BarChart3, RefreshCw } from 'lucide-react'
import { api } from '../services/api'
import { Tip, Chip, Loader } from '../components/ui'

const PAIRS = [
  { id: 'BTC-USDT-SWAP', label: 'BTC/USDT' },
  { id: 'ETH-USDT-SWAP', label: 'ETH/USDT' },
  { id: 'SOL-USDT-SWAP', label: 'SOL/USDT' },
  { id: 'BNB-USDT-SWAP', label: 'BNB/USDT' },
]
const INTERVALS = ['5m', '15m', '1H', '4H', '1D']
const INDICATORS = [
  { id: 'sma', label: 'SMA 20', color: '#4a9eff', tip: 'Simple Moving Average — средняя цена за 20 периодов. Помогает определить тренд.' },
  { id: 'ema', label: 'EMA 50', color: '#ff9500', tip: 'Exponential Moving Average — придает больший вес последним ценам. Быстрее реагирует.' },
]

export default function ChartPage() {
  const [selectedPair, setSelectedPair] = useState('BTC-USDT-SWAP')
  const [chartData, setChartData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [tf, setTf] = useState('1D')
  const [chartTrades, setChartTrades] = useState({ markers: [], trade_lines: [] })
  const [activeIndicators, setActiveIndicators] = useState(['sma'])
  const chartRef = useRef(null)
  const containerRef = useRef(null)
  const markersRef = useRef(null)
  const indicatorSeriesRef = useRef({})

  const loadChartData = useCallback(async () => {
    try {
      const data = await api.momentumChartData()
      setChartTrades(data)
    } catch {
      setChartTrades({ markers: [], trade_lines: [] })
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
      const result = await api.getCandles(selectedPair, tf, 300)
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

  // Compute SMA/EMA
  function computeSMA(data, period) {
    const result = []
    for (let i = period - 1; i < data.length; i++) {
      let sum = 0
      for (let j = i - period + 1; j <= i; j++) sum += data[j].close
      result.push({ time: data[i].time, value: sum / period })
    }
    return result
  }

  function computeEMA(data, period) {
    const k = 2 / (period + 1)
    const result = []
    let ema = data[0]?.close || 0
    for (let i = 0; i < data.length; i++) {
      ema = data[i].close * k + ema * (1 - k)
      if (i >= period - 1) result.push({ time: data[i].time, value: ema })
    }
    return result
  }

  useEffect(() => {
    if (!chartData || chartData.length === 0) return

    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }
    markersRef.current = {}
    indicatorSeriesRef.current = {}

    const container = containerRef.current
    if (!container) return

    const bgColor = getComputedStyle(document.documentElement).getPropertyValue('--surface').trim() || '#13161d'
    const gridColor = getComputedStyle(document.documentElement).getPropertyValue('--border').trim() || 'rgba(255,255,255,0.06)'
    const textColor = getComputedStyle(document.documentElement).getPropertyValue('--txt-muted').trim() || '#5c6370'

    const chart = createChart(container, {
      layout: { background: { type: ColorType.Solid, color: bgColor }, textColor, fontSize: 11 },
      grid: { vertLines: { color: gridColor }, horzLines: { color: gridColor } },
      crosshair: {
        mode: 0,
        vertLine: { color: 'var(--border-hover)', width: 1, style: 3, labelBackgroundColor: bgColor },
        horzLine: { color: 'var(--border-hover)', width: 1, style: 3, labelBackgroundColor: bgColor },
      },
      timeScale: { borderColor: gridColor, timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: gridColor, scaleMargins: { top: 0.1, bottom: 0.1 } },
      width: container.clientWidth,
      height: container.clientHeight || 500,
    })

    // Candlestick series
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: 'var(--profit)', downColor: 'var(--loss)',
      borderUpColor: 'var(--profit)', borderDownColor: 'var(--loss)',
      wickUpColor: 'var(--profit)', wickDownColor: 'var(--loss)',
      priceFormat: { type: 'price', precision: selectedPair.includes('BTC') ? 0 : 2, minMove: selectedPair.includes('BTC') ? 1 : 0.01 },
    })
    candleSeries.setData(chartData)

    // Price lines
    const lastClose = chartData[chartData.length - 1].close
    const step = selectedPair.includes('BTC') ? 500 : 10
    const center = Math.round(lastClose / step) * step
    for (let i = -4; i <= 4; i++) {
      candleSeries.createPriceLine({
        price: center + i * step,
        color: i === 0 ? 'rgba(74,158,255,0.3)' : 'rgba(255,255,255,0.04)',
        lineWidth: 1, lineStyle: i === 0 ? 1 : 2, axisLabelVisible: true, title: '',
      })
    }

    // Indicators
    INDICATORS.forEach(ind => {
      if (!activeIndicators.includes(ind.id)) return
      let data
      if (ind.id === 'sma') data = computeSMA(chartData, 20)
      else if (ind.id === 'ema') data = computeEMA(chartData, 50)
      if (!data || data.length === 0) return
      const s = chart.addSeries(LineSeries, {
        color: ind.color, lineWidth: 1, lineStyle: 0,
        priceScaleId: 'right', lastValueVisible: true, priceLineVisible: false,
        title: ind.label,
      })
      s.setData(data)
      indicatorSeriesRef.current[ind.id] = s
    })

    // Trade markers
    const pairName = selectedPair.replace('-USDT-SWAP', '')
    const filteredMarkers = chartTrades.markers
      .filter(m => m.symbol === selectedPair || m.symbol === pairName || m.symbol?.includes(pairName))
      .map(m => {
        if (m.side === 'buy') return { time: m.time, position: 'belowBar', color: 'var(--profit)', shape: 'arrowUp', text: `$${m.entry?.toFixed?.(0) || m.entry}` }
        const pnl = m.pnl || 0
        return { time: m.time, position: 'aboveBar', color: pnl >= 0 ? 'var(--profit)' : 'var(--loss)', shape: 'arrowDown', text: `$${m.exit_price?.toFixed?.(0) || m.exit_price} ${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)}` }
      }).sort((a, b) => a.time - b.time)
    if (filteredMarkers.length > 0) markersRef.current.main = createSeriesMarkers(candleSeries, filteredMarkers)

    // Trade lines (TP/SL/BE)
    chartTrades.trade_lines
      .filter(p => p.inst_id === selectedPair || p.symbol === pairName || p.symbol?.includes(pairName))
      .forEach(pos => {
        if (chartData.length === 0) return
        const endTime = chartData[chartData.length - 1].time
        const makeLine = (value, color, width, style) => {
          if (value == null || value <= 0) return
          const s = chart.addSeries(LineSeries, { color, lineWidth: width, lineStyle: style, priceScaleId: 'right', lastValueVisible: true, priceLineVisible: false })
          s.setData([{ time: chartData[0].time, value }, { time: endTime, value }])
        }
        makeLine(pos.stop, 'var(--loss)', 2, 2)
        makeLine(pos.entry, 'rgba(0,255,136,0.5)', 1, 1)
        makeLine(pos.breakeven, 'rgba(255,215,0,0.5)', 1, 2)
        makeLine(pos.tp1, 'rgba(74,158,255,0.5)', 1, 2)
      })

    // Visible range
    const lastTime = chartData[chartData.length - 1].time
    const firstTime = chartData[0].time
    const range = lastTime - firstTime
    const visibleSecs = Math.max(Math.floor(range * 0.3), 3600)
    chart.timeScale().setVisibleRange({ from: lastTime - visibleSecs, to: lastTime + 60 })
    chartRef.current = chart

    const handleResize = () => { if (container) chart.applyOptions({ width: container.clientWidth, height: container.clientHeight }) }
    window.addEventListener('resize', handleResize)
    const ro = new ResizeObserver(handleResize)
    ro.observe(container)
    return () => { ro.disconnect(); window.removeEventListener('resize', handleResize); chart.remove(); chartRef.current = null }
  }, [chartData, selectedPair, chartTrades, activeIndicators])

  const toggleIndicator = (id) => {
    setActiveIndicators(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  return (
    <div data-tour="chart" className="h-full flex flex-col p-4 gap-3">
      {/* Toolbar */}
      <div className="flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <BarChart3 size={18} className="text-[var(--info)]" />
          <h2 className="text-lg font-bold text-[var(--txt)]">График</h2>
        </div>
        <div className="flex items-center gap-4 flex-wrap">
          <select className="!py-1.5 !px-3 !text-xs" value={selectedPair} onChange={e => setSelectedPair(e.target.value)}>
            {PAIRS.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
          <div className="flex gap-1">
            {INTERVALS.map(i => (
              <Chip key={i} active={tf === i} onClick={() => setTf(i)}>{i}</Chip>
            ))}
          </div>
          <div className="flex gap-1">
            {INDICATORS.map(ind => (
              <button
                key={ind.id}
                onClick={() => toggleIndicator(ind.id)}
                className={`chip ${activeIndicators.includes(ind.id) ? 'active' : ''}`}
                style={activeIndicators.includes(ind.id) ? { borderColor: ind.color, color: ind.color, background: ind.color + '15' } : {}}
              >
                {ind.label}
                <Tip text={ind.tip} />
              </button>
            ))}
          </div>
          <button className="btn btn-ghost btn-sm" onClick={() => { loadCandles(); loadChartData() }}><RefreshCw size={12} /> Обновить</button>
        </div>
      </div>

      {/* Active position info bar */}
      {(() => {
        const pairLines = chartTrades.trade_lines.filter(p => p.inst_id === selectedPair || p.symbol === selectedPair.replace('-USDT-SWAP', '') || p.symbol?.includes(selectedPair.replace('-USDT-SWAP', '')))
        const activeLines = pairLines.filter(p => p.stage !== 'closed')
        if (activeLines.length === 0) return null
        return (
          <div className="flex gap-2 flex-shrink-0 overflow-x-auto">
            {activeLines.map((p, i) => (
              <div key={i} className="panel flex items-center gap-3 px-3 py-2 flex-shrink-0">
                <span className="text-2xs font-bold text-[var(--profit)]">▲ LONG</span>
                <span className="text-2xs text-[var(--txt-muted)]">Entry: <span className="mono text-[var(--txt)]">${p.entry}</span></span>
                <span className="text-2xs text-[var(--txt-muted)]">SL: <span className="mono text-[var(--loss)]">${p.stop}</span></span>
                <span className="text-2xs text-[var(--txt-muted)]">BE: <span className="mono text-[var(--warn)]">${p.breakeven}</span></span>
                <span className="text-2xs text-[var(--txt-muted)]">TP1: <span className="mono text-[var(--info)]">${p.tp1}</span></span>
                <span className="text-2xs text-[var(--txt-muted)]">Stage: <span className="text-accent-purple font-semibold">{p.stage}</span></span>
              </div>
            ))}
          </div>
        )
      })()}

      {/* Chart */}
      <div className="panel flex-1 min-h-0">
        {loading && (
          <div className="flex items-center justify-center py-16"><Loader /></div>
        )}
        {chartData && chartData.length > 0 && (
          <div ref={containerRef} className="w-full h-full" style={{ minHeight: 300 }} />
        )}
        {!loading && chartData?.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <span className="text-sm text-[var(--txt-muted)]">Нет данных</span>
          </div>
        )}
      </div>
    </div>
  )
}
