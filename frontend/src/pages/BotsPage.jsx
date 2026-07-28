import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import {
  Plus, Play, Pause, Square, Copy, Trash2, Edit3, Bot, Settings2,
  TrendingUp, X, Loader2, Zap, ChevronRight, Clock
} from 'lucide-react'
import { api } from '../services/api'
import { SliderPanel, Tip, StatusBadge, MetricCard, ConfirmDialog, STRATEGY_DESC, EmptyState, Loader } from '../components/ui'

const STRATEGIES = [
  { id: 'momentum', name: 'Momentum', icon: TrendingUp, desc: STRATEGY_DESC.momentum, params: ['risk_per_trade', 'max_positions', 'poll_interval_sec', 'trail_pct', 'breakeven_pct', 'tp1_pct', 'tp1_frac', 'sl1_pct', 'sl1_frac', 'adx_threshold'] },
  { id: 'grid', name: 'Grid', icon: Settings2, desc: STRATEGY_DESC.grid, params: ['position_size', 'grid_levels', 'grid_step', 'max_positions', 'tp_pct', 'sl_pct'] },
  { id: 'dca', name: 'DCA', icon: TrendingUp, desc: STRATEGY_DESC.dca, params: ['position_size', 'dca_orders', 'dca_step', 'max_positions', 'tp_pct'] },
  { id: 'scalping', name: 'Scalping', icon: Zap, desc: STRATEGY_DESC.scalping, params: ['position_size', 'tp_pct', 'sl_pct', 'max_positions', 'poll_interval_sec'] },
  { id: 'custom', name: 'Custom', icon: Settings2, desc: STRATEGY_DESC.custom, params: [] },
]

const PAIRS_OPTIONS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'BNB-USDT-SWAP']

const PARAM_META = {
  risk_per_trade:      { label: 'Риск на сделку', min: 0.5, max: 10, step: 0.5, unit: '%', div: 100, tip: 'Процент капитала, рискуемый в одной сделке. 3% = стандартный риск-менеджмент.' },
  max_positions:       { label: 'Макс. позиций', min: 1, max: 10, step: 1, unit: '', div: 1, tip: 'Максимальное количество одновременно открытых позиций.' },
  poll_interval_sec:   { label: 'Интервал опроса', min: 15, max: 300, step: 15, unit: 'с', div: 1, tip: 'Как часто бот проверяет условия входа. Меньше = быстрее реакция, но больше нагрузка.' },
  trail_pct:           { label: 'Trailing Stop', min: 0.5, max: 10, step: 0.5, unit: '%', div: 100, tip: 'Откат от пика цены для закрытия позиции трейлинг-стопом.' },
  breakeven_pct:       { label: 'Безубыток при', min: 0.1, max: 3, step: 0.1, unit: '%', div: 100, tip: 'При достижении этого профита стоп перемещается на уровень входа.' },
  tp1_pct:             { label: 'TP1 уровень', min: 0.5, max: 10, step: 0.5, unit: '%', div: 100, tip: 'Уровень частичной фиксации прибыли. При достижении закрывается указанная доля.' },
  tp1_frac:            { label: 'TP1 доля', min: 20, max: 100, step: 5, unit: '%', div: 100, tip: 'Какая часть позиции закрывается при достижении TP1.' },
  sl1_pct:             { label: 'SL1 каскад', min: 0, max: 5, step: 0.5, unit: '%', div: 1, tip: 'При просадке на этот % закрывается указанная доля позиции.' },
  sl1_frac:            { label: 'SL1 доля', min: 20, max: 100, step: 5, unit: '%', div: 100, tip: 'Какая часть позиции закрывается при каскадном стопе.' },
  adx_threshold:       { label: 'ADX порог', min: 10, max: 50, step: 1, unit: '', div: 1, tip: 'Минимальное значение ADX для подтверждения тренда. > 20 = умеренный тренд.' },
  position_size:       { label: 'Размер позиции', min: 1, max: 100, step: 1, unit: 'USDT', div: 1, tip: 'Размер позиции в USDT.' },
  grid_levels:         { label: 'Уровни сетки', min: 2, max: 20, step: 1, unit: '', div: 1, tip: 'Количество ордеров, расставляемых выше и ниже текущей цены.' },
  grid_step:           { label: 'Шаг сетки', min: 0.1, max: 5, step: 0.1, unit: '%', div: 1, tip: 'Расстояние в % между соседними ордерами сетки.' },
  tp_pct:              { label: 'Take Profit', min: 0.1, max: 20, step: 0.1, unit: '%', div: 1, tip: 'Целевая прибыль в % для закрытия позиции.' },
  sl_pct:              { label: 'Stop Loss', min: 0.1, max: 20, step: 0.1, unit: '%', div: 1, tip: 'Максимальный убыток в % для принудительного закрытия.' },
  dca_orders:          { label: 'DCA ордеров', min: 1, max: 10, step: 1, unit: '', div: 1, tip: 'Количество дополнительных ордеров усреднения.' },
  dca_step:            { label: 'DCA шаг', min: 0.1, max: 10, step: 0.1, unit: '%', div: 1, tip: 'Расстояние в % между DCA ордерами.' },
}

