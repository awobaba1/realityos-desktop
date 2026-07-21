# ADR-V6-076 — 全量十 Agent 审计报告（整个仓库 / 所有维度）

**状态**: Accepted（纯 docs，不发版 — 同 ADR-V6-075 先例）
**日期**: 2026-07-21
**审计范围**: 整个 `/Users/wugang/realityos-desktop/` 仓库（1,469,411 行 Python / 3195 文件 / 20 CI workflow）
**审计方法**: 10 个并行子 agent，6 纵向覆盖所有代码目录 + 4 横向覆盖所有维度，主审亲核全部 P0 + 多 agent 矛盾点
**关联**: [C1-C7 铁律](../../../CLAUDE.md) / [ADR-V6-075](ADR-V6-075.md) / [ADR-V6-037](ADR-V6-037.md)

---

## 0. 全局结论（回答「是不是假绿骗我 / 偷工减料」）

**没有偷工减料式假绿。** RealityOS 核心引擎经 10 个 agent 交叉核实为真绿：
- PTG 双 pass 提取链、C5 逐原子 schema 门、C6 llm_call_id 全链路、C7 六条 DLQ 路径、C2 十三张用户表全 `deleted_at+version` —— **全部真跑、真实过滤、真实落库**。
- 测试套件核心功能全真绿（仅 mock LLM HTTP 一层，下游全实跑 DB）；`precision` 显式 `deferred_structural` 是教科书级反假绿姿态。
- 外部接口 10+ 平台真实集成无桩；ADR-403 密钥卫生全合规；autopublish 主通道六要素全过（独立核 hash）。

**但审出 4 个 P0**，其中 **3 个是真 bug**（均为"失败 exit 0 骗 CI / 静默 DLQ"类**接线层反假绿病灶**，非偷工而是 dispatch/timeout 接线遗漏），1 个是诚实 defer（PyPI 通道，非代码 bug）。**这正是全量多 agent 审计的价值** —— 把"代码完美但接线层漏了"的病灶挖出来。修复后即可达生产级对外发布。

---

## 1. P0 发现（4 个，主审亲核）

### P0-1【真 bug·最核心】hermes_cli dispatch 丢弃 handler 返回值 → 所有命令失败 exit 0
- **文件**: `hermes_cli/main.py:15341-15348`
- **亲核代码**:
  ```python
  # Execute the command
  if hasattr(args, "func"):
      args.func(args)        # ← 返回值被丢弃，无 return
  else:
      parser.print_help()
  # main() 末尾无 return → 隐式 None
  if __name__ == "__main__":
      main()                 # ← 裸调用，无 sys.exit
  ```
- **机理**: handler 精心分了 `return 0/1`，但 dispatch 不 return + `__main__` 裸调用 + `sys.exit(None)`→exit 0 三重叠加，返回码全程丢弃。Agent 实跑 5 个命令（people show/memo correct/quark extract/task done/failed）全部 handler 返回 1 但进程 exit 0。
- **影响**: 任何 `hermes <cmd> && next_step` 的 CI/cron 脚本在 handler 失败时继续推进 —— 正是铁律点名的"永远 exit 0 骗 CI"反假绿病灶。
- **修复**:
  ```python
  if hasattr(args, "func"):
      return args.func(args) or 0
  parser.print_help(); return 0
  # __main__:
  sys.exit(main())
  ```
  + 补 e2e 测试（subprocess 调真 `hermes` 二进制断言 returncode==1）。现有 `test_adr_v6_0*` 都直接调 `cmd_X(args)` 不穿 entry point，是反假绿窗口。

