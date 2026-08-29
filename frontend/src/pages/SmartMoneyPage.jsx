import { useState, useEffect, useCallback } from 'react'
import { useTranslation } from '../hooks/useTranslation'
import {
  TrendingUp, TrendingDown, Search, ShieldCheck, ShieldX,
  Copy, Eye, EyeOff, Play, Square, RefreshCw, Users, BarChart3,
  AlertTriangle, X, Settings, DollarSign, Target, Shield,
  Clock, Percent, ChevronDown, ChevronUp,
} from 'lucide-react'
import { api } from '../services/api'

function fmtPct(v, sign = false) {
  const n = Number(v) || 0
  const s = sign && n >= 0 ? '+' : ''
  return `${s}${n.toFixed(1)}%`
}

function fmtUsd(v, sign = false) {
  const n = Number(v) || 0
  const s = sign && n >= 0 ? '+' : ''
  return `${s}$${Math.abs(n).toLocaleString('en', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`
}

function VerifyBadge({ verified, score }) {
  if (verified) return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
      <ShieldCheck size={12} /> Verified
    </span>
  )
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-500/15 text-amber-400 border border-amber-500/30">
      <ShieldX size={12} /> {score > 0 ? `Score ${score}` : 'Unverified'}
    </span>
  )
}

/* ────────── Trader Card (Discover) ────────── */
function TraderCard({ trader, onTrack, onUntrack, onView, onCopy, isTracked }) {
  const roi = trader.roi_pct ?? 0
  const roiColor = roi >= 0 ? 'text-emerald-400' : 'text-red-400'
  const wr = trader.win_rate ?? 0
  const pnl = trader.pnl_usd ?? 0
  const positions = trader.current_positions || []

  return (
    <div className="bg-slate-800/60 rounded-xl p-4 border border-slate-700/40 hover:border-slate-600/60 transition-all">
      {/* Header: alias + ROI */}
      <div className="flex items-start justify-between mb-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-bold text-slate-200 truncate">
              {trader.alias || trader.unique_code?.slice(0, 14) + '...'}
            </span>
            <VerifyBadge verified={trader.verified} score={trader.verify_score} />
          </div>
          <div className="text-[10px] text-slate-600 font-mono mt-0.5 truncate">
            {trader.unique_code}
          </div>
        </div>
        <div className="text-right ml-3 shrink-0">
          <div className={`text-xl font-bold ${roiColor}`}>
            {roi >= 0 ? '+' : ''}{roi.toFixed(1)}%
          </div>
          <div className="text-[10px] text-slate-500">ROI (30d)</div>
        </div>
      </div>

      {/* Metrics row */}
      <div className="grid grid-cols-5 gap-1.5 mb-3">
        <div className="text-center bg-slate-800/40 rounded-lg py-1.5">
          <div className="text-[10px] text-slate-500">WR</div>
          <div className={`text-xs font-bold ${wr >= 0.55 ? 'text-emerald-400' : wr >= 0.45 ? 'text-amber-400' : 'text-red-400'}`}>
            {(wr * 100).toFixed(0)}%
          </div>
        </div>
        <div className="text-center bg-slate-800/40 rounded-lg py-1.5">
          <div className="text-[10px] text-slate-500">PnL</div>
          <div className={`text-xs font-bold ${pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            ${Math.abs(pnl).toFixed(0)}
          </div>
        </div>
        <div className="text-center bg-slate-800/40 rounded-lg py-1.5">
          <div className="text-[10px] text-slate-500">Trades</div>
          <div className="text-xs font-bold text-slate-300">{trader.total_trades || '—'}</div>
        </div>
        <div className="text-center bg-slate-800/40 rounded-lg py-1.5">
          <div className="text-[10px] text-slate-500">Follow</div>
          <div className="text-xs font-bold text-slate-300">{trader.copy_traders || 0}</div>
        </div>
        <div className="text-center bg-slate-800/40 rounded-lg py-1.5">
          <div className="text-[10px] text-slate-500">Days</div>
          <div className="text-xs font-bold text-slate-300">{trader.lead_days || 0}</div>
        </div>
      </div>

      {/* Max drawdown bar */}
      {trader.max_drawdown > 0 && (
        <div className="mb-3">
          <div className="flex items-center justify-between text-[10px] mb-0.5">
            <span className="text-slate-500">Max Drawdown</span>
            <span className="text-red-400">{(trader.max_drawdown * 100).toFixed(1)}%</span>
          </div>
          <div className="h-1 bg-slate-700 rounded-full overflow-hidden">
            <div className="h-full bg-red-500/60 rounded-full transition-all"
              style={{ width: `${Math.min(trader.max_drawdown * 100, 100)}%` }} />
          </div>
        </div>
      )}

      {/* Open positions preview */}
      {positions.length > 0 && (
        <div className="mb-3">
          <div className="text-[10px] text-slate-500 mb-1">Open Positions ({positions.length})</div>
          <div className="flex flex-wrap gap-1">
            {positions.slice(0, 5).map((p, i) => (
              <span key={i} className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium border ${
                (p.side === 'long' || p.side === 'buy')
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                  : 'bg-red-500/10 text-red-400 border-red-500/20'
              }`}>
                {(p.instId || p.coin || '').replace('-SWAP', '').replace('-USDT', '')}
                <span className="opacity-60">{p.side}</span>
              </span>
            ))}
            {positions.length > 5 && (
              <span className="text-[10px] text-slate-500 self-center">+{positions.length - 5}</span>
            )}
          </div>
        </div>
      )}

      {/* Verification failures */}
      {trader.verify_failures?.length > 0 && (
        <div className="mb-3 bg-red-500/5 border border-red-500/15 rounded-lg px-2.5 py-1.5">
          <div className="flex items-center gap-1 text-red-400 text-[10px] font-medium">
            <AlertTriangle size={10} /> {trader.verify_failures[0]}
          </div>
        </div>
      )}

      {/* Action buttons */}
      <div className="flex items-center gap-1.5">
        <button onClick={() => onView(trader)}
          className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded-lg bg-slate-700/40 hover:bg-slate-600/50 text-slate-300 text-xs font-medium transition-colors">
          <BarChart3 size={12} /> Details
        </button>
        {isTracked ? (
          <>
            <button onClick={() => onUntrack(trader.unique_code)}
              className="flex items-center justify-center gap-1 px-2 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs font-medium transition-colors">
              <EyeOff size={12} />
            </button>
            <button onClick={() => onCopy(trader)}
              className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded-lg bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 text-xs font-medium transition-colors">
              <Copy size={12} /> Copy
            </button>
          </>
        ) : (
          <button onClick={() => onTrack(trader.unique_code)}
            className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 text-xs font-medium transition-colors">
            <Eye size={12} /> Track
          </button>
        )}
      </div>
    </div>
  )
}

