import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SovereigntySettings } from './sovereignty-settings'

const getMinorMode = vi.fn()
const setMinorMode = vi.fn()
const exportSovereigntyData = vi.fn()
const deleteSovereigntyData = vi.fn()

vi.mock('@/hermes', () => ({
  getMinorMode: () => getMinorMode(),
  setMinorMode: (enabled: boolean) => setMinorMode(enabled),
  exportSovereigntyData: () => exportSovereigntyData(),
  deleteSovereigntyData: (body: unknown) => deleteSovereigntyData(body)
}))

function renderSovereignty() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <SovereigntySettings />
    </QueryClientProvider>
  )
}

describe('SovereigntySettings (ADR-V6-023)', () => {
  beforeEach(() => {
    getMinorMode.mockReset()
    setMinorMode.mockReset()
    exportSovereigntyData.mockReset()
    deleteSovereigntyData.mockReset()
    getMinorMode.mockResolvedValue({ status: 'ok', enabled: false })
  })
  afterEach(() => cleanup())

  it('renders the three sovereignty sections', async () => {
    renderSovereignty()
    expect(await screen.findByText('Data sovereignty')).toBeTruthy()
    expect(screen.getByText('Export my data')).toBeTruthy()
    expect(screen.getByText('Delete a window of memory')).toBeTruthy()
    expect(screen.getByText('Minor mode')).toBeTruthy()
  })

  it('toggles minor mode from off to on', async () => {
    setMinorMode.mockResolvedValue({ status: 'ok', enabled: true })
    renderSovereignty()
    // The minor toggle button reads "Off" when disabled (the default).
    const toggle = await screen.findByRole('button', { name: 'Off' })
    fireEvent.click(toggle)
    expect(setMinorMode).toHaveBeenCalledWith(true)
    expect(await screen.findByRole('button', { name: 'On' })).toBeTruthy()
  })

  it('exports data and triggers a JSON download', async () => {
    const createObjectURL = vi.fn(() => 'blob:url')
    const revokeObjectURL = vi.fn()
    Object.defineProperty(globalThis.URL, 'createObjectURL', { value: createObjectURL, configurable: true })
    Object.defineProperty(globalThis.URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true })
    exportSovereigntyData.mockResolvedValue({
      status: 'ok',
      data: { _export_meta: { user_id: 'u1' }, memos: [] }
    })
    renderSovereignty()
    fireEvent.click(await screen.findByRole('button', { name: /Export JSON/ }))
    await waitFor(() => expect(exportSovereigntyData).toHaveBeenCalled())
    expect(createObjectURL).toHaveBeenCalled()
  })

  it('soft-deletes in mode B after the confirm dialog', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    deleteSovereigntyData.mockResolvedValue({
      status: 'ok',
      mode: 'B',
      marked: { memos: 2, identity_events: 1 }
    })
    renderSovereignty()
    await screen.findByText('Data sovereignty')
    fireEvent.click(screen.getByRole('button', { name: 'Mode B: total forgetting' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(deleteSovereigntyData).toHaveBeenCalledWith(expect.objectContaining({ mode: 'B' })))
    expect(await screen.findByText(/Soft-delete complete/)).toBeTruthy()
    confirmSpy.mockRestore()
  })

  it('does not delete when the confirm dialog is cancelled', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderSovereignty()
    await screen.findByText('Data sovereignty')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(deleteSovereigntyData).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })
})