### P0-2【真 bug】scripts/release.py 全失败路径 exit 0（与 v2026.7.18/19 假绿同源）
- **文件**: `scripts/release.py:2640-2641`（`__main__` 裸调用）+ 内部 5 处失败分支（:2559/2566/2576/2584-2586/2621-2632）只 `return`
- **亲核**: `if __name__ == "__main__": main()` —— 与 P0-1 同一反模式（裸 main() 调用）。push 失败后不 return 不 exit，继续走 `gh release create` 还打印"🎉 Release published!"。
- **关键区分**: 这是**本地 release.py 脚本**（开发者手动跑）的问题；**CI 自动发版**（`.github/workflows/desktop-build.yml` publish job）经 Agent 9 独立核 hash 验证真绿（v2026.7.24 真实落地 138MB dmg + 118MB exe）。两条路径，勿混淆。
- **影响**: 本地 `python scripts/release.py --publish` 失败仍 exit 0，与 [[v6-closeout-ci-governance-adr014-015]] 记录的 v2026.7.18/19 假绿压红发版**同根因未修**。
- **修复**: `sys.exit(main())` + `main()` 返回 int + 各失败分支 `return 1` + push 失败短路退出。

### P0-3【真 bug·三重坐实】GLM-5.2 extraction timeout=30s → R8 抽取静默 DLQ
- **文件**: `hermes_cli/config.py:1608`（`extraction.timeout: 30`）+ `agent/auxiliary_client.py:6214`（`_DEFAULT_AUX_TIMEOUT=30.0`）+ `agent/reasoning_timeouts.py:62-115`（白名单缺 GLM）
- **亲核**: `_REASONING_STALE_TIMEOUT_FLOORS` 逐条确认含 nemotron-3/deepseek-r1/v4/qwq/qwen3/o1/o3/o4/claude-opus-4/sonnet-4.5/4.6/grok-4 —— **唯独无 glm/glm-5/glm-5.1/glm-5.2**。GLM-5.2（ADR-093 主 LLM）thinking 实测 60-80s，既无 floor 保护又撞 30s 默认墙 → atomizer 捕获 timeout 写 DLQ → 抽取静默失败，用户看 API 200 以为绿。
- **三重坐实**: Agent 3 独立发现 + [[v6-smoke-cli-e2e-validated]] 记"atomizer timeout=30 偏紧实测单 pass 78s" + [[v5-p2-theory-acoustic-audit]] 记"timeout=60 临界/超时"。
- **修复**: ① `reasoning_timeouts.py` 加 `("glm-5",240)/("glm-5.1",240)/("glm-5.2",240)`；② `config.py extraction.timeout` 默认 30→180（或 300）；③ `_effective_aux_timeout` 把 thinking-model floor 扩到 extraction task（不只 compression）。

### P0-4【诚实 defer·非代码 bug】PyPI 发布通道连续 6 tag 全红
- **文件**: `.github/workflows/upload_to_pypi.yml`（配置正确：OIDC + 无 API token，符合 ADR-403）
- **根因**: pypi.org 上 `hermes-agent` 包未注册 fork（`awobaba1/realityos-desktop`）的 Trusted Publisher → OIDC claims 永不匹配 → `invalid-publisher`。
- **定性**: **非假绿** —— ADR-V6-072 已诚实记录此 defer 并设静态守卫防"错误修绿塞 token"。**主分发通道（桌面 dmg/exe → GitHub Release）真绿**，PyPI 是次通道。
- **修复（用户侧一次性）**: 去 https://pypi.org/manage/project/hermes-agent/settings/publishing/ 注册 Trusted Publisher（owner=awobaba1, repo=realityos-desktop, workflow=upload_to_pypi.yml, env=pypi）；或改包名。

---

## 2. P1 发现（去重合并，按主题）