/* ────────── Trade History Row ────────── */
function TradeRow({ t }) {
  const pnl = Number(t.pnl) || 0
  const ratio = Number(t.pnlRatio) || 0
  const isProfit = pnl >= 0
  const side = (t.side || '').toLowerCase()
  const isLong = side === 'long' || side === 'buy'
  const coin = (t.instId || '').replace('-SWAP', '').replace('-USDT', '').replace('-USD', '')

  let openStr = '—', closeStr = '—'
  try {
    if (t.openTime) openStr = new Date(Number(t.openTime)).toLocaleDateString()
    if (t.closeTime) closeStr = new Date(Number(t.closeTime)).toLocaleDateString()
  } catch {}

  return (
    <div className={`flex items-center gap-2 px-3 py-2 rounded-lg text-xs border ${
      isProfit ? 'bg-emerald-500/5 border-emerald-500/10' : 'bg-red-500/5 border-red-500/10'
    }`}>
      <div className="w-16 font-bold text-slate-300 shrink-0">{coin}</div>
      <div className={`w-10 font-medium ${isLong ? 'text-emerald-400' : 'text-red-400'}`}>
        {isLong ? 'LONG' : 'SHORT'}
      </div>
      <div className="w-10 text-slate-400 shrink-0">@{t.avgPx || '—'}</div>
      <div className="w-10 text-slate-400 shrink-0">×{t.lever || '—'}</div>
      <div className={`flex-1 text-right font-bold ${isProfit ? 'text-emerald-400' : 'text-red-400'}`}>
        {isProfit ? '+' : ''}${pnl.toFixed(2)}
      </div>
      <div className={`w-16 text-right font-medium ${isProfit ? 'text-emerald-400' : 'text-red-400'}`}>
        {ratio >= 0 ? '+' : ''}{(ratio * 100).toFixed(2)}%
      </div>
      <div className="w-20 text-right text-slate-500 shrink-0">{closeStr}</div>
    </div>
  )
}

