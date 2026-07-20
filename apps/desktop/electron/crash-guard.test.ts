import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { appendCrashRecordSync, formatCrashLine, installCrashGuard } from './crash-guard'

describe('formatCrashLine', () => {
  it('formats an Error with message + stack', () => {
    const err = new Error('boom')
    const line = formatCrashLine('uncaughtException', err, '2026-07-20T00:00:00.000Z')
    expect(line).toContain('[CRASH 2026-07-20T00:00:00.000Z]')
    expect(line).toContain('kind=uncaughtException')
    expect(line).toContain('msg=boom')
    expect(line).toContain('stack=Error: boom')
    expect(line.endsWith('\n')).toBe(true)
  })

  it('formats a non-Error reason as its String() with empty stack', () => {
    const line = formatCrashLine('unhandledRejection', 'string-reason', 'iso')
    expect(line).toContain('kind=unhandledRejection')
    expect(line).toContain('msg=string-reason')
    // stack= present but empty (non-Error has no stack)
    expect(line).toMatch(/stack=\n$/)
  })

  it('truncates over-long message (capped at 500)', () => {
    const err = new Error('y'.repeat(600))
    err.stack = '' // 排除 stack 干扰,孤立测 msg 截断
    const line = formatCrashLine('uncaughtException', err, 'iso')
    expect(line).not.toContain('y'.repeat(501))
  })

  it('truncates over-long stack (capped at 2000)', () => {
    const err = new Error('short')
    err.stack = 's'.repeat(3000)
    const line = formatCrashLine('uncaughtException', err, 'iso')
    expect(line).not.toContain('s'.repeat(2001))
  })
})

describe('appendCrashRecordSync', () => {
  let tmp: string
  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'crash-guard-'))
  })
  afterEach(() => {
    fs.rmSync(tmp, { recursive: true, force: true })
  })

  it('writes a crash line to the log, creating parent dirs', () => {
    const logPath = path.join(tmp, 'nested', 'dir', 'desktop.log')
    appendCrashRecordSync('uncaughtException', new Error('boom'), logPath)
    const text = fs.readFileSync(logPath, 'utf8')
    expect(text).toContain('[CRASH')
    expect(text).toContain('msg=boom')
  })

  it('never throws even on an unwritable path', () => {
    // A path whose parent is a file → mkdirSync fails → must be swallowed.
    const blockingFile = path.join(tmp, 'blocker')
    fs.writeFileSync(blockingFile, 'x', 'utf8')
    const logPath = path.join(blockingFile, 'child', 'desktop.log')
    expect(() => appendCrashRecordSync('uncaughtException', new Error('x'), logPath)).not.toThrow()
  })
})

describe('installCrashGuard', () => {
  it('registers uncaughtException + unhandledRejection listeners on process', () => {
    const spy = vi.spyOn(process, 'on')
    installCrashGuard('/tmp/whatever.log')
    expect(spy).toHaveBeenCalledWith('uncaughtException', expect.any(Function))
    expect(spy).toHaveBeenCalledWith('unhandledRejection', expect.any(Function))
    spy.mockRestore()
  })

  it('persisted record lands in the log when the handler fires', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'crash-guard-emit-'))

    try {
      const logPath = path.join(tmp, 'desktop.log')
      installCrashGuard(logPath)
      // Emit directly; Node serializes 'uncaughtException' specially, so use
      // 'unhandledRejection' which process.emit dispatches to listeners.
      process.emit('unhandledRejection', 'rejected-reason', Promise.resolve())
      const text = fs.readFileSync(logPath, 'utf8')
      expect(text).toContain('kind=unhandledRejection')
      expect(text).toContain('msg=rejected-reason')
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true })
    }
  })
})
