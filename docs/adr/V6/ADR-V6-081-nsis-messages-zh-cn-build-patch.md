# ADR-V6-081：NSIS messages.yml 补齐 zh_CN（构建期补丁）— 安装包真零英文

- 状态：已采纳（Accepted）
- 日期：2026-07-23
- 关联：[[ADR-V6-077]]（安装包中文化）、[[ADR-V6-080]]（应用界面零英文，曾把本项列为「刻意残留」）

## 背景

[[ADR-V6-080]] 收口了应用界面用户可见表面，但**诚实记录**了一项刻意保留的英文残留：

> NSIS `messages.yml` 不覆盖：3 条罕见 NSIS 错误弹窗（来自第三方 `app-builder-lib` 模板，CI 重装即丢失）。覆盖需自定义 nsis 模板，有扰动 exe 构建的风险 → 刻意不覆盖。

本次排查定位到根因：**不是硬编码 MessageBox**，而是 `node_modules/app-builder-lib/templates/nsis/messages.yml` 里 **3 个 key 漏了 `zh_CN` locale**。NSIS 对缺失 locale 回退到 `en`：

| key | 状态 | 触发场景 |
| --- | --- | --- |
| `decompressionFailed` | 缺 zh_CN（仅 zh_TW） | 安装期解压失败 |
| `uninstallFailed` | 缺 zh_CN（仅 zh_TW） | 卸载旧版文件失败 |
| `appClosing` | 缺 zh_CN（仅 en/cs…） | 关闭运行中的进程 |

`assistedMessages.yml`（向导主流程，`oneClick:false` 走它）17 个 key 全有 zh_CN，故向导页全中文；这 3 条来自 `messages.yml`（向导 + one-click 共用的运行期消息）。

根因一旦明确，原「自定义 nsis 模板有构建风险」的顾虑**不成立**——只需往 YAML **补 locale 串**（加 key-value），不改 NSIS 模板结构，零构建风险。这与「安装包不要出现任何英文」的对外发布要求直接冲突，必须收口。

## 决策

**构建期补丁脚本把 3 个缺失 zh_CN 注入 messages.yml，挂到既有 `prebuilder` 生命周期钩子。**

- 脚本：`apps/desktop/scripts/patch-nsis-zh-cn.mjs`，对齐既有 `patch-electron-builder-mac-binary.mjs` 范式（repoRoot 解析 `node_modules/app-builder-lib`、幂等、找不到文件优雅 skip、console 日志）。
- 挂载：`package.json` 的 `prebuilder` 改为 `patch-electron-builder-mac-binary.mjs && patch-nsis-zh-cn.mjs`。`prebuilder` 是 npm 生命周期钩子，`builder` 脚本运行前自动触发；CI `desktop-build.yml:88` 走 `npm run dist:win:nsis --workspace apps/desktop` → `npm run builder` → **prebuilder 必触发**（已核，非直接 `node` 绕过）。
- 为何挂 prebuilder 而非 `patch-package`：复用既有基建，零新依赖；且 prebuilder 在 `npm ci`（擦 node_modules）之后、electron-builder 之前运行，时机正确。

### 纯函数化 + 反假绿守卫

`patchMessagesYml(src)` 导出为纯函数（text → text），便于测试：

- **逐 key 存在性检查幂等**：key 块内已有 `zh_CN` 则跳过；对上游将来补 zh_CN 也安全（不重复注入）。
- **结构变化 loud-fail（C7）**：目标 key 找不到、或其下找不到 `en:` 行 → `throw`，构建中断。防止 app-builder-lib 升级改了 messages.yml 结构后，补丁**静默失效、英文悄悄回潮**——这正是反假绿的核心。
- 文件级优雅 skip：messages.yml 整体不存在（app-builder-lib 大改）→ warn + exit 0，不阻塞。

### 回归测试（C4）

`apps/desktop/scripts/patch-nsis-zh-cn.test.mjs`（7 用例，归 `electronNative` vitest project，`include: scripts/**.test.{ts,mjs}` 覆盖，CI 必跑）：覆盖注入、appClosing 引号化、单 key 幂等、整体幂等、缺 en loud-fail、非目标 key 不受扰、注入位置紧跟 en。

## 教训（反假绿）

- **正则 `.test()` 过 ≠ `.match()[1]` 可用**：本地首轮调试，KEY_RE `/^[A-Za-z0-9_]+:\s*$/` 用 `.test('decompressionFailed:')` 返回 true（隔离测正则时用的就是 test），但**没有捕获组** → `match()[1]` 永远 undefined → key 恒 null → 补丁从不触发，函数原样返回输入。表现：vitest 5/7 红、实跑输出等于输入。修法：`/^([A-Za-z0-9_]+):\s*$/` 加捕获组。**教训：验证正则提取时必须测真实的 `match()[1]` 输出，不能只信 `.test()`。**
- **「补丁跑了」≠「补丁生效」**：若 CI 直接 `node run-electron-builder.mjs` 绕过 `npm run builder`，prebuilder 不触发 → 补丁形同虚设（假绿）。必须核 CI 调用链确认走 npm 生命周期（已核 `desktop-build.yml:88`）。
- **🔥 ESM 主模块守卫必须跨平台（v2026.7.28 Windows 假绿根因）**：为可测试把补丁逻辑包进 `main()`，用 `if (import.meta.url === \`file://${process.argv[1]}\`)` 守卫。这在 macOS 成立（argv 是 posix 路径），但 **Windows 上 `process.argv[1]` 是 `C:\...\x.mjs`（反斜杠+盘符），`import.meta.url` 是 `file:///C:/...`，字符串拼接永不匹配 → `main()` 不执行 → NSIS 补丁在 Windows CI 静默未注入，而 Windows 构建照样绿、exe 照样产出（英文）= 假绿**。而这恰是 NSIS 补丁唯一需要的平台。修法：改用 Node 官方 canonical 检测 `pathToFileURL(process.argv[1]).href === import.meta.url`（导出 `isMainModule` 便于回归测试）。**教训：(1) 平台相关字符串比对要用 `pathToFileURL`/`fileURLToPath`，勿手拼 `file://`；(2) 「构建日志有 prebuilder 命令行」≠「补丁逻辑真跑了」——必须 grep 补丁自身的 stdout（如「已注入 zh_CN」）确认 main() 执行，且要在目标平台（Windows）核，不能只在 mac 核。** 回归测试用含空格路径（`%20` 编码）区分两种实现，posix/Windows CI 都可跑。

## 状态

messages.yml 3 个 key 已补 zh_CN；本地实跑 + 幂等 + 10 用例（含 isMainModule 跨平台守卫）全绿；CI 调用链已核走 prebuilder。**v2026.7.28 因 Windows 守卫 bug 为假绿（exe 仍英文），切 v2026.7.29 用 `pathToFileURL` 守卫修复后，Windows CI 日志确认「已注入 zh_CN」、exe 真零英文。**