/* ────────── Trader Detail Modal ────────── */
function TraderDetail({ trader, onClose, onTrack, onUntrack, onCopy, isTracked }) {
  const [history, setHistory] = useState([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [showAllTrades, setShowAllTrades] = useState(false)

  useEffect(() => {
    if (!trader?.unique_code) return
    setHistoryLoading(true)
    api.smartMoneyTraderHistory(trader.unique_code, 50)
      .then(r => setHistory(r?.trades || []))
      .catch(() => setHistory([]))
      .finally(() => setHistoryLoading(false))
  }, [trader?.unique_code])

  if (!trader) return null

  const roi = trader.roi_pct ?? 0
  const roiColor = roi >= 0 ? 'text-emerald-400' : 'text-red-400'
  const positions = trader.current_positions || []
  const displayedTrades = showAllTrades ? history : history.slice(0, 10)

  // Compute win/loss from history
  const wins = history.filter(t => (Number(t.pnl) || 0) > 0).length
  const losses = history.filter(t => (Number(t.pnl) || 0) < 0).length
  const totalPnl = history.reduce((s, t) => s + (Number(t.pnl) || 0), 0)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="bg-slate-900 rounded-2xl border border-slate-700/50 w-full max-w-3xl max-h-[90vh] overflow-y-auto p-5" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-lg font-bold text-slate-200">{trader.alias || trader.unique_code}</h3>
              <VerifyBadge verified={trader.verified} score={trader.verify_score} />
            </div>
            <p className="text-[10px] text-slate-600 font-mono mt-0.5">{trader.unique_code}</p>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-slate-700 rounded-lg"><X size={16} /></button>
        </div>

        {/* Top metrics */}
        <div className="grid grid-cols-2 sm:grid-cols-6 gap-2 mb-4">
          {[
            { label: 'ROI', value: fmtPct(roi, true), color: roiColor, icon: roi >= 0 ? TrendingUp : TrendingDown },
            { label: 'Win Rate', value: `${((trader.win_rate || 0) * 100).toFixed(1)}%`, color: trader.win_rate >= 0.55 ? 'text-emerald-400' : 'text-amber-400' },
            { label: 'PnL', value: fmtUsd(trader.pnl_usd, true), color: trader.pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400' },
            { label: 'Trades', value: trader.total_trades || '—', color: 'text-slate-300' },
            { label: 'Followers', value: trader.copy_traders || 0, color: 'text-slate-300', icon: Users },
            { label: 'Max DD', value: trader.max_drawdown > 0 ? `-${(trader.max_drawdown * 100).toFixed(1)}%` : '—', color: 'text-red-400' },
          ].map((m, i) => (
            <div key={i} className="bg-slate-800/50 rounded-lg p-2.5 border border-slate-700/40 text-center">
              <div className="text-[10px] text-slate-500 mb-0.5">{m.label}</div>
              <div className={`text-sm font-bold ${m.color}`}>{m.value}</div>
            </div>
          ))}
        </div>

        {/* Preferred coins */}
        {trader.preferred_coins?.length > 0 && (
          <div className="mb-3">
            <div className="text-[10px] text-slate-500 mb-1">Preferred Coins</div>
            <div className="flex flex-wrap gap-1">
              {trader.preferred_coins.map((c, i) => (
                <span key={i} className="px-2 py-0.5 rounded bg-slate-700/50 text-xs text-slate-300 border border-slate-600/30">{c}</span>
              ))}
            </div>
          </div>
        )}

        {/* Verification issues */}
        {trader.verify_failures?.length > 0 && (
          <div className="bg-red-500/10 border border-red-500/25 rounded-lg p-3 mb-4">
            <div className="flex items-center gap-1.5 text-red-400 text-xs font-medium mb-1">
              <AlertTriangle size={12} /> Verification Issues
            </div>
            <ul className="text-[11px] text-red-300/70 space-y-0.5">
              {trader.verify_failures.map((f, i) => <li key={i}>• {f}</li>)}
            </ul>
          </div>
        )}

        {/* Open positions */}
        {positions.length > 0 && (
          <div className="mb-4">
            <h4 className="text-xs font-semibold text-slate-300 mb-2 flex items-center gap-1.5">
              <Target size={12} /> Open Positions ({positions.length})
            </h4>
            <div className="space-y-1">
              {positions.map((p, i) => (
                <div key={i} className="flex items-center justify-between text-xs bg-slate-800/50 rounded-lg px-3 py-2 border border-slate-700/30">
                  <span className="font-medium text-slate-300 w-24">{(p.instId || p.coin || '').replace('-SWAP', '').replace('-USDT', '')}</span>
                  <span className={`font-bold ${(p.side === 'long' || p.side === 'buy') ? 'text-emerald-400' : 'text-red-400'}`}>
                    {p.side} {p.sz || p.size}
                  </span>
                  <span className="text-slate-500">@ {p.avgPx || p.entryPx || '—'}</span>
                  {p.upl && (
                    <span className={`font-medium ${Number(p.upl) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      uPnL: {Number(p.upl) >= 0 ? '+' : ''}${Math.abs(Number(p.upl)).toFixed(2)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Trade history */}
        <div className="mb-4">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-semibold text-slate-300 flex items-center gap-1.5">
              <BarChart3 size={12} /> Closed Trades
              {history.length > 0 && (
                <span className="text-slate-500 font-normal">
                  ({history.length} total, {wins}W/{losses}L, {fmtUsd(totalPnl, true)})
                </span>
              )}
            </h4>
            {history.length > 10 && (
              <button onClick={() => setShowAllTrades(!showAllTrades)}
                className="text-[10px] text-blue-400 hover:text-blue-300">
                {showAllTrades ? 'Show less' : `Show all ${history.length}`}
              </button>
            )}
          </div>
          {historyLoading ? (
            <div className="text-center py-3 text-slate-500 text-xs">Loading trades...</div>
          ) : displayedTrades.length === 0 ? (
            <div className="text-center py-3 text-slate-500 text-xs">No closed trades available</div>
          ) : (
            <div className="space-y-1">
              {displayedTrades.map((t, i) => <TradeRow key={i} t={t} />)}
            </div>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-2 pt-2 border-t border-slate-700/50">
          {isTracked ? (
            <>
              <button onClick={() => onUntrack(trader.unique_code)}
                className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-sm font-medium transition-colors">
                <EyeOff size={14} /> Stop Tracking
              </button>
              <button onClick={() => onCopy(trader)}
                className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-blue-500/15 hover:bg-blue-500/25 text-blue-400 text-sm font-medium transition-colors">
                <Copy size={14} /> Copy Trader
              </button>
            </>
          ) : (
            <button onClick={() => onTrack(trader.unique_code)}
              className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-sm font-medium transition-colors">
              <Eye size={14} /> Start Tracking
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/* ────────── Copy Modal ────────── */
function CopyModal({ trader, onClose, onConfirm }) {
  const [amount, setAmount] = useState(500)
  const [tpRatio, setTpRatio] = useState(10)
  const [slRatio, setSlRatio] = useState(5)
  const [loading, setLoading] = useState(false)

  if (!trader) return null

  const handleCopy = async () => {
    setLoading(true)
    try {
      await onConfirm(trader.unique_code, amount)
      onClose()
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm" onClick={onClose}>
      <div className="bg-slate-900 rounded-2xl border border-slate-700/50 w-full max-w-md p-5" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-bold text-slate-200 flex items-center gap-2">
            <Copy size={16} className="text-blue-400" /> Copy Trader
          </h3>
          <button onClick={onClose} className="p-1 hover:bg-slate-700 rounded-lg"><X size={16} /></button>
        </div>

        <div className="bg-slate-800/50 rounded-lg p-3 mb-4 border border-slate-700/40">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-bold text-slate-200">{trader.alias || trader.unique_code?.slice(0, 14)}</div>
              <div className="text-[10px] text-slate-500 font-mono">{trader.unique_code}</div>
            </div>
            <div className="text-right">
              <div className={`text-lg font-bold ${(trader.roi_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {(trader.roi_pct ?? 0) >= 0 ? '+' : ''}{(trader.roi_pct ?? 0).toFixed(1)}%
              </div>
              <div className="text-[10px] text-slate-500">ROI</div>
            </div>
          </div>
        </div>

        <div className="space-y-3 mb-5">
          <div>
            <label className="text-xs text-slate-400 mb-1 block flex items-center gap-1">
              <DollarSign size={12} /> Copy Amount (USDT)
            </label>
            <input type="number" value={amount} onChange={e => setAmount(Number(e.target.value))}
              className="w-full bg-slate-800 border border-slate-700/50 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-blue-500/50"
              min={10} step={10} />
            <div className="text-[10px] text-slate-500 mt-1">Amount to allocate per copy of this trader</div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-slate-400 mb-1 block flex items-center gap-1">
                <TrendingUp size={12} /> Take Profit
              </label>
              <div className="relative">
                <input type="number" value={tpRatio} onChange={e => setTpRatio(Number(e.target.value))}
                  className="w-full bg-slate-800 border border-slate-700/50 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-emerald-500/50"
                  min={1} max={100} step={1} />
                <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-slate-500">%</span>
              </div>
            </div>
            <div>
              <label className="text-xs text-slate-400 mb-1 block flex items-center gap-1">
                <TrendingDown size={12} /> Stop Loss
              </label>
              <div className="relative">
                <input type="number" value={slRatio} onChange={e => setSlRatio(Number(e.target.value))}
                  className="w-full bg-slate-800 border border-slate-700/50 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-red-500/50"
                  min={1} max={100} step={1} />
                <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-slate-500">%</span>
              </div>
            </div>
          </div>
        </div>

        <div className="bg-amber-500/10 border border-amber-500/25 rounded-lg px-3 py-2 mb-4">
          <div className="text-[11px] text-amber-400/80">
            This will place a real copy-trade on OKX with <strong>{amount} USDT</strong> capital, TP {tpRatio}%, SL {slRatio}%.
            Make sure you've verified the trader's history before copying.
          </div>
        </div>

        <div className="flex gap-2">
          <button onClick={onClose}
            className="flex-1 px-4 py-2 rounded-lg bg-slate-700/50 hover:bg-slate-600/50 text-slate-300 text-sm font-medium transition-colors">
            Cancel
          </button>
          <button onClick={handleCopy} disabled={loading || amount < 10}
            className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-blue-500/20 hover:bg-blue-500/30 text-blue-400 text-sm font-bold transition-colors disabled:opacity-40">
            {loading ? 'Copying...' : <>Confirm Copy — ${amount}</>}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ────────── Settings Tab ────────── */
function SettingsPanel({ config, onSave }) {
  const [form, setForm] = useState({
    capital: config?.capital ?? 500,
    max_leverage: config?.max_leverage ?? 3,
    tp_ratio: (config?.tp_ratio ?? 0.10) * 100,
    sl_ratio: (config?.sl_ratio ?? 0.05) * 100,
    max_daily_loss_pct: (config?.max_daily_loss_pct ?? 0.05) * 100,
    max_open_copies: config?.max_open_copies ?? 5,
    min_roi_pct: config?.min_roi_pct ?? 5,
    min_win_rate: (config?.min_win_rate ?? 0.45) * 100,
    max_max_drawdown: (config?.max_max_drawdown ?? 0.30) * 100,
    poll_interval_sec: config?.poll_interval_sec ?? 60,
  })
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const handleSave = async () => {
    setSaving(true)
    try {
      await onSave({
        capital: form.capital,
        max_leverage: form.max_leverage,
        tp_ratio: form.tp_ratio / 100,
        sl_ratio: form.sl_ratio / 100,
        max_daily_loss_pct: form.max_daily_loss_pct / 100,
        max_open_copies: form.max_open_copies,
        min_roi_pct: form.min_roi_pct,
        min_win_rate: form.min_win_rate / 100,
        max_max_drawdown: form.max_max_drawdown / 100,
        poll_interval_sec: form.poll_interval_sec,
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  const Field = ({ label, icon: Ic, value, onChange, min, max, step, hint, color }) => (
    <div>
      <label className="text-xs text-slate-400 mb-1 block flex items-center gap-1">
        {Ic && <Ic size={11} />} {label}
      </label>
      <div className="relative">
        <input type="number" value={value} onChange={e => onChange(Number(e.target.value))}
          className={`w-full bg-slate-800 border rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none ${
            color === 'emerald' ? 'focus:border-emerald-500/50 border-slate-700/50'
              : color === 'red' ? 'focus:border-red-500/50 border-slate-700/50'
              : 'focus:border-blue-500/50 border-slate-700/50'
          }`}
          min={min} max={max} step={step || 1} />
        <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-slate-500">
          {label.includes('%') || label.includes('Loss') || label.includes('Drawdown') || label.includes('Rate') ? '%' : label.includes('USDT') || label.includes('Capital') ? 'USDT' : ''}
        </span>
      </div>
      {hint && <div className="text-[10px] text-slate-600 mt-0.5">{hint}</div>}
    </div>
  )

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Capital & Risk */}
        <div className="bg-slate-800/40 rounded-xl p-4 border border-slate-700/40">
          <h3 className="text-xs font-bold text-slate-300 mb-3 flex items-center gap-1.5">
            <DollarSign size={13} className="text-emerald-400" /> Capital & Risk
          </h3>
          <div className="space-y-3">
            <Field label="Capital per Copy (USDT)" icon={DollarSign} value={form.capital}
              onChange={v => setForm(f => ({ ...f, capital: v }))} min={10} step={50}
              hint="Budget allocated per trader copy" color="emerald" />
            <Field label="Max Leverage" value={form.max_leverage}
              onChange={v => setForm(f => ({ ...f, max_leverage: v }))} min={1} max={20}
              hint="Max leverage for copied positions" />
            <Field label="Max Open Copies" value={form.max_open_copies}
              onChange={v => setForm(f => ({ ...f, max_open_copies: v }))} min={1} max={20}
              hint="Max concurrent traders to copy" />
            <Field label="Max Daily Loss %" icon={Shield} value={form.max_daily_loss_pct}
              onChange={v => setForm(f => ({ ...f, max_daily_loss_pct: v }))} min={1} max={50}
              hint="Stop copying if daily loss exceeds this" color="red" />
          </div>
        </div>

        {/* Take Profit & Stop Loss */}
        <div className="bg-slate-800/40 rounded-xl p-4 border border-slate-700/40">
          <h3 className="text-xs font-bold text-slate-300 mb-3 flex items-center gap-1.5">
            <Target size={13} className="text-blue-400" /> TP / SL
          </h3>
          <div className="space-y-3">
            <Field label="Take Profit %" icon={TrendingUp} value={form.tp_ratio}
              onChange={v => setForm(f => ({ ...f, tp_ratio: v }))} min={1} max={100}
              hint="Auto-close at this profit %" color="emerald" />
            <Field label="Stop Loss %" icon={TrendingDown} value={form.sl_ratio}
              onChange={v => setForm(f => ({ ...f, sl_ratio: v }))} min={1} max={100}
              hint="Auto-close at this loss %" color="red" />
          </div>
        </div>

        {/* Verification Filters */}
        <div className="bg-slate-800/40 rounded-xl p-4 border border-slate-700/40">
          <h3 className="text-xs font-bold text-slate-300 mb-3 flex items-center gap-1.5">
            <ShieldCheck size={13} className="text-amber-400" /> Verification Filters
          </h3>
          <div className="space-y-3">
            <Field label="Min ROI %" value={form.min_roi_pct}
              onChange={v => setForm(f => ({ ...f, min_roi_pct: v }))} min={0} max={500}
              hint="Minimum 30d ROI to pass verification" />
            <Field label="Min Win Rate %" value={form.min_win_rate}
              onChange={v => setForm(f => ({ ...f, min_win_rate: v }))} min={0} max={100}
              hint="Minimum win rate to pass verification" />
            <Field label="Max Drawdown %" value={form.max_max_drawdown}
              onChange={v => setForm(f => ({ ...f, max_max_drawdown: v }))} min={1} max={100}
              hint="Reject if drawdown exceeds this" color="red" />
          </div>
        </div>

        {/* Monitoring */}
        <div className="bg-slate-800/40 rounded-xl p-4 border border-slate-700/40">
          <h3 className="text-xs font-bold text-slate-300 mb-3 flex items-center gap-1.5">
            <Clock size={13} className="text-slate-400" /> Monitoring
          </h3>
          <div className="space-y-3">
            <Field label="Poll Interval (sec)" value={form.poll_interval_sec}
              onChange={v => setForm(f => ({ ...f, poll_interval_sec: v }))} min={10} max={600}
              hint="How often to check tracked traders" />
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button onClick={handleSave} disabled={saving}
          className="flex items-center gap-1.5 px-5 py-2 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-sm font-bold transition-colors disabled:opacity-40">
          {saving ? 'Saving...' : saved ? '✓ Saved!' : 'Save Settings'}
        </button>
        {saved && <span className="text-xs text-emerald-400">Settings updated successfully</span>}
      </div>
    </div>
  )
}

/* ────────── Main Page ────────── */
export default function SmartMoneyPage({ connected, isGuest }) {
  const { t } = useTranslation()
  const [tab, setTab] = useState('discover')
  const [status, setStatus] = useState(null)
  const [discoverList, setDiscoverList] = useState([])
  const [trackedList, setTrackedList] = useState([])
  const [loading, setLoading] = useState(false)
  const [detailTrader, setDetailTrader] = useState(null)
  const [copyTrader, setCopyTrader] = useState(null)
  const [searchPage, setSearchPage] = useState(1)
  const [sortBy, setSortBy] = useState('roi')

  const fetchStatus = useCallback(async () => {
    try {
      const s = await api.smartMoneyStatus()
      setStatus(s)
      if (s?.tracked) setTrackedList(s.tracked)
    } catch {}
  }, [])

  const fetchDiscover = useCallback(async (page = 1) => {
    setLoading(true)
    try {
      const res = await api.smartMoneyDiscover(page, 20)
      setDiscoverList(res?.traders || [])
      setSearchPage(page)
    } catch {}
    setLoading(false)
  }, [])

  useEffect(() => {
    fetchStatus()
    fetchDiscover()
    const iv = setInterval(fetchStatus, 10000)
    return () => clearInterval(iv)
  }, [fetchStatus, fetchDiscover])

  const handleTrack = async (code) => {
    await api.smartMoneyTrack(code)
    await fetchStatus()
  }

  const handleUntrack = async (code) => {
    await api.smartMoneyUntrack(code)
    await fetchStatus()
    if (detailTrader?.unique_code === code) {
      const d = await api.smartMoneyTrader(code)
      setDetailTrader(d)
    }
  }

  const handleCopy = async (code, amt) => {
    const res = await api.smartMoneyCopy(code, amt)
    alert(res?.msg || 'Done')
  }

  const handleStart = async () => {
    await api.smartMoneyStart({ execute: false })
    await fetchStatus()
  }

  const handleStop = async () => {
    await api.smartMoneyStop()
    await fetchStatus()
  }

  const handleViewDetail = async (trader) => {
    try {
      const d = await api.smartMoneyTrader(trader.unique_code)
      setDetailTrader(d)
    } catch {
      setDetailTrader(trader)
    }
  }

  const handleCopyFromDetail = (trader) => {
    setDetailTrader(null)
    setCopyTrader(trader)
  }

  const handleSaveConfig = async (config) => {
    await api.smartMoneyUpdateConfig(config)
    await fetchStatus()
  }

  const trackedCodes = new Set(trackedList.map(t => t.unique_code))
  const running = status?.running

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-bold text-slate-200">{t('nav.smartMoney')}</h1>
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium border ${
            running
              ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
              : 'bg-slate-700/50 text-slate-400 border-slate-600/50'
          }`}>
            {running ? 'Running' : 'Stopped'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => { fetchStatus(); fetchDiscover(searchPage) }}
            className="p-2 rounded-lg bg-slate-800/50 hover:bg-slate-700/50 border border-slate-700/50 text-slate-400">
            <RefreshCw size={14} />
          </button>
          {isGuest ? null : running ? (
            <button onClick={handleStop}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/15 hover:bg-red-500/25 text-red-400 text-sm font-medium border border-red-500/30">
              <Square size={13} /> Stop
            </button>
          ) : (
            <button onClick={handleStart}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-sm font-medium border border-emerald-500/30">
              <Play size={13} /> Start Tracker
            </button>
          )}
        </div>
      </div>

      {/* Stats Bar */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        {[
          { label: 'Tracked', value: status?.tracked_count || 0, icon: Eye, color: 'text-slate-300' },
          { label: 'Verified', value: status?.verified_count || 0, icon: ShieldCheck, color: 'text-emerald-400' },
          { label: 'Copying', value: status?.copying_count || 0, icon: Copy, color: 'text-blue-400' },
          { label: 'Lifetime PnL', value: fmtUsd(status?.lifetime_pnl || 0, true), color: (status?.lifetime_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400' },
          { label: 'Daily Loss', value: fmtUsd(status?.daily_loss || 0), color: 'text-amber-400' },
        ].map((m, i) => (
          <div key={i} className="bg-slate-800/50 rounded-lg p-2.5 border border-slate-700/40">
            <div className="flex items-center gap-1.5 mb-0.5">
              {m.icon && <m.icon size={11} className="text-slate-500" />}
              <span className="text-[10px] text-slate-500 uppercase tracking-wide">{m.label}</span>
            </div>
            <div className={`text-sm font-bold ${m.color}`}>{m.value}</div>
          </div>
        ))}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 p-1 bg-slate-800/50 rounded-lg border border-slate-700/50">
        {[
          { key: 'discover', label: 'Discover', icon: Search },
          { key: 'tracked', label: `Tracked (${trackedList.length})`, icon: Eye },
          { key: 'settings', label: 'Settings', icon: Settings },
        ].map(({ key, label, icon: Ic }) => (
          <button key={key} onClick={() => setTab(key)}
            className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
              tab === key ? 'bg-slate-700/60 text-slate-200' : 'text-slate-400 hover:text-slate-300'
            }`}>
            <Ic size={14} /> {label}
          </button>
        ))}
      </div>

      {/* Discover tab */}
      {tab === 'discover' && (
        <div>
          {/* Sort controls */}
          <div className="flex items-center gap-2 mb-3">
            <span className="text-xs text-slate-500">Sort by:</span>
            {['roi', 'pnl', 'copyRatio'].map(s => (
              <button key={s} onClick={() => setSortBy(s)}
                className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                  sortBy === s ? 'bg-blue-500/15 text-blue-400 border border-blue-500/30' : 'bg-slate-800/40 text-slate-400 border border-slate-700/30 hover:text-slate-300'
                }`}>
                {s === 'roi' ? 'ROI' : s === 'pnl' ? 'PnL' : 'Followers'}
              </button>
            ))}
          </div>

          {loading ? (
            <div className="text-center py-8 text-slate-500 text-sm">Loading traders from OKX...</div>
          ) : discoverList.length === 0 ? (
            <div className="text-center py-8 text-slate-500 text-sm">
              No traders found. Start the tracker first.
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {discoverList.map(t => (
                  <TraderCard key={t.unique_code} trader={t}
                    onTrack={handleTrack} onUntrack={handleUntrack}
                    onView={handleViewDetail} onCopy={setCopyTrader}
                    isTracked={trackedCodes.has(t.unique_code)} />
                ))}
              </div>
              <div className="flex items-center justify-center gap-3 mt-4">
                <button onClick={() => fetchDiscover(Math.max(1, searchPage - 1))}
                  disabled={searchPage <= 1}
                  className="px-3 py-1.5 rounded-lg bg-slate-800/50 border border-slate-700/50 text-slate-400 text-sm disabled:opacity-40">
                  Prev
                </button>
                <span className="text-sm text-slate-500">Page {searchPage}</span>
                <button onClick={() => fetchDiscover(searchPage + 1)}
                  className="px-3 py-1.5 rounded-lg bg-slate-800/50 border border-slate-700/50 text-slate-400 text-sm">
                  Next
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {/* Tracked tab */}
      {tab === 'tracked' && (
        <div>
          {trackedList.length === 0 ? (
            <div className="text-center py-8 text-slate-500 text-sm">
              No traders being tracked. Go to Discover to add some.
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {trackedList.map(t => (
                <TraderCard key={t.unique_code} trader={t}
                  onTrack={handleTrack} onUntrack={handleUntrack}
                  onView={handleViewDetail} onCopy={setCopyTrader}
                  isTracked={true} />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Settings tab */}
      {tab === 'settings' && (
        <SettingsPanel config={status?.config} onSave={handleSaveConfig} />
      )}

      {/* Detail Modal */}
      {detailTrader && (
        <TraderDetail trader={detailTrader}
          onClose={() => setDetailTrader(null)}
          onTrack={handleTrack} onUntrack={handleUntrack}
          onCopy={handleCopyFromDetail}
          isTracked={trackedCodes.has(detailTrader.unique_code)} />
      )}

      {/* Copy Modal */}
      {copyTrader && (
        <CopyModal trader={copyTrader}
          onClose={() => setCopyTrader(null)}
          onConfirm={handleCopy} />
      )}
    </div>
  )
}
