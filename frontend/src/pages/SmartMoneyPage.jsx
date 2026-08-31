import { useState, useCallback } from 'react'
import { Search, RefreshCw, TrendingUp, ExternalLink, Trophy, ShieldCheck } from 'lucide-react'

const fmtNum = (v, d = 0) => {
  const n = Number(v) || 0
  return n.toLocaleString('en', { minimumFractionDigits: d, maximumFractionDigits: d })
}
const fmtUsd = (v) => {
  const n = Number(v) || 0
  const s = n >= 0 ? '+' : ''
  return `${s}$${fmtNum(Math.abs(n))}`
}

export default function SmartMoneyPage({ isGuest }) {
  const [list, setList] = useState([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [sources, setSources] = useState('okx,hyperliquid')
  const [minRoi, setMinRoi] = useState(0)
  const [lastUpdate, setLastUpdate] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const r = await fetch(`/api/smart-money/discover?limit=25&min_roi=${minRoi}&sources=${encodeURIComponent(sources)}`, { credentials: 'include' })
      if (r.status === 401) {
        setErr('Требуется авторизация')
        setList([])
        return
      }
      const data = await r.json()
      if (data?.error && !(data?.traders || []).length) {
        setErr(String(data.error || data.message || 'Ошибка загрузки'))
        setList([])
      } else {
        setList(data?.traders || [])
        setLastUpdate(new Date())
        if (data?.errors?.length) setErr(data.errors.join('; '))
      }
    } catch (e) {
      setErr(e.message || 'Не удалось загрузить')
      setList([])
    } finally {
      setLoading(false)
    }
  }, [sources, minRoi])

  return (
    <div className="h-full overflow-y-auto overscroll-contain max-w-4xl mx-auto space-y-4 px-1 pb-16">
      {/* Header */}
      <div className="rounded-2xl border border-[var(--border)] bg-gradient-to-br from-[var(--bg-card)] to-[var(--bg-elevated)] p-5">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Trophy className="text-amber-400" size={20} />
            <h1 className="text-lg font-bold text-[var(--txt)]">Умные деньги</h1>
            <span className="text-[10px] text-[var(--txt-muted)] border border-[var(--border)] rounded-full px-2 py-0.5">read-only</span>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <select
              value={sources}
              onChange={e => setSources(e.target.value)}
              className="rounded-lg border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5 text-xs text-[var(--txt)]"
            >
              <option value="okx">OKX</option>
              <option value="hyperliquid">Hyperliquid</option>
              <option value="okx,hyperliquid">OKX + Hyperliquid</option>
              <option value="okx,hyperliquid,social">Все источники</option>
            </select>
            <input
              type="number"
              value={minRoi}
              onChange={e => setMinRoi(Number(e.target.value))}
              placeholder="мин ROI %"
              className="w-20 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5 text-xs text-[var(--txt)]"
            />
            <button className="btn btn-primary btn-sm" onClick={load} disabled={loading}>
              {loading ? 'Загрузка…' : <><Search size={13} /> Найти</>}
            </button>
            <button className="btn btn-secondary btn-sm" onClick={load} disabled={loading}>
              <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>
        <p className="text-xs text-[var(--txt-muted)] mt-2 leading-relaxed">
          Топ-лидеры OKX Copy Trading и Hyperliquid по ROI. Просмотр только для ознакомления — без автоследования.
          {lastUpdate && <span className="ml-1 opacity-70">Обновлено {lastUpdate.toLocaleTimeString()}</span>}
        </p>
        {err && <div className="text-xs text-amber-400 mt-2">{err}</div>}
      </div>

      {/* Table */}
      {list.length === 0 && !loading ? (
        <div className="text-center py-16 text-sm text-[var(--txt-muted)]">
          Нажмите «Найти», чтобы загрузить лидеров.
        </div>
      ) : (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-[var(--bg-elevated)] text-[var(--txt-muted)]">
                <tr>
                  <th className="text-left px-3 py-2">#</th>
                  <th className="text-left px-3 py-2">Трейдер</th>
                  <th className="text-right px-3 py-2">ROI</th>
                  <th className="text-right px-3 py-2">PnL</th>
                  <th className="text-right px-3 py-2">Win Rate</th>
                  <th className="text-right px-3 py-2">Подписчики</th>
                  <th className="text-right px-3 py-2">Дни</th>
                  <th className="text-center px-3 py-2">Источник</th>
                  <th className="text-center px-3 py-2">Профиль</th>
                </tr>
              </thead>
              <tbody>
                {list.map((t, i) => {
                  const roi = Number(t.roi_pct) || 0
                  const wr = Number(t.win_rate) || 0
                  const src = (t.source || '').toLowerCase()
                  return (
                    <tr key={t.unique_code || i} className="border-t border-[var(--border)] hover:bg-[var(--bg-elevated)]/50">
                      <td className="px-3 py-2 text-[var(--txt-muted)]">{i + 1}</td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-1.5">
                          <span className="font-semibold text-[var(--txt)] max-w-[160px] truncate">{t.alias || t.unique_code?.slice(0, 14)}</span>
                          {t.verified && <ShieldCheck size={12} className="text-emerald-400 shrink-0" />}
                        </div>
                        <div className="text-[10px] text-[var(--txt-muted)] font-mono">{t.unique_code?.slice(0, 18)}</div>
                      </td>
                      <td className={`px-3 py-2 text-right font-bold mono ${roi >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {roi >= 0 ? '+' : ''}{fmtNum(roi, 1)}%
                      </td>
                      <td className={`px-3 py-2 text-right mono ${Number(t.pnl_usd) >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                        {fmtUsd(t.pnl_usd)}
                      </td>
                      <td className="px-3 py-2 text-right">{wr > 0 ? `${fmtNum(wr * 100, 0)}%` : '—'}</td>
                      <td className="px-3 py-2 text-right">{fmtNum(t.copy_traders)}</td>
                      <td className="px-3 py-2 text-right">{t.lead_days || t.period_days || '—'}</td>
                      <td className="px-3 py-2 text-center">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] border ${
                          src === 'okx' ? 'bg-blue-500/10 text-blue-400 border-blue-500/20'
                          : src === 'hyperliquid' ? 'bg-purple-500/10 text-purple-400 border-purple-500/20'
                          : 'bg-slate-500/10 text-slate-400 border-slate-500/20'
                        }`}>
                          {src === 'okx' ? 'OKX' : src === 'hyperliquid' ? 'HyperL' : 'Social'}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-center">
                        {t.profile_url ? (
                          <a href={t.profile_url} target="_blank" rel="noreferrer" className="text-[var(--info)] inline-flex items-center gap-0.5 hover:underline">
                            <ExternalLink size={11} />
                          </a>
                        ) : '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <div className="text-[10px] text-[var(--txt-muted)] leading-relaxed">
        {isGuest ? 'Гостевой режим: отображение без операций.' : 'Только просмотр. Управление и автоследование отключены.'}
      </div>
    </div>
  )
}
