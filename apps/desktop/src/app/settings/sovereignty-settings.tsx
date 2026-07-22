import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { deleteSovereigntyData, exportSovereigntyData, getMinorMode, setMinorMode } from '@/hermes'
import { useI18n } from '@/i18n'
import { Download, Loader2, Lock, Trash2 } from '@/lib/icons'

import { SectionHeading, SettingsContent } from './primitives'

// RealityOS V6 — sovereignty settings (ADR-V6-023). The user-visible surface
// for the §6 sovereignty primitives: one-click JSON export (PIPL §45), §6.2
// cascade soft-delete (mode A memos / mode B total forgetting), and the §6.7
// minor-mode toggle (Atomizer drops R1/R9 biometric atoms when on). Every
// action is fail-open: backend errors surface as an inline status line, never
// thrown. Delete is soft-mark only — physical purge runs as separate nightly
// maintenance and is never a UI one-click.

type DeleteMode = 'A' | 'B'

export function SovereigntySettings() {
  const { t } = useI18n()
  const s = t.settings.sovereignty
  const [minor, setMinor] = useState<boolean | null>(null) // null = loading
  const [minorBusy, setMinorBusy] = useState(false)
  const [exportBusy, setExportBusy] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteMode, setDeleteMode] = useState<DeleteMode>('A')
  const [statusMsg, setStatusMsg] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getMinorMode()
      .then(res => {
        if (cancelled) {
          return
        }

        setMinor(res.enabled === true)

        if (res.status === 'error') {
          setStatusMsg(s.loadFailed)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setStatusMsg(s.loadFailed)
        }
      })

    return () => {
      cancelled = true
    }
  }, [s.loadFailed])

  const handleExport = async () => {
    setExportBusy(true)
    setStatusMsg(null)

    try {
      const res = await exportSovereigntyData()

      if (res.status === 'ok' && res.data) {
        const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `realityos-export-${new Date().toISOString().slice(0, 10)}.json`
        a.click()
        URL.revokeObjectURL(url)
      } else {
        setStatusMsg(res.message || s.exportFailed)
      }
    } catch {
      setStatusMsg(s.exportFailed)
    } finally {
      setExportBusy(false)
    }
  }

  const handleDelete = async () => {
    const confirmMsg = deleteMode === 'A' ? s.deleteConfirmA : s.deleteConfirmB

    if (!window.confirm(confirmMsg)) {
      return
    }

    setDeleteBusy(true)
    setStatusMsg(null)

    try {
      const res = await deleteSovereigntyData({ mode: deleteMode })

      if (res.status === 'ok' && res.marked) {
        const summary = Object.entries(res.marked)
          .map(([table, n]) => `${table}: ${n}`)
          .join('、')

        setStatusMsg(s.deleteDone(summary))
      } else {
        setStatusMsg(res.message || s.deleteFailed)
      }
    } catch {
      setStatusMsg(s.deleteFailed)
    } finally {
      setDeleteBusy(false)
    }
  }

  const handleToggleMinor = async () => {
    if (minor === null) {
      return
    }

    const next = !minor
    setMinorBusy(true)
    setStatusMsg(null)

    try {
      const res = await setMinorMode(next)

      if (res.status === 'ok') {
        setMinor(res.enabled === true)
      } else {
        setStatusMsg(res.message || s.loadFailed)
      }
    } catch {
      setStatusMsg(s.loadFailed)
    } finally {
      setMinorBusy(false)
    }
  }

  return (
    <SettingsContent>
      <div className="mx-auto w-full max-w-2xl space-y-6 pt-2">
        <header>
          <SectionHeading icon={Lock} title={s.title} />
          <p className="mt-1 text-xs text-muted-foreground">{s.intro}</p>
        </header>

        {statusMsg && (
          <div className="rounded-lg border border-border/70 bg-muted/30 px-3 py-2 text-xs" role="status">
            {statusMsg}
          </div>
        )}

        <section>
          <SectionHeading icon={Download} title={s.exportTitle} />
          <p className="mt-1 text-xs text-muted-foreground">{s.exportDesc}</p>
          <Button className="mt-2" disabled={exportBusy} onClick={() => void handleExport()} size="sm">
            {exportBusy ? <Loader2 className="size-3 animate-spin" /> : <Download className="size-3" />}
            {s.exportBtn}
          </Button>
        </section>

        <section>
          <SectionHeading icon={Trash2} title={s.deleteTitle} />
          <p className="mt-1 text-xs text-muted-foreground">{s.deleteDesc}</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button onClick={() => setDeleteMode('A')} size="sm" variant={deleteMode === 'A' ? 'textStrong' : 'text'}>
              {s.deleteModeA}
            </Button>
            <Button onClick={() => setDeleteMode('B')} size="sm" variant={deleteMode === 'B' ? 'textStrong' : 'text'}>
              {s.deleteModeB}
            </Button>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            {deleteMode === 'A' ? s.deleteModeADesc : s.deleteModeBDesc}
          </p>
          <Button
            className="mt-2 hover:text-destructive"
            disabled={deleteBusy}
            onClick={() => void handleDelete()}
            size="sm"
            variant="text"
          >
            {deleteBusy ? <Loader2 className="size-3 animate-spin" /> : <Trash2 className="size-3" />}
            {s.deleteBtn}
          </Button>
        </section>

        <section>
          <SectionHeading icon={Lock} title={s.minorTitle} />
          <p className="mt-1 text-xs text-muted-foreground">{s.minorDesc}</p>
          <Button
            className="mt-2"
            disabled={minor === null || minorBusy}
            onClick={() => void handleToggleMinor()}
            size="sm"
            variant={minor ? 'textStrong' : 'text'}
          >
            {minorBusy ? <Loader2 className="size-3 animate-spin" /> : null}
            {minor === null ? '…' : minor ? s.minorOn : s.minorOff}
          </Button>
        </section>
      </div>
    </SettingsContent>
  )
}
