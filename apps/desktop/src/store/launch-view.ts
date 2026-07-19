import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

// ADR-V6-036 (action 21): the user-chosen default view the app lands on at cold
// start — the "data-asset home" (chat / memory / insights). 'chat' (default) is a
// NO-OP: it preserves the legacy remembered-route / remembered-session resume
// behavior (use-desktop-integrations.ts) unchanged. 'memory' / 'insights' are an
// EXPLICIT override that always wins on launch, so the user always opens on their
// chosen home instead of wherever they last were.
const KEY = 'hermes.desktop.launchView.v1'

export type LaunchView = 'chat' | 'memory' | 'insights'

const VALID_VIEWS: readonly LaunchView[] = ['chat', 'memory', 'insights']

function coerce(raw: null | string): LaunchView {
  return raw && (VALID_VIEWS as readonly string[]).includes(raw) ? (raw as LaunchView) : 'chat'
}

/** Synchronous cold-start read (mirrors getRememberedRoute in session.ts). */
export function getLaunchView(): LaunchView {
  return coerce(storedString(KEY))
}

/** The cold-start target route for a launch view, or null when the view is
 *  'chat' — meaning "no override, fall through to the legacy resume behavior".
 *  Paths match the contributed routes registered in contrib/controller.tsx. */
export function launchViewRoute(view: LaunchView): null | string {
  if (view === 'memory') {return '/memory'}

  if (view === 'insights') {return '/insights'}

  return null
}

export const $launchView = atom<LaunchView>(getLaunchView())

$launchView.subscribe(view => persistString(KEY, view))

export function setLaunchView(view: LaunchView): void {
  if (!(VALID_VIEWS as readonly string[]).includes(view)) {
    return
  }

  $launchView.set(view)
}
