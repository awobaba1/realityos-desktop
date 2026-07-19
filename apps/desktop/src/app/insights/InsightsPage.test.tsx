import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import type { InsightReportResponse } from '@/types/hermes'

import { InsightsPage } from './InsightsPage'

// Mock the markdown renderer — the chat renderer needs assistant-ui providers
// we don't want here; the unit under test is THIS component's state→UI mapping,
// not markdown rendering. Render the text plainly so we can assert it surfaced.
vi.mock('@/components/assistant-ui/markdown-text', () => ({
  MarkdownTextContent: ({ text }: { text: string }) => <div data-testid="md">{text}</div>
}))

// Mock the API client; each test seeds the response for the kind it exercises.
const getWeeklyMirror = vi.fn()
const getDailyReport = vi.fn()
vi.mock('@/hermes', () => ({
  getWeeklyMirror: (...a: unknown[]) => getWeeklyMirror(...a),
  getDailyReport: (...a: unknown[]) => getDailyReport(...a)
}))

vi.mock('@/store/notifications', () => ({ notifyError: () => 'err' }))

function mirror(over: Partial<InsightReportResponse> = {}): InsightReportResponse {
  return {
    kind: 'weekly-mirror',
    status: 'mirror',
    period_key: '2026-07-06',
    period_start: '2026-07-06',
    period_end: '2026-07-12',
    data_sufficiency: 'sufficient',
    content: '# 本周镜面\n\n你这周围绕述职报告转。',
    version: 1,
    generated_by: 'scheduled',
    llm_call_id: 'llm-1',
    created_at: '2026-07-13T01:00:00Z',
    confidence: 0.82,
    message: null,
    ...over
  }
}

function daily(over: Partial<InsightReportResponse> = {}): InsightReportResponse {
  return {
    kind: 'daily-report',
    status: 'report',
    period_key: '2026-07-14',
    period_start: '2026-07-14',
    period_end: '2026-07-14',
    data_sufficiency: 'sufficient',
    content: '# 今日报告\n\n今天你和张三推进了述职。',
    version: 1,
    generated_by: 'manual',
    llm_call_id: 'llm-2',
    created_at: '2026-07-15T01:00:00Z',
    confidence: 0.8,
    message: null,
    ...over
  }
}

function renderPage() {
  return render(
    <I18nProvider configClient={null} initialLocale="zh">
      <InsightsPage />
    </I18nProvider>
  )
}

describe('InsightsPage', () => {
  beforeEach(() => {
    getWeeklyMirror.mockReset()
    getDailyReport.mockReset()
  })
  afterEach(() => cleanup())

  it('renders a weekly mirror report with the sufficiency badge + period', async () => {
    getWeeklyMirror.mockResolvedValue(mirror())
    renderPage()
    await waitFor(() => expect(screen.getByText('数据充分')).toBeTruthy())
    expect(screen.getByTestId('md').textContent).toContain('本周镜面')
    expect(screen.getByText('2026-07-06 ～ 2026-07-12')).toBeTruthy()
  })

  it('switches to the daily tab and fetches the daily report', async () => {
    getWeeklyMirror.mockResolvedValue(mirror())
    getDailyReport.mockResolvedValue(daily())
    renderPage()
    await waitFor(() => expect(screen.getByText('数据充分')).toBeTruthy())
    screen.getByText('每日报告').click()
    await waitFor(() => expect(getDailyReport).toHaveBeenCalled())
    expect(screen.getByTestId('md').textContent).toContain('今日报告')
  })

  it('renders the cold-start placeholder distinctly (warming-up badge)', async () => {
    getWeeklyMirror.mockResolvedValue(
      mirror({ status: 'placeholder', data_sufficiency: 'insufficient',
               content: '# 本周镜面\n\n我还在了解你，继续和我聊几天。' })
    )
    renderPage()
    await waitFor(() => expect(screen.getByText('还在热身')).toBeTruthy())
    expect(screen.getByText(/我还在了解你——内容还不够/)).toBeTruthy()
    expect(screen.getByTestId('md').textContent).toContain('我还在了解你')
  })

  it('renders the no_data empty state when there is no report yet', async () => {
    getWeeklyMirror.mockResolvedValue(
      mirror({ status: 'no_data', data_sufficiency: null, content: null,
               period_key: '2026-07-06', message: 'custom nudge' })
    )
    renderPage()
    await waitFor(() => expect(screen.getByText('还没有报告')).toBeTruthy())
    expect(screen.queryByTestId('md')).toBeNull()
  })

  it('renders the error state (never throws, never 5xx-equivalent)', async () => {
    getWeeklyMirror.mockResolvedValue(
      mirror({ status: 'error', data_sufficiency: null, content: null,
               period_key: null, message: 'disk fell over' })
    )
    renderPage()
    await waitFor(() => expect(screen.getByText('暂时读不到报告')).toBeTruthy())
    expect(screen.queryByTestId('md')).toBeNull()
  })

  it('the refresh button force-regenerates', async () => {
    getWeeklyMirror.mockResolvedValue(mirror())
    renderPage()
    await waitFor(() => expect(screen.getByText('数据充分')).toBeTruthy())
    screen.getByText('刷新').click()
    await waitFor(() =>
      expect(getWeeklyMirror).toHaveBeenCalledWith(expect.objectContaining({ force: true }))
    )
  })
})
