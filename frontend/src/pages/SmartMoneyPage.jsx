import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  TrendingUp, TrendingDown, Search, ShieldCheck, ShieldX,
  Copy, Eye, Play, Square, RefreshCw, Users, BarChart3,
  AlertTriangle, X, Settings, DollarSign, Target, Trophy,
  Filter, ChevronRight, Star,
} from 'lucide-react'
import { api } from '../services/api'

function fmtPct(v, sign = true) {
  const n = Number(v) || 0
  const s = sign && n > 0 ? '+' : ''
  return `${s}${n.toFixed(1)}%`
}

function fmtUsd(v, sign = true) {
  const n = Number(v) || 0
  const s = sign && n > 0 ? '+' : n < 0 ? '-' : ''
  return `${s}$${Math.abs(n).toLocaleString('en', { maximumFractionDigits: 0 })}`
}

function SourceBadge({ source }) {
  const s = (source || 'okx').toLowerCase()
  const map = {
    okx: { label: 'OKX', cls: 'bg-blue-500/15 text-blue-300 border-blue-500/30' },
    hyperliquid: { label: 'Hyperliquid', cls: 'bg-purple-500/15 text-purple-300 border-purple-500/30' },
    social: { label: 'Соцсети', cls: 'bg-slate-500/15 text-slate-300 border-slate-500/30' },
  }
  const m = map[s] || { label: s, cls: 'bg-slate-500/15 text-slate-300 border-slate-500/30' }
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium border ${m.cls}`}>
      {m.label}
    </span>
  )
}

function VerifyBadge({ verified, score }) {
  if (verified) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
        <ShieldCheck size={11} /> Проверен
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-500/10 text-amber-400/90 border border-amber-500/25">
      <ShieldX size={11} /> {score > 0 ? `Скор ${Math.round(score)}` : 'Без проверки'}
    </span>
  )
}

function Stat({ label, value, color }) {
  return (
    <div className="min-w-0">
      <div className={`text-sm font-semibold mono truncate ${color || 'text-[var(--txt)]'}`}>{value}</div>
      <div className="text-[10px] text-[var(--txt-muted)]">{label}</div>
    </div>
  )
}

function TraderCard({ trader, rank, onView, onCopy, onTrack, onUntrack, isTracked, isGuest }) {
  const roi = Number(trader.roi_pct) || 0
  const wr = Number(trader.win_rate) || 0
  const wrPct = wr <= 1 ? wr * 100 : wr
  const dd = Number(trader.max_drawdown) || 0
  const ddPct = dd <= 1 ? dd * 100 : dd
  const followers = Number(trader.copy_traders) || 0
  const pnl = Number(trader.pnl_usd) || 0
  const name = trader.alias || (trader.unique_code ? `${trader.unique_code.slice(0, 10)}…` : 'Трейдер')

  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4 hover:border-[var(--border-hover)] transition-colors">
      <div className="flex items-start gap-3 mb-3">
        <div className={`w-9 h-9 rounded-lg flex items-center justify-center text-sm font-bold shrink-0 ${
          rank <= 3 ? 'bg-amber-500/20 text-amber-300' : 'bg-[var(--bg-elevated)] text-[var(--txt-muted)]'
        }`}>
          {rank || '·'}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold text-[var(--txt)] truncate">{name}</span>
            <SourceBadge source={trader.source} />
            <VerifyBadge verified={trader.verified} score={trader.verify_score} />
            {isTracked && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400 border border-blue-500/30">
                в списке
              </span>
            )}
          </div>
          <div className="text-[10px] text-[var(--txt-muted)] mono truncate mt-0.5">
            {trader.unique_code}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-xl font-bold mono ${roi >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
            {fmtPct(roi)}
          </div>
          <div className="text-[10px] text-[var(--txt-muted)]">ROI</div>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-2 mb-3 py-2 border-y border-[var(--border)]">
        <Stat label="PnL" value={fmtUsd(pnl)} color={pnl >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'} />
        <Stat label="Win rate" value={`${wrPct.toFixed(0)}%`} />
        <Stat label="Max DD" value={`${ddPct.toFixed(0)}%`} color={ddPct > 25 ? 'text-[var(--loss)]' : undefined} />
        <Stat label="Подписчики" value={followers.toLocaleString()} />
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => onView(trader)}
          className="btn btn-secondary btn-sm flex-1 min-w-[100px] inline-flex items-center justify-center gap-1"
        >
          <Eye size={14} /> Подробнее
        </button>
        {!isGuest && (
          <>
            {trader.copyable !== false && (trader.source || 'okx') === 'okx' ? (
              <button
                type="button"
                onClick={() => onCopy(trader)}
                className="btn btn-primary btn-sm flex-1 min-w-[100px] inline-flex items-center justify-center gap-1"
              >
                <Copy size={14} /> Копировать
              </button>
            ) : (
              <a
                href={trader.profile_url || '#'}
                target="_blank"
                rel="noreferrer"
                className="btn btn-secondary btn-sm flex-1 min-w-[100px] inline-flex items-center justify-center gap-1"
              >
                Профиль
              </a>
            )}
            <button
              type="button"
              onClick={() => (isTracked ? onUntrack(trader) : onTrack(trader))}
              className="btn btn-secondary btn-sm inline-flex items-center justify-center gap-1"
              title={isTracked ? 'Убрать из отслеживания' : 'Отслеживать'}
            >
              <Star size={14} className={isTracked ? 'text-amber-400 fill-amber-400' : ''} />
            </button>
          </>
        )}
      </div>
    </div>
  )
}

function CopyModal({ trader, onClose, onConfirm, busy }) {
  const [amount, setAmount] = useState(100)
  const [tp, setTp] = useState(10)
  const [sl, setSl] = useState(5)
  const name = trader?.alias || trader?.unique_code?.slice(0, 12) || 'трейдер'

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-3 bg-black/60" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-5 shadow-xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="text-lg font-bold text-[var(--txt)] flex items-center gap-2">
              <Copy size={18} className="text-blue-400" /> Автокопирование
            </div>
            <div className="text-sm text-[var(--txt-muted)] mt-1">{name}</div>
          </div>
          <button type="button" onClick={onClose} className="p-1 rounded-lg hover:bg-[var(--bg-elevated)]">
            <X size={18} className="text-[var(--txt-muted)]" />
          </button>
        </div>

        <div className="rounded-xl bg-[var(--bg-elevated)] border border-[var(--border)] p-3 mb-4 flex justify-between">
          <div>
            <div className="text-[10px] text-[var(--txt-muted)]">ROI лидера</div>
            <div className={`text-lg font-bold mono ${(trader?.roi_pct || 0) >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
              {fmtPct(trader?.roi_pct)}
            </div>
          </div>
          <div className="text-right">
            <div className="text-[10px] text-[var(--txt-muted)]">Скор</div>
            <div className="text-lg font-bold mono text-[var(--txt)]">{Math.round(trader?.verify_score || 0)}</div>
          </div>
        </div>

        <label className="block text-xs text-[var(--txt-muted)] mb-1">Сумма на копирование (USDT)</label>
        <input
          type="number"
          min={10}
          step={10}
          value={amount}
          onChange={e => setAmount(Number(e.target.value))}
          className="w-full mb-3 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]"
        />
        <div className="grid grid-cols-2 gap-3 mb-4">
          <div>
            <label className="block text-xs text-[var(--txt-muted)] mb-1">Take-profit %</label>
            <input type="number" min={1} max={100} value={tp} onChange={e => setTp(Number(e.target.value))}
              className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]" />
          </div>
          <div>
            <label className="block text-xs text-[var(--txt-muted)] mb-1">Stop-loss %</label>
            <input type="number" min={1} max={100} value={sl} onChange={e => setSl(Number(e.target.value))}
              className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]" />
          </div>
        </div>

        <p className="text-[11px] text-[var(--txt-muted)] mb-4 leading-relaxed">
          Сделки лидера будут копироваться на OKX с выбранной суммой. Это не гарантия прибыли —
          проверяйте ROI, просадку и историю перед запуском.
        </p>

        <div className="flex gap-2">
          <button type="button" className="btn btn-secondary flex-1" onClick={onClose}>Отмена</button>
          <button
            type="button"
            className="btn btn-primary flex-1"
            disabled={busy || amount < 10}
            onClick={() => onConfirm({ amount, tp_ratio: tp / 100, sl_ratio: sl / 100 })}
          >
            {busy ? '…' : 'Запустить копирование'}
          </button>
        </div>
      </div>
    </div>
  )
}

