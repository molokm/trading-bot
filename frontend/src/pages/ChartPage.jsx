import React from 'react'
import { BarChart3 } from 'lucide-react'
import { useTranslation } from '../hooks/useTranslation'

/**
 * Chart section stub — full candlestick UI temporarily disabled
 * to reduce bundle size and load time. Route kept for nav links.
 */
export default function ChartPage() {
  const { t } = useTranslation()
  return (
    <div className="h-full flex flex-col items-center justify-center gap-4 p-8 text-center">
      <div className="w-16 h-16 rounded-2xl bg-[var(--bg-elevated)] border border-[var(--border)] flex items-center justify-center">
        <BarChart3 size={28} className="text-[var(--txt-muted)]" />
      </div>
      <div>
        <h2 className="text-lg font-semibold text-[var(--txt)]">
          {t('chart.title') || 'График'}
        </h2>
        <p className="mt-2 text-sm text-[var(--txt-muted)] max-w-md">
          Раздел временно отключён для оптимизации загрузки приложения.
          Свечной график и оверлей сделок вернём в одной из следующих версий.
        </p>
        <p className="mt-1 text-2xs text-[var(--txt-muted)] mono">
          Chart placeholder · lightweight-charts removed from bundle
        </p>
      </div>
    </div>
  )
}
