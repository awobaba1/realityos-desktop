import { useCallback, useEffect, useState } from 'react'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { PageLoader } from '@/components/page-loader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { getDailyReport, getWeeklyMirror } from '@/hermes'
import { useI18n } from '@/i18n'
import { Loader2, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import type { InsightReportKind, InsightReportResponse } from '@/types/hermes'

type TabKind = InsightReportKind

const TABS: ReadonlyArray<{ kind: TabKind; labelKey: 'tabWeekly' | 'tabDaily' }> = [
  { kind: 'weekly-mirror', labelKey: 'tabWeekly' },
  { kind: 'daily-report', labelKey: 'tabDaily' }
]

/**
 * RealityOS V6 — Insights page (PRD #4 weekly mirror + #2 daily report,
 * ADR-V6-020). Cache-first read of `insight_aggregation` rendered as markdown;
 * the Refresh button force-regenerates (one LLM call). The cold-start
 * placeholder (§0.5③) is rendered verbatim with a distinct "warming up" badge,
 * never mistaken for a real report.
 *
 * Registered as a contributed full page at `/insights` (ROUTES_AREA) with a
 * sidebar nav entry (SIDEBAR_NAV_AREA) — see app/contrib/controller.tsx.
 */
export function InsightsPage() {
  const { t } = useI18n()
  const ti = t.insights
  const [tab, setTab] = useState<TabKind>('weekly-mirror')

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-(--ui-stroke-secondary) px-4 py-2.5">
        <div className="flex items-center gap-1">
          {TABS.map(tb => (
            <button
              aria-pressed={tab === tb.kind}
              className={cn(
                'rounded-[2.5px] px-2.5 py-1 text-xs font-medium transition-colors',
                tab === tb.kind
                  ? 'bg-(--ui-bg-quaternary) text-(--ui-text-primary)'
                  : 'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)'
              )}
              key={tb.kind}
              onClick={() => setTab(tb.kind)}
            >
              {ti[tb.labelKey]}
            </button>
          ))}
        </div>
        <h1 className="text-xs font-medium text-(--ui-text-secondary)">{ti.title}</h1>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <ReportPanel kind={tab} />
      </div>
    </div>
  )
}

interface ReportState {
  loading: boolean
  refreshing: boolean
  data: InsightReportResponse | null
}

function ReportPanel({ kind }: { kind: TabKind }) {
  const { t } = useI18n()
  const ti = t.insights
  const [state, setState] = useState<ReportState>({ loading: true, refreshing: false, data: null })

  const fetcher = kind === 'weekly-mirror' ? getWeeklyMirror : getDailyReport

  const load = useCallback(
    async (force: boolean) => {
      if (force) {
        setState(s => ({ ...s, refreshing: true }))
      }

      try {
        const data = await fetcher({ force })
        setState({ loading: false, refreshing: false, data })
      } catch (err) {
        setState(s => ({ ...s, loading: false, refreshing: false }))
        notifyError(err, ti.errorTitle)
      }
    },
    [fetcher, ti.errorTitle]
  )

  useEffect(() => {
    setState({ loading: true, refreshing: false, data: null })
    void load(false)
  }, [kind, load])

  if (state.loading) {
    return <PageLoader className="grid min-h-64 place-items-center" label={ti.refreshing} />
  }

  const data = state.data

  if (!data || data.status === 'no_data') {
    return (
      <div className="px-4 py-6">
        <EmptyState description={data?.message ?? ti.noDataDesc} title={ti.noDataTitle} />
      </div>
    )
  }

  if (data.status === 'error') {
    return (
      <div className="px-4 py-6">
        <EmptyState description={data.message ?? ti.errorDesc} title={ti.errorTitle} />
      </div>
    )
  }

  const isPlaceholder = data.status === 'placeholder'

  return (
    <div className="px-4 py-4">
      <div className="mx-auto max-w-3xl">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <SufficiencyBadge sufficiency={data.data_sufficiency} />
            <span className="text-xs text-muted-foreground">{ti.periodLabel(data.period_start, data.period_end)}</span>
          </div>
          <Button
            disabled={state.refreshing}
            onClick={() => void load(true)}
            size="xs"
            title={ti.refreshHelp}
            variant="ghost"
          >
            {state.refreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {ti.refresh}
          </Button>
        </div>

        {isPlaceholder && (
          <p className="mb-3 rounded-[2.5px] bg-(--ui-bg-quaternary) px-3 py-2 text-xs text-muted-foreground">
            {ti.placeholderNote}
          </p>
        )}

        <article
          className={cn('prose prose-sm max-w-none dark:prose-invert', isPlaceholder && 'opacity-70')}
          data-insight-content={kind}
        >
          {data.content ? (
            <MarkdownTextContent isRunning={false} text={data.content} />
          ) : (
            <EmptyState description={ti.noDataDesc} title={ti.noDataTitle} />
          )}
        </article>
      </div>
    </div>
  )
}

function SufficiencyBadge({ sufficiency }: { sufficiency: InsightReportResponse['data_sufficiency'] }) {
  const { t } = useI18n()
  const ti = t.insights

  if (sufficiency === 'sufficient') {
    return <Badge variant="default">{ti.sufficiencySufficient}</Badge>
  }

  if (sufficiency === 'partial') {
    return <Badge variant="warn">{ti.sufficiencyPartial}</Badge>
  }

  return <Badge variant="muted">{ti.sufficiencyInsufficient}</Badge>
}
