// ADR-V6-038 D3 — electron 主进程崩溃兜底(C7 无静默失败)。
//
// 全仓核实确认:此前 main.ts 无 process.on('uncaughtException'|'unhandledRejection')
// (grep 0 命中)——崩溃只在散点 handler 里 console.error/局部 appendFile,全局兜底缺失
// = 崩溃可能静默丢失无追溯。本模块让任何未捕获异常都结构化落盘到 desktop.log。
//
// 设计取舍(诚实,见 ADR-V6-038 D3):
//  * 不外发——崩溃堆栈含 PII(文件路径/用户名),自动外发违「数据不出设备」主权叙事
//    (ADR-V6-025 net-policy 接线已确立)。下次启动可提示用户用 `hermes debug share`
//    主动上报(opt-in)。
//  * 不 exit——exit 会加剧 boot loop(崩溃→重启→崩溃,见 main.ts desktop.log bound
//    注释提到的 326GB 撑爆磁盘事故)。落盘即达成 C7(可追溯,不静默)。
//  * 不调 rotate——崩溃记录单行不触发文件 bound 问题;rotate 由正常 log flush 路径定期做,
//    避免本模块对 rotate helper 定义顺序的依赖。

import fs from 'node:fs'
import path from 'node:path'

export type CrashKind = 'uncaughtException' | 'unhandledRejection'

const MAX_MSG = 500
const MAX_STACK = 2000

/** Format a crash record as a single desktop.log line. Pure → unit-testable. */
export function formatCrashLine(kind: CrashKind, err: unknown, iso: string): string {
  const message = err instanceof Error ? (err.message ?? '') : String(err)
  const stack = err instanceof Error ? (err.stack ?? '') : ''

  return `[CRASH ${iso}] kind=${kind} ` + `msg=${message.slice(0, MAX_MSG)} ` + `stack=${stack.slice(0, MAX_STACK)}\n`
}

/** Append a structured crash record to logPath, synchronously. Never throws. */
export function appendCrashRecordSync(kind: CrashKind, err: unknown, logPath: string): void {
  try {
    const line = formatCrashLine(kind, err, new Date().toISOString())
    fs.mkdirSync(path.dirname(logPath), { recursive: true })
    fs.appendFileSync(logPath, line)
  } catch {
    // 崩溃兜底本身绝不能再崩溃——静默吞掉是本兜底唯一的合法 except(它是兜底,非业务路径)。
  }
}

/** Register process-level crash handlers that persist to logPath. Idempotent-safe. */
export function installCrashGuard(logPath: string): void {
  process.on('uncaughtException', (err: unknown) => {
    appendCrashRecordSync('uncaughtException', err, logPath)
  })
  process.on('unhandledRejection', (reason: unknown) => {
    appendCrashRecordSync('unhandledRejection', reason, logPath)
  })
}
