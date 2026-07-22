# ADR-V6-080：应用界面全量中文化（错误路径 / 工具展示 / 命令面板 / 终端）— 零英文收口

- 状态：已采纳（Accepted）
- 日期：2026-07-22
- 关联：[[ADR-V6-077]]（安装包与 DEFAULT_LOCALE 中文化）、[[ADR-V6-078]]（release bump）、[[ADR-V6-079]]（changelog 解析）

## 背景

ADR-V6-077 把 NSIS 安装向导、`DEFAULT_LOCALE`、硬编码 UI 文案推向中文，但**审计仍发现大量用户可见英文残留**散落在主进程错误路径与渲染层：

- 主进程（electron/）：bootstrap/恢复首启文案、超时/保存图片/git 兜底、卸载包装、右键菜单 role 标签、IPC `purpose` 插值（媒体流/打开外部文件/预览目标…）、hardening、connection-config、dashboard-token、vscode-marketplace、git-worktree/git-review 等共 40+ 处。
- 渲染层（src/）：onboarding 就绪门（`Add a provider credential before sending your first message.`）、embed 加载失败、工具调用错误兜底（`Tool returned an error.` / `Error details`）、slash 命令面板描述、终端启动失败、消息恢复、归档/语言保存/星图分享码等 50+ 处。

铁律 C7（无静默失败）要求错误路径**可见可追溯**；但「可见」若以英文呈现给中文用户，等于把故障变成看不懂的噪声。这与「安装包不要出现任何英文」的对外发布要求冲突，必须收口。

## 决策

**应用界面（安装包运行后用户能看到的全部表面）全面中文化，目标零英文。**

具体：

1. **主进程错误路径全量中文化**：bootstrap 首启/恢复、各 IPC `purpose` 插值点（统一 `${purpose}失败：…` 中文模板）、hardening/connection-config/dashboard-token/vscode-marketplace/git-ops/desktop-uninstall 的用户可见错误。
2. **渲染层中文化**：onboarding 就绪门默认理由 + setup/runtime 不一致后缀、embed 失败展示、工具调用错误兜底与详情标签、slash 命令描述、终端启动失败、消息恢复、归档/语言保存失败、星图分享码错误、desktop-fs 不可用提示等。
3. **i18n 架构不动**：`src/i18n/en.ts` 作为英文 locale **源头字典保留英文**——这是 i18n 的规范化底座（en 是 canonical fallback locale），不是泄漏；生产默认 locale 经 [[ADR-V6-077]] 已锁定为 `zh`。

## 明确保留英文的边界（Deliberate Residuals）

诚实记录「仍是英文且为什么」，避免假绿：

1. **NSIS `messages.yml` 不覆盖**：3 条罕见 NSIS 错误弹窗（来自第三方 `app-builder-lib` 模板，CI 重装即丢失）。覆盖需自定义 nsis 模板，**有扰动 exe 构建的风险**，收益边际（正常安装流程不触发）→ 刻意不覆盖，作为已知残留接受。安装向导主流程（`installerLanguages: ["zh_CN"]`）已全中文。
2. **开发者契约 throw 保留英文**：`settings/helpers.ts`（Unsafe config path/key）、`settings/field-copy.ts`（Invalid/Duplicate field copy key）、`skills/mcp-tab.tsx`（Wrap server in mcpServers）。这些只在**开发期坏配置/坏契约**时触发，从不展示给终端用户，保留英文便于开发者定位。
3. **纯 catch+log 内部错误**：经核对只写入 console/dev-tools、不触达用户表面的 throw，按 B 类判断保留（在审计报告中逐项注明原因）。

## 教训（反假绿 + 测试耦合）

- **字符串翻译必须锁步更新测试断言**：渲染层多处 `expect(...).toContain/match` 直接断言英文文案。翻译后必须同步改断言。本次实证：`runtime-readiness.test.ts:34` 与 `onboarding.test.ts:108/342` 断言 `setup.status reports configured credentials`，翻译成「报告已配置凭证」后必须同步——否则测试红。
- **只跑单文件 slice 测试 + 定向 grep 会漏子短语断言**：定向 grep「setup.status reports configured credentials」能命中，但 grep「is not available」会误命中无关的 `Composer is not available` / `Selected runtime is not available`（不同字符串）。**唯一可靠门禁是全量 `npx vitest run src/`**——子短语断言、跨文件耦合只有全量跑才暴露。
- **B 类「先读再决定」纪律**：throw 是否用户可见不能靠猜，必须读调用链确认 error.message 是否触达 toast/屏幕/终端/i18n miss 兜底。拿不准就翻译（倾向中文化）。

## 状态

应用界面用户可见表面**零英文**（除上述 3 类刻意保留的残留，均已记录）。发布 v2026.7.27 取代 v2026.7.26 假绿 Latest。
