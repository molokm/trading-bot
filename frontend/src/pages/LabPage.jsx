import React, { useEffect, useState } from 'react'
import { FlaskConical, Play, Loader2, TrendingUp, AlertTriangle, CheckCircle2 } from 'lucide-react'
import { api } from '../services/api'

const SYMBOLS = ['BTC', 'ETH', 'SOL', 'XRP']

function pboTone(pbo) {
  if (pbo == null || Number.isNaN(Number(pbo))) return 'text-[var(--txt-muted)]'
  const v = Number(pbo)
  if (v <= 0.2) return 'text-[var(--profit)]'
  if (v <= 0.5) return 'text-amber-400'
  return 'text-[var(--loss)]'
}

export default function LabPage() {
  const [symbols, setSymbols] = useState(['BTC', 'ETH', 'SOL'])
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState(null)
  const [err, setErr] = useState('')
  const [hop, setHop] = useState(null)
  const [hopBusy, setHopBusy] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    api.labBacktestLast().then(r => { if (r && r.best) setRes(r) }).catch(() => {})
  }, [])

  const toggle = (s) => setSymbols(prev => prev.includes(s)
    ? (prev.length > 1 ? prev.filter(x => x !== s) : prev)
    : [...prev, s].slice(0, 4))

  const run = async () => {
    setBusy(true); setErr(''); setMsg(''); setHop(null)
    try {
      const r = await api.labBacktestRun({ symbols, bar: '1H', pages: 4 })
      setRes(r)
    } catch (e) {
      setErr(e.message || 'Ошибка бэктеста')
    } finally {
      setBusy(false)
    }
  }

  const suggest = async () => {
    setHopBusy(true); setMsg('')
    try {
      const r = await api.labHyperopt()
      setHop(r)
    } catch (e) {
      setMsg(e.message || 'Ошибка hyperopt')
    } finally {
      setHopBusy(false)
    }
  }

  const apply = async () => {
    if (!hop?.overrides) return
    setHopBusy(true); setMsg('')
    try {
      const cfg = await api.aiGetConfig()
      const base = (cfg && (cfg.demo || cfg.runtime)) || {}
      await api.aiSaveConfig({ ...base, ...hop.overrides })
      setMsg('Применено к DEMO-конфигу (в LIVE — через «Продвинуть» в настройках)')
    } catch (e) {
      setMsg(e.message || 'Не удалось применить')
    } finally {
      setHopBusy(false)
    }
  }

  const best = res?.best
  const configs = res?.configs || []

  return (
    <div className="p-4 sm:p-6 max-w-6xl mx-auto space-y-4">
      <div className="flex items-center gap-2">
        <FlaskConical size={18} className="text-[var(--info)]" />
        <h1 className="text-lg font-semibold text-[var(--txt)]">Лаборатория бэктеста</h1>
      </div>
      <p className="text-xs text-[var(--txt-muted)]">
        Point-in-time прогон детерминированного скелета входов/выходов по закрытым барам
        (вход по открытию следующего бара, издержки 0.14% за круг + 0.02%/сторону).
        PBO — вероятность переобучения сетки параметров (CSCV).
      </p>

      <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-[var(--txt-muted)]">Монеты:</span>
          {SYMBOLS.map(s => (
            <button
              key={s}
              onClick={() => toggle(s)}
              className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition ${
                symbols.includes(s)
                  ? 'bg-[var(--info-dim)] text-[var(--info)] border-[var(--info)]/40'
                  : 'bg-[var(--bg)] text-[var(--txt-muted)] border-[var(--border)]'
              }`}
            >{s}</button>
          ))}
          <button
            onClick={run}
            disabled={busy}
            className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-[var(--info)] text-white disabled:opacity-50"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            {busy ? 'Считаю…' : 'Запустить'}
          </button>
        </div>
        {err && <div className="text-xs text-[var(--loss)]">{err}</div>}
        {res?.ts && (
          <div className="text-2xs text-[var(--txt-muted)]">
            {new Date(res.ts).toLocaleString('ru-RU')} · {res.bar} · {Object.entries(res.bars || {}).map(([s, n]) => `${s}:${n}`).join('  ')}
          </div>
        )}
      </div>

      {best && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3">
            <div className="text-2xs text-[var(--txt-muted)]">PBO</div>
            <div className={`text-xl font-bold ${pboTone(res.pbo)}`}>
              {res.pbo == null ? '—' : Number(res.pbo).toFixed(2)}
            </div>
            <div className="text-2xs text-[var(--txt-muted)]">
              {Number(res.pbo) <= 0.2 ? 'устойчиво' : Number(res.pbo) <= 0.5 ? 'умеренно' : 'переобучение'}
            </div>
          </div>
          <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3">
            <div className="text-2xs text-[var(--txt-muted)]">Лучший Net</div>
            <div className={`text-xl font-bold ${best.net_return_pct >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
              {best.net_return_pct.toFixed(1)}%
            </div>
            <div className="text-2xs text-[var(--txt-muted)]">{best.trades} сделок · PF {Number(best.profit_factor).toFixed(2)}</div>
          </div>
          <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3">
            <div className="text-2xs text-[var(--txt-muted)]">Win rate</div>
            <div className="text-xl font-bold text-[var(--txt)]">{(best.win_rate * 100).toFixed(1)}%</div>
            <div className="text-2xs text-[var(--txt-muted)]">Sharpe {Number(best.sharpe).toFixed(2)}</div>
          </div>
          <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3">
            <div className="text-2xs text-[var(--txt-muted)]">Max DD</div>
            <div className="text-xl font-bold text-[var(--txt)]">{best.max_drawdown_pct.toFixed(1)}%</div>
            <div className="text-2xs text-[var(--txt-muted)]">expectancy {best.expectancy_pct.toFixed(2)}%</div>
          </div>
        </div>
      )}

      {best && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4 space-y-2">
          <div className="flex items-center gap-2">
            <TrendingUp size={14} className="text-[var(--info)]" />
            <span className="text-sm font-semibold text-[var(--txt)]">Hyperopt порогов</span>
            <button
              onClick={suggest}
              disabled={hopBusy}
              className="ml-auto px-2.5 py-1 rounded-lg text-xs font-medium border border-[var(--border)] text-[var(--txt)] disabled:opacity-50"
            >
              {hopBusy ? '…' : 'Предложить'}
            </button>
          </div>
          {hop && (
            <div className="text-xs space-y-2">
              {hop.apply ? (
                <>
                  <div className="flex items-center gap-1.5 text-[var(--profit)]">
                    <CheckCircle2 size={13} /> PBO {Number(hop.pbo).toFixed(2)} — можно применять
                  </div>
                  <pre className="text-2xs bg-[var(--bg)] rounded-lg p-2 overflow-x-auto">{JSON.stringify(hop.overrides, null, 2)}</pre>
                  <div className="text-2xs text-[var(--txt-muted)]">{hop.note}</div>
                  <button
                    onClick={apply}
                    disabled={hopBusy}
                    className="px-2.5 py-1 rounded-lg text-xs font-semibold bg-[var(--profit)] text-white disabled:opacity-50"
                  >Применить к DEMO</button>
                </>
              ) : (
                <div className="flex items-center gap-1.5 text-amber-400">
                  <AlertTriangle size={13} /> {hop.reason || 'Недостаточно доказательств'}
                </div>
              )}
              {msg && <div className="text-2xs text-[var(--txt-muted)]">{msg}</div>}
            </div>
          )}
        </div>
      )}

      {configs.length > 0 && (
        <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] overflow-hidden">
          <div className="px-4 py-2 text-xs font-semibold text-[var(--txt)] border-b border-[var(--border)]">
            Сетка параметров ({configs.length})
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-[var(--txt-muted)] text-2xs">
                <tr className="text-left">
                  <th className="px-3 py-2">ADX</th>
                  <th className="px-3 py-2">ROC</th>
                  <th className="px-3 py-2">ATR stop</th>
                  <th className="px-3 py-2">TP, R</th>
                  <th className="px-3 py-2 text-right">Net %</th>
                  <th className="px-3 py-2 text-right">Trades</th>
                  <th className="px-3 py-2 text-right">WR</th>
                  <th className="px-3 py-2 text-right">PF</th>
                  <th className="px-3 py-2 text-right">DD %</th>
                </tr>
              </thead>
              <tbody>
                {configs.slice(0, 40).map((c, i) => (
                  <tr key={i} className="border-t border-[var(--border)]">
                    <td className="px-3 py-1.5">{c.params.min_adx}</td>
                    <td className="px-3 py-1.5">{c.params.min_roc}</td>
                    <td className="px-3 py-1.5">{c.params.atr_stop_mult}</td>
                    <td className="px-3 py-1.5">{c.params.tp_r}</td>
                    <td className={`px-3 py-1.5 text-right font-medium ${c.net_return_pct >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                      {c.net_return_pct.toFixed(1)}
                    </td>
                    <td className="px-3 py-1.5 text-right">{c.trades}</td>
                    <td className="px-3 py-1.5 text-right">{(c.win_rate * 100).toFixed(0)}%</td>
                    <td className="px-3 py-1.5 text-right">{Number(c.profit_factor).toFixed(2)}</td>
                    <td className="px-3 py-1.5 text-right">{c.max_drawdown_pct.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
