import React, { useState, useEffect, useRef } from 'react'
import { Key, Shield, CheckCircle, XCircle, Loader2, Eye, EyeOff, Wifi, Trash2, AlertTriangle } from 'lucide-react'
import { api } from '../services/api'
import { MetricCard, Tip } from '../components/ui'

const TEST_STEP_LABELS = ['Подключение...', 'Аутентификация...', 'Проверка...']

export default function SettingsPage({ onConnected, onDemoMode }) {
  const [form, setForm] = useState({ api_key: '', secret_key: '', passphrase: '', demo: true })
  const [backendConfig, setBackendConfig] = useState(null)
  const [showSecret, setShowSecret] = useState(false)
  const [testing, setTesting] = useState(false)
  const [status, setStatus] = useState(null)
  const [testSteps, setTestSteps] = useState([])
  const [dangerConfirm, setDangerConfirm] = useState(false)
  const timersRef = useRef([])

  useEffect(() => {
    api.health().then(h => {
      if (h.env_configured) {
        setBackendConfig(h)
        setForm(f => ({ ...f, demo: h.env_demo }))
        if (h.has_credentials) {
          setStatus({ ok: true, message: 'Подключено через .env' })
          onConnected?.(true)
          onDemoMode?.(h.env_demo)
        }
      }
    }).catch(() => {})
  }, [])

  const clearTimers = () => {
    timersRef.current.forEach(t => clearTimeout(t))
    timersRef.current = []
  }

  const handleTest = async () => {
    setTesting(true); setStatus(null); setDangerConfirm(false)
    setTestSteps([
      { label: TEST_STEP_LABELS[0], state: 'active' },
      { label: TEST_STEP_LABELS[1], state: 'pending' },
      { label: TEST_STEP_LABELS[2], state: 'pending' },
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
        setStatus({ ok: true, message: 'Подключение успешно!' })
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
      setStatus({ ok: true, message: 'Ключи сохранены и подключены.' })
      onConnected?.(true); onDemoMode?.(form.demo)
    } catch (err) { setStatus({ ok: false, message: err.message }) }
    setTesting(false)
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
    setStatus({ ok: false, message: 'Все сохранённые учётные данные удалены.' })
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
        <h2 className="text-lg font-bold text-[var(--txt)]">Настройки</h2>
        <p className="text-xs text-[var(--txt-muted)]">Подключение к OKX API и параметры безопасности</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-4">
        {/* API Credentials */}
        <div className="space-y-4">
          <div className="panel">
            <div className="panel-header">
              <Key size={13} className="text-[var(--profit)]" /> API Ключи
              {backendConfig?.has_credentials && (
                <span className="ml-auto status-badge status-live"><span className="dot" /> Подключено</span>
              )}
            </div>
            <div className="p-4 space-y-4">
              {backendConfig?.env_configured && (
                <div className="flex items-center gap-3 p-3 rounded-lg bg-[var(--info-dim)] border border-[var(--info)]/20">
                  <CheckCircle size={16} className="text-[var(--info)] shrink-0" />
                  <div className="text-xs text-[var(--txt-secondary)]">
                    Ключи настроены через <code className="mono text-2xs text-[var(--info)]">.env</code>
                  </div>
                </div>
              )}

              <div>
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider flex items-center gap-1">
                  API Key <Tip text="Получите в OKX → API → Создать API ключ" />
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
                <label className="text-2xs font-medium text-[var(--txt-muted)] uppercase tracking-wider">Парольная фраза</label>
                <input className="w-full mt-1.5" placeholder="OKX Passphrase" value={form.passphrase} onChange={e => setForm({ ...form, passphrase: e.target.value })} />
              </div>

              <label className="flex items-center gap-3 pt-2">
                <input type="checkbox" checked={form.demo} onChange={e => setForm({ ...form, demo: e.target.checked })} />
                <div>
                  <span className="text-sm text-[var(--txt)] font-medium">Демо-режим</span>
                  <p className="text-2xs text-[var(--txt-muted)]">Тестовая среда OKX с виртуальными средствами</p>
                </div>
              </label>

              <div className="flex gap-3 pt-2">
                <button className="btn btn-primary flex-1" onClick={handleTest} disabled={testing}>
                  {testing ? <Loader2 size={14} className="animate-spin" /> : <Wifi size={14} />}
                  Проверить
                </button>
                <button className="btn btn-ghost flex-1" onClick={handleSave} disabled={testing}>
                  {testing ? <Loader2 size={14} className="animate-spin" /> : <Key size={14} />}
                  Сохранить
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

          {/* Danger Zone */}
          <div className="panel border-[var(--loss)]/30">
            <div className="panel-header border-b-[var(--loss)]/20">
              <AlertTriangle size={13} className="text-[var(--loss)]" /> Опасная зона
            </div>
            <div className="p-4 flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-medium text-[var(--txt)]">Удалить сохранённые учётные данные</p>
                <p className="text-2xs text-[var(--txt-muted)] mt-1">Очистить все API ключи из localStorage браузера. Это действие необратимо.</p>
              </div>
              <button
                className={`btn btn-sm shrink-0 ${dangerConfirm ? 'btn-danger' : 'btn-ghost'}`}
                onClick={handleClearCredentials}
              >
                <Trash2 size={12} />
                {dangerConfirm ? 'Подтвердить удаление' : 'Удалить'}
              </button>
            </div>
          </div>
        </div>

        {/* Security Info */}
        <div className="space-y-3">
          <div className="panel">
            <div className="panel-header"><Shield size={13} className="text-[var(--warn)]" /> Безопасность</div>
            <div className="p-4 space-y-3">
              {[
                'Ключи хранятся только в вашем браузере (localStorage)',
                'Сначала тестируйте в демо-режиме',
                'Ключи не логируются и не передаются третьим лицам',
                'Регулярно обновляйте API ключи в OKX',
              ].map((text, i) => (
                <div key={i} className="flex items-start gap-2 text-xs text-[var(--txt-secondary)]">
                  <CheckCircle size={12} className="text-[var(--profit)] mt-0.5 shrink-0" />
                  {text}
                </div>
              ))}
            </div>
          </div>

          <div className="panel">
            <div className="panel-header">Рекомендации</div>
            <div className="p-4 space-y-2 text-2xs text-[var(--txt-muted)] leading-relaxed">
              <p>1. Создайте отдельный API ключ с минимальными правами (только торговля)</p>
              <p>2. Ограничьте IP-адреса в настройках API на OKX</p>
              <p>3. Используйте 2FA для аккаунта OKX</p>
              <p>4. Начинайте с малого — тестируйте на демо-счёте</p>
              <p>5. Не рискуйте средствами, которые не можете позволить себе потерять</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
