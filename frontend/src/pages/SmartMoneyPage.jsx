import { useState, useEffect, useCallback } from 'react'
import { useTranslation } from '../hooks/useTranslation'
import {
  TrendingUp, TrendingDown, Search, Shield, ShieldCheck, ShieldX,
  Copy, Eye, EyeOff, Play, Square, RefreshCw, Users, BarChart3,
  AlertTriangle, Check, X, ChevronDown, ChevronUp, ExternalLink,
} from 'lucide-react'
import { api } from '../services/api'

const SCORE_COLORS = {
  high: 'text-emerald-400',
  mid: 'text-amber-400',
  low: 'text-red-400',
}

function scoreColor(score) {
  if (score >= 70) return SCORE_COLORS.high
  if (score >= 40) return SCORE_COLORS.mid
  return SCORE_COLORS.low
}

function VerifyBadge({ verified, score }) {
  if (verified) return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
      <ShieldCheck size={12} /> Verified
    </span>
  )
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-500/15 text-amber-400 border border-amber-500/30">
      <ShieldX size={12} /> {score > 0 ? `Score: ${score}` : 'Not verified'}
    </span>
  )
}

function StatCard({ label, value, sub, icon: Icon, color = 'text-slate-300' }) {
  return (
    <div className="bg-slate-800/50 rounded-lg p-3 border border-slate-700/50">
      <div className="flex items-center gap-2 mb-1">
        {Icon && <Icon size={14} className="text-slate-500" />}
        <span className="text-xs text-slate-500 uppercase tracking-wide">{label}</span>
      </div>
      <div className={`text-lg font-bold ${color}`}>{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-0.5">{sub}</div>}
    </div>
  )
}

function TraderCard({ trader, onTrack, onUntrack, onView, isTracked }) {
  const roi = trader.roi_pct ?? 0
  const roiColor = roi >= 0 ? 'text-emerald-400' : 'text-red-400'
  const wr = trader.win_rate ?? 0

  return (
    <div className="bg-slate-800/60 rounded-xl p-4 border border-slate-700/40 hover:border-slate-600/60 transition-all">
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-bold text-slate-200">
              {trader.alias || trader.unique_code?.slice(0, 12) + '...'}
            </span>
            <VerifyBadge verified={trader.verified} score={trader.verify_score} />
          </div>
          <div className="text-xs text-slate-500 mt-1 font-mono">
            {trader.unique_code?.slice(0, 16)}...
          </div>
        </div>
        <div className="text-right">
          <div className={`text-xl font-bold ${roiColor}`}>
            {roi >= 0 ? '+' : ''}{roi.toFixed(1)}%
          </div>
          <div className="text-xs text-slate-500">ROI</div>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-2 mb-3">
        <div className="text-center">
          <div className="text-xs text-slate-500">Win Rate</div>
          <div className={`text-sm font-semibold ${wr >= 0.55 ? 'text-emerald-400' : wr >= 0.45 ? 'text-amber-400' : 'text-red-400'}`}>
            {(wr * 100).toFixed(1)}%
          </div>
        </div>
        <div className="text-center">
          <div className="text-xs text-slate-500">PnL</div>
          <div className={`text-sm font-semibold ${trader.pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            ${Math.abs(trader.pnl_usd || 0).toFixed(0)}
          </div>
        </div>
        <div className="text-center">
          <div className="text-xs text-slate-500">Followers</div>
          <div className="text-sm font-semibold text-slate-300">{trader.copy_traders || 0}</div>
        </div>
        <div className="text-center">
          <div className="text-xs text-slate-500">Days</div>
          <div className="text-sm font-semibold text-slate-300">{trader.lead_days || 0}</div>
        </div>
      </div>

      {trader.max_drawdown > 0 && (
        <div className="mb-3">
          <div className="flex items-center justify-between text-xs mb-1">
            <span className="text-slate-500">Max Drawdown</span>
            <span className="text-red-400">{(trader.max_drawdown * 100).toFixed(1)}%</span>
          </div>
          <div className="h-1.5 bg-slate-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-red-500/60 rounded-full"
              style={{ width: `${Math.min(trader.max_drawdown * 100, 100)}%` }}
            />
          </div>
        </div>
      )}

      <div className="flex items-center gap-2">
        <button
          onClick={() => onView(trader)}
          className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-700/50 hover:bg-slate-600/50 text-slate-300 text-xs font-medium transition-colors"
        >
          <BarChart3 size={13} /> Details
        </button>
        {isTracked ? (
          <button
            onClick={() => onUntrack(trader.unique_code)}
            className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs font-medium transition-colors"
          >
            <EyeOff size={13} /> Untrack
          </button>
        ) : (
          <button
            onClick={() => onTrack(trader.unique_code)}
            className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 text-xs font-medium transition-colors"
          >
            <Eye size={13} /> Track
          </button>
        )}
      </div>
    </div>
  )
}

function TraderDetail({ trader, onClose, onTrack, onUntrack, onCopy, onStopCopy, isTracked }) {
  if (!trader) return null

  const roi = trader.roi_pct ?? 0
  const roiColor = roi >= 0 ? 'text-emerald-400' : 'text-red-400'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="bg-slate-900 rounded-2xl border border-slate-700/50 w-full max-w-2xl max-h-[85vh] overflow-y-auto p-6" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between mb-4">
          <div>
            <h3 className="text-lg font-bold text-slate-200">
              {trader.alias || trader.unique_code}
            </h3>
            <p className="text-xs text-slate-500 font-mono">{trader.unique_code}</p>
          </div>
          <button onClick={onClose} className="p-1 hover:bg-slate-700 rounded-lg"><X size={18} /></button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
          <StatCard label="ROI" value={`${roi >= 0 ? '+' : ''}${roi.toFixed(1)}%`} color={roiColor} icon={roi >= 0 ? TrendingUp : TrendingDown} />
          <StatCard label="Win Rate" value={`${((trader.win_rate || 0) * 100).toFixed(1)}%`} color={trader.win_rate >= 0.55 ? 'text-emerald-400' : 'text-amber-400'} />
          <StatCard label="PnL" value={`$${Math.abs(trader.pnl_usd || 0).toFixed(0)}`} color={trader.pnl_usd >= 0 ? 'text-emerald-400' : 'text-red-400'} />
          <StatCard label="Followers" value={trader.copy_traders || 0} icon={Users} />
        </div>

        {trader.verify_failures?.length > 0 && (
          <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 mb-4">
            <div className="flex items-center gap-2 text-red-400 text-sm font-medium mb-1">
              <AlertTriangle size={14} /> Verification Issues
            </div>
            <ul className="text-xs text-red-300/80 space-y-0.5">
              {trader.verify_failures.map((f, i) => <li key={i}>• {f}</li>)}
            </ul>
          </div>
        )}

        {trader.current_positions?.length > 0 && (
          <div className="mb-4">
            <h4 className="text-sm font-semibold text-slate-300 mb-2">Open Positions</h4>
            <div className="space-y-1">
              {trader.current_positions.map((p, i) => (
                <div key={i} className="flex items-center justify-between text-xs bg-slate-800/50 rounded-lg px-3 py-2">
                  <span className="text-slate-300">{p.instId || p.coin}</span>
                  <span className={p.side === 'long' ? 'text-emerald-400' : 'text-red-400'}>
                    {p.side} {p.sz || p.size}
                  </span>
                  <span className="text-slate-500">@ {p.avgPx || p.entryPx || '—'}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 mt-4">
          {isTracked ? (
            <>
              <button
                onClick={() => onUntrack(trader.unique_code)}
                className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-sm font-medium transition-colors"
              >
                <EyeOff size={14} /> Stop Tracking
              </button>
              <button
                onClick={() => onCopy(trader.unique_code)}
                className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-sm font-medium transition-colors"
              >
                <Copy size={14} /> Start Copying
              </button>
            </>
          ) : (
            <button
              onClick={() => onTrack(trader.unique_code)}
              className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-sm font-medium transition-colors"
            >
              <Eye size={14} /> Start Tracking
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default function SmartMoneyPage({ connected, isGuest }) {
  const { t } = useTranslation()
  const [tab, setTab] = useState('discover')
  const [status, setStatus] = useState(null)
  const [discoverList, setDiscoverList] = useState([])
  const [trackedList, setTrackedList] = useState([])
  const [loading, setLoading] = useState(false)
  const [detailTrader, setDetailTrader] = useState(null)
  const [searchPage, setSearchPage] = useState(1)

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
    if (detailTrader?.unique_code === code) {
      const d = await api.smartMoneyTrader(code)
      setDetailTrader(d)
    }
  }

  const handleUntrack = async (code) => {
    await api.smartMoneyUntrack(code)
    await fetchStatus()
    if (detailTrader?.unique_code === code) {
      const d = await api.smartMoneyTrader(code)
      setDetailTrader(d)
    }
  }

  const handleCopy = async (code) => {
    const res = await api.smartMoneyCopy(code)
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

  const trackedCodes = new Set(trackedList.map(t => t.unique_code))
  const running = status?.running

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
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
          <button onClick={() => { fetchStatus(); fetchDiscover(searchPage) }} className="p-2 rounded-lg bg-slate-800/50 hover:bg-slate-700/50 border border-slate-700/50 text-slate-400">
            <RefreshCw size={14} />
          </button>
          {isGuest ? null : running ? (
            <button onClick={handleStop} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/15 hover:bg-red-500/25 text-red-400 text-sm font-medium border border-red-500/30">
              <Square size={13} /> Stop
            </button>
          ) : (
            <button onClick={handleStart} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-sm font-medium border border-emerald-500/30">
              <Play size={13} /> Start Tracker
            </button>
          )}
        </div>
      </div>

      {/* Stats Bar */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        <StatCard label="Tracked" value={status?.tracked_count || 0} icon={Eye} />
        <StatCard label="Verified" value={status?.verified_count || 0} icon={ShieldCheck} color="text-emerald-400" />
        <StatCard label="Copying" value={status?.copying_count || 0} icon={Copy} color="text-blue-400" />
        <StatCard label="Lifetime PnL" value={`$${(status?.lifetime_pnl || 0).toFixed(0)}`} color={status?.lifetime_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'} />
        <StatCard label="Daily Loss" value={`$${(status?.daily_loss || 0).toFixed(0)}`} color="text-amber-400" />
      </div>

      {/* Tabs */}
      <div className="flex gap-1 p-1 bg-slate-800/50 rounded-lg border border-slate-700/50">
        {[
          { key: 'discover', label: 'Discover', icon: Search },
          { key: 'tracked', label: `Tracked (${trackedList.length})`, icon: Eye },
        ].map(({ key, label, icon: Ic }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
              tab === key ? 'bg-slate-700/60 text-slate-200' : 'text-slate-400 hover:text-slate-300'
            }`}
          >
            <Ic size={14} /> {label}
          </button>
        ))}
      </div>

      {/* Content */}
      {tab === 'discover' && (
        <div>
          {loading ? (
            <div className="text-center py-8 text-slate-500">Loading traders...</div>
          ) : discoverList.length === 0 ? (
            <div className="text-center py-8 text-slate-500">
              No traders found. Start the tracker first.
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {discoverList.map((t) => (
                  <TraderCard
                    key={t.unique_code}
                    trader={t}
                    onTrack={handleTrack}
                    onUntrack={handleUntrack}
                    onView={handleViewDetail}
                    isTracked={trackedCodes.has(t.unique_code)}
                  />
                ))}
              </div>
              <div className="flex items-center justify-center gap-3 mt-4">
                <button
                  onClick={() => fetchDiscover(Math.max(1, searchPage - 1))}
                  disabled={searchPage <= 1}
                  className="px-3 py-1.5 rounded-lg bg-slate-800/50 border border-slate-700/50 text-slate-400 text-sm disabled:opacity-40"
                >
                  Prev
                </button>
                <span className="text-sm text-slate-500">Page {searchPage}</span>
                <button
                  onClick={() => fetchDiscover(searchPage + 1)}
                  className="px-3 py-1.5 rounded-lg bg-slate-800/50 border border-slate-700/50 text-slate-400 text-sm"
                >
                  Next
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {tab === 'tracked' && (
        <div>
          {trackedList.length === 0 ? (
            <div className="text-center py-8 text-slate-500">
              No traders being tracked. Go to Discover to add some.
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {trackedList.map((t) => (
                <TraderCard
                  key={t.unique_code}
                  trader={t}
                  onTrack={handleTrack}
                  onUntrack={handleUntrack}
                  onView={handleViewDetail}
                  isTracked={true}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Detail Modal */}
      {detailTrader && (
        <TraderDetail
          trader={detailTrader}
          onClose={() => setDetailTrader(null)}
          onTrack={handleTrack}
          onUntrack={handleUntrack}
          onCopy={handleCopy}
          onStopCopy={(code) => api.smartMoneyStopCopy(code)}
          isTracked={trackedCodes.has(detailTrader.unique_code)}
        />
      )}
    </div>
  )
}
