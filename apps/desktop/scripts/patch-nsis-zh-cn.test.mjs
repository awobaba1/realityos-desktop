import { describe, it, expect } from 'vitest'
import { patchMessagesYml } from './patch-nsis-zh-cn.mjs'

// ADR-V6-081 回归测试：防 app-builder-lib 升级改了 messages.yml 结构后，
// 补丁静默失效（key 找不到→throw / 结构变→不注入），让英文弹窗悄悄回潮。
// 这是反假绿守卫——补丁「跑了」≠「真补上了」。

describe('patchMessagesYml', () => {
  it('为缺 zh_CN 的目标 key 注入 zh_CN', () => {
    const src = [
      'decompressionFailed:',
      '  en: Failed to decompress files.',
      '  fr: Échec.',
      '  zh_TW: 解壓縮失敗。',
      'uninstallFailed:',
      '  en: Failed to uninstall old application files.',
      '  zh_TW: 無法。',
      '',
    ].join('\n')
    const out = patchMessagesYml(src)
    expect(out).toContain('  zh_CN: 解压文件失败。请尝试重新运行安装程序。')
    expect(out).toContain('  zh_CN: 卸载旧版应用文件失败。请尝试重新运行安装程序。')
  })

  it('appClosing 因 en 用引号 → zh_CN 同样引号化', () => {
    const src = ['appClosing:', '  en: "Closing running ${PRODUCT_NAME}..."', ''].join('\n')
    const out = patchMessagesYml(src)
    expect(out).toContain('  zh_CN: "正在关闭运行中的 ${PRODUCT_NAME}..."')
  })

  it('已有 zh_CN 的目标 key 不重复注入（单 key 幂等）', () => {
    const src = [
      'decompressionFailed:',
      '  en: Failed to decompress files.',
      '  zh_CN: 已存在的翻译。',
      '',
    ].join('\n')
    const out = patchMessagesYml(src)
    expect(out.match(/zh_CN:/g)).toHaveLength(1)
  })

  it('跑两次结果一致（整体幂等）', () => {
    const src = [
      'decompressionFailed:',
      '  en: Failed to decompress files.',
      '  zh_TW: 解壓縮失敗。',
      'appClosing:',
      '  en: "Closing running ${PRODUCT_NAME}..."',
      '',
    ].join('\n')
    const once = patchMessagesYml(src)
    const twice = patchMessagesYml(once)
    expect(twice).toBe(once)
  })

  it('目标 key 缺 en: 行 → throw（防静默失效，C7）', () => {
    const src = ['decompressionFailed:', '  fr: Échec.', ''].join('\n')
    expect(() => patchMessagesYml(src)).toThrow(
      /en: line not found under key "decompressionFailed"/,
    )
  })

  it('非目标 key 的 zh_CN 保留不动', () => {
    const src = [
      'win7Required:',
      '  en: Windows 7 required',
      '  zh_CN: 需要 Windows 7',
      'decompressionFailed:',
      '  en: Failed to decompress files.',
      '',
    ].join('\n')
    const out = patchMessagesYml(src)
    const zhLines = out.split('\n').filter(l => l.includes('zh_CN:'))
    expect(zhLines).toHaveLength(2)
    expect(out).toContain('  zh_CN: 需要 Windows 7')
    expect(out).toContain('  zh_CN: 解压文件失败。请尝试重新运行安装程序。')
  })

  it('zh_CN 注入位置紧跟 en: 之后（保留 locale 顺序可读性）', () => {
    const src = ['decompressionFailed:', '  en: Failed to decompress files.', '  fr: Échec.', ''].join(
      '\n',
    )
    const out = patchMessagesYml(src)
    const lines = out.split('\n')
    const enIdx = lines.findIndex(l => l === '  en: Failed to decompress files.')
    expect(lines[enIdx + 1]).toBe('  zh_CN: 解压文件失败。请尝试重新运行安装程序。')
  })
})
