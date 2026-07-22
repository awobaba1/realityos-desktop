import { useCallback, useEffect, useState } from 'react'

import { PageLoader } from '@/components/page-loader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { getMemoryBrowse } from '@/hermes'
import { useI18n } from '@/i18n'
import { Loader2, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import type { AtomType, MemoryAtom, MemoryBrowseResponse, MemoryEntity, MemoryRelation } from '@/types/hermes'

type TabKey = 'atoms' | 'entities' | 'relations'

const TABS: ReadonlyArray<{ key: TabKey; labelKey: 'tabAtoms' | 'tabEntities' | 'tabRelations' }> = [
  { key: 'atoms', labelKey: 'tabAtoms' },
  { key: 'entities', labelKey: 'tabEntities' },
  { key: 'relations', labelKey: 'tabRelations' }
]

/**
 * RealityOS V6 — Memory browser page (ADR-V6-021). A read-only view of what the
 * brain has captured: an atom timeline (people/events/tasks/emotions/...), an
 * entity directory (people/orgs/places), and relation edges. De-black-boxes the
 * memory so the user can SEE it remembers (Phase 1b deliverable "记住人/事/状态").
 *
 * One GET /api/memory/browse drives all three tabs. Registered as a contributed
 * full page at /memory (ROUTES_AREA) with a sidebar nav entry — see
 * app/contrib/controller.tsx. Pure read, fail-open (C7).
 */
export function MemoryPage() {
  const { t } = useI18n()
  const tm = t.memory
  const [tab, setTab] = useState<TabKey>('atoms')
  const [data, setData] = useState<MemoryBrowseResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(
    async (force: boolean) => {
      if (force) {
        setRefreshing(true)
      }

      try {
        setData(await getMemoryBrowse({ limit: 200 }))
      } catch (err) {
        notifyError(err, tm.errorTitle)
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [tm.errorTitle]
  )

  useEffect(() => {
    void load(false)
  }, [load])

  if (loading) {
    return <PageLoader className="grid min-h-64 place-items-center" label={tm.refreshing} />
  }

  // No founder yet (first-launch race) or store unreachable → warm empty state.
  if (!data || data.status === 'no_data') {
    return (
      <div className="px-4 py-6">
        <EmptyState description={data?.message ?? tm.noDataDesc} title={tm.noDataTitle} />
      </div>
    )
  }

  if (data.status === 'error') {
    return (
      <div className="px-4 py-6">
        <EmptyState description={data.message ?? tm.errorDesc} title={tm.errorTitle} />
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-(--ui-stroke-secondary) px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <h1 className="truncate text-xs font-medium text-(--ui-text-secondary)">{tm.title}</h1>
          <span className="shrink-0 text-xs text-muted-foreground">{tm.memoCountLabel(data.memo_count)}</span>
          {data.created_at && (
            <span className="shrink-0 text-xs text-muted-foreground">{tm.memberSinceLabel(data.created_at)}</span>
          )}
        </div>
        <Button disabled={refreshing} onClick={() => void load(true)} size="xs" title={tm.refreshHelp} variant="ghost">
          {refreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          {tm.refresh}
        </Button>
      </header>

      <div className="flex items-center gap-1 border-b border-(--ui-stroke-secondary) px-4 py-1.5">
        {TABS.map(tb => (
          <button
            aria-pressed={tab === tb.key}
            className={cn(
              'rounded-[2.5px] px-2.5 py-1 text-xs font-medium transition-colors',
              tab === tb.key
                ? 'bg-(--ui-bg-quaternary) text-(--ui-text-primary)'
                : 'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)'
            )}
            key={tb.key}
            onClick={() => setTab(tb.key)}
          >
            {tm[tb.labelKey]}
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'atoms' && <AtomsPanel atoms={data.atoms} />}
        {tab === 'entities' && <EntitiesPanel entities={data.entities} />}
        {tab === 'relations' && <RelationsPanel relations={data.relations} />}
      </div>
    </div>
  )
}

// ── atoms ────────────────────────────────────────────────────────────────────

const TYPE_BADGE: Record<AtomType, 'default' | 'warn' | 'muted'> = {
  R3_Person: 'default',
  R2_Task: 'default',
  R7_Expression: 'default',
  R12_Outcome: 'default',
  R1_SelfState: 'warn',
  R9_Emotion: 'warn',
  R8_Cognition: 'muted',
  R0_Entity: 'muted'
}

const TYPE_LABEL_KEY: Record<
  AtomType,
  | 'typePerson'
  | 'typeTask'
  | 'typeExpression'
  | 'typeCognition'
  | 'typeOutcome'
  | 'typeSelfState'
  | 'typeEmotion'
  | 'typeEntity'
> = {
  R3_Person: 'typePerson',
  R2_Task: 'typeTask',
  R7_Expression: 'typeExpression',
  R8_Cognition: 'typeCognition',
  R12_Outcome: 'typeOutcome',
  R1_SelfState: 'typeSelfState',
  R9_Emotion: 'typeEmotion',
  R0_Entity: 'typeEntity'
}

function AtomsPanel({ atoms }: { atoms: MemoryAtom[] }) {
  const { t } = useI18n()
  const tm = t.memory

  if (atoms.length === 0) {
    return (
      <div className="px-4 py-6">
        <EmptyState description={tm.emptyAtomsDesc} title={tm.emptyAtomsTitle} />
      </div>
    )
  }

  return (
    <ul className="divide-y divide-(--ui-stroke-secondary)" data-memory-atoms>
      {atoms.map((atom, i) => (
        <li className="px-4 py-3" key={`${atom.type}-${atom.timestamp ?? i}`}>
          <div className="mb-1 flex items-center gap-2">
            <Badge variant={TYPE_BADGE[atom.type]}>{tm[TYPE_LABEL_KEY[atom.type]]}</Badge>
            {atom.confidence != null && (
              <span className="text-[10px] text-muted-foreground">
                {tm.confidenceLabel} {(atom.confidence * 100).toFixed(0)}%
              </span>
            )}
          </div>
          <p className="text-sm text-(--ui-text-primary)">{atomPrimary(atom)}</p>
          {atomDetails(atom).length > 0 && (
            <p className="mt-0.5 text-xs text-muted-foreground">{atomDetails(atom).join(' · ')}</p>
          )}
          {atom.timestamp && <p className="mt-0.5 text-[10px] text-muted-foreground">{formatTs(atom.timestamp)}</p>}
        </li>
      ))}
    </ul>
  )
}

function atomPrimary(a: MemoryAtom): string {
  const f = a.fields as Record<string, unknown>

  switch (a.type) {
    case 'R3_Person':
      return String(f.person_name ?? '')

    case 'R2_Task':
      return String(f.task_description ?? '')

    case 'R7_Expression':
      return String(f.content_summary ?? f.intent_class ?? '')

    case 'R8_Cognition':
      return String(f.topic ?? '')

    case 'R12_Outcome':
      return String(f.task_ref ?? '')

    case 'R1_SelfState':
      return [f.state_type, f.direction, f.intensity].filter(Boolean).join(' · ')

    case 'R9_Emotion':
      return String(f.emotion_label ?? '')

    case 'R0_Entity':
      return String(f.entity_name ?? '')

    default:
      return ''
  }
}

function atomDetails(a: MemoryAtom): string[] {
  const f = a.fields as Record<string, unknown>
  const out: string[] = []

  switch (a.type) {
    case 'R3_Person':
      if (f.mention_context) {
        out.push(String(f.mention_context))
      }

      if (f.sentiment) {
        out.push(String(f.sentiment))
      }

      if (f.interaction_type) {
        out.push(String(f.interaction_type))
      }

      break

    case 'R2_Task':
      if (f.urgency) {
        out.push(`${f.urgency}`)
      }

      if (f.deadline) {
        out.push(`⏰ ${f.deadline}`)
      }

      break

    case 'R7_Expression':
      if (f.intent_class) {
        out.push(String(f.intent_class))
      }

      break

    case 'R8_Cognition':
      if (f.engagement) {
        out.push(String(f.engagement))
      }

      if (f.is_question) {
        out.push('?')
      }
      pushTags(out, f.knowledge_tags)

      break

    case 'R12_Outcome':
      if (f.outcome) {
        out.push(String(f.outcome))
      }

      if (f.resolution_note) {
        out.push(String(f.resolution_note))
      }

      break

    case 'R9_Emotion':
      if (f.valence) {
        out.push(String(f.valence))
      }

      if (f.arousal) {
        out.push(String(f.arousal))
      }

      if (f.trigger) {
        out.push(String(f.trigger))
      }

      break

    case 'R0_Entity':
      if (f.entity_category) {
        out.push(String(f.entity_category))
      }

      if (f.mention_context) {
        out.push(String(f.mention_context))
      }

      break

    case 'R1_SelfState':

    default:
      break
  }

  return out
}

function pushTags(out: string[], tags: unknown): void {
  if (Array.isArray(tags)) {
    for (const tag of tags) {
      if (tag) {
        out.push(`#${tag}`)
      }
    }
  }
}

function formatTs(ts: string): string {
  const d = new Date(ts)

  if (Number.isNaN(d.getTime())) {
    return ts
  }

  return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}

// ── entities ─────────────────────────────────────────────────────────────────

const ENTITY_TYPE_LABEL_KEY: Record<
  string,
  'entityTypePerson' | 'entityTypeTask' | 'entityTypeTopic' | 'entityTypeContext'
> = {
  person: 'entityTypePerson',
  task: 'entityTypeTask',
  topic: 'entityTypeTopic',
  context: 'entityTypeContext'
}

function EntitiesPanel({ entities }: { entities: MemoryEntity[] }) {
  const { t } = useI18n()
  const tm = t.memory

  if (entities.length === 0) {
    return (
      <div className="px-4 py-6">
        <EmptyState description={tm.emptyEntitiesDesc} title={tm.emptyEntitiesTitle} />
      </div>
    )
  }

  return (
    <ul className="divide-y divide-(--ui-stroke-secondary)" data-memory-entities>
      {entities.map((e, i) => (
        <li className="flex items-center justify-between gap-3 px-4 py-2.5" key={`${e.entity_name}-${i}`}>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="truncate text-sm text-(--ui-text-primary)">{e.entity_name}</span>
              <Badge variant="muted">{tm[ENTITY_TYPE_LABEL_KEY[e.entity_type] ?? 'entityTypeContext']}</Badge>
            </div>
            {e.aliases.length > 0 && (
              <p className="mt-0.5 truncate text-xs text-muted-foreground">
                {tm.aliasesLabel}: {e.aliases.join(' / ')}
              </p>
            )}
          </div>
          <span className="shrink-0 text-xs text-muted-foreground">{tm.mentionsLabel(e.mention_count)}</span>
        </li>
      ))}
    </ul>
  )
}

// ── relations ────────────────────────────────────────────────────────────────

function RelationsPanel({ relations }: { relations: MemoryRelation[] }) {
  const { t } = useI18n()
  const tm = t.memory

  if (relations.length === 0) {
    return (
      <div className="px-4 py-6">
        <EmptyState description={tm.emptyRelationsDesc} title={tm.emptyRelationsTitle} />
      </div>
    )
  }

  return (
    <ul className="divide-y divide-(--ui-stroke-secondary)" data-memory-relations>
      {relations.map((r, i) => (
        <li className="px-4 py-2.5" key={`${r.subject_name}-${r.object_name}-${r.relation_type}-${i}`}>
          <div className="flex items-center gap-2 text-sm text-(--ui-text-primary)">
            <span className="font-medium">{r.subject_name}</span>
            <span className="text-xs text-muted-foreground">
              —{r.relation_type}
              {r.value ? `: ${r.value}` : ''}→
            </span>
            <span className="font-medium">{r.object_name}</span>
          </div>
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            {tm.confidenceLabel} {(r.confidence * 100).toFixed(0)}% · {tm.mentionsLabel(r.evidence_count)}
          </p>
        </li>
      ))}
    </ul>
  )
}
