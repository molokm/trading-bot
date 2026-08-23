import React, { useState, useEffect, useCallback, useMemo } from 'react'
import {
  Activity, Play, Square, RefreshCw, Layers, Gauge, Zap, AlertTriangle, BookOpen
} from 'lucide-react'
import { api } from '../services/api'
import { StatusBadge, Loader } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

const COINS = ['BTC', 'ETH', 'SOL', 'XRP']

function DepthBars({ bids = [], asks = [], maxLevels = 12 }) {
  const rows = useMemo(() => {
    const b = bids.slice(0, maxLevels)
    const a = asks.slice(0, maxLevels)
    const maxSz = Math.max(
      1,
      ...b.map(x => Number(x.sz) || 0),
      ...a.map(x => Number(x.sz) || 0),
    )
    const n = Math.max(b.length, a.length)
    const out = []
    for (let i = 0; i < n; i++) {
      out.push({
        bid: b[i] || null,
        ask: a[i] || null,
        maxSz,
      })
    }
    return out
  }, [bids, asks, maxLevels])

  return (
    <div className="space-y-0.5 font-mono text-2xs">
      <div className="grid grid-cols-[1fr_auto_1fr] gap-2 text-[var(--txt-muted)] px-1 mb-1">
        <span className="text-right">Bid sz</span>
        <span>Price</span>
        <span>Ask sz</span>
      </div>
      {rows.map((r, i) => {
        const bp = r.bid ? Number(r.bid.px) : null
        const bs = r.bid ? Number(r.bid.sz) : 0
        const ap = r.ask ? Number(r.ask.px) : null
        const asz = r.ask ? Number(r.ask.sz) : 0
        const bw = Math.min(100, (bs / r.maxSz) * 100)
        const aw = Math.min(100, (asz / r.maxSz) * 100)
        return (
          <div key={i} className="grid grid-cols-[1fr_auto_1fr] gap-2 items-center">
            <div className="relative h-5 flex justify-end items-center">
              <div
                className="absolute right-0 top-0.5 bottom-0.5 rounded-l bg-[var(--profit)]/25"
                style={{ width: `${bw}%` }}
              />
              <span className="relative z-10 pr-1 text-[var(--profit)]">{bs ? bs.toPrecision(4) : '—'}</span>
            </div>
            <div className="text-center min-w-[5.5rem] text-[var(--txt)]">
              <div className="text-[var(--profit)]">{bp != null ? bp.toLocaleString(undefined, { maximumFractionDigits: 4 }) : '—'}</div>
              <div className="text-[var(--loss)]">{ap != null ? ap.toLocaleString(undefined, { maximumFractionDigits: 4 }) : '—'}</div>
            </div>
            <div className="relative h-5 flex items-center">
              <div
                className="absolute left-0 top-0.5 bottom-0.5 rounded-r bg-[var(--loss)]/25"
                style={{ width: `${aw}%` }}
              />
              <span className="relative z-10 pl-1 text-[var(--loss)]">{asz ? asz.toPrecision(4) : '—'}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function ObiGauge({ value = 0, label = 'OBI₅' }) {
  const v = Math.max(-1, Math.min(1, Number(value) || 0))
  const pct = ((v + 1) / 2) * 100
  const color = v > 0.2 ? 'var(--profit)' : v < -0.2 ? 'var(--loss)' : 'var(--txt-muted)'
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-2xs text-[var(--txt-muted)]">
        <span>{label}</span>
        <span className="mono font-bold" style={{ color }}>{v >= 0 ? '+' : ''}{v.toFixed(3)}</span>
      </div>
      <div className="h-2 rounded-full bg-[var(--bg)] ring-1 ring-[var(--border)] relative overflow-hidden">
        <div className="absolute inset-y-0 left-1/2 w-px bg-[var(--border)] z-10" />
        <div
          className="absolute inset-y-0 rounded-full transition-all duration-300"
          style={{
            left: v >= 0 ? '50%' : `${pct}%`,
            width: `${Math.abs(v) * 50}%`,
            background: color,
            opacity: 0.7,
          }}
        />
      </div>
      <div className="flex justify-between text-[0.6rem] text-[var(--txt-muted)]">
        <span>Ask heavy</span>
        <span>Bid heavy</span>
      </div>
    </div>
  )
}

export default function ScalpPage({ connected, isGuest }) {
  const { t } = useTranslation()
  const [coin, setCoin] = useState('BTC')
  const [book, setBook] = useState(null)
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const loadBook = useCallback(async () => {
    if (!connected) return
    try {
      const b = await api.scalpBook(coin, 12)
      if (b?.error) setErr(b.message || 'book error')
      else {
        setBook(b)
        setErr(null)
      }
    } catch (e) {
      setErr(e.message)
    }
  }, [connected, coin])

  const loadStatus = useCallback(async () => {
    if (!connected) return
    try {
      const s = await api.scalpStatus()
      setStatus(s)
    } catch { /* ignore */ }
  }, [connected])

  useEffect(() => {
    loadBook()
    loadStatus()
    const id = setInterval(() => {
      loadBook()
      loadStatus()
    }, 2000)
    return () => clearInterval(id)
  }, [loadBook, loadStatus])

  const running = !!status?.running
  const liveBook = (status?.books && status.books[coin]) || book

  const toggle = async () => {
    if (isGuest) return
    setBusy(true)
    try {
      if (running) await api.scalpStop()
      else {
        await api.scalpStart({
          symbols: COINS,
          capital: 200,
          execute: true,
          use_llm: true,
          obi_threshold: 0.35,
          persist_n: 3,
        })
      }
      await loadStatus()
    } catch (e) {
      alert(e.message)
    }
    setBusy(false)
  }

  return (
    <div className="h-full flex flex-col p-4 gap-4 overflow-auto">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3 flex-shrink-0">
        <div>
          <div className="flex items-center gap-2">
            <Layers size={18} className="text-cyan-400" />
            <h2 className="text-lg font-bold text-[var(--txt)]">{t('scalp.title')}</h2>
            <span className="text-[0.65rem] font-bold mono px-1.5 py-0.5 rounded-md bg-cyan-500/15 text-cyan-400">
              {status?.version || 'v0.1'}
            </span>
            {running ? <StatusBadge mode="live" label={t('bots.status_running')} /> : <StatusBadge mode="stopped" label={t('bots.status_stopped')} />}
          </div>
          <p className="text-xs text-[var(--txt-muted)] mt-1 max-w-2xl leading-relaxed">
            {t('scalp.subtitle')}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {!isGuest && (
            <button
              className={`btn btn-sm ${running ? 'btn-danger' : 'btn-primary'}`}
              disabled={busy || !connected}
              onClick={toggle}
            >
              {busy ? <Loader /> : running ? <><Square size={12} /> {t('bots.stop')}</> : <><Play size={12} /> {t('bots.start')}</>}
            </button>
          )}
          <button className="btn btn-ghost btn-sm" onClick={() => { setLoading(true); loadBook().finally(() => setLoading(false)) }}>
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Method strip */}
      <div className="panel p-3 flex-shrink-0">
        <div className="flex items-start gap-2">
          <BookOpen size={14} className="text-cyan-400 mt-0.5 flex-shrink-0" />
          <div className="text-2xs text-[var(--txt-secondary)] leading-relaxed space-y-1">
            <p>{t('scalp.method')}</p>
            <p className="text-[var(--txt-muted)]">{t('scalp.disclaimer')}</p>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-3 flex-1 min-h-0">
        {/* Left: coin + metrics */}
        <div className="space-y-3">
          <div className="panel p-3">
            <div className="flex gap-1.5 flex-wrap mb-3">
              {COINS.map(c => (
                <button
                  key={c}
                  className={`px-2.5 py-1 rounded-md text-xs font-semibold transition-colors ${
                    coin === c
                      ? 'bg-cyan-500/20 text-cyan-300 ring-1 ring-cyan-500/40'
                      : 'bg-[var(--bg)] text-[var(--txt-muted)] hover:text-[var(--txt)]'
                  }`}
                  onClick={() => setCoin(c)}
                >
                  {c}
                </button>
              ))}
            </div>
            {err && (
              <div className="flex items-center gap-1.5 text-2xs text-[var(--loss)] mb-2">
                <AlertTriangle size={12} /> {err}
              </div>
            )}
            <div className="grid grid-cols-2 gap-2 text-2xs">
              <div className="p-2 rounded-md bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Mid</div>
                <div className="mono font-bold text-[var(--txt)]">{liveBook?.mid != null ? Number(liveBook.mid).toLocaleString() : '—'}</div>
              </div>
              <div className="p-2 rounded-md bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Micro</div>
                <div className="mono font-bold text-[var(--txt)]">{liveBook?.micro != null ? Number(liveBook.micro).toLocaleString() : '—'}</div>
              </div>
              <div className="p-2 rounded-md bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Spread</div>
                <div className="mono font-semibold text-[var(--txt)]">{liveBook?.spread_bps != null ? `${Number(liveBook.spread_bps).toFixed(2)} bps` : '—'}</div>
              </div>
              <div className="p-2 rounded-md bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Bid/Ask₅</div>
                <div className="mono font-semibold text-[var(--txt)]">{liveBook?.ratio_5 != null ? `${Number(liveBook.ratio_5).toFixed(2)}×` : '—'}</div>
              </div>
            </div>
            <div className="mt-3 space-y-3">
              <ObiGauge value={liveBook?.obi_5} label="OBI₅" />
              <ObiGauge value={liveBook?.obi_10} label="OBI₁₀" />
              <ObiGauge value={liveBook?.w_obi} label="Weighted OBI" />
            </div>
          </div>

          <div className="panel p-3">
            <div className="text-2xs font-semibold text-[var(--txt-muted)] uppercase tracking-wider mb-2 flex items-center gap-1">
              <Gauge size={12} /> {t('scalp.bot_stats')}
            </div>
            <div className="grid grid-cols-2 gap-2 text-2xs">
              <div className="p-2 rounded-md bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Execute</div>
                <div className={`mono font-bold ${status?.execute ? 'text-[var(--loss)]' : 'text-[var(--txt-muted)]'}`}>
                  {status?.execute ? 'ON' : 'OFF'}
                </div>
              </div>
              <div className="p-2 rounded-md bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">PnL</div>
                <div className={`mono font-bold ${(status?.total_pnl || 0) >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                  ${(status?.total_pnl || 0).toFixed(2)}
                </div>
              </div>
              <div className="p-2 rounded-md bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">{t('bots.trades_count')}</div>
                <div className="mono font-semibold">{status?.total_trades ?? 0}</div>
              </div>
              <div className="p-2 rounded-md bg-[var(--bg)]">
                <div className="text-[var(--txt-muted)]">Open</div>
                <div className="mono font-semibold">{status?.open_positions?.length || 0}</div>
              </div>
            </div>
            {status?.last_llm && (
              <div className="mt-2 p-2 rounded-md bg-[var(--bg)] text-2xs">
                <div className="text-[var(--txt-muted)] mb-0.5">LLM</div>
                <div className="text-[var(--txt)]">
                  {status.last_llm.confirm ? '✓ confirm' : '✗ veto'} · conf {status.last_llm.confidence}
                  {status.last_llm.side ? ` · ${status.last_llm.side} ${status.last_llm.coin}` : ''}
                </div>
                {status.last_llm.reason && (
                  <div className="text-[var(--txt-muted)] mt-0.5 line-clamp-2">{status.last_llm.reason}</div>
                )}
              </div>
            )}
            {status?.last_exec && (
              <div className="mt-2 p-2 rounded-md bg-[var(--bg)] text-2xs mono">
                <span className="text-[var(--txt-muted)]">exec: </span>
                {status.last_exec.event} {status.last_exec.coin || ''} {status.last_exec.reason || ''}
              </div>
            )}
          </div>
        </div>

        {/* Center: DOM */}
        <div className="panel p-3 xl:col-span-1 flex flex-col min-h-[320px]">
          <div className="flex items-center gap-2 mb-3">
            <Activity size={14} className="text-cyan-400" />
            <span className="text-xs font-semibold text-[var(--txt)]">{t('scalp.dom_title')} — {coin}-USDT-SWAP</span>
          </div>
          <div className="flex-1 overflow-auto">
            {liveBook?.bids ? (
              <DepthBars bids={liveBook.bids} asks={liveBook.asks} />
            ) : (
              <div className="text-2xs text-[var(--txt-muted)] p-4 text-center">{t('scalp.no_book')}</div>
            )}
          </div>
          {(liveBook?.walls_bid?.length > 0 || liveBook?.walls_ask?.length > 0) && (
            <div className="mt-3 pt-2 border-t border-[var(--border)] text-2xs space-y-1">
              <div className="text-[var(--txt-muted)] font-semibold">Walls (≥3× median)</div>
              {liveBook.walls_bid?.slice(0, 2).map((w, i) => (
                <div key={`b${i}`} className="text-[var(--profit)] mono">BID {w.px} · {w.sz}</div>
              ))}
              {liveBook.walls_ask?.slice(0, 2).map((w, i) => (
                <div key={`a${i}`} className="text-[var(--loss)] mono">ASK {w.px} · {w.sz}</div>
              ))}
            </div>
          )}
        </div>

        {/* Right: signals */}
        <div className="panel p-3 flex flex-col min-h-[240px]">
          <div className="flex items-center gap-2 mb-3">
            <Zap size={14} className="text-cyan-400" />
            <span className="text-xs font-semibold text-[var(--txt)]">{t('scalp.signals')}</span>
          </div>
          <div className="flex-1 overflow-auto space-y-1.5">
            {(status?.recent_signals || []).length === 0 && (
              <div className="text-2xs text-[var(--txt-muted)]">{t('scalp.no_signals')}</div>
            )}
            {[...(status?.recent_signals || [])].reverse().map((s, i) => (
              <div key={i} className="p-2 rounded-md bg-[var(--bg)] text-2xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold text-[var(--txt)]">
                    <span className={s.side === 'long' ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
                      {s.side?.toUpperCase()}
                    </span>
                    {' '}{s.coin}
                  </span>
                  <span className="mono text-[var(--txt-muted)]">OBI {s.obi_5}</span>
                </div>
                <div className="text-[var(--txt-muted)] mt-0.5">
                  {s.llm?.confirm ? 'LLM ✓' : 'LLM ✗'} · {s.llm?.reason || '—'}
                </div>
              </div>
            ))}
          </div>
          {(status?.open_positions || []).length > 0 && (
            <div className="mt-3 pt-2 border-t border-[var(--border)] space-y-1">
              <div className="text-2xs text-[var(--txt-muted)] font-semibold">{t('dash.open_positions')}</div>
              {status.open_positions.map((p, i) => (
                <div key={i} className="text-2xs mono flex justify-between">
                  <span>{p.side} {p.coin}</span>
                  <span>@{Number(p.entry_price).toFixed(4)} · {p.held_sec}s</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
