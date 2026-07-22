import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

// ADR-V6-081: app-builder-lib 的 nsis/messages.yml 有 3 个 key 漏了 zh_CN
// (decompressionFailed / uninstallFailed / appClosing)。NSIS 对缺失 locale
// 回退到 `en` → 安装期出现 3 条英文错误弹窗，违反「安装包不要出现任何英文」。
// 这里在构建期把这 3 个 zh_CN 注入 messages.yml。node_modules 在 `npm ci`
// 时会被擦掉，故挂在 prebuilder 上每次构建重打（对齐 patch-electron-builder-mac-binary
// 范式）。逐 key 存在性检查 → 幂等，且对上游将来补 zh_CN 也安全。
const ZH_CN_PATCHES = {
  decompressionFailed: '解压文件失败。请尝试重新运行安装程序。',
  uninstallFailed: '卸载旧版应用文件失败。请尝试重新运行安装程序。',
  appClosing: '正在关闭运行中的 ${PRODUCT_NAME}...',
}

// appClosing 的 en 用了双引号（值含 ${VAR} + ...），zh_CN 同样引号化与其对齐。
const QUOTED_KEYS = new Set(['appClosing'])

const KEY_RE = /^([A-Za-z0-9_]+):\s*$/
const LOC_RE = /^  ([a-zA-Z]{2}(?:_[A-Za-z0-9]+)?): /

/**
 * 纯函数：在 messages.yml 文本里为缺失 zh_CN 的目标 key 注入 zh_CN。
 * 幂等：key 块内已有 zh_CN 则跳过。找不到 key 或 en: 行 → throw（loud fail，
 * 防止上游改了结构后补丁静默失效、英文悄悄回潮——C7 反假绿）。
 */
export function patchMessagesYml(src) {
  const lines = src.split('\n')
  const out = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    out.push(line)
    const km = line.match(KEY_RE)
    const key = km ? km[1] : null
    if (key && key in ZH_CN_PATCHES) {
      let j = i + 1
      let enIdx = -1
      let hasZhCn = false
      while (j < lines.length && !lines[j].match(KEY_RE)) {
        const lm = lines[j].match(LOC_RE)
        if (lm) {
          if (lm[1] === 'zh_CN') hasZhCn = true
          if (lm[1] === 'en' && enIdx === -1) enIdx = j
        }
        j++
      }
      if (!hasZhCn) {
        if (enIdx === -1) {
          throw new Error(`patch-nsis-zh-cn: en: line not found under key "${key}"`)
        }
        for (let k = i + 1; k <= enIdx; k++) out.push(lines[k])
        const value = QUOTED_KEYS.has(key) ? `"${ZH_CN_PATCHES[key]}"` : ZH_CN_PATCHES[key]
        out.push(`  zh_CN: ${value}`)
        i = enIdx + 1
        continue
      }
    }
    i++
  }
  return out.join('\n')
}

function main() {
  const desktopRoot = path.resolve(import.meta.dirname, '..')
  const repoRoot = path.resolve(desktopRoot, '..', '..')
  const target = path.join(repoRoot, 'node_modules', 'app-builder-lib', 'templates', 'nsis', 'messages.yml')
  if (!fs.existsSync(target)) {
    console.warn(`[patch-nsis-zh-cn] skipped: ${target} not found`)
    process.exit(0)
  }
  const before = fs.readFileSync(target, 'utf8')
  let after
  try {
    after = patchMessagesYml(before)
  } catch (err) {
    console.error(`[patch-nsis-zh-cn] FAILED: ${err.message}`)
    process.exit(1)
  }
  if (after === before) {
    console.log('[patch-nsis-zh-cn] 3 个 key 的 zh_CN 均已就位，跳过')
    process.exit(0)
  }
  fs.writeFileSync(target, after)
  console.log('[patch-nsis-zh-cn] 已注入 zh_CN: decompressionFailed / uninstallFailed / appClosing')
}

// 仅在直接执行时跑 main（被 import 做测试时不跑）。
// 必须用 pathToFileURL 跨平台比对：Windows 的 process.argv[1] 是
// `C:\...\x.mjs`（反斜杠 + 盘符），而 import.meta.url 是 `file:///C:/...`，
// 字符串拼接 `file://${argv[1]}` 在 Windows 永不匹配 → main() 不跑。
// 历史教训：v2026.7.28 因此在 Windows CI 未注入 zh_CN（构建绿但 exe 仍英文 =
// 假绿），已切 v2026.7.29 修。导出 isMainModule 便于回归测试（C4）。
export function isMainModule(argv1, moduleUrl) {
  try {
    return pathToFileURL(argv1).href === moduleUrl
  } catch {
    return false
  }
}

if (isMainModule(process.argv[1], import.meta.url)) {
  main()
}
