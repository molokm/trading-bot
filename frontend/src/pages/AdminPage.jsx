import { useCallback, useEffect, useState } from 'react'
import { Users, Shield, RefreshCw, KeyRound, ToggleLeft, CreditCard } from 'lucide-react'
import { api } from '../services/api'

export default function AdminPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const r = await api.adminUsers()
      setData(r)
    } catch (e) {
      setErr(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const act = async (key, fn) => {
    setBusy(key)
    setMsg('')
    try {
      await fn()
      setMsg('Сохранено')
      await load()
    } catch (e) {
      setErr(e.message || String(e))
    } finally {
      setBusy('')
    }
  }

  const users = data?.users || []
  const counts = data?.counts || {}
  const platform = data?.platform || {}

  return (
    <div className="h-full flex flex-col p-4 gap-3 overflow-auto">
      <div className="flex items-center gap-2 flex-wrap">
        <Users size={16} className="text-[var(--accent)]" />
        <h1 className="text-sm font-bold">Админка · пользователи и счета</h1>
        <button className="btn btn-ghost btn-sm ml-auto" onClick={load} disabled={loading}>
          <RefreshCw size={12} className={loading ? 'animate-spin' : ''} /> Обновить
        </button>
      </div>

      {err && (
        <div className="text-2xs text-[var(--loss)] border border-[var(--loss)]/30 bg-[var(--loss-dim)] rounded-lg px-3 py-2">
          {err}
        </div>
      )}
      {msg && (
        <div className="text-2xs text-[var(--profit)] border border-[var(--profit)]/30 bg-[var(--profit-dim)] rounded-lg px-3 py-2">
          {msg}
        </div>
      )}

      {/* KPI */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
        {[
          ['Пользователи', counts.users ?? '—'],
          ['С ключами OKX', counts.with_creds ?? '—'],
          ['Live', counts.live ?? '—'],
          ['Demo', counts.demo ?? '—'],
          ['Активный план', counts.active_plans ?? '—'],
        ].map(([l, v]) => (
          <div key={l} className="panel p-3">
            <div className="text-2xs text-[var(--txt-muted)]">{l}</div>
            <div className="text-lg font-bold mono mt-0.5">{v}</div>
          </div>
        ))}
      </div>

      {/* Platform showcase */}
      <div className="panel p-3">
        <div className="flex items-center gap-2 mb-1">
          <Shield size={14} className="text-[var(--warn)]" />
          <span className="text-xs font-semibold">Витрина DEMO (платформа)</span>
          <span className={`ml-auto text-2xs font-bold px-2 py-0.5 rounded ${
            platform.mode === 'live' ? 'bg-[var(--loss-dim)] text-[var(--loss)]' : 'bg-[var(--warn-dim)] text-[var(--warn)]'
          }`}>
            {(platform.mode || 'demo').toUpperCase()}
          </span>
        </div>
        <p className="text-2xs text-[var(--txt-muted)] leading-relaxed">
          {platform.note || 'Общий demo-счёт: гости смотрят как работает продукт до оплаты. Live-торговля — только на личных ключах пользователя с подпиской.'}
        </p>
        <div className="text-2xs mt-1.5 mono text-[var(--txt)]">
          Env OKX: {platform.connected ? 'подключён' : 'нет ключей'} · режим {platform.mode || '—'}
        </div>
      </div>

      {/* Users table */}
      <div className="panel overflow-hidden">
        <div className="panel-header">
          <KeyRound size={13} /> Аккаунты пользователей
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-2xs">
            <thead>
              <tr className="text-[var(--txt-muted)] border-b border-[var(--border)]">
                <th className="text-left p-2 font-medium">User</th>
                <th className="text-left p-2 font-medium">План</th>
                <th className="text-left p-2 font-medium">OKX</th>
                <th className="text-left p-2 font-medium">Режим</th>
                <th className="text-left p-2 font-medium">Боты</th>
                <th className="text-left p-2 font-medium">Действия</th>
              </tr>
            </thead>
            <tbody>
              {users.length === 0 && (
                <tr>
                  <td colSpan={6} className="p-4 text-center text-[var(--txt-muted)]">
                    {loading ? 'Загрузка…' : 'Пользователей пока нет (появятся после входа через Telegram Mini App)'}
                  </td>
                </tr>
              )}
              {users.map((u) => {
                const id = u.telegram_id
                const label = u.username ? `@${u.username}` : (u.first_name || id)
                return (
                  <tr key={id} className="border-b border-[var(--border)]/60 hover:bg-[var(--bg)]/40">
                    <td className="p-2">
                      <div className="font-semibold text-[var(--txt)]">{label}</div>
                      <div className="mono text-[var(--txt-muted)]">{id}</div>
                    </td>
                    <td className="p-2">
                      <div className="font-medium">{u.plan || 'free'}</div>
                      <div className={u.active ? 'text-[var(--profit)]' : 'text-[var(--txt-muted)]'}>
                        {u.active ? 'active' : '—'}
                        {u.active_until ? ` · до ${String(u.active_until).slice(0, 10)}` : ''}
                      </div>
                    </td>
                    <td className="p-2">
                      {u.creds_configured ? (
                        <span className="text-[var(--profit)] font-semibold">ключи есть</span>
                      ) : (
                        <span className="text-[var(--txt-muted)]">не подключены</span>
                      )}
                    </td>
                    <td className="p-2">
                      <span className={`font-bold ${u.mode === 'live' ? 'text-[var(--loss)]' : 'text-[var(--warn)]'}`}>
                        {(u.mode || 'demo').toUpperCase()}
                      </span>
                    </td>
                    <td className="p-2 mono text-[var(--txt-muted)]">
                      {(u.bots_running || []).length ? (u.bots_running || []).join(', ') : '—'}
                    </td>
                    <td className="p-2">
                      <div className="flex flex-wrap gap-1">
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={busy === `pro-${id}`}
                          title="Выдать Pro на 30 дней"
                          onClick={() => act(`pro-${id}`, () =>
                            api.adminUserPlan({ telegram_id: id, plan: 'pro', days: 30 })
                          )}
                        >
                          <CreditCard size={11} /> Pro 30д
                        </button>
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={busy === `free-${id}`}
                          onClick={() => act(`free-${id}`, () =>
                            api.adminUserPlan({ telegram_id: id, plan: 'free' })
                          )}
                        >
                          Free
                        </button>
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={busy === `demo-${id}` || !u.creds_configured}
                          onClick={() => act(`demo-${id}`, () =>
                            api.adminUserMode({ telegram_id: id, mode: 'demo' })
                          )}
                        >
                          <ToggleLeft size={11} /> Demo
                        </button>
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={busy === `live-${id}` || !u.creds_configured}
                          onClick={() => {
                            if (!window.confirm(`Перевести ${label} в LIVE?`)) return
                            act(`live-${id}`, () =>
                              api.adminUserMode({ telegram_id: id, mode: 'live' })
                            )
                          }}
                        >
                          Live
                        </button>
                        <button
                          className="btn btn-danger btn-sm"
                          disabled={busy === `clr-${id}` || !u.creds_configured}
                          onClick={() => {
                            if (!window.confirm(`Удалить OKX ключи у ${label}?`)) return
                            act(`clr-${id}`, () =>
                              api.adminClearCreds({ telegram_id: id })
                            )
                          }}
                        >
                          Сброс ключей
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="text-2xs text-[var(--txt-muted)] leading-relaxed max-w-3xl">
        <strong className="text-[var(--txt)]">Как устроено:</strong>{' '}
        витрина (env OKX DEMO) — для гостей и превью. Пользователь подключает <em>свои</em> API-ключи
        в настройках / Mini App. Режим <strong>Live</strong> сохраняется только при активной подписке
        (signals/pro). Торговля бота идёт на счёте пользователя, не на витрине.
      </div>
    </div>
  )
}
