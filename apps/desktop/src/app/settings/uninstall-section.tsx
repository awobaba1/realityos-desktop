import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import type { DesktopUninstallMode, DesktopUninstallSummary } from '@/global'
import { AlertTriangle, Loader2, Trash2 } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { SectionHeading } from './primitives'

interface ModeOption {
  mode: DesktopUninstallMode
  title: string
  description: string
  /** Shown in the confirm step so people know exactly what disappears. */
  consequence: string
  /** True when the option removes the Python agent (hidden if no agent). */
  needsAgent: boolean
}

const OPTIONS: ModeOption[] = [
  {
    mode: 'gui',
    title: '仅卸载聊天界面',
    description: '移除本桌面应用。RealityOS agent、你的配置和聊天记录都会保留。',
    consequence: '桌面聊天界面（本应用及其数据）',
    needsAgent: false
  },
  {
    mode: 'lite',
    title: '卸载界面 + agent，保留我的数据',
    description: '移除应用和 RealityOS agent，但保留配置、聊天和密钥，以便日后重装。',
    consequence: '聊天界面和 RealityOS agent（配置、聊天和密钥会被保留）',
    needsAgent: true
  },
  {
    mode: 'full',
    title: '全部卸载',
    description: '移除应用、agent 及所有用户数据 — 配置、聊天、定时任务、密钥、日志。',
    consequence: '全部内容 — 聊天界面、RealityOS agent 以及你的所有配置、聊天、密钥和日志',
    // full removes the agent (and user data), so it's an agent-removing option:
    // hide it on a lite client with no local agent, same as lite. A lite client
    // connecting to a remote backend has no local agent OR local user data the
    // GUI installed, so gui-only is the correct (and only) option there.
    needsAgent: true
  }
]

export function UninstallSection() {
  const [summary, setSummary] = useState<DesktopUninstallSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState<DesktopUninstallMode | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const bridge = window.hermesDesktop?.uninstall

    if (!bridge) {
      setLoading(false)

      return
    }

    void bridge
      .summary()
      .then(result => {
        if (alive) {
          setSummary(result)
        }
      })
      .catch(() => {
        // Non-fatal — we degrade to offering the GUI-only option.
      })
      .finally(() => {
        if (alive) {
          setLoading(false)
        }
      })

    return () => {
      alive = false
    }
  }, [])

  const bridge = window.hermesDesktop?.uninstall

  if (!bridge) {
    return null
  }

  // Gate the agent-removing options on whether an agent is actually present.
  // A future lite client that ships without the bundled agent shows GUI-only.
  const agentInstalled = summary?.agent_installed ?? false
  const visibleOptions = OPTIONS.filter(opt => agentInstalled || !opt.needsAgent)

  const handleConfirm = async () => {
    if (!pending) {
      return
    }

    setRunning(true)
    setError(null)

    try {
      const result = await bridge.run(pending)

      if (!result.ok) {
        setError(result.message || result.error || '无法启动卸载。')
        setRunning(false)
        setPending(null)
      }
      // On success the app quits shortly; keep the spinner up until it does.
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setRunning(false)
      setPending(null)
    }
  }

  const pendingOption = OPTIONS.find(opt => opt.mode === pending) ?? null

  return (
    <div className="mx-auto mt-8 w-full max-w-2xl">
      <SectionHeading icon={AlertTriangle} title="危险区" />

      <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3">
        {loading ? (
          <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            正在检查已安装内容…
          </div>
        ) : pendingOption ? (
          <div>
            <p className="text-sm font-medium text-destructive">确认卸载</p>
            <p className="mt-1 text-xs text-muted-foreground">
              这将移除{pendingOption.consequence}。该操作无法撤销。
            </p>
            {summary?.running_app_path && (
              <p className="mt-1 font-mono text-[0.68rem] text-muted-foreground/60">应用：{summary.running_app_path}</p>
            )}
            {error && <p className="mt-2 text-xs text-destructive">{error}</p>}
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <Button disabled={running} onClick={() => void handleConfirm()} size="sm" variant="destructive">
                {running && <Loader2 className="size-3 animate-spin" />}
                {running ? '卸载中…' : '确认卸载'}
              </Button>
              <Button disabled={running} onClick={() => setPending(null)} size="sm" variant="text">
                取消
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">卸载 Hermes</p>
            <p className="text-xs text-muted-foreground">
              选择要移除的范围。应用会关闭以完成卸载；随时重新打开安装器即可回归。
            </p>
            <div className="mt-1 flex flex-col gap-2">
              {visibleOptions.map(opt => (
                <button
                  className={cn(
                    'flex items-start gap-3 rounded-lg border border-border/60 bg-background/40 px-3 py-2.5 text-left transition',
                    'hover:border-destructive/40 hover:bg-destructive/5'
                  )}
                  key={opt.mode}
                  onClick={() => {
                    setError(null)
                    setPending(opt.mode)
                  }}
                  type="button"
                >
                  <Trash2 className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0">
                    <span className="block text-sm font-medium text-foreground">{opt.title}</span>
                    <span className="mt-0.5 block text-xs text-muted-foreground">{opt.description}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
