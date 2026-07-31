import React, { useState, useEffect, useRef, useCallback } from 'react'
import { createChart, ColorType, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { BarChart3, RefreshCw } from 'lucide-react'
import { api } from '../services/api'
import { Tip, Chip, Loader } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

const PAIRS = [
  { id: 'BTC-USDT-SWAP', label: 'BTC/USDT' },
  { id: 'ETH-USDT-SWAP', label: 'ETH/USDT' },
  { id: 'SOL-USDT-SWAP', label: 'SOL/USDT' },
  { id: 'BNB-USDT-SWAP', label: 'BNB/USDT' },
]
const INTERVALS = ['5m', '15m', '1H', '4H', '1D']
const INDICATORS = [
  { id: 'sma', label: 'SMA 20', color: '#4a9eff' },
  { id: 'ema', label: 'EMA 50', color: '#ff9500' },
]

export default function ChartPage() {
  const { t } = useTranslation()
  const [selectedPair, setSelectedPair] = useState('BTC-USDT-SWAP')
  const [chartData, setChartData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [tf, setTf] = useState('1D')
  const [markers, setMarkers] = useState([])
  const [markersStatus, setMarkersStatus] = useState('')
  const [activeIndicators, setActiveIndicators] = useState(['sma'])
  const chartRef = useRef(null)
  const containerRef = useRef(null)
  const markersRef = useRef(null)
  const indicatorSeriesRef = useRef({})

  const loadTradeMarkers = useCallback(async (instId) => {
    try {
      const data = await api.chartTrades(instId)
      const m = data.markers || []
      setMarkers(m)
      setMarkersStatus(`${m.length}`)
      console.log(`[chart] ${instId}: ${m.length} markers, debug:`, data.debug)
    } catch (err) {
      console.error('[chart] failed to load markers:', err)
      setMarkers([])
      setMarkersStatus('err')
    }
  }, [])

  useEffect(() => {
    loadCandles()
    loadTradeMarkers(selectedPair)
  }, [selectedPair, tf])

  useEffect(() => {
    const interval = setInterval(() => loadTradeMarkers(selectedPair), 30000)
    return () => clearInterval(interval)
  }, [selectedPair, loadTradeMarkers])

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
        vertLine: { color: 'rgba(255,255,255,0.15)', width: 1, style: 3, labelBackgroundColor: '#1e2028' },
        horzLine: { color: 'rgba(255,255,255,0.15)', width: 1, style: 3, labelBackgroundColor: '#1e2028' },
      },
      timeScale: { borderColor: gridColor, timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: gridColor, scaleMargins: { top: 0.05, bottom: 0.2 } },
      width: container.clientWidth,
      height: container.clientHeight || 500,
    })

    // Candlestick series
    const isBtc = selectedPair.includes('BTC')
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#00ff88', downColor: '#ff4757',
      borderUpColor: '#00ff88', borderDownColor: '#ff4757',
      wickUpColor: '#00ff88', wickDownColor: '#ff4757',
      priceFormat: { type: 'price', precision: isBtc ? 0 : 2, minMove: isBtc ? 1 : 0.01 },
    })
    candleSeries.setData(chartData)

    // Volume series
    const volumeSeries = chart.addSeries(LineSeries, {
      color: 'rgba(74,158,255,0.15)',
      lineWidth: 1,
      lineStyle: 0,
      priceScaleId: 'volume',
      lastValueVisible: false,
      priceLineVisible: false,
    })
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    })
    volumeSeries.setData(chartData.map(c => ({ time: c.time, value: c.volume })))

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

    // Trade markers from OKX — setMarkers directly, no time filtering
    if (markers.length > 0) {
      try {
        const sorted = [...markers].sort((a, b) => a.time - b.time)
        markersRef.current.main = createSeriesMarkers(candleSeries, sorted)
        console.log(`[chart] applied ${sorted.length} markers to chart`)
      } catch (err) {
        console.error('[chart] failed to create markers:', err)
      }
    }

    // Fit chart to show all data
    chart.timeScale().fitContent()
    chartRef.current = chart

    const handleResize = () => {
      if (container) chart.applyOptions({ width: container.clientWidth, height: container.clientHeight })
    }
    window.addEventListener('resize', handleResize)
    const ro = new ResizeObserver(handleResize)
    ro.observe(container)
    return () => { ro.disconnect(); window.removeEventListener('resize', handleResize); chart.remove(); chartRef.current = null }
  }, [chartData, selectedPair, markers, activeIndicators])

  const toggleIndicator = (id) => {
    setActiveIndicators(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  return (
    <div data-tour="chart" className="h-full flex flex-col p-4 gap-3">
      {/* Toolbar */}
      <div className="flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <BarChart3 size={18} className="text-[var(--info)]" />
          <h2 className="text-lg font-bold text-[var(--txt)]">{t('chart.title')}</h2>
          {markersStatus && markersStatus !== '0' && (
            <span className="text-2xs px-1.5 py-0.5 rounded bg-[var(--info)]/20 text-[var(--info)]">
              {markersStatus === 'err' ? '!' : `${markersStatus} ${t('chart.trades_from_exchange')}`}
            </span>
          )}
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
                <Tip text={ind.id === 'sma' ? t('chart.sma_tip') : t('chart.ema_tip')} />
              </button>
            ))}
          </div>
          <button className="btn btn-ghost btn-sm" onClick={() => { loadCandles(); loadTradeMarkers(selectedPair) }}><RefreshCw size={12} /> {t('chart.refresh')}</button>
        </div>
      </div>

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
            <span className="text-sm text-[var(--txt-muted)]">{t('chart.no_data')}</span>
          </div>
        )}
      </div>
    </div>
  )
}