const DEFAULT_BOT = {
  id: 'mom-1',
  name: 'Momentum Bot',
  strategy: 'momentum',
  pair: 'BTC-USDT-SWAP',
  status: 'stopped',
  config: {
    risk_per_trade: 3, max_positions: 4, poll_interval_sec: 60,
    trail_pct: 1.5, breakeven_pct: 0.5, tp1_pct: 2, tp1_frac: 75,
    sl1_pct: 1, sl1_frac: 50, adx_threshold: 20,
  },
  pnl: 0, trades: 0, created: new Date().toISOString(),
}

function simpleHash(str) {
  let h = 0
  for (let i = 0; i < str.length; i++) { h = ((h << 5) - h + str.charCodeAt(i)) | 0 }
  return Math.abs(h)
}
function seededRandom(seed) {
  let s = seed
  return () => { s = (s * 16807) % 2147483647; return (s - 1) / 2147483646 }
}

function BotSparkline({ botId, pnl }) {
  const points = useMemo(() => {
    const rng = seededRandom(simpleHash(botId || 'default') % 2147483647)
    const data = [100]
    for (let i = 1; i < 8; i++) {
      const delta = (rng() - 0.45) * 12
      data.push(Math.max(80, data[i - 1] + delta))
    }
    if (pnl < 0) data[data.length - 1] = Math.min(data[data.length - 1], 95)
    else if (pnl > 0) data[data.length - 1] = Math.max(data[data.length - 1], 105)
    return data
  }, [botId, pnl])

  const w = 80, h = 28, pad = 2
  const min = Math.min(...points) * 0.98
  const max = Math.max(...points) * 1.02
  const range = max - min || 1
  const xStep = (w - pad * 2) / (points.length - 1)
  const coords = points.map((v, i) => ({
    x: pad + i * xStep,
    y: pad + (1 - (v - min) / range) * (h - pad * 2)
  }))
  const lineD = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ')
  const areaD = lineD + ` L${coords[coords.length - 1].x.toFixed(1)},${(h - pad).toFixed(1)} L${coords[0].x.toFixed(1)},${(h - pad).toFixed(1)} Z`
  const color = pnl >= 0 ? 'var(--profit)' : 'var(--loss)'

  return (
    <svg width={w} height={h} className="block flex-shrink-0">
      <defs>
        <linearGradient id={`spk-${botId}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.3" />
          <stop offset="100%" stopColor={color} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      <path d={areaD} fill={`url(#spk-${botId})`} />
      <path d={lineD} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function useRuntime(startedAt) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!startedAt) return
    const id = setInterval(() => setNow(Date.now()), 30000)
    return () => clearInterval(id)
  }, [startedAt])
  if (!startedAt) return null
  const diffMs = now - startedAt
  const totalMin = Math.floor(diffMs / 60000)
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  if (h === 0 && m === 0) return 'Работает <1м'
  if (h === 0) return `Работает ${m}м`
  return `Работает ${h}ч ${m}м`
}

function RiskMeter({ value }) {
  const pct = ((value - 0.5) / 9.5) * 100
  let color
  if (pct <= 33) color = 'var(--profit)'
  else if (pct <= 66) color = 'var(--warn)'
  else color = 'var(--loss)'
  let label
  if (pct <= 33) label = 'Низкий риск'
  else if (pct <= 66) label = 'Средний риск'
  else label = 'Высокий риск'
  return (
    <div className="mt-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-2xs text-[var(--txt-muted)]">Уровень риска</span>
        <span className="text-2xs font-medium" style={{ color }}>{label}</span>
      </div>
      <div className="h-2 rounded-full bg-[var(--surface-overlay)] overflow-hidden">
        <div className="h-full rounded-full transition-all duration-200" style={{ width: `${Math.max(pct, 2)}%`, background: color }} />
      </div>
    </div>
  )
}

