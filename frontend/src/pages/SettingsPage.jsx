import React, { useState, useEffect } from 'react'
import { Key, Shield, CheckCircle, XCircle, Loader2, Eye, EyeOff, Wifi } from 'lucide-react'
import { api } from '../services/api'
import { useTranslation } from '../hooks/useTranslation'

export default function SettingsPage({ onConnected, onDemoMode }) {
  const { t } = useTranslation()
  const [form, setForm] = useState({
    api_key: '',
    secret_key: '',
    passphrase: '',
    demo: true,
  })
  const [backendConfig, setBackendConfig] = useState(null)
  const [showSecret, setShowSecret] = useState(false)
  const [testing, setTesting] = useState(false)
  const [status, setStatus] = useState(null)

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

  const handleTest = async () => {
    setTesting(true)
    setStatus(null)
    try {
      const result = await api.testCredentials(form)
      setStatus({ ok: true, message: t('settings.success_test') })
      onConnected?.(true)
      onDemoMode?.(form.demo)
    } catch (err) {
      setStatus({ ok: false, message: err.message })
    }
    setTesting(false)
  }

  const handleSave = async () => {
    setTesting(true)
    setStatus(null)
    try {
      await api.initCredentials(form)
      setStatus({ ok: true, message: t('settings.success_save') })
      onConnected?.(true)
      onDemoMode?.(form.demo)
    } catch (err) {
      setStatus({ ok: false, message: err.message })
    }
    setTesting(false)
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-white">{t('settings.title')}</h2>
        <p className="text-sm text-gray-400 mt-1">{t('settings.subtitle')}</p>
      </div>

      <div className="glass p-6 space-y-5">
        <div className="flex items-center gap-3 pb-4 border-b border-white/5">
          <div className="w-10 h-10 rounded-lg bg-neon-green/10 flex items-center justify-center">
            <Key size={20} className="text-neon-green" />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-white">{t('settings.api_credentials')}</h3>
            <p className="text-xs text-gray-500">{t('settings.keys_stored')}</p>
          </div>
          {backendConfig?.has_credentials && (
            <div className="ml-auto flex items-center gap-2 px-3 py-1.5 rounded-lg bg-neon-green/5 border border-neon-green/20">
              <span className="status-dot online" />
              <span className="text-xs text-neon-green font-medium">{t('settings.connected')}</span>
            </div>
          )}
        </div>

        {backendConfig?.env_configured && (
          <div className="bg-neon-blue/5 border border-neon-blue/20 rounded-xl p-3 flex items-center gap-3">
            <CheckCircle size={16} className="text-neon-blue shrink-0" />
            <div className="text-sm text-gray-300">
              {t('settings.env_configured')} <code className="mono text-xs text-neon-blue">.env</code>
            </div>
            <button
              className="ml-auto text-xs text-neon-blue hover:text-white px-3 py-1.5 rounded-lg bg-white/5 transition-colors"
              onClick={() => {
                setForm(f => ({ ...f, demo: backendConfig.env_demo }))
                setStatus({ ok: true, message: t('settings.connected_env') })
              }}
            >
              {t('settings.use_env_config')}
            </button>
          </div>
        )}

        <div className="space-y-4">
          <div>
            <label className="text-xs font-medium text-gray-400 uppercase tracking-wider">{t('settings.api_key_label')}</label>
            <input
              type="text"
              className="w-full mt-1"
              placeholder={t('settings.api_key_placeholder')}
              value={form.api_key}
              onChange={e => setForm({ ...form, api_key: e.target.value })}
            />
          </div>

          <div>
            <label className="text-xs font-medium text-gray-400 uppercase tracking-wider">{t('settings.secret_key_label')}</label>
            <div className="relative mt-1">
              <input
                type={showSecret ? 'text' : 'password'}
                className="w-full pr-10"
                placeholder={t('settings.secret_key_placeholder')}
                value={form.secret_key}
                onChange={e => setForm({ ...form, secret_key: e.target.value })}
              />
              <button
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-white"
                onClick={() => setShowSecret(!showSecret)}
              >
                {showSecret ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          <div>
            <label className="text-xs font-medium text-gray-400 uppercase tracking-wider">{t('settings.passphrase_label')}</label>
            <input
              type="text"
              className="w-full mt-1"
              placeholder={t('settings.passphrase_placeholder')}
              value={form.passphrase}
              onChange={e => setForm({ ...form, passphrase: e.target.value })}
            />
          </div>

          <label className="flex items-center gap-3 pt-2">
            <input
              type="checkbox"
              className="form-checkbox"
              checked={form.demo}
              onChange={e => setForm({ ...form, demo: e.target.checked })}
            />
            <div>
              <span className="text-sm text-white font-medium">{t('settings.demo_mode')}</span>
              <p className="text-xs text-gray-400">{t('settings.demo_description')}</p>
            </div>
          </label>
        </div>

        <div className="flex gap-3">
          <button
            className="btn-neon flex-1 py-2.5 rounded-xl text-sm font-semibold flex items-center justify-center gap-2"
            onClick={handleTest}
            disabled={testing}
          >
            {testing ? <Loader2 size={14} className="animate-spin" /> : <Wifi size={14} />}
            {testing ? t('settings.testing') : t('settings.test_connection')}
          </button>
          <button
            className="bg-white/10 text-white flex-1 py-2.5 rounded-xl text-sm font-semibold hover:bg-white/20 flex items-center justify-center gap-2"
            onClick={handleSave}
            disabled={testing}
          >
            {testing ? <Loader2 size={14} className="animate-spin" /> : <Key size={14} />}
            {testing ? t('settings.saving') : t('settings.save_connect')}
          </button>
        </div>

        {status && (
          <div className={`flex items-center gap-2 p-3 rounded-lg text-sm ${
            status.ok ? 'bg-neon-green/5 text-neon-green' : 'bg-neon-red/5 text-neon-red'
          }`}>
            {status.ok ? <CheckCircle size={14} /> : <XCircle size={14} />}
            {status.message}
          </div>
        )}
      </div>

      {/* Security */}
      <div className="glass p-6 space-y-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-neon-yellow/10 flex items-center justify-center">
            <Shield size={20} className="text-neon-yellow" />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-white">{t('settings.security_title')}</h3>
          </div>
        </div>
        <ul className="space-y-2 text-xs text-gray-400">
          <li className="flex items-start gap-2">
            <CheckCircle size={12} className="text-neon-green mt-0.5 shrink-0" />
            {t('settings.security_keys_local')}
          </li>
          <li className="flex items-start gap-2">
            <CheckCircle size={12} className="text-neon-green mt-0.5 shrink-0" />
            {t('settings.security_demo_first')}
          </li>
          <li className="flex items-start gap-2">
            <CheckCircle size={12} className="text-neon-green mt-0.5 shrink-0" />
            {t('settings.security_never_logged')}
          </li>
          <li className="flex items-start gap-2">
            <CheckCircle size={12} className="text-neon-green mt-0.5 shrink-0" />
            {t('settings.security_rotate')}
          </li>
        </ul>
      </div>
    </div>
  )
}