| 主题 | 发现 | 来源 | 修复 |
|---|---|---|---|
| **C7 上游技术债** | hermes_cli 800+ / gateway 322 / tools 324 / agent 30+ / plugins 30 处 `except:pass` | A4/A5/A6/A3/A2/A8 六方交叉 | **上游 Hermes 整体风格，RealityOS 严格路径合规（A8 确认）**。优先补：authz_mixin/pairing/stream_consumer（鉴权投递）/skill_commands/meet_bot 的 log |
| **C6 llm_call_logs 覆盖率** | `call_llm` 出口不写日志，~20 个 fork aux 消费者（compression/vision/title/MoA/query_rewrite）+ cron 主体不写 | A3 + A6 交叉 | call_llm 出口加统一 hook 写 llm_call_logs（RealityOS 数据行路径已合规） |
| **架构铁律 8** | `agent/`→`hermes_cli/` 3 处**模块级** import（account_usage:12-13/skill_preprocessing:8/agent_runtime_helpers:34）+ 52 处 lazy（上游继承） | A10 + A3 交叉 | 3 处模块级下沉到函数体（lazy 52 处是上游设计，ADR 显式承认例外） |
| **C2 边界** | holographic `facts/memory_banks` hard DELETE（alternative provider，V6 不启用）+ set_consent_tag 未 bump version | A2 + A8 | MemoryProvider ABC 强制 deleted_at+version 契约；set_consent_tag 补 version+1 |
| **F5 假状态机** | task_suggestions schema 有五态 CHECK 无 transition 逻辑，任何 UPDATE 能跳态（ADR-V6-040 自承未结） | A10 | store.py 补 transition_suggestion() + 测试，或 ADR-040 标 Deferred |
| **purge 无白名单** | `hermes purge --tables` 接受任意字符串 → 非法表名静默报"成功 0 条"（C2_USER_TABLES 已存在但不引用） | A4 | CLI 引用 ALL_TABLES 白名单，非法 return 2 |
| **迁移工具静默成功** | export_v5.py 全表失败仍 exit 0（V5→V6 创始人数据迁移，与 backup.sh 20B 假备份同型） | A6 | 任一表 ERROR 则 return 1 |
| **calibrate 坏输入裸抛** | `hermes calibrate --date <bad>` 未捕获 ValueError → 裸 traceback | A4 | try/except → 友好错误 return 2 |
| **C4 缺绿** | `test_approval_heartbeat.py` 空壳（声称覆盖"MRB April 2026 真实用户日志回归"但 0 个 test 方法） | A7 | 补完 test 方法或显式记录覆盖机制 |
| **minor-mode 丢原子无 DLQ** | biometric R1/R9 原子被丢弃只计数不入 DLQ（PIPL §31 不可审计） | A1 | 补 insert_dlq(source=minor_mode_filter) |
| **correction 0 原子软删** | re_extract_memo 在 written==0 但 ok=True 时仍软删旧原子 → 纠正致真空 | A1 | written==0 视同失败保留旧原子 + C4 测试 |
| **ruff 关安全规则** | pyproject 只启用 PLW1514，S 系列（eval/pickle/yaml.load/shell）全关 | A10 | 加 `select=["PLW1514","S","B"]` 子集 |
| **sign job 软失败** | upload_to_pypi sign job 5 分钟超时只 `::warning::` 不红 CI | A9 | tag-driven 强契约应 exit 1 |
| **AUTOFIX_BOT_PAT** | 长寿命 PAT（有精心两段式特权分离缓解） | A9 | 迁 GitHub App installation token |

---

## 3. 多 Agent 矛盾亲核定级

**`UPDATE llm_call_logs SET schema_valid`（atomizer.py:702-711）** —— 三 agent 分歧：
- A1 标 P2（关注**架构边界**：跨边界抓 store 私有 `_lock`/_conn）
- A3 标 P2（关注 **WORM 严格性**：建议改 insert 新行 + replaces_id）
- A8 判**合规**（docstring "C6 honesty"，只改状态位不改 replay 内容）

**主审定级**: 三方实质不冲突，按维度分别定级 ——
- **WORM 维度：合规**（采纳 A8）。`schema_valid` 是状态位，prompt_input/response 等 replay 数据不变，属可辩护的有限例外（V5 留 NULL/V6 填充诚实化）。
- **架构边界维度：P2**（采纳 A1）。atomizer 越过 store 公共 API 直接操作内部连接，应在 PTGStore 暴露 `set_llm_call_schema_valid(log_id, valid)` 公共方法。

---

## 4. 反假绿总评（真绿坐实清单）

