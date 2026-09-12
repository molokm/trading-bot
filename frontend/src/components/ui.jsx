import { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react'
import { X, Info, AlertTriangle, CheckCircle, XCircle, HelpCircle, Moon, Sun, ChevronRight, ChevronLeft, SkipForward } from 'lucide-react'
import { useTheme } from '../context/ThemeContext'
import { useOnboarding } from '../context/OnboardingContext'
import { useTranslation } from '../hooks/useTranslation'

/* ═══════ Tooltip ═══════ */
export function Tip({ text, className = '' }) {
  if (!text) return null
  return (
    <span className={`tip-wrap ${className}`}>
      <span className="tip-icon">?</span>
      <span className="tip-content">{text}</span>
    </span>
  )
}

/* ═══════ Status Badge ═══════ */
export function StatusBadge({ mode, label }) {
  const cls = mode === 'live' ? 'status-live'
    : mode === 'demo' ? 'status-demo'
    : mode === 'error' ? 'status-error'
    : mode === 'paused' ? 'status-paused'
    : 'status-stopped'
  return (
    <span className={`status-badge ${cls}`}>
      <span className="dot" />
      {label || mode.toUpperCase()}
    </span>
  )
}

/* ═══════ Metric Card ═══════ */
export function MetricCard({ label, value, change, changeType, tip, mono = true, sparkData, className }) {
  const color = changeType === 'positive' ? 'text-[var(--profit)]' : changeType === 'negative' ? 'text-[var(--loss)]' : 'text-[var(--txt-secondary)]'
  return (
    <div className={`metric-card ${className || ''}`}>
      <div className="flex items-center gap-1">
        <span className="label">{label}</span>
        {tip && <Tip text={tip} />}
      </div>
      <div className="flex items-end justify-between gap-2">
        <span className={`value ${mono ? 'mono' : ''}`}>{value}</span>
        {sparkData && sparkData.length > 1 && <SparklineSvg data={sparkData} />}
      </div>
      {change != null && (
        <span className={`change ${color}`}>
          {changeType === 'positive' ? '▲' : changeType === 'negative' ? '▼' : ''}
          {change}
        </span>
      )}
    </div>
  )
}

/* ═══════ Enhanced Metric Card — modern design with gradients ═══════ */
export function EnhancedMetricCard({ 
  label, 
  value, 
  change, 
  changeType, 
  tip, 
  mono = true, 
  sparkData, 
  className,
  icon: Icon,
  trend,
  subtitle
}) {
  const isPositive = changeType === 'positive'
  const isNegative = changeType === 'negative'
  
  const gradientClass = isPositive 
    ? 'from-[var(--profit)]/5 to-transparent' 
    : isNegative 
    ? 'from-[var(--loss)]/5 to-transparent'
    : 'from-[var(--info)]/5 to-transparent'
  
  const borderClass = isPositive
    ? 'border-[var(--profit)]/20 hover:border-[var(--profit)]/40'
    : isNegative
    ? 'border-[var(--loss)]/20 hover:border-[var(--loss)]/40'
    : 'border-[var(--border)] hover:border-[var(--border-hover)]'
  
  const iconColor = isPositive
    ? 'text-[var(--profit)]'
    : isNegative
    ? 'text-[var(--loss)]'
    : 'text-[var(--info)]'

  return (
    <div 
      className={`
        relative overflow-hidden rounded-xl border ${borderClass}
        bg-gradient-to-br ${gradientClass} backdrop-blur-sm
        p-4 transition-all duration-300 hover:scale-[1.02] hover:shadow-lg
        ${className || ''}
      `}
    >
      {/* Animated background gradient */}
      <div className="absolute inset-0 bg-gradient-to-br from-white/[0.02] to-transparent opacity-0 hover:opacity-100 transition-opacity duration-500" />
      
      <div className="relative z-10">
        {/* Header */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            {Icon && <Icon size={16} className={`${iconColor} opacity-80`} />}
            <span className="text-xs font-medium text-[var(--txt-secondary)] uppercase tracking-wider">
              {label}
            </span>
            {tip && <Tip text={tip} />}
          </div>
          {sparkData && sparkData.length > 1 && (
            <SparklineSvg data={sparkData} width={48} height={16} />
          )}
        </div>

        {/* Value */}
        <div className="flex items-baseline gap-3 mb-2">
          <span className={`text-2xl font-bold ${mono ? 'mono' : ''} text-[var(--txt)]`}>
            {value}
          </span>
          {change != null && (
            <span className={`text-sm font-semibold ${isPositive ? 'text-[var(--profit)]' : isNegative ? 'text-[var(--loss)]' : 'text-[var(--txt-secondary)]'}`}>
              {isPositive ? '↗' : isNegative ? '↘' : '→'} {change}
            </span>
          )}
        </div>

        {/* Subtitle or trend */}
        {(subtitle || trend) && (
          <div className="text-xs text-[var(--txt-muted)]">
            {subtitle || trend}
          </div>
        )}
      </div>
    </div>
  )
}

/* ═══════ Inline Sparkline SVG (used by MetricCard) ═══════ */
function SparklineSvg({ data, width = 60, height = 20 }) {
  if (!data || data.length < 2) return null
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const stepX = width / (data.length - 1)
  const pathD = data.map((v, i) => {
    const x = i * stepX
    const y = height - ((v - min) / range) * height
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const isPositive = data[data.length - 1] >= data[0]
  const color = isPositive ? 'var(--profit)' : 'var(--loss)'
  return (
    <svg width={width} height={height} className="flex-shrink-0">
      <path d={pathD} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/* ═══════ Chip / Filter ═══════ */
export function Chip({ children, active, onClick, color }) {
  const activeStyle = color === 'green' ? 'border-[var(--profit)] text-[var(--profit)] bg-[var(--profit-dim)]'
    : color === 'red' ? 'border-[var(--loss)] text-[var(--loss)] bg-[var(--loss-dim)]'
    : ''
  return (
    <button
      className={`chip ${active ? 'active ' + activeStyle : ''}`}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

/* ═══════ Slider Panel (side drawer) ═══════ */
export function SliderPanel({ open, onClose, title, children, footer }) {
  if (!open) return null
  return (
    <>
      <div className="slider-overlay" onClick={onClose} />
      <div className="slider-panel">
        <div className="slider-panel-header">
          <h3 className="text-sm font-semibold text-[var(--txt)]">{title}</h3>
          <button className="btn-icon" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="slider-panel-body">{children}</div>
        {footer && <div className="slider-panel-footer">{footer}</div>}
      </div>
    </>
  )
}

/* ═══════ Modal ═══════ */
export function Modal({ open, onClose, title, children, footer, wide }) {
  if (!open) return null
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-box" style={wide ? { width: 'min(720px, 92vw)' } : {}} onClick={e => e.stopPropagation()}>
        <div className="modal-box-header flex items-center justify-between">
          <h3 className="text-sm font-semibold text-[var(--txt)]">{title}</h3>
          <button className="btn-icon" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="modal-box-body">{children}</div>
        {footer && <div className="modal-box-footer">{footer}</div>}
      </div>
    </div>
  )
}

/* ═══════ Confirm Dialog ═══════ */
export function ConfirmDialog({ open, onClose, onConfirm, title, text, confirmText, danger }) {
  const { t } = useTranslation()
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{t('ui.cancel')}</button>
          <button className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={() => { onConfirm(); onClose(); }}>{confirmText || t('ui.confirm')}</button>
        </>
      }
    >
      <p className="text-sm text-[var(--txt-secondary)] leading-relaxed">{text}</p>
    </Modal>
  )
}

/* ═══════ Theme Toggle ═══════ */
export function ThemeToggle() {
  const { theme, toggle } = useTheme()
  const { t } = useTranslation()
  return (
    <button className="btn-icon" onClick={toggle} title={theme === 'dark' ? t('nav.light_theme') : t('nav.dark_theme')}>
      {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
    </button>
  )
}

/* ═══════ Onboarding Tour ═══════ */
export function OnboardingTour() {
  const { active, step, steps, next, prev, close } = useOnboarding()
  const { t } = useTranslation()
  const [pos, setPos] = useState({ top: 0, left: 0 })

  useEffect(() => {
    if (!active) return
    const el = document.querySelector(steps[step]?.target)
    if (el) {
      el.classList.add('tour-highlight')
      const rect = el.getBoundingClientRect()
      const tooltipW = 300
      let top = rect.bottom + 12
      let left = rect.left + rect.width / 2 - tooltipW / 2
      if (top + 180 > window.innerHeight) top = rect.top - 180
      if (left < 10) left = 10
      if (left + tooltipW > window.innerWidth - 10) left = window.innerWidth - tooltipW - 10
      setPos({ top, left })
      return () => el.classList.remove('tour-highlight')
    }
  }, [active, step])

  if (!active) return null
  const s = steps[step]
  if (!s) return null

  return (
    <>
      <div className="fixed inset-0 bg-black/40 z-40" onClick={close} />
      <div className="tour-tooltip" style={{ top: pos.top, left: pos.left }}>
        <h4>{s.title}</h4>
        <p>{s.text}</p>
        <div className="flex items-center justify-between">
          <span className="tour-steps">{step + 1} / {steps.length}</span>
          <div className="flex gap-2">
            <button className="btn btn-ghost btn-sm" onClick={prev} disabled={step === 0}><ChevronLeft size={12} /></button>
            <button className="btn btn-ghost btn-sm" onClick={close}><SkipForward size={12} /></button>
            <button className="btn btn-primary btn-sm" onClick={next}>{step < steps.length - 1 ? t('ui.next') : t('ui.done')} <ChevronRight size={12} /></button>
          </div>
        </div>
      </div>
    </>
  )
}

/* ═══════ Glossary Modal ═══════ */
export function getGlossary(t) {
  return [
    { term: t('ui.glossary_pnl_term'), desc: t('ui.glossary_pnl_desc') },
    { term: t('ui.glossary_roi_term'), desc: t('ui.glossary_roi_desc') },
    { term: t('ui.glossary_winrate_term'), desc: t('ui.glossary_winrate_desc') },
    { term: t('ui.glossary_pfactor_term'), desc: t('ui.glossary_pfactor_desc') },
    { term: t('ui.glossary_sharpe_term'), desc: t('ui.glossary_sharpe_desc') },
    { term: t('ui.glossary_mdd_term'), desc: t('ui.glossary_mdd_desc') },
    { term: t('ui.glossary_tp_term'), desc: t('ui.glossary_tp_desc') },
    { term: t('ui.glossary_sl_term'), desc: t('ui.glossary_sl_desc') },
    { term: t('ui.glossary_trail_term'), desc: t('ui.glossary_trail_desc') },
    { term: t('ui.glossary_be_term'), desc: t('ui.glossary_be_desc') },
    { term: t('ui.glossary_rotation_term'), desc: t('ui.glossary_rotation_desc') },
    { term: t('ui.glossary_roe_term'), desc: t('ui.glossary_roe_desc') },
  ]
}

export function GlossaryModal({ open, onClose }) {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const glossary = getGlossary(t)
  const filtered = glossary.filter(g =>
    g.term.toLowerCase().includes(search.toLowerCase()) ||
    g.desc.toLowerCase().includes(search.toLowerCase())
  )
  return (
    <Modal open={open} onClose={onClose} title={t('ui.glossary_title')} wide>
      <input
        placeholder={t('ui.glossary_search')}
        value={search}
        onChange={e => setSearch(e.target.value)}
        className="w-full mb-4"
      />
      <div className="space-y-3 max-h-[50vh] overflow-y-auto">
        {filtered.map(g => (
          <div key={g.term} className="p-3 rounded-lg bg-[var(--bg)]">
            <div className="text-sm font-semibold text-[var(--txt)] mb-1">{g.term}</div>
            <div className="text-xs text-[var(--txt-secondary)] leading-relaxed">{g.desc}</div>
          </div>
        ))}
        {filtered.length === 0 && <p className="text-xs text-[var(--txt-muted)] text-center py-4">{t('ui.nothing_found')}</p>}
      </div>
    </Modal>
  )
}

/* ═══════ PnL Bar (visual gradient) ═══════ */
export function PnlBar({ value, maxAbs = 100 }) {
  const pct = Math.min(Math.abs(value) / maxAbs * 100, 100)
  const isPos = value >= 0
  const bg = isPos
    ? `linear-gradient(90deg, rgba(0,255,136,0.1) ${pct}%, transparent ${pct}%)`
    : `linear-gradient(90deg, rgba(255,51,102,0.1) ${pct}%, transparent ${pct}%)`
  return (
    <span
      className="pnl-bar"
      style={{
        background: bg,
        borderLeft: isPos ? '2px solid var(--profit)' : '2px solid var(--loss)',
      }}
    />
  )
}

/* ═══════ Empty State ═══════ */
export function EmptyState({ icon: Icon, text, sub }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      {Icon && <Icon size={36} className="text-[var(--txt-muted)] mb-3 opacity-40" />}
      <p className="text-sm text-[var(--txt-muted)]">{text}</p>
      {sub && <p className="text-2xs text-[var(--txt-muted)] mt-1">{sub}</p>}
    </div>
  )
}

/* ═══════ Loader ═══════ */
export function Loader() {
  return <div className="animate-spin w-5 h-5 border-2 border-[var(--info)] border-t-transparent rounded-full" />
}

/* ═══════ Skeleton Loader — shimmer effect for loading states ═══════ */
export function Skeleton({ className = '', variant = 'default' }) {
  const variantClass = variant === 'text' 
    ? 'h-4 rounded' 
    : variant === 'title'
    ? 'h-6 rounded'
    : variant === 'circle'
    ? 'rounded-full aspect-square'
    : variant === 'card'
    ? 'h-32 rounded-xl'
    : 'h-12 rounded-lg'
  
  return (
    <div className={`skeleton-shimmer ${variantClass} ${className}`} />
  )
}

/* ═══════ Skeleton Metric Card ═══════ */
export function SkeletonMetricCard() {
  return (
    <div className="metric-card">
      <Skeleton variant="text" className="w-24 mb-2" />
      <Skeleton variant="title" className="w-32 mb-1" />
      <Skeleton variant="text" className="w-16" />
    </div>
  )
}

/* ═══════ Skeleton Table Row ═══════ */
export function SkeletonTableRow({ columns = 4 }) {
  return (
    <tr>
      {Array.from({ length: columns }).map((_, i) => (
        <td key={i} className="px-4 py-3">
          <Skeleton variant="text" />
        </td>
      ))}
    </tr>
  )
}

/* ═══════ Strategy Description Tip ═══════ */
export function getStrategyDesc(t) {
  return {
    momentum: t('ui.strategy_desc.momentum'),
    impulse: t('ui.strategy_desc.impulse'),
  }
}
