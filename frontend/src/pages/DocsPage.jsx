import React, { useState, useMemo, useRef, useEffect } from 'react'
import { BookOpen, ChevronRight, Bot, BarChart3, TrendingUp, Settings2, Zap, AlertTriangle, Search } from 'lucide-react'
import { STRATEGY_DESC } from '../components/ui'

const SECTIONS = [
  {
    id: 'getting-started',
    title: 'Начало работы',
    icon: BookOpen,
    items: [
      { title: 'Подключение к OKX', text: 'Перейдите в раздел Settings и введите API ключи. Рекомендуется начать с демо-режима. API ключи создаются в OKX → API → Создать API ключ. Для бота достаточно прав «Торговля».' },
      { title: 'Запуск первого бота', text: 'На странице Bots нажмите «Новый бот». Выберите стратегию Momentum, настройте параметры риска и нажмите «Сохранить». Затем нажмите «Start» на карточке бота.' },
      { title: 'Мониторинг', text: 'На Dashboard отображаются все открытые позиции, PnL в реальном времени и лог бота. Используйте фильтры для быстрого поиска по сделкам.' },
    ],
  },
  {
    id: 'strategies',
    title: 'Стратегии',
    icon: Bot,
    items: [
      { title: 'Momentum', text: STRATEGY_DESC.momentum + '\n\nПараметры: риск на сделку, макс. позиций, трейлинг стоп, безубыток, TP1, каскадный SL, ADX порог.' },
      { title: 'Grid', text: STRATEGY_DESC.grid + '\n\nПараметры: размер позиции, уровни сетки, шаг сетки, макс. позиций, TP, SL.' },
      { title: 'DCA', text: STRATEGY_DESC.dca + '\n\nПараметры: размер позиции, количество DCA ордеров, шаг DCA, TP.' },
      { title: 'Scalping', text: STRATEGY_DESC.scalping + '\n\nПараметры: размер позиции, TP, SL, макс. позиций, интервал опроса.' },
    ],
  },
  {
    id: 'backtesting',
    title: 'Бэктестинг',
    icon: BarChart3,
    items: [
      { title: 'Запуск бэктеста', text: 'На странице Backtest выберите инструменты, период и таймфрейм. Нажмите «Запустить» — через несколько секунд появятся результаты.' },
      { title: 'Чтение метрик', text: 'Total Return — общая доходность. Win Rate — % прибыльных сделок. Profit Factor — отношение прибыли к убытку (> 1 = хорошо). Sharpe Ratio — доходность относительно риска (> 1 = хорошо). Max Drawdown — максимальное падение капитала.' },
      { title: 'Сравнение стратегий', text: 'Активируйте режим Compare и запустите несколько бэктестов с разными параметрами. Лучшая стратегия отмечена звёздочкой.' },
      { title: 'Экспорт', text: 'Результаты бэктеста можно экспортировать в CSV для дальнейшего анализа в Excel или Python.' },
    ],
  },
  {
    id: 'risk',
    title: 'Управление рисками',
    icon: AlertTriangle,
    items: [
      { title: 'Риск-менеджмент', text: 'Никогда не рискуйте более 2-3% капитала на одну сделку. Используйте Stop Loss на каждой позиции. Начинайте с демо-режима.' },
      { title: 'Проскальзывание', text: 'На волатильных рынках фактическая цена исполнения может отличаться от ожидаемой. Учитывайте это при установке tight стопов.' },
    ],
  },
]

export default function DocsPage() {
  const [active, setActive] = useState(0)
  const [search, setSearch] = useState('')
  const breadcrumbRef = useRef(null)
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
  }, [search])

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
                placeholder="Поиск по документации..."
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>

            {search.trim() && (
              <p className="text-2xs text-[var(--txt-muted)]">
                Найдено {filteredSections.reduce((acc, s) => acc + s.items.length, 0)} результатов
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
                <p className="text-sm text-[var(--txt-muted)]">Ничего не найдено</p>
                <p className="text-2xs text-[var(--txt-muted)] mt-1">Попробуйте изменить запрос</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