export default function BotsPage({ connected, isGuest }) {
  const [bots, setBots] = useState(() => {
    const saved = localStorage.getItem('bots_config')
    return saved ? JSON.parse(saved) : [DEFAULT_BOT]
  })
  const [momentumStatus, setMomentumStatus] = useState(null)
  const [sliderOpen, setSliderOpen] = useState(false)
  const [editingBot, setEditingBot] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [confirmStopAll, setConfirmStopAll] = useState(false)
  const [saving, setSaving] = useState(false)
  const formRef = useRef(null)

  useEffect(() => {
    api.momentumStatus().then(s => {
      if (s?.running) {
        setMomentumStatus(s)
        setBots(prev => prev.map(b => b.id === 'mom-1' ? { ...b, status: 'running', startedAt: b.startedAt || Date.now() } : b))
      }
    }).catch(() => {})
  }, [connected])

  const saveBots = useCallback((updaterOrArray) => {
    setBots(prev => {
      const next = typeof updaterOrArray === 'function' ? updaterOrArray(prev) : updaterOrArray
      localStorage.setItem('bots_config', JSON.stringify(next))
      return next
    })
  }, [])

  const handleSave = (botData) => {
    setSaving(true)
    setTimeout(() => {
      saveBots(prev => {
        if (botData.id) {
          return prev.map(b => b.id === botData.id ? { ...b, ...botData } : b)
        }
        return [...prev, { ...botData, id: `bot-${Date.now()}`, status: 'stopped', pnl: 0, trades: 0, created: new Date().toISOString() }]
      })
      setSaving(false)
      setSliderOpen(false)
      setEditingBot(null)
    }, 300)
  }

  const handleClone = (bot) => {
    const clone = { ...bot, id: `bot-${Date.now()}`, name: `${bot.name} (копия)`, status: 'stopped', pnl: 0, trades: 0, startedAt: undefined }
    saveBots(prev => [...prev, clone])
  }

  const handleDelete = (id) => {
    saveBots(prev => prev.filter(b => b.id !== id))
    setConfirmDelete(null)
  }

  const handleToggle = async (bot) => {
    if (bot.status === 'running') {
      try { await api.momentumStop() } catch (e) { alert(e.message) }
      saveBots(prev => prev.map(b => b.id === bot.id ? { ...b, status: 'stopped' } : b))
    } else {
      try { await api.momentumStart(bot.config || {}) } catch (e) { alert(e.message) }
      saveBots(prev => prev.map(b => b.id === bot.id ? { ...b, status: 'running', startedAt: Date.now() } : b))
    }
  }

  const statusMap = {
    running: { mode: 'live', label: 'Running' },
    paused: { mode: 'paused', label: 'Paused' },
    stopped: { mode: 'stopped', label: 'Stopped' },
    error: { mode: 'error', label: 'Error' },
  }

  return (
    <div className="h-full flex flex-col p-4 gap-4 overflow-auto">
      <div className="flex items-center justify-between flex-shrink-0">
        <div>
          <h2 className="text-lg font-bold text-[var(--txt)]">Боты</h2>
          <p className="text-xs text-[var(--txt-muted)]">Управление торговыми ботами и стратегиями</p>
        </div>
        <div className="flex gap-2">
          {!isGuest && (
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setConfirmStopAll(true)}>
                <Square size={12} /> Stop All
              </button>
              <button className="btn btn-primary btn-sm" onClick={() => { setEditingBot(null); setSliderOpen(true) }}>
                <Plus size={12} /> Новый бот
              </button>
            </>
          )}
        </div>
      </div>

      {bots.length === 0 ? (
        <EmptyState icon={Bot} text="Боты не созданы" sub="Нажмите «Новый бот» для настройки" />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {bots.map(bot => {
            const st = statusMap[bot.status] || statusMap.stopped
            const strat = STRATEGIES.find(s => s.id === bot.strategy)
            const runtime = useRuntime(bot.startedAt)
            return (
              <div key={bot.id} className="panel hover:border-[var(--border-hover)] transition-colors">
                <div className="p-4 space-y-3">
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-2">
                      {strat && <strat.icon size={16} className="text-[var(--info)]" />}
                      <div>
                        <div className="text-sm font-semibold text-[var(--txt)]">{bot.name}</div>
                        <div className="text-2xs text-[var(--txt-muted)] mono">{bot.id}</div>
                      </div>
                    </div>
                    <StatusBadge mode={st.mode} label={st.label} />
                  </div>

                  <div className="flex items-center gap-2 text-xs">
                    <span className="text-[var(--txt-muted)]">Пара:</span>
                    <span className="text-[var(--txt)] font-medium">{bot.pair?.replace('-USDT-SWAP', '/USDT')}</span>
                  </div>

                  <div className="text-2xs text-[var(--txt-secondary)] leading-relaxed">
                    {strat?.desc?.substring(0, 100)}...
                  </div>

                  <BotSparkline botId={bot.id} pnl={bot.pnl} />

                  {bot.status === 'running' && runtime && (
                    <div className="flex items-center gap-1.5 text-2xs text-[var(--profit)]">
                      <Clock size={11} className="flex-shrink-0" />
                      <span>{runtime}</span>
                    </div>
                  )}

                  <div className="grid grid-cols-2 gap-2">
                    <div className="p-2 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">Всего PnL</div>
                      <div className={`mono text-sm font-bold ${bot.pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>${bot.pnl.toFixed(2)}</div>
                    </div>
                    <div className="p-2 rounded-md bg-[var(--bg)]">
                      <div className="text-2xs text-[var(--txt-muted)]">Сделок</div>
                      <div className="mono text-sm font-bold text-[var(--txt)]">{bot.trades}</div>
                    </div>
                  </div>

                  {!isGuest && (
                    <div className="flex gap-1.5 pt-1">
                      <button className={`btn btn-sm flex-1 ${bot.status === 'running' ? 'btn-danger' : 'btn-primary'}`} onClick={() => handleToggle(bot)}>
                        {bot.status === 'running' ? <><Square size={11} /> Stop</> : <><Play size={11} /> Start</>}
                      </button>
                      <button className="btn btn-ghost btn-sm" onClick={() => { setEditingBot(bot); setSliderOpen(true) }}><Edit3 size={12} /></button>
                      <button className="btn btn-ghost btn-sm" onClick={() => handleClone(bot)}><Copy size={12} /></button>
                      <button className="btn btn-ghost btn-sm text-[var(--loss)]" onClick={() => setConfirmDelete(bot.id)}><Trash2 size={12} /></button>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}

      <SliderPanel
        open={sliderOpen}
        onClose={() => { setSliderOpen(false); setEditingBot(null) }}
        title={editingBot ? `Редактировать: ${editingBot.name}` : 'Новый бот'}
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => { setSliderOpen(false); setEditingBot(null) }}>Отмена</button>
            <button className="btn btn-primary" onClick={() => formRef.current?.requestSubmit()} disabled={saving}>
              {saving ? <Loader /> : <><Zap size={13} /> Сохранить</>}
            </button>
          </>
        }
      >
        <BotConfigForm ref={formRef} bot={editingBot} onSave={handleSave} />
      </SliderPanel>

      <ConfirmDialog
        open={!!confirmDelete}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => handleDelete(confirmDelete)}
        title="Удалить бота"
        text="Вы уверены? Конфигурация бота будет удалена безвозвратно."
        danger
      />
      <ConfirmDialog
        open={confirmStopAll}
        onClose={() => setConfirmStopAll(false)}
        onConfirm={async () => {
          try { await api.momentumStop() } catch {}
          saveBots(prev => prev.map(b => ({ ...b, status: 'stopped' })))
          setConfirmStopAll(false)
        }}
        title="Остановить все боты"
        text="Все активные боты будут остановлены. Открытые позиции НЕ закрываются автоматически."
        danger
        confirmText="Остановить все"
      />
    </div>
  )
}

import { forwardRef } from 'react'

const BotConfigForm = forwardRef(function BotConfigForm({ bot, onSave }, ref) {
  const strat = bot?.strategy || 'momentum'
  const [form, setForm] = useState({
    name: bot?.name || '',
    strategy: bot?.strategy || 'momentum',
    pair: bot?.pair || 'BTC-USDT-SWAP',
    config: bot?.config || {
      risk_per_trade: 3, max_positions: 4, poll_interval_sec: 60,
      trail_pct: 1.5, breakeven_pct: 0.5, tp1_pct: 2, tp1_frac: 75,
      sl1_pct: 1, sl1_frac: 50, adx_threshold: 20,
    },
  })

  const activeStrategy = STRATEGIES.find(s => s.id === form.strategy)
  const activeParams = activeStrategy?.params || []

  const handleSubmit = (e) => {
    e.preventDefault()
    onSave({ id: bot?.id, ...form })
  }

  const updateConfig = (key, val) => {
    setForm(f => ({ ...f, config: { ...f.config, [key]: val } }))
  }

  return (
    <form ref={ref} onSubmit={handleSubmit} className="space-y-5">
      <div>
        <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider flex items-center gap-1">
          Название <Tip text="Произвольное имя для идентификации бота" />
        </label>
        <input className="w-full mt-1.5" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} placeholder="My Bot" autoFocus />
      </div>

      <div>
        <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">Стратегия</label>
        <div className="grid grid-cols-2 gap-2 mt-2">
          {STRATEGIES.map(s => (
            <button
              key={s.id}
              type="button"
              onClick={() => setForm(f => ({ ...f, strategy: s.id, name: f.name || s.name }))}
              className={`p-3 rounded-lg text-left border transition-all ${
                form.strategy === s.id
                  ? 'border-[var(--info)] bg-[var(--info-dim)]'
                  : 'border-[var(--border)] hover:border-[var(--border-hover)]'
              }`}
            >
              <div className="flex items-center gap-2 mb-1">
                <s.icon size={14} className={form.strategy === s.id ? 'text-[var(--info)]' : 'text-[var(--txt-muted)]'} />
                <span className={`text-xs font-semibold ${form.strategy === s.id ? 'text-[var(--info)]' : 'text-[var(--txt)]'}`}>{s.name}</span>
              </div>
              <div className="text-2xs text-[var(--txt-muted)] leading-relaxed line-clamp-2">{s.desc?.substring(0, 80)}...</div>
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">Торговая пара</label>
        <select className="w-full mt-1.5" value={form.pair} onChange={e => setForm(f => ({ ...f, pair: e.target.value }))}>
          {PAIRS_OPTIONS.map(p => <option key={p} value={p}>{p.replace('-USDT-SWAP', '/USDT')}</option>)}
        </select>
      </div>

      {activeParams.length > 0 && (
        <div>
          <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider mb-3 block">Параметры стратегии</label>
          <div className="space-y-4">
            {activeParams.map(key => {
              const meta = PARAM_META[key]
              if (!meta) return null
              const rawVal = form.config[key] ?? meta.min
              const displayVal = meta.div < 1 ? (rawVal * meta.div).toFixed(meta.step < 1 ? 1 : 0) : rawVal
              return (
                <div key={key}>
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-xs text-[var(--txt-secondary)] flex items-center gap-1">
                      {meta.label}
                      <Tip text={meta.tip} />
                    </span>
                    <span className="mono text-xs font-semibold text-[var(--txt)]">{displayVal}{meta.unit}</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <input
                      type="range"
                      min={meta.min}
                      max={meta.max}
                      step={meta.step}
                      value={rawVal}
                      onChange={e => updateConfig(key, parseFloat(e.target.value))}
                      className="flex-1"
                    />
                    <input
                      type="number"
                      className="w-20 text-right mono"
                      value={typeof rawVal === 'number' ? displayVal : rawVal}
                      onChange={e => {
                        const v = parseFloat(e.target.value)
                        if (!isNaN(v)) updateConfig(key, meta.div < 1 ? v / meta.div : v)
                      }}
                      step={meta.step}
                      min={meta.min}
                      max={meta.max}
                    />
                  </div>
                  {key === 'risk_per_trade' && <RiskMeter value={rawVal} />}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {form.strategy === 'grid' && form.config.grid_levels && form.config.grid_step && (
        <div>
          <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider mb-2 block">Визуализация сетки</label>
          <div className="relative h-40 bg-[var(--bg)] rounded-lg border border-[var(--border)] p-3">
            <div className="absolute inset-x-3 top-1/2 h-px bg-[var(--txt-muted)] opacity-30" />
            <span className="absolute left-1/2 -translate-x-1/2 -translate-y-1/2 text-2xs text-[var(--txt-muted)]">Текущая цена</span>
            {Array.from({ length: form.config.grid_levels }).map((_, i) => {
              const offset = (i + 1) * 12
              return (
                <React.Fragment key={i}>
                  <div className="absolute left-3 right-3 border-t border-dashed border-[var(--profit)] opacity-40" style={{ top: `calc(50% - ${offset}px)` }}>
                    <span className="absolute right-0 -top-3 text-2xs mono text-[var(--profit)]">Buy</span>
                  </div>
                  <div className="absolute left-3 right-3 border-t border-dashed border-[var(--loss)] opacity-40" style={{ top: `calc(50% + ${offset}px)` }}>
                    <span className="absolute right-0 -top-3 text-2xs mono text-[var(--loss)]">Sell</span>
                  </div>
                </React.Fragment>
              )
            })}
          </div>
        </div>
      )}
    </form>
  )
})
