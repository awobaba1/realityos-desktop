import { beforeEach, describe, expect, it, vi } from 'vitest'

// ADR-V6-036 (action 21): the launch-view store decides which page the app
// lands on at cold start. Drives the REAL store, then re-imports it (fresh
// module state reading persisted localStorage) to simulate a relaunch — the
// same "localStorage is the carry-over" pattern as sidebar-collapse-persistence.
async function loadStore() {
  return await import('./launch-view')
}

const relaunch = () => vi.resetModules()

describe('launch-view store (ADR-V6-036)', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('defaults to chat when nothing is stored', async () => {
    const store = await loadStore()
    expect(store.getLaunchView()).toBe('chat')
    expect(store.$launchView.get()).toBe('chat')
  })

  it('persists a memory choice across a relaunch', async () => {
    const s1 = await loadStore()
    s1.setLaunchView('memory')
    expect(s1.$launchView.get()).toBe('memory')

    relaunch()
    const s2 = await loadStore()
    expect(s2.getLaunchView()).toBe('memory') // the cold-start read sees the persisted value
    expect(s2.$launchView.get()).toBe('memory')
  })

  it('persists an insights choice across a relaunch', async () => {
    const s1 = await loadStore()
    s1.setLaunchView('insights')

    relaunch()
    const s2 = await loadStore()
    expect(s2.getLaunchView()).toBe('insights')
  })

  it('maps each view to its cold-start route, chat => no override', async () => {
    const store = await loadStore()
    expect(store.launchViewRoute('chat')).toBeNull() // null = fall through to legacy resume
    expect(store.launchViewRoute('memory')).toBe('/memory')
    expect(store.launchViewRoute('insights')).toBe('/insights')
  })

  it('coerces a corrupt stored value back to chat (never crashes cold start)', async () => {
    window.localStorage.setItem('hermes.desktop.launchView.v1', 'nonsense')
    const store = await loadStore()
    expect(store.getLaunchView()).toBe('chat')
  })

  it('rejects an invalid view setter (defensive, no-op)', async () => {
    const store = await loadStore()
    expect(store.$launchView.get()).toBe('chat')
    // @ts-expect-error — simulating untrusted input
    store.setLaunchView('bogus')
    expect(store.$launchView.get()).toBe('chat') // unchanged
    // 'insights' was never written; the only persisted value is the benign
    // initial 'chat' (the store persists its default at module load, mirroring
    // backdrop.ts). A bogus setter must NOT mutate it to anything else.
    expect(window.localStorage.getItem('hermes.desktop.launchView.v1')).toBe('chat')
  })
})