function DetailModal({ trader, onClose, onCopy, history }) {
  const name = trader?.alias || trader?.unique_code?.slice(0, 14) || 'Трейдер'
  const roi = Number(trader?.roi_pct) || 0
  const wr = Number(trader?.win_rate) || 0
  const wrPct = wr <= 1 ? wr * 100 : wr

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-3 bg-black/60" onClick={onClose}>
      <div
        className="w-full max-w-lg max-h-[85vh] overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-5 shadow-xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex justify-between items-start mb-4">
          <div>
            <h3 className="text-lg font-bold text-[var(--txt)]">{name}</h3>
            <VerifyBadge verified={trader?.verified} score={trader?.verify_score} />
          </div>
          <button type="button" onClick={onClose} className="p-1"><X size={18} className="text-[var(--txt-muted)]" /></button>
        </div>

        <div className="grid grid-cols-3 gap-3 mb-4">
          <div className="rounded-xl bg-[var(--bg-elevated)] p-3 text-center">
            <div className={`text-xl font-bold mono ${roi >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>{fmtPct(roi)}</div>
            <div className="text-[10px] text-[var(--txt-muted)]">ROI</div>
          </div>
          <div className="rounded-xl bg-[var(--bg-elevated)] p-3 text-center">
            <div className="text-xl font-bold mono text-[var(--txt)]">{wrPct.toFixed(0)}%</div>
            <div className="text-[10px] text-[var(--txt-muted)]">Win rate</div>
          </div>
          <div className="rounded-xl bg-[var(--bg-elevated)] p-3 text-center">
            <div className="text-xl font-bold mono text-[var(--txt)]">{trader?.copy_traders || 0}</div>
            <div className="text-[10px] text-[var(--txt-muted)]">Подписчики</div>
          </div>
        </div>

        {trader?.verify_failures?.length > 0 && (
          <div className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
            <div className="font-semibold mb-1">Замечания проверки</div>
            <ul className="list-disc pl-4 space-y-0.5">
              {trader.verify_failures.map((f, i) => <li key={i}>{f}</li>)}
            </ul>
          </div>
        )}

        <div className="mb-4">
          <div className="text-xs font-semibold text-[var(--txt-muted)] mb-2">Последние сделки</div>
          {!history?.length ? (
            <div className="text-sm text-[var(--txt-muted)]">Нет данных по истории</div>
          ) : (
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {history.slice(0, 20).map((h, i) => (
                <div key={i} className="flex justify-between text-xs py-1.5 border-b border-[var(--border)]">
                  <span className="text-[var(--txt)]">{h.instId || h.inst_id || h.symbol || '—'}</span>
                  <span className={Number(h.pnl || h.upl || 0) >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}>
                    {fmtUsd(h.pnl ?? h.upl ?? 0)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        <button type="button" className="btn btn-primary w-full" onClick={() => onCopy(trader)}>
          Копировать этого трейдера
        </button>
      </div>
    </div>
  )
}

export default function SmartMoneyPage({ connected, isGuest }) {
  const [tab, setTab] = useState('discover')
  const [status, setStatus] = useState(null)
  const [discoverList, setDiscoverList] = useState([])
  const [trackedList, setTrackedList] = useState([])
  const [copies, setCopies] = useState([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [sort, setSort] = useState('roi')
  const [minRoi, setMinRoi] = useState(10)
  const [verifiedOnly, setVerifiedOnly] = useState(false)
  const [srcOkx, setSrcOkx] = useState(true)
  const [srcHl, setSrcHl] = useState(true)
  const [srcSocial, setSrcSocial] = useState(true)
  const [q, setQ] = useState('')
  const [copyTrader, setCopyTrader] = useState(null)
  const [detailTrader, setDetailTrader] = useState(null)
  const [history, setHistory] = useState([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const trackedCodes = useMemo(
    () => new Set((trackedList || []).map(t => t.unique_code)),
    [trackedList],
  )

  const loadStatus = useCallback(async () => {
    try {
      const s = await api.smartMoneyStatus()
      setStatus(s)
    } catch {
      setStatus(null)
    }
  }, [])

  const fetchDiscover = useCallback(async (p = 1) => {
    setLoading(true)
    setErr('')
    try {
      const sources = [
        srcOkx && 'okx',
        srcHl && 'hyperliquid',
        srcSocial && 'social',
      ].filter(Boolean).join(',') || 'okx'
      const r = await api.smartMoneyDiscover(p, 20, {
        sort, min_roi: minRoi, verified_only: verifiedOnly, sources,
      })
      if (r?.error) {
        setErr(r.message || 'Ошибка загрузки')
        setDiscoverList([])
      } else {
        setDiscoverList(r?.traders || [])
        setPage(p)
      }
    } catch (e) {
      setErr(e.message || 'Не удалось загрузить лидеров')
      setDiscoverList([])
    } finally {
      setLoading(false)
    }
  }, [sort, minRoi, verifiedOnly, srcOkx, srcHl, srcSocial])

  const loadTracked = useCallback(async () => {
    try {
      const r = await api.smartMoneyTracked()
      setTrackedList(r?.tracked || [])
    } catch {
      setTrackedList([])
    }
  }, [])

  const loadCopies = useCallback(async () => {
    try {
      const r = await api.smartMoneyMyCopies()
      setCopies(r?.copies || [])
    } catch {
      setCopies([])
    }
  }, [])

  useEffect(() => {
    loadStatus()
    fetchDiscover(1)
    loadTracked()
    loadCopies()
  }, [loadStatus, fetchDiscover, loadTracked, loadCopies])

  const filtered = useMemo(() => {
    const list = discoverList || []
    if (!q.trim()) return list
    const s = q.trim().toLowerCase()
    return list.filter(
      t =>
        (t.alias || '').toLowerCase().includes(s) ||
        (t.unique_code || '').toLowerCase().includes(s),
    )
  }, [discoverList, q])

  const handleTrack = async (trader) => {
    if (isGuest) return
    try {
      await api.smartMoneyTrack(trader.unique_code)
      await loadTracked()
    } catch (e) {
      alert(e.message)
    }
  }

  const handleUntrack = async (trader) => {
    if (isGuest) return
    try {
      await api.smartMoneyUntrack(trader.unique_code)
      await loadTracked()
    } catch (e) {
      alert(e.message)
    }
  }

  const handleView = async (trader) => {
    setDetailTrader(trader)
    setHistory([])
    try {
      const [d, h] = await Promise.all([
        api.smartMoneyTrader(trader.unique_code).catch(() => null),
        api.smartMoneyTraderHistory(trader.unique_code, 30).catch(() => null),
      ])
      if (d && !d.error) setDetailTrader({ ...trader, ...d })
      setHistory(h?.trades || h?.data || [])
    } catch {
      /* ignore */
    }
  }

  const handleCopyConfirm = async ({ amount }) => {
    if (!copyTrader || isGuest) return
    setBusy(true)
    try {
      const r = await api.smartMoneyCopy(copyTrader.unique_code, amount)
      if (r?.ok === false) throw new Error(r.msg || r.message || 'Ошибка')
      setCopyTrader(null)
      await loadCopies()
      alert('Копирование запущено (проверьте OKX Copy Trading)')
    } catch (e) {
      alert(e.message || 'Не удалось запустить копирование')
    } finally {
      setBusy(false)
    }
  }

  const toggleTracker = async () => {
    if (isGuest) return
    setBusy(true)
    try {
      if (status?.running) await api.smartMoneyStop()
      else await api.smartMoneyStart({})
      await loadStatus()
    } catch (e) {
      alert(e.message)
    } finally {
      setBusy(false)
    }
  }

  const tabs = [
    { id: 'discover', label: 'Лидеры', icon: Trophy },
    { id: 'tracked', label: 'Отслеживание', icon: Star },
    { id: 'copies', label: 'Мои копии', icon: Copy },
  ]

  return (
    <div className="max-w-5xl mx-auto space-y-5 pb-10">
      {/* Header */}
      <div className="rounded-2xl border border-[var(--border)] bg-gradient-to-br from-[var(--bg-card)] to-[var(--bg-elevated)] p-5">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <h1 className="text-xl font-bold text-[var(--txt)] flex items-center gap-2">
              <Trophy className="text-amber-400" size={22} />
              Умные деньги
            </h1>
            <p className="text-sm text-[var(--txt-muted)] mt-1 max-w-xl leading-relaxed">
              Лидеры OKX Copy Trading с проверкой ROI, win rate и просадки.
              OKX (копирование), Hyperliquid (on-chain ROI) и открытые соц-профили. Автокопирование — только для OKX.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <div className={`px-2.5 py-1 rounded-full text-xs font-medium border ${
              status?.running
                ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
                : 'bg-[var(--bg)] text-[var(--txt-muted)] border-[var(--border)]'
            }`}>
              {status?.running ? 'Трекер ON' : 'Трекер OFF'}
            </div>
            {!isGuest && (
              <button type="button" className="btn btn-secondary btn-sm inline-flex items-center gap-1" disabled={busy} onClick={toggleTracker}>
                {status?.running ? <><Square size={14} /> Стоп</> : <><Play size={14} /> Старт</>}
              </button>
            )}
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => fetchDiscover(page)} disabled={loading}>
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
          <div className="rounded-xl bg-[var(--bg)]/60 border border-[var(--border)] p-3">
            <div className="text-[10px] text-[var(--txt-muted)]">В списке лидеров</div>
            <div className="text-lg font-bold text-[var(--txt)]">{discoverList.length}</div>
          </div>
          <div className="rounded-xl bg-[var(--bg)]/60 border border-[var(--border)] p-3">
            <div className="text-[10px] text-[var(--txt-muted)]">Проверенные</div>
            <div className="text-lg font-bold text-emerald-400">
              {discoverList.filter(t => t.verified).length}
            </div>
          </div>
          <div className="rounded-xl bg-[var(--bg)]/60 border border-[var(--border)] p-3">
            <div className="text-[10px] text-[var(--txt-muted)]">Отслеживаю</div>
            <div className="text-lg font-bold text-blue-400">{trackedList.length}</div>
          </div>
          <div className="rounded-xl bg-[var(--bg)]/60 border border-[var(--border)] p-3">
            <div className="text-[10px] text-[var(--txt-muted)]">Активные копии</div>
            <div className="text-lg font-bold text-[var(--txt)]">{copies.length}</div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 p-1 rounded-xl bg-[var(--bg-elevated)] border border-[var(--border)]">
        {tabs.map(t => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg text-sm font-medium transition-colors ${
              tab === t.id
                ? 'bg-[var(--bg-card)] text-[var(--txt)] shadow-sm'
                : 'text-[var(--txt-muted)] hover:text-[var(--txt)]'
            }`}
          >
            <t.icon size={15} />
            {t.label}
          </button>
        ))}
      </div>

      {/* Discover filters */}
      {tab === 'discover' && (
        <>
          <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3 flex flex-col sm:flex-row gap-3 sm:items-end">
            <div className="flex-1">
              <label className="text-[10px] text-[var(--txt-muted)] mb-1 flex items-center gap-1">
                <Search size={11} /> Поиск
              </label>
              <input
                value={q}
                onChange={e => setQ(e.target.value)}
                placeholder="Имя или код трейдера"
                className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]"
              />
            </div>
            <div>
              <label className="text-[10px] text-[var(--txt-muted)] mb-1 flex items-center gap-1">
                <Filter size={11} /> Сортировка
              </label>
              <select
                value={sort}
                onChange={e => setSort(e.target.value)}
                className="rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]"
              >
                <option value="roi">По ROI</option>
                <option value="pnl">По PnL</option>
                <option value="copyRatio">По подписчикам</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] text-[var(--txt-muted)] mb-1">Мин. ROI %</label>
              <input
                type="number"
                value={minRoi}
                onChange={e => setMinRoi(Number(e.target.value))}
                className="w-24 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]"
              />
            </div>
            <label className="flex items-center gap-2 text-xs text-[var(--txt-muted)] pb-2 cursor-pointer">
              <input type="checkbox" checked={verifiedOnly} onChange={e => setVerifiedOnly(e.target.checked)} />
              Только проверенные
            </label>
            <div className="flex flex-wrap items-center gap-2 pb-2 text-xs text-[var(--txt-muted)]">
              <span className="opacity-70">Источники:</span>
              <label className="inline-flex items-center gap-1 cursor-pointer">
                <input type="checkbox" checked={srcOkx} onChange={e => setSrcOkx(e.target.checked)} /> OKX
              </label>
              <label className="inline-flex items-center gap-1 cursor-pointer">
                <input type="checkbox" checked={srcHl} onChange={e => setSrcHl(e.target.checked)} /> Hyperliquid
              </label>
              <label className="inline-flex items-center gap-1 cursor-pointer">
                <input type="checkbox" checked={srcSocial} onChange={e => setSrcSocial(e.target.checked)} /> Соцсети
              </label>
            </div>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={loading}
              onClick={() => fetchDiscover(1)}
            >
              {loading ? 'Загрузка…' : 'Найти'}
            </button>
          </div>

          {err && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 flex items-center gap-2">
              <AlertTriangle size={16} /> {err}
            </div>
          )}

          {loading && !filtered.length ? (
            <div className="text-center py-12 text-[var(--txt-muted)] text-sm">Загружаем лидеров с OKX…</div>
          ) : !filtered.length ? (
            <div className="text-center py-12 text-[var(--txt-muted)] text-sm">
              Никого не нашли. Смягчите фильтр ROI или нажмите «Найти» ещё раз.
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {filtered.map((t, i) => (
                <TraderCard
                  key={t.unique_code || i}
                  trader={t}
                  rank={t.rank || i + 1}
                  onView={handleView}
                  onCopy={setCopyTrader}
                  onTrack={handleTrack}
                  onUntrack={handleUntrack}
                  isTracked={trackedCodes.has(t.unique_code)}
                  isGuest={isGuest}
                />
              ))}
            </div>
          )}

          <div className="flex items-center justify-center gap-3">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={page <= 1 || loading}
              onClick={() => fetchDiscover(page - 1)}
            >
              Назад
            </button>
            <span className="text-sm text-[var(--txt-muted)]">Стр. {page}</span>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={loading}
              onClick={() => fetchDiscover(page + 1)}
            >
              Далее <ChevronRight size={14} />
            </button>
          </div>
        </>
      )}

      {tab === 'tracked' && (
        <div>
          {!trackedList.length ? (
            <div className="text-center py-12 text-[var(--txt-muted)] text-sm">
              Список пуст. На вкладке «Лидеры» добавьте трейдеров звёздочкой.
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {trackedList.map((t, i) => (
                <TraderCard
                  key={t.unique_code || i}
                  trader={t}
                  rank={i + 1}
                  onView={handleView}
                  onCopy={setCopyTrader}
                  onTrack={handleTrack}
                  onUntrack={handleUntrack}
                  isTracked
                  isGuest={isGuest}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {tab === 'copies' && (
        <div>
          {!copies.length ? (
            <div className="text-center py-12 text-[var(--txt-muted)] text-sm">
              Активных автокопирований нет. Выберите лидера и нажмите «Копировать».
            </div>
          ) : (
            <div className="space-y-2">
              {copies.map((c, i) => (
                <div
                  key={c.unique_code || i}
                  className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-4 flex items-center justify-between gap-3"
                >
                  <div>
                    <div className="font-semibold text-[var(--txt)]">{c.alias || c.unique_code}</div>
                    <div className="text-xs text-[var(--txt-muted)]">Копирование активно</div>
                  </div>
                  {!isGuest && (
                    <button
                      type="button"
                      className="btn btn-danger btn-sm"
                      onClick={async () => {
                        try {
                          await api.smartMoneyStopCopy(c.unique_code)
                          await loadCopies()
                        } catch (e) {
                          alert(e.message)
                        }
                      }}
                    >
                      Остановить
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {copyTrader && (
        <CopyModal
          trader={copyTrader}
          onClose={() => setCopyTrader(null)}
          onConfirm={handleCopyConfirm}
          busy={busy}
        />
      )}
      {detailTrader && (
        <DetailModal
          trader={detailTrader}
          history={history}
          onClose={() => setDetailTrader(null)}
          onCopy={(t) => { setDetailTrader(null); setCopyTrader(t) }}
        />
      )}
    </div>
  )
}
