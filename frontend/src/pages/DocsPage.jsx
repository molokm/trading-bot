import React, { useState, useMemo, useRef, useEffect } from 'react'
import { BookOpen, Bot, BarChart3, AlertTriangle, Search } from 'lucide-react'
import { useTranslation } from '../hooks/useTranslation'
import { getStrategyDesc } from '../components/ui'

function getSections(t) {
  return [
    {
      id: 'getting-started',
      title: t('docs.sec_getting_started'),
      icon: BookOpen,
      items: [
        { title: t('docs.getting_connected_title'), text: t('docs.getting_connected_text') },
        { title: t('docs.first_bot_title'), text: t('docs.first_bot_text') },
        { title: t('docs.monitoring_title'), text: t('docs.monitoring_text') },
      ],
    },
    {
      id: 'strategies',
      title: t('docs.sec_strategies'),
      icon: Bot,
      items: [
        { title: t('docs.strat_momentum_title'), text: getStrategyDesc(t).momentum + '\n\n' + t('docs.strat_params') + t('docs.strat_momentum_params') },
        { title: t('docs.strat_alpha_title'), text: getStrategyDesc(t).alpha + '\n\n' + t('docs.strat_params') + t('docs.strat_alpha_params') },
      ],
    },
    {
      id: 'backtesting',
      title: t('docs.sec_backtesting'),
      icon: BarChart3,
      items: [
        { title: t('docs.bt_run_title'), text: t('docs.bt_run_text') },
        { title: t('docs.bt_metrics_title'), text: t('docs.bt_metrics_text') },
        { title: t('docs.bt_compare_title'), text: t('docs.bt_compare_text') },
        { title: t('docs.bt_export_title'), text: t('docs.bt_export_text') },
      ],
    },
    {
      id: 'risk',
      title: t('docs.sec_risk'),
      icon: AlertTriangle,
      items: [
        { title: t('docs.risk_mgmt_title'), text: t('docs.risk_mgmt_text') },
        { title: t('docs.slippage_title'), text: t('docs.slippage_text') },
      ],
    },
  ]
}

export default function DocsPage() {
  const { t } = useTranslation()
  const [active, setActive] = useState(0)
  const [search, setSearch] = useState('')
  const breadcrumbRef = useRef(null)

  const SECTIONS = useMemo(() => getSections(t), [t])
  const section = SECTIONS[active]

  const filteredSections = useMemo(() => {
    if (!search.trim()) return SECTIONS.map((s, i) => ({ ...s, originalIndex: i }))
    const q = search.toLowerCase()
    return SECTIONS
      .map((s, i) => ({
        ...s,
        originalIndex: i,
        items: s.items.filter(
          item => item.title.toLowerCase().includes(q) || item.text.toLowerCase().includes(q)
        ),
      }))
      .filter(s => s.items.length > 0)
  }, [search, SECTIONS])

  const displaySections = search.trim() ? filteredSections : [section]

  useEffect(() => {
    if (breadcrumbRef.current) {
      const btn = breadcrumbRef.current.querySelector(`[data-sec="${active}"]`)
      btn?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' })
    }
  }, [active])

  return (
    <div className="h-full flex flex-col p-4 gap-3 overflow-hidden">
      {/* Breadcrumb nav */}
      <div ref={breadcrumbRef} className="flex items-center gap-2 overflow-x-auto flex-shrink-0 pb-1" style={{ scrollbarWidth: 'none' }}>
        {SECTIONS.map((s, i) => (
          <button
            key={s.id}
            data-sec={i}
            onClick={() => { setActive(i); setSearch('') }}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition-all shrink-0 ${
              active === i && !search.trim()
                ? 'bg-[var(--info-dim)] text-[var(--info)] border border-[var(--info)]/30'
                : 'text-[var(--txt-muted)] hover:text-[var(--txt)] hover:bg-[var(--surface-overlay)] border border-transparent'
            }`}
          >
            <s.icon size={13} />
            {s.title}
          </button>
        ))}
      </div>

      <div className="flex-1 flex gap-4 overflow-hidden min-h-0">
        {/* Sidebar */}
        {!search.trim() && (
          <div className="w-52 flex-shrink-0 space-y-1 overflow-y-auto">
            {SECTIONS.map((s, i) => (
              <button
                key={s.id}
                onClick={() => setActive(i)}
                className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-all text-left ${
                  active === i ? 'bg-[var(--info-dim)] text-[var(--info)]' : 'text-[var(--txt-muted)] hover:text-[var(--txt)] hover:bg-[var(--surface-overlay)]'
                }`}
              >
                <s.icon size={14} />
                {s.title}
              </button>
            ))}
          </div>
        )}

        {/* Content */}
        <div className="flex-1 overflow-y-auto">
          <div className="max-w-2xl space-y-4">
            {/* Search bar */}
            <div className="relative">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--txt-muted)]" />
              <input
                className="w-full pl-9 !py-2 !text-xs"
                placeholder={t('docs.search')}
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>

            {search.trim() && (
              <p className="text-2xs text-[var(--txt-muted)]">
                {t('docs.results', { count: filteredSections.reduce((acc, s) => acc + s.items.length, 0) })}
              </p>
            )}

            {displaySections.map((sec, si) => (
              <div key={sec.id}>
                {(search.trim() || si === 0) && (
                  <h2 className="text-lg font-bold text-[var(--txt)] mb-4 flex items-center gap-2">
                    <sec.icon size={18} className="text-[var(--info)]" />
                    {sec.title}
                  </h2>
                )}
                <div className="space-y-3">
                  {sec.items.map((item, i) => (
                    <div key={i} className="panel">
                      <div className="p-4">
                        <h3 className="text-sm font-semibold text-[var(--txt)] mb-2">{item.title}</h3>
                        {item.text.split('\n').map((p, j) => (
                          <p key={j} className="text-xs text-[var(--txt-secondary)] leading-relaxed mb-1">{p}</p>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}

            {search.trim() && filteredSections.length === 0 && (
              <div className="text-center py-12">
                <Search size={32} className="text-[var(--txt-muted)] mx-auto mb-3" />
                <p className="text-sm text-[var(--txt-muted)]">{t('docs.no_results')}</p>
                <p className="text-2xs text-[var(--txt-muted)] mt-1">{t('docs.no_results_hint')}</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