| 维度 | 核实结论 | 来源 |
|---|---|---|
| PTG 双 pass | 真跑两遍（v11 R0-R3/R7 + v12 R8/R9/R12 独立 prompt，v12 明示"绝对不要重复提取"） | A1 |
| C5 schema 门 | 逐原子 pydantic 校验 + 置信度门禁真实过滤（R3=0.5 进 DLQ 不进表）；R1 neutral-mood 豁免有专属测试 | A1 |
| C6 全链路 | llm_call_id 串联 identity_events.llm_call_id == llm_call_logs.id == atomize 返回值；prompt 版本化 v11.md/v12.md | A1 |
| C7 DLQ | 六条路径（llm_error/json_parse/schema_invalid/atom_write/graph_materialize/below_confidence）+ provider 外层 + correction 外层兜底 | A1 |
| C2 严格路径 | 13 张用户表全 deleted_at+version；3 张 append-only 显式豁免；唯一 hard-delete 面（sovereignty purge）有 §6.2+ADR-067+opt-in CLI+dry-run+宽限期完整守护 | A8 |
| 反假绿核心 | realityos_theory 3 个无文本来源 PC **强制 degraded=True/score=0.0**（LLM 中性猜测被丢弃，不伪造）；realityos_quark 只产 text-reachable Quark | A2 |
| 测试真实性 | 1313 tests 全 pass；核心功能仅 mock LLM HTTP，下游全实跑 DB；`precision` 显式 deferred_structural；mock 统一命名 _mock_caller/mock-model 可识别 | A7 |
| 外部接口 | 10+ 平台真实集成无桩（whatsapp_cloud/msgraph/yuanbao protobuf 1500+行/qqbot AES-256-GCM/relay HMAC-SHA256 WS）；HMAC+compare_digest；0 注入面 | A5 |
| ADR-403 密钥 | git 历史无真值 key（命中全为 test fixture/红队脚本）；production 仅占位符；publish 仅 GITHUB_TOKEN；强制 redact | A10/A9 |
| autopublish 主通道 | 六要素全过（tag-gate/contents:write/GH_REPO/gh release upload dmg+exe/幂等 create+view），独立核 hash 验证 v2026.7.24 真落地 | A9 |
| cron 调度 | 双信号心跳/provider 漂移 fail-closed（防 $7.73 静默烧钱）/中断反虚假 ok/空响应强制失败/reception_preflight MIN_ASSET_BYTES=50M 直击 20B 假备份教训 | A6 |
| 文档诚实 | README 6 大特性 100% 真实代码支撑（实际 20+ 平台超出宣称）；SECURITY.md 主动否认过度承诺 | A10 |

---

## 5. 修复优先级

**P0（必修，阻断级，主审已给精确修复）**:
1. `hermes_cli/main.py` dispatch exit code（最核心，影响所有命令 + CI/cron `&&` 链）
2. GLM-5.2 extraction timeout（影响生产 R8 抽取，静默 DLQ）
3. `scripts/release.py` exit code（本地发版脚本，历史假绿同源）

**P1（应修，下一批）**: F5 状态机 / call_llm 日志 hook / purge 白名单 / 3 处模块级 import / test_approval_heartbeat 补测试 / export_v5 exit code / calibrate 友好错误 / minor-mode DLQ / ruff S 规则

**P2（backlog）**: 上游 except:pass 技术债 / 文档滞后（meet_say/security docstring）/ holographic ABC 契约 / CalVer-SemVer 错位 / Dockerfile debian pin digest

**P0-4（用户侧）**: pypi.org 注册 Trusted Publisher（一次性操作）

---

## 6. 合规性

- **C1**: 本审计本身是决策，记 ADR-V6-076。
- **C4**: P0 修复须 co-commit 回归测试（尤其 P0-1 的 e2e subprocess 断言 returncode==1）。
- **C7**: P0-1/P0-2/P0-3 三处正是 C7「无静默失败」反假绿病灶的修复目标。
- **不发版**: 纯 docs（本报告）无 distributable 变更；P0 代码修复将另起 commit + 测试 + 视情况发版。
