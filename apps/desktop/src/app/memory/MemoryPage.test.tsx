import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import type { MemoryBrowseResponse } from '@/types/hermes'

import { MemoryPage } from './MemoryPage'

// Mock the API client; each test seeds the browse response it exercises.
const getMemoryBrowse = vi.fn()
vi.mock('@/hermes', () => ({
  getMemoryBrowse: (...a: unknown[]) => getMemoryBrowse(...a)
}))

vi.mock('@/store/notifications', () => ({ notifyError: () => 'err' }))

function payload(over: Partial<MemoryBrowseResponse> = {}): MemoryBrowseResponse {
  return {
    status: 'ok',
    atoms: [
      { type: 'R3_Person', confidence: 0.9, timestamp: '2026-07-14T05:00:00+00:00',
        fields: { person_name: '张三', mention_context: '晚餐聊了项目', sentiment: 'positive' } },
      { type: 'R2_Task', confidence: 0.85, timestamp: '2026-07-14T06:00:00+00:00',
        fields: { task_description: '写述职报告', urgency: 'high', deadline: '2026-07-15' } },
      { type: 'R0_Entity', confidence: 0.8, timestamp: '2026-07-14T08:00:00+00:00',
        fields: { entity_name: '国金证券', entity_category: 'organization' } }
    ],
    entities: [
      { entity_name: '张三', entity_type: 'person', mention_count: 5, aliases: ['老张'] },
      { entity_name: '国金证券', entity_type: 'context', mention_count: 2, aliases: [] }
    ],
    relations: [
      { relation_type: 'works_at', value: null, confidence: 0.8, evidence_count: 2,
        subject_name: '张三', subject_type: 'person', object_name: '国金证券', object_type: 'context' }
    ],
    memo_count: 12,
    created_at: '2026-07-01T00:00:00+00:00',
    message: null,
    ...over
  }
}

function renderPage() {
  return render(
    <I18nProvider configClient={null} initialLocale="zh">
      <MemoryPage />
    </I18nProvider>
  )
}

describe('MemoryPage', () => {
  beforeEach(() => getMemoryBrowse.mockReset())
  afterEach(() => cleanup())

  it('renders the atom timeline with type badges + primary text', async () => {
    getMemoryBrowse.mockResolvedValue(payload())
    renderPage()
    await waitFor(() => expect(screen.getByText('人物')).toBeTruthy())
    expect(screen.getByText('张三')).toBeTruthy()
    expect(screen.getByText('写述职报告')).toBeTruthy()
    expect(screen.getByText('国金证券')).toBeTruthy()
    expect(screen.getByText(/12 条记录/)).toBeTruthy()
  })

  it('switches to the entities tab and renders the entity directory', async () => {
    getMemoryBrowse.mockResolvedValue(payload())
    renderPage()
    await waitFor(() => expect(screen.getByText('张三')).toBeTruthy())
    screen.getByText('人物与实体').click()
    expect(await screen.findByText('5 次提及')).toBeTruthy()
    expect(screen.getByText(/老张/)).toBeTruthy() // alias surfaced
  })

  it('switches to the relations tab and renders edges', async () => {
    getMemoryBrowse.mockResolvedValue(payload())
    renderPage()
    await waitFor(() => expect(screen.getByText('张三')).toBeTruthy())
    screen.getByText('关系').click()
    expect(await screen.findByText('works_at', { exact: false })).toBeTruthy()
  })

  it('renders the no_data empty state when no founder/memory yet', async () => {
    getMemoryBrowse.mockResolvedValue(
      payload({ status: 'no_data', atoms: [], entities: [], relations: [],
                memo_count: 0, created_at: null, message: 'custom nudge' })
    )
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有记忆')).toBeTruthy())
    expect(screen.queryByText('张三')).toBeNull()
  })

  it('renders the error state (never throws)', async () => {
    getMemoryBrowse.mockResolvedValue(
      payload({ status: 'error', atoms: [], entities: [], relations: [],
                memo_count: 0, created_at: null, message: 'disk fell over' })
    )
    renderPage()
    await waitFor(() => expect(screen.getByText('暂时打不开')).toBeTruthy())
    expect(screen.queryByText('张三')).toBeNull()
  })

  it('re-fetches on refresh', async () => {
    getMemoryBrowse.mockResolvedValue(payload())
    renderPage()
    await waitFor(() => expect(screen.getByText('张三')).toBeTruthy())
    screen.getByText('刷新').click()
    await waitFor(() => expect(getMemoryBrowse).toHaveBeenCalledTimes(2))
  })
})
