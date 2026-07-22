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

## 状态

messages.yml 3 个 key 已补 zh_CN；本地实跑 + 幂等 + 7 用例全绿；CI 调用链已核走 prebuilder。需切 **v2026.7.28** 重新构建 exe，补丁才随新镜像真正进入安装包（v2026.7.27 的 exe 已固化英文，需新构建替换）。
