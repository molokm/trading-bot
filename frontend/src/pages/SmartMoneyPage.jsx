import { useState, useCallback, useEffect } from 'react'
import { Search, RefreshCw, ExternalLink, Trophy, ShieldCheck, Copy, Square, X } from 'lucide-react'

const fmtNum = (v, d = 0) => {
  const n = Number(v) || 0
  return n.toLocaleString('en', { minimumFractionDigits: d, maximumFractionDigits: d })
}
const fmtUsd = (v) => {
  const n = Number(v) || 0
  const s = n >= 0 ? '+' : ''
  return `${s}$${fmtNum(Math.abs(n))}`
}

function CopyModal({ trader, onClose, onConfirm, busy }) {
  const [amount, setAmount] = useState(500)
  const [tp, setTp] = useState(10)
  const [sl, setSl] = useState(5)
  if (!trader) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3" onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] p-5" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-1">
          <h3 className="font-bold text-[var(--txt)] flex items-center gap-2"><Copy size={16} className="text-blue-400" /> Копирование на OKX</h3>
          <button onClick={onClose} className="p-1"><X size={16} /></button>
        </div>
        <p className="text-sm text-[var(--txt)] mb-3 truncate">{trader.alias || trader.unique_code}</p>
        <div className="rounded-lg bg-[var(--bg-elevated)] p-3 mb-3">
          <div className="flex justify-between text-xs text-[var(--txt-muted)]">
            <span>ROI</span><span className="text-[var(--profit)] mono">{(trader.roi_pct || 0) >= 0 ? '+' : ''}{fmtNum(trader.roi_pct, 1)}%</span>
          </div>
          <div className="flex justify-between text-xs text-[var(--txt-muted)] mt-1">
            <span>Подписчики</span><span className="mono">{fmtNum(trader.copy_traders)}</span>
          </div>
        </div>
        <label className="block text-xs text-[var(--txt-muted)] mb-1">Сумма копирования (USDT)</label>
        <input type="number" min={10} step={10} value={amount} onChange={e => setAmount(Number(e.target.value))}
          className="w-full mb-3 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]" />
        <div className="grid grid-cols-2 gap-3 mb-4">
          <div>
            <label className="block text-xs text-[var(--txt-muted)] mb-1">Take Profit %</label>
            <input type="number" min={1} max={100} value={tp} onChange={e => setTp(Number(e.target.value))}
              className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]" />
          </div>
          <div>
            <label className="block text-xs text-[var(--txt-muted)] mb-1">Stop Loss %</label>
            <input type="number" min={1} max={100} value={sl} onChange={e => setSl(Number(e.target.value))}
              className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--txt)]" />
          </div>
        </div>
        <div className="flex gap-2">
          <button className="btn btn-secondary flex-1" onClick={onClose}>Отмена</button>
          <button className="btn btn-primary flex-1" disabled={busy || amount < 10}
            onClick={() => onConfirm({ amount, tp_ratio: tp / 100, sl_ratio: sl / 100 })}>
            {busy ? 'Подключение…' : 'Копировать'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function SmartMoneyPage({ isGuest }) {
  const [tab, setTab] = useState('discover')
  const [list, setList] = useState([])
  const [copies, setCopies] = useState([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [sources, setSources] = useState('okx,hyperliquid')
  const [minRoi, setMinRoi] = useState(0)
  const [lastUpdate, setLastUpdate] = useState(null)
  const [copyTrader, setCopyTrader] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const r = await fetch(`/api/smart-money/discover?limit=25&min_roi=${minRoi}&sources=${encodeURIComponent(sources)}`, { credentials: 'include' })
      if (r.status === 401) { setErr('Требуется авторизация'); setList([]); return }
      const data = await r.json()
      if (data?.error && !(data?.traders || []).length) {
        setErr(String(data.error || data.message || 'Ошибка загрузки')); setList([])
      } else {
        setList(data?.traders || []); setLastUpdate(new Date())
        if (data?.errors?.length) setErr(data.errors.join('; '))
      }
    } catch (e) { setErr(e.message || 'Не удалось загрузить'); setList([]) }
    finally { setLoading(false) }
  }, [sources, minRoi])

  const loadCopies = useCallback(async () => {
    try {
      const r = await fetch('/api/smart-money/my-copies', { credentials: 'include' })
      if (r.status === 401) { setCopies([]); return }
      const data = await r.json()
      setCopies(data?.copies || [])
    } catch { setCopies([]) }
  }, [])

  useEffect(() => { if (tab === 'copies') loadCopies() }, [tab, loadCopies])

  const handleCopy = async ({ amount, tp_ratio, sl_ratio }) => {
    if (!copyTrader) return
    setBusy(true)
    try {
      const r = await fetch('/api/smart-money/copy', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ unique_code: copyTrader.unique_code, copy_amt: amount, tp_ratio, sl_ratio }),
      })
      const data = await r.json()
      alert(data.msg || (data.ok ? 'Копирование запущено' : 'Ошибка'))
      setCopyTrader(null)
      if (data.ok) loadCopies()
    } catch (e) { alert(e.message || 'Ошибка') }
    finally { setBusy(false) }
  }

  const handleStopCopy = async (code) => {
    try {
      const r = await fetch('/api/smart-money/stop-copy', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ unique_code: code }),
      })
      const data = await r.json()
      alert(data.msg || 'Готово')
      loadCopies()
    } catch (e) { alert(e.message || 'Ошибка') }
  }

  return (
    <div className="h-full overflow-y-auto overscroll-contain max-w-4xl mx-auto space-y-4 px-1 pb-16">
      {/* Tabs */}
      <div className="flex gap-1 p-1 bg-[var(--bg-elevated)] rounded-lg border border-[var(--border)]">
        {[{ id: 'discover', label: 'Лидеры' }, { id: 'copies', label: `Мои копии (${copies.length})` }].map(t => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={`flex-1 px-3 py-2 rounded-md text-sm font-medium transition-colors ${tab === t.id ? 'bg-[var(--bg-card)] text-[var(--txt)]' : 'text-[var(--txt-muted)] hover:text-[var(--txt)]'}`}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'discover' && (
        <>
          <div className="rounded-2xl border border-[var(--border)] bg-gradient-to-br from-[var(--bg-card)] to-[var(--bg-elevated)] p-5">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Trophy className="text-amber-400" size={20} />
                <h1 className="text-lg font-bold text-[var(--txt)]">Умные деньги</h1>
                <span className="text-[10px] text-[var(--txt-muted)] border border-[var(--border)] rounded-full px-2 py-0.5">copy</span>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <select value={sources} onChange={e => setSources(e.target.value)}
                  className="rounded-lg border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5 text-xs text-[var(--txt)]">
                  <option value="okx">OKX</option>
                  <option value="hyperliquid">Hyperliquid</option>
                  <option value="okx,hyperliquid">OKX + Hyperliquid</option>
                  <option value="okx,hyperliquid,social">Все источники</option>
                </select>
                <input type="number" value={minRoi} onChange={e => setMinRoi(Number(e.target.value))} placeholder="мин ROI %"
                  className="w-20 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5 text-xs text-[var(--txt)]" />
                <button className="btn btn-primary btn-sm" onClick={load} disabled={loading}>
                  {loading ? 'Загрузка…' : <><Search size={13} /> Найти</>}
                </button>
                <button className="btn btn-secondary btn-sm" onClick={load} disabled={loading}>
                  <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
                </button>
              </div>
            </div>
            <p className="text-xs text-[var(--txt-muted)] mt-2 leading-relaxed">
              Выберите OKX-лидера и нажмите «Копировать» — сделки будут зеркалиться на ваш аккаунт через OKX Copy Trading.
              {lastUpdate && <span className="ml-1 opacity-70">Обновлено {lastUpdate.toLocaleTimeString()}</span>}
            </p>
            {err && <div className="text-xs text-amber-400 mt-2">{err}</div>}
          </div>

          {list.length === 0 && !loading ? (
            <div className="text-center py-16 text-sm text-[var(--txt-muted)]">Нажмите «Найти», чтобы загрузить лидеров.</div>
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
                      <th className="text-center px-3 py-2">Источник</th>
                      <th className="text-center px-3 py-2">Действие</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((t, i) => {
                      const roi = Number(t.roi_pct) || 0
                      const wr = Number(t.win_rate) || 0
                      const src = (t.source || '').toLowerCase()
                      const isOkx = src === 'okx' || Boolean(t.copyable)
                      return (
                        <tr key={t.unique_code || i} className="border-t border-[var(--border)] hover:bg-[var(--bg-elevated)]/50">
                          <td className="px-3 py-2 text-[var(--txt-muted)]">{i + 1}</td>
                          <td className="px-3 py-2">
                            <div className="flex items-center gap-1.5">
                              <span className="font-semibold text-[var(--txt)] max-w-[150px] truncate">{t.alias || t.unique_code?.slice(0, 14)}</span>
                              {t.verified && <ShieldCheck size={12} className="text-emerald-400 shrink-0" />}
                            </div>
                            <div className="text-[10px] text-[var(--txt-muted)] font-mono">{t.unique_code?.slice(0, 18)}</div>
                          </td>
                          <td className={`px-3 py-2 text-right font-bold mono ${roi >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                            {roi >= 0 ? '+' : ''}{fmtNum(roi, 1)}%
                          </td>
                          <td className={`px-3 py-2 text-right mono ${Number(t.pnl_usd) >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>{fmtUsd(t.pnl_usd)}</td>
                          <td className="px-3 py-2 text-right">{wr > 0 ? `${fmtNum(wr * 100, 0)}%` : '—'}</td>
                          <td className="px-3 py-2 text-right">{fmtNum(t.copy_traders)}</td>
                          <td className="px-3 py-2 text-center">
                            <span className={`px-1.5 py-0.5 rounded text-[10px] border ${isOkx ? 'bg-blue-500/10 text-blue-400 border-blue-500/20' : 'bg-purple-500/10 text-purple-400 border-purple-500/20'}`}>
                              {isOkx ? 'OKX' : 'HyperL'}
                            </span>
                          </td>
                          <td className="px-3 py-2 text-center">
                            {isOkx ? (
                              <button className="btn btn-primary btn-sm" disabled={isGuest} onClick={() => setCopyTrader(t)}>
                                <Copy size={12} /> Копировать
                              </button>
                            ) : (
                              <span className="text-[10px] text-[var(--txt-muted)]">—</span>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}

      {tab === 'copies' && (
        <div>
          {copies.length === 0 ? (
            <div className="text-center py-16 text-sm text-[var(--txt-muted)]">
              Активных копирований нет. Перейдите на вкладку «Лидеры» и выберите OKX-трейдера.
            </div>
          ) : (
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)] overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="bg-[var(--bg-elevated)] text-[var(--txt-muted)]">
                    <tr>
                      <th className="text-left px-3 py-2">Трейдер</th>
                      <th className="text-left px-3 py-2">Аккаунт</th>
                      <th className="text-right px-3 py-2">Режим</th>
                      <th className="text-center px-3 py-2">Действие</th>
                    </tr>
                  </thead>
                  <tbody>
                    {copies.map((c, i) => (
                      <tr key={c.uniqueCode || c.unique_code || i} className="border-t border-[var(--border)]">
                        <td className="px-3 py-2 font-semibold text-[var(--txt)]">{c.leadTraderNickName || c.nickName || c.uniqueCode || c.unique_code || '—'}</td>
                        <td className="px-3 py-2 text-[var(--txt-muted)]">{c.instId || c.instType || '—'}</td>
                        <td className="px-3 py-2 text-right">{c.copyMode || '—'}</td>
                        <td className="px-3 py-2 text-center">
                          <button className="btn btn-danger btn-sm" disabled={isGuest}
                            onClick={() => handleStopCopy(c.uniqueCode || c.unique_code)}>
                            <Square size={12} /> Стоп
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {copyTrader && <CopyModal trader={copyTrader} onClose={() => setCopyTrader(null)} onConfirm={handleCopy} busy={busy} />}
    </div>
  )
}
