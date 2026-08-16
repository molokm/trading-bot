import React, { useState, useEffect, useRef } from 'react'
import { Key, Shield, CheckCircle, XCircle, Loader2, Eye, EyeOff, Wifi, Trash2, AlertTriangle, Send, MessageCircle, RotateCw, Download, Users, Star, RefreshCw } from 'lucide-react'
import { api } from '../services/api'
import { MetricCard, Tip } from '../components/ui'
import { useTranslation } from '../hooks/useTranslation'

export default function SettingsPage({ onConnected, onDemoMode }) {
  const { t } = useTranslation()
  const [form, setForm] = useState({ api_key: '', secret_key: '', passphrase: '', demo: true })
  const [backendConfig, setBackendConfig] = useState(null)
  const [showSecret, setShowSecret] = useState(false)
  const [testing, setTesting] = useState(false)
  const [status, setStatus] = useState(null)
  const [testSteps, setTestSteps] = useState([])
  const [dangerConfirm, setDangerConfirm] = useState(false)
  const [risk, setRisk] = useState(null)
  const [riskBusy, setRiskBusy] = useState(false)
  const [tg, setTg] = useState({ token: '', chat_id: '', channel_id: '', configured: false, status: 'no_token', token_masked: '', loaded: false })
  const [tgTesting, setTgTesting] = useState(false)
  const [tgSaving, setTgSaving] = useState(false)
  const [tgStatus, setTgStatus] = useState(null)
  const [logDownloading, setLogDownloading] = useState(false)
  const [logStatus, setLogStatus] = useState(null)
  const [subs, setSubs] = useState(null)
  const [subsConfig, setSubsConfig] = useState(null)
  const [subsLoading, setSubsLoading] = useState(false)
  const [subsAction, setSubsAction] = useState(null)
  const timersRef = useRef([])

  useEffect(() => {
    api.health().then(h => {
      if (h.env_configured) {
        setBackendConfig(h)
        setForm(f => ({ ...f, demo: h.env_demo }))
        if (h.has_credentials) {
          setStatus({ ok: true, message: t('settings.connected_env') })
          onConnected?.(true)
          onDemoMode?.(h.env_demo)
        }
      }
    }).catch(() => {})
  }, [])

  useEffect(() => {
    api.telegramStatus().then(s => {
      setTg({ ...s, loaded: true })
    }).catch(() => {})
  }, [])

  useEffect(() => {
    api.riskStatus().then(setRisk).catch(() => setRisk(null))
  }, [])

  const toggleKill = async (enabled) => {
    setRiskBusy(true)
    try {
      const r = await api.riskKill(enabled)
      setRisk(r)
    } catch (e) {
      console.error(e)
    }
    setRiskBusy(false)
  }

  const loadSubs = async () => {
    setSubsLoading(true)
    try {
      const [s, c] = await Promise.all([
        api.subsList().catch(() => null),
        api.subsConfig().catch(() => null),
      ])
      setSubs(s)
      setSubsConfig(c)
    } catch (e) { /* admin-only, ignore */ }
    setSubsLoading(false)
  }

  useEffect(() => { loadSubs() }, [])

  const clearTimers = () => {
    timersRef.current.forEach(t => clearTimeout(t))
    timersRef.current = []
  }

  const handleTest = async () => {
    setTesting(true); setStatus(null); setDangerConfirm(false)
    setTestSteps([
      { label: t('settings.connecting'), state: 'active' },
      { label: t('settings.authenticating'), state: 'pending' },
      { label: t('settings.testing'), state: 'pending' },
    ])
    const t1 = setTimeout(() => {
      setTestSteps(prev => [
        { ...prev[0], state: 'done' },
        { ...prev[1], state: 'active' },
        { ...prev[2], state: 'pending' },
      ])
    }, 800)
    const t2 = setTimeout(() => {
      setTestSteps(prev => [
        { ...prev[0], state: 'done' },
        { ...prev[1], state: 'done' },
        { ...prev[2], state: 'active' },
      ])
    }, 1600)
    timersRef.current = [t1, t2]

    try {
      await api.testCredentials(form)
      const t3 = setTimeout(() => {
        setTestSteps(prev => [
          { ...prev[0], state: 'done' },
          { ...prev[1], state: 'done' },
          { ...prev[2], state: 'done' },
        ])
      }, 400)
      const t4 = setTimeout(() => {
        setTestSteps([])
        setStatus({ ok: true, message: t('settings.connect_success') })
        onConnected?.(true); onDemoMode?.(form.demo)
        setTesting(false)
      }, 1200)
      timersRef.current.push(t3, t4)
    } catch (err) {
      const t5 = setTimeout(() => {
        setTestSteps(prev => prev.map((s, i) => i === 2 ? { ...s, state: 'error' } : { ...s, state: 'done' }))
      }, 400)
      const t6 = setTimeout(() => {
        setTestSteps([])
        setStatus({ ok: false, message: err.message })
        setTesting(false)
      }, 1200)
      timersRef.current.push(t5, t6)
    }
  }

  const handleSave = async () => {
    setTesting(true); setStatus(null)
    try {
      await api.initCredentials(form)
      setStatus({ ok: true, message: t('settings.keys_saved') })
      onConnected?.(true); onDemoMode?.(form.demo)
    } catch (err) { setStatus({ ok: false, message: err.message }) }
    setTesting(false)
  }

  const handleSaveTelegram = async () => {
    setTgSaving(true); setTgStatus(null)
    try {
      const s = await api.telegramConfig({
        token: tg.token, chat_id: tg.chat_id, channel_id: tg.channel_id || undefined,
      })
      setTg({ ...tg, token: '', chat_id: '', ...s, loaded: true })
      setTgStatus({ ok: true, message: t('settings.tg_saved') })
      loadSubs()
    } catch (err) { setTgStatus({ ok: false, message: err.message }) }
    setTgSaving(false)
  }

  const handleSubsActivate = async (userId) => {
    if (!userId) return
    setSubsAction(userId)
    const days = prompt(t('settings.subs_activate_prompt'), subsConfig?.pro_plan_days || 30)
    if (!days) { setSubsAction(null); return }
    try {
      await api.subsActivate({ user_id: userId, days: parseInt(days, 10) || 30 })
      await loadSubs()
    } catch (err) { alert(err.message) }
    setSubsAction(null)
  }

  const handleSubsDeactivate = async (userId) => {
    if (!userId) return
    if (!window.confirm(t('settings.subs_deactivate_confirm'))) return
    setSubsAction(userId)
    try {
      await api.subsDeactivate({ user_id: userId })
      await loadSubs()
    } catch (err) { alert(err.message) }
    setSubsAction(null)
  }

  const handleTestTelegram = async () => {
    setTgTesting(true); setTgStatus(null)
    try {
      const payload = {}
      if (tg.token) payload.token = tg.token
      if (tg.chat_id) payload.chat_id = tg.chat_id
      const r = await api.telegramTest(payload)
      setTgStatus({ ok: r.ok, message: r.message })
    } catch (err) { setTgStatus({ ok: false, message: err.message }) }
    setTgTesting(false)
  }

  const handleSimulateTelegram = async () => {
    setTgTesting(true); setTgStatus(null)
    try {
      const payload = {}
      if (tg.token) payload.token = tg.token
      if (tg.chat_id) payload.chat_id = tg.chat_id
      const r = await api.telegramSimulate(payload)
      setTgStatus({ ok: r.ok, message: r.message })
    } catch (err) { setTgStatus({ ok: false, message: err.message }) }
    setTgTesting(false)
  }

  const handleDownloadLog = async () => {
    setLogDownloading(true); setLogStatus(null)
    try {
      const blob = await api.downloadAnalysisLog()
      const name = blob.name || `analysis_${new Date().toISOString().slice(0, 10)}.jsonl`
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      setLogStatus({ ok: true, message: t('settings.log_downloaded') })
    } catch (err) {
      setLogStatus({ ok: false, message: err.message || t('settings.log_failed') })
    }
    setLogDownloading(false)
  }

  const tgReady = tg.configured || (tg.token && tg.chat_id)

  const tgBadge = () => {
    if (!tg.configured) return <span className="ml-auto status-badge status-off"><span className="dot" /> {t('settings.tg_off')}</span>
    if (tg.status === 'no_token') return <span className="ml-auto status-badge status-off"><span className="dot" /> {t('settings.tg_no_token')}</span>
    return <span className="ml-auto status-badge status-live"><span className="dot" /> {t('settings.tg_on')}</span>
  }

  const handleClearCredentials = () => {
    if (!dangerConfirm) {
      setDangerConfirm(true)
      return
    }
    localStorage.removeItem('okx_api_key')
    localStorage.removeItem('okx_secret_key')
    localStorage.removeItem('okx_passphrase')
    localStorage.removeItem('okx_demo')
    setDangerConfirm(false)
    setStatus({ ok: false, message: t('settings.keys_deleted') })
    onConnected?.(false)
  }

  const stepIcon = (s) => {
    if (s.state === 'done') return <CheckCircle size={12} className="text-[var(--profit)]" />
    if (s.state === 'error') return <XCircle size={12} className="text-[var(--loss)]" />
    if (s.state === 'active') return <Loader2 size={12} className="text-[var(--info)] animate-spin" />
    return <div className="w-3 h-3 rounded-full border border-[var(--border)]" />
  }

  return (
    <div className="h-full flex flex-col p-4 gap-4 overflow-auto">
      <div>
        <h2 className="text-lg font-bold text-[var(--txt)]">{t('settings.title')}</h2>
        <p className="text-xs text-[var(--txt-muted)]">{t('settings.subtitle')}</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-4">
        {/* API Credentials */}
        <div className="space-y-4">
          <div className="panel">
            <div className="panel-header">
              <Key size={13} className="text-[var(--profit)]" /> {t('settings.api_keys')}
              {backendConfig?.has_credentials && (
                <span className="ml-auto status-badge status-live"><span className="dot" /> {t('settings.connected')}</span>
              )}
            </div>
            <div className="p-4 space-y-4">
              {backendConfig?.env_configured && (
                <div className="flex items-center gap-3 p-3 rounded-lg bg-[var(--info-dim)] border border-[var(--info)]/20">
                  <CheckCircle size={16} className="text-[var(--info)] shrink-0" />
                  <div className="text-xs text-[var(--txt-secondary)]">
                    {t('settings.connected_via')} <code className="mono text-2xs text-[var(--info)]">.env</code>
                  </div>
                </div>
              )}

              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider flex items-center gap-1">
                  API Key <Tip text={t('settings.get_key_tip')} />
                </label>
                <input className="w-full mt-1.5" placeholder="OKX API Key" value={form.api_key} onChange={e => setForm({ ...form, api_key: e.target.value })} />
              </div>

              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">Secret Key</label>
                <div className="relative mt-1.5">
                  <input type={showSecret ? 'text' : 'password'} className="w-full pr-10" placeholder="OKX Secret Key" value={form.secret_key} onChange={e => setForm({ ...form, secret_key: e.target.value })} />
                  <button className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--txt-muted)] hover:text-[var(--txt)]" onClick={() => setShowSecret(!showSecret)}>
                    {showSecret ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </div>

              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">{t('settings.passphrase')}</label>
                <input className="w-full mt-1.5" placeholder="OKX Passphrase" value={form.passphrase} onChange={e => setForm({ ...form, passphrase: e.target.value })} />
              </div>

              <label className="flex items-center gap-3 pt-2">
                <input type="checkbox" checked={form.demo} onChange={e => setForm({ ...form, demo: e.target.checked })} />
                <div>
                  <span className="text-sm text-[var(--txt)] font-medium">{t('settings.demo_mode')}</span>
                  <p className="text-2xs text-[var(--txt-muted)]">{t('settings.demo_tip')}</p>
                </div>
              </label>

              <div className="flex gap-3 pt-2">
                <button className="btn btn-primary flex-1" onClick={handleTest} disabled={testing}>
                  {testing ? <Loader2 size={14} className="animate-spin" /> : <Wifi size={14} />}
                  {t('settings.test')}
                </button>
                <button className="btn btn-ghost flex-1" onClick={handleSave} disabled={testing}>
                  {testing ? <Loader2 size={14} className="animate-spin" /> : <Key size={14} />}
                  {t('settings.save')}
                </button>
              </div>

              {/* Animated test steps */}
              {testSteps.length > 0 && (
                <div className="flex items-center gap-4 p-3 rounded-lg bg-[var(--bg)] border border-[var(--border)]">
                  {testSteps.map((step, i) => (
                    <React.Fragment key={i}>
                      <div className="flex items-center gap-1.5">
                        {stepIcon(step)}
                        <span className={`text-2xs ${step.state === 'active' ? 'text-[var(--info)]' : step.state === 'error' ? 'text-[var(--loss)]' : step.state === 'done' ? 'text-[var(--profit)]' : 'text-[var(--txt-muted)]'}`}>
                          {step.label}
                        </span>
                      </div>
                      {i < testSteps.length - 1 && <div className="w-4 h-px bg-[var(--border)]" />}
                    </React.Fragment>
                  ))}
                </div>
              )}

              {status && (
                <div className={`flex items-center gap-2 p-3 rounded-lg text-xs ${status.ok ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                  {status.ok ? <CheckCircle size={14} /> : <XCircle size={14} />}
                  {status.message}
                </div>
              )}
            </div>
          </div>

          {/* Telegram Notifications */}
          <div className="panel">
            <div className="panel-header">
              <MessageCircle size={13} className="text-[var(--info)]" /> {t('settings.tg_title')}
              {tgBadge()}
            </div>
            <div className="p-4 space-y-4">
              <p className="text-2xs text-[var(--txt-muted)]">{t('settings.tg_tip')}</p>

              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">Bot Token</label>
                <input
                  className="w-full mt-1.5 mono text-2xs"
                  type="password"
                  placeholder="123456789:AAbb..."
                  value={tg.token}
                  onChange={e => setTg({ ...tg, token: e.target.value })}
                />
              </div>

              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">Chat ID</label>
                <input
                  className="w-full mt-1.5 mono text-2xs"
                  placeholder="123456789"
                  value={tg.chat_id}
                  onChange={e => setTg({ ...tg, chat_id: e.target.value })}
                />
              </div>

              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">
                  {t('settings.tg_channel')} <Tip text={t('settings.tg_channel_tip')} />
                </label>
                <input
                  className="w-full mt-1.5 mono text-2xs"
                  placeholder="-1001234567890"
                  value={tg.channel_id || ''}
                  onChange={e => setTg({ ...tg, channel_id: e.target.value })}
                />
              </div>

              {tg.configured && (
                <div className="flex items-center gap-3 p-3 rounded-lg bg-[var(--info-dim)] border border-[var(--info)]/20">
                  <CheckCircle size={16} className="text-[var(--info)] shrink-0" />
                  <div className="text-2xs text-[var(--txt-secondary)]">
                    {t('settings.tg_configured_to')} <code className="mono text-[var(--info)]">{tg.chat_id || '—'}</code>
                    {tg.token_masked && <div>{t('settings.tg_token')} <code className="mono">{tg.token_masked}</code></div>}
                  </div>
                </div>
              )}

              <div className="flex gap-3">
                <button className="btn btn-primary flex-1" onClick={handleTestTelegram} disabled={tgTesting || !tgReady}>
                  {tgTesting ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
                  {t('settings.tg_test')}
                </button>
                <button className="btn btn-ghost flex-1" onClick={handleSaveTelegram} disabled={tgSaving}>
                  {tgSaving ? <Loader2 size={14} className="animate-spin" /> : <RotateCw size={14} />}
                  {t('settings.tg_save')}
                </button>
              </div>
              <div>
                <button className="btn btn-ghost w-full" onClick={handleSimulateTelegram} disabled={tgTesting || !tgReady}>
                  {tgTesting ? <Loader2 size={14} className="animate-spin" /> : <MessageCircle size={14} />}
                  {t('settings.tg_simulate')}
                </button>
              </div>

              {tgStatus && (
                <div className={`flex items-center gap-2 p-3 rounded-lg text-xs ${tgStatus.ok ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                  {tgStatus.ok ? <CheckCircle size={14} /> : <XCircle size={14} />}
                  {tgStatus.message}
                </div>
              )}
            </div>
          </div>

          {/* Paid Signal Subscriptions */}
          <div className="panel">
            <div className="panel-header">
              <Users size={13} className="text-[var(--profit)]" /> {t('settings.subs_title')}
              <button className="ml-auto btn btn-ghost btn-sm" onClick={loadSubs} disabled={subsLoading}>
                <RefreshCw size={12} className={subsLoading ? 'animate-spin' : ''} /> {t('settings.subs_refresh')}
              </button>
            </div>
            <div className="p-4 space-y-4">
              <p className="text-2xs text-[var(--txt-muted)]">{t('settings.subs_tip')}</p>

              {subsConfig && (
                <div className="flex flex-wrap items-center gap-2 text-2xs">
                  <span className={`status-badge ${subsConfig.poller_running ? 'status-live' : 'status-off'}`}>
                    <span className="dot" />
                    {subsConfig.poller_running ? t('settings.subs_poller_on') : t('settings.subs_poller_off')}
                  </span>
                  <span className="status-badge status-live">
                    <Star size={10} /> PRO {subsConfig.pro_price_stars} ⭐ / {subsConfig.pro_plan_days} {t('settings.subs_days')}
                  </span>
                  <span className="status-badge status-live">
                    {t('settings.subs_signals_free')}
                  </span>
                </div>
              )}

              {subs?.stats && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  <MetricCard
                    label={t('settings.subs_active')}
                    value={subs.stats.active}
                    icon={<Users size={14} />}
                  />
                  <MetricCard
                    label={t('settings.subs_total')}
                    value={subs.stats.total}
                    icon={<RefreshCw size={14} />}
                  />
                  <MetricCard
                    label={t('settings.subs_revenue')}
                    value={`${subs.stats.revenue_stars} ⭐`}
                    icon={<Star size={14} />}
                  />
                  <MetricCard
                    label="PRO"
                    value={`${subs.stats.revenue_pro ?? 0} ⭐`}
                    icon={<Star size={14} />}
                  />
                </div>
              )}

              {subs?.subscribers?.length > 0 ? (
                <div className="space-y-2">
                  {subs.subscribers.map(s => (
                    <div key={s.user_id} className="flex items-center justify-between gap-3 p-3 rounded-lg bg-[var(--bg)] border border-[var(--border)]">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-xs font-medium text-[var(--txt)]">
                          <span className="truncate">{s.username ? '@' + s.username : s.first_name || s.user_id}</span>
                          <span className={`status-badge ${s.status === 'active' ? 'status-live' : 'status-off'}`}>
                            <span className="dot" /> {s.status === 'active' ? t('settings.subs_active_short') : t('settings.subs_expired')}
                          </span>
                        </div>
                        <div className="text-2xs text-[var(--txt-muted)] mt-0.5 truncate">
                          {t('settings.subs_until')} {s.active_until || '—'}
                        </div>
                      </div>
                      <div className="flex items-center gap-1.5 shrink-0">
                        <button
                          className="btn btn-ghost btn-sm"
                          onClick={() => handleSubsActivate(s.user_id)}
                          disabled={subsAction === s.user_id}
                        >
                          {t('settings.subs_extend')}
                        </button>
                        <button
                          className="btn btn-ghost btn-sm text-[var(--loss)]"
                          onClick={() => handleSubsDeactivate(s.user_id)}
                          disabled={subsAction === s.user_id}
                        >
                          {t('settings.subs_off')}
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-2xs text-[var(--txt-muted)] text-center py-4">
                  {t('settings.subs_empty')}
                </div>
              )}

              {(subs?.users?.length > 0) && (
                <div>
                  <div className="text-2xs font-semibold uppercase tracking-wider text-[var(--txt-muted)] mb-2">
                    {t('settings.subs_users')}
                  </div>
                  <div className="space-y-2">
                    {subs.users.map(u => (
                      <div key={u.telegram_id} className="flex items-center justify-between gap-3 p-3 rounded-lg bg-[var(--bg)] border border-[var(--border)]">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2 text-xs font-medium text-[var(--txt)]">
                            <span className="truncate">{u.username ? '@' + u.username : u.first_name || u.telegram_id}</span>
                            <span className={`px-1.5 py-0.5 rounded text-2xs font-bold ${
                              u.plan === 'pro' ? 'bg-[var(--info-dim)] text-[var(--info)]' : 'bg-[var(--surface-overlay)] text-[var(--txt-muted)]'
                            }`}>
                              {u.plan}
                            </span>
                          </div>
                          <div className="text-2xs text-[var(--txt-muted)] mt-0.5 truncate">
                            {u.creds_configured ? t('settings.subs_keys_on') : t('settings.subs_keys_off')}
                            {u.active_until ? ` • ${t('settings.subs_until')} ${u.active_until}` : ''}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Analysis Log Download */}
          <div className="panel">
            <div className="panel-header">
              <Download size={13} className="text-[var(--info)]" /> {t('settings.log_title')}
            </div>
            <div className="p-4 space-y-4">
              <p className="text-2xs text-[var(--txt-muted)]">{t('settings.log_tip')}</p>
              <button className="btn btn-primary w-full" onClick={handleDownloadLog} disabled={logDownloading}>
                {logDownloading ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                {t('settings.log_download')}
              </button>
              {logStatus && (
                <div className={`flex items-center gap-2 p-3 rounded-lg text-xs ${logStatus.ok ? 'bg-[var(--profit-dim)] text-[var(--profit)]' : 'bg-[var(--loss-dim)] text-[var(--loss)]'}`}>
                  {logStatus.ok ? <CheckCircle size={14} /> : <XCircle size={14} />}
                  {logStatus.message}
                </div>
              )}
            </div>
          </div>

          {/* Danger Zone */}
          <div className="panel border-[var(--loss)]/30">
            
        {/* Risk guards */}
        <div className="panel p-4 space-y-3">
          <div className="flex items-center gap-2 text-sm font-medium text-[var(--txt)]">
            <Shield size={13} className="text-[var(--warn)]" /> {t('settings.risk_title')}
          </div>
          <p className="text-2xs text-[var(--txt-muted)]">{t('settings.risk_tip')}</p>
          {risk ? (
            <div className="grid grid-cols-2 gap-2 text-2xs">
              <div className="rounded-lg border border-[var(--border)] p-2">
                <div className="text-[var(--txt-muted)]">{t('settings.risk_kill')}</div>
                <div className={`font-semibold ${risk.kill_switch ? 'text-[var(--loss)]' : 'text-[var(--profit)]'}`}>
                  {risk.kill_switch ? t('settings.risk_on') : t('settings.risk_off')}
                </div>
              </div>
              <div className="rounded-lg border border-[var(--border)] p-2">
                <div className="text-[var(--txt-muted)]">DEMO</div>
                <div className="font-semibold">{risk.okx_demo ? 'yes' : 'no'}</div>
              </div>
              <div className="rounded-lg border border-[var(--border)] p-2">
                <div className="text-[var(--txt-muted)]">{t('settings.risk_max_daily')}</div>
                <div className="mono">{risk.max_daily_loss_usd > 0 ? `$${risk.max_daily_loss_usd}` : '—'}</div>
                {risk.daily_pnl_usd != null && (
                  <div className={`text-[10px] mt-0.5 ${risk.daily_pnl_usd >= 0 ? 'text-[var(--profit)]' : 'text-[var(--loss)]'}`}>
                    today {risk.daily_pnl_usd >= 0 ? '+' : ''}{Number(risk.daily_pnl_usd).toFixed(2)}
                  </div>
                )}
              </div>
              <div className="rounded-lg border border-[var(--border)] p-2">
                <div className="text-[var(--txt-muted)]">{t('settings.risk_max_pos')}</div>
                <div className="mono">{risk.max_position_usd > 0 ? `$${risk.max_position_usd}` : '—'}</div>
              </div>
            </div>
          ) : (
            <div className="text-2xs text-[var(--txt-muted)]">—</div>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              className="btn btn-danger btn-sm"
              disabled={riskBusy || risk?.kill_switch}
              onClick={() => toggleKill(true)}
            >
              {t('settings.risk_enable_kill')}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={riskBusy || !risk?.kill_switch}
              onClick={() => toggleKill(false)}
            >
              {t('settings.risk_disable_kill')}
            </button>
          </div>
        </div>

<div className="panel-header border-b-[var(--loss)]/20">
              <AlertTriangle size={13} className="text-[var(--loss)]" /> {t('settings.danger_zone')}
            </div>
            <div className="p-4 flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-medium text-[var(--txt)]">{t('settings.delete_keys')}</p>
                <p className="text-2xs text-[var(--txt-muted)] mt-1">{t('settings.delete_tip')}</p>
              </div>
              <button
                className={`btn btn-sm shrink-0 ${dangerConfirm ? 'btn-danger' : 'btn-ghost'}`}
                onClick={handleClearCredentials}
              >
                <Trash2 size={12} />
                {dangerConfirm ? t('settings.confirm_delete') : t('settings.delete')}
              </button>
            </div>
          </div>
        </div>

        {/* Security Info */}
        <div className="space-y-3">
          <div className="panel">
            <div className="panel-header"><Shield size={13} className="text-[var(--warn)]" /> {t('settings.security')}</div>
            <div className="p-4 space-y-3">
              {[t('settings.security_tip_1'), t('settings.security_tip_2'), t('settings.security_tip_3'), t('settings.security_tip_4')].map((text, i) => (
                <div key={i} className="flex items-start gap-2 text-xs text-[var(--txt-secondary)]">
                  <CheckCircle size={12} className="text-[var(--profit)] mt-0.5 shrink-0" />
                  {text}
                </div>
              ))}
            </div>
          </div>

          <div className="panel">
            <div className="panel-header">{t('settings.recommendations')}</div>
            <div className="p-4 space-y-2 text-2xs text-[var(--txt-muted)] leading-relaxed">
              <p>1. {t('settings.recommendation_1')}</p>
              <p>2. {t('settings.recommendation_2')}</p>
              <p>3. {t('settings.recommendation_3')}</p>
              <p>4. {t('settings.recommendation_4')}</p>
              <p>5. {t('settings.recommendation_5')}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
