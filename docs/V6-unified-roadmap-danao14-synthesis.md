# V6 统一规划 — danao14 调研综合（7 轮）→ 全量完成执行路线

> **状态**：执行锚点（2026-07-20，ADR-V6-039 之后）
> **来源**：danao14（V5 reference impl workspace，Phase 2.5 声学+机制已闭合）7 轮代码级调研
> **对照**：V6 fork = `/Users/wugang/realityos-desktop/`（HEAD `269760226`，main 干净）
> **铁律**：C1-C7；反假绿三连核验（headSha + step 级非 skip + 真实运行）
> **用户指令**：「不要再问我，你自己全量完成」「必须 100% 全量完成，别假绿」

---

## 0. 一句话结论

**danao14 是 V5 Phase 2.5 全集实现，但「不直接可移植」** —— 3 个硬阻塞（schema 列不兼容 / 缺音频上游 / 砍依赖）+ 5 个消费层空洞（G1-G5）。V6 fork 在**工程纪律 + atomizer + confidence + 校准**上反超 danao14，但**消费层闭环 + 基础设施**有 6 个已核验假绿/缺口。

**执行路线 = 先 A（消费层闭环）后 B（合成层）**，而非 ADR-V6-039 的 quark+theory 优先。理由：Agent ④ 实证 Phase 2 无声学下 theory 只能撑 4/7 PC 维且 Cognition 严重降级，quark+theory 在 Phase 2 价值空心化；而 G1 RAG 引用缺失是 strategy 02 判定的**整个可信度系统单点故障**，必须先堵。

---

## 1. 7 轮调研关键发现（已实地核验）

### 1.1 V6 fork 反超 danao14 的点（保留不动）
- **Atomizer 双 pass**（ADR-V6-012）：precision⑦ 双路径证死天花板 ~70%，比 danao14 单 pass 强
- **confidence 分级 + 创始人校准 CLI**（ADR-V6-028）：atom_id 行 PK 定位，比 danao14 强
- **PTGStore 4950 行**：单文件内聚，比 danao14 散包强
- **反假绿方法论**（ADR-V6-032~038）：可见化/不可验证不写/契约先于实现

### 1.2 已核验的假绿 / 缺口（P0，动手前实地核验过）

| # | 缺口 | 证据（已核验） | 假绿类型 |
|---|---|---|---|
| **F1** | `.pyc 防御`缺失 | `tests/conftest.py` 确无 `sys.dont_write_bytecode`（仅 docker 测试有，无关） | 整个可信度系统单点故障（strategy 02 T-0） |
| **F2** | G1 RAG 无引用校验 | `recall.py`(130行)+`provider.py`(403行) **零 citation 校验** | LLM 可幻觉引用，可信度系统根基崩 |
| **F3** | atomizer R9 缺 `entity` key | `atomizer.py:520-521` 写 `{"trigger":..., "atom":"R9_Emotion"}`，无 entity | 移植 K域 compute.py = silent 零输出 |
| **F4** | `upsert_relation` 无 delta / 无 transaction / 无 stale_at / 无 mark_k_correlation_stale | `store.py:962-999` SELECT-then-UPDATE 手工幂等，confidence=max | K域/R12 移植前置阻塞 |
| **F5** | `task_suggestions` 假状态机 | `schema.py:299` 有 5 态 CHECK，store.py 零 insert/transition/query | schema-only stub（又一个假绿） |
| **F6** | `insight_aggregation` 无 sample-size 硬门 | LLM 可在 3-5 条数据编强结论 | V6 最大假绿源头（Agent ③ 对症药） |
| **F7** | K_Correlation 零实现 | grep 零命中 | 承重墙表无人读 |
| **F8** | R12 仅靠 LLM 从 memo 识别 | 无用户主权显式通路（`/完成`） | 漏抽/错抽无法落库 |

### 1.3 danao14 可借鉴（按 ROI 排序，Agent ⑤ 裁决）

| Tier | 借鉴点 | 操作 | 工作量 | 备注 |
|---|---|---|---|---|
| **Tier1** | sample-size 分级 confidence 门（`compute.py:23-27 _grade_confidence`） | **提取为公共门** | 0.5 天 | <10 不发 / 10-30 中 0.6 / ≥30 高 0.9；F6 对症药 |
| **Tier1** | G1 RAG citation 范式（danao13 生产 `rag_service.py:711-731`） | **对齐移植** | 1-2 天 | JSON 合成索引 `[idx]` + response_format + 越界裁剪 + DLQ 兜底（比 danao14 regex 严） |
| **Tier2** | K域 `compute.py` K_Correlation | **适配移植** | 2-3 天 | 需先补 F3+F4 基建 |
| **Tier2** | R12 显式通路（`record_outcome`） | **移植** | 1-2 天 | `/完成` 用户主权信号 |
| **Tier2** | `plugin-package-contract`（openclaw 单文件 semver 门） | **直接移植** | 0.5-1 天 | 纯收益 |
| **Tier2** | `memory-host-sdk` facade 思路 | **适配重写** | 2-3 天 | `hermes.ts`(45K) 抽 facade |
| **Tier3** | quark+theory（ADR-V6-039） | **LLM 近似 + 诚实降级** | Batch1-3 | Phase 2 仅 4/7 PC 维，Cognition 降级 |
| **Tier3** | `bundled-backend`（解决首发失败） | **适配重写** | 代码1-2天+CI1-2周 | 源码不在仓库，从 main.cjs 反推 |
| **不做** | `memory-graph`（@supermemory） | **仅参考** | — | 语义鸿沟巨大，首屏必空白；V6 已有 starmap |
| **不做** | `plugin-sdk` 60 子路径 / `net-policy` TS SSRF | **仅参考** | — | 过度工程 / V6 Python 版够用 |

---

## 2. 核心张力裁决：ADR-V6-039 vs strategy 02

### 2.1 张力
- **ADR-V6-039**（已 Accepted，commit `269760226`）：Phase 2 = quark（Batch1）+ theory（Batch2）+ 调度接线（Batch3），LLM 近似 PC/FR 骨架
- **strategy 02**（danao14 V5 审计）：先 A（消费层闭环：RAG引用/纠错/M域/K域/R12）后 B（洞察广度：quark/theory）

### 2.2 裁决：**strategy 02「先 A 后 B」正确，ADR-V6-039 推迟但不变**
**理由（Agent ④ 实证）**：
1. Phase 2 无声学下 theory **只能撑 4/7 PC 维**（Time/Cognition/Emotion/Execution），且 **Cognition 因 V6 R8 无连续 score 严重降级**
2. **Energy/Social 缺音频链路 = 不可**，**Environment 永不可**（scene.py POC fail defer）
3. V6 fork 4 张 event 表与 danao14 列结构**完全不兼容**，每个 producer 都要适配层 → **不是 drop-in 移植**
4. 最大假绿陷阱：移植 `social_state` schema 但不补 audio_clips→speaker_label 全链路 = theory 表面跑通但 Social 维永远空

**而 G1 RAG 引用缺失**（F2）是整个可信度系统的根基 —— LLM 可幻觉引用，比「洞察广度不够」致命得多。

### 2.3 处置
- **ADR-V6-039 不撤销**（它定义的 IDL 契约 + 实现边界仍有效），但 **Batch 1-3 推迟到 A 层之后**
- 新增 **ADR-V6-040**（本规划执行时写）：记录「先 A 后 B」优先级重排 + ADR-V6-039 推迟理由
- A 层（消费层闭环）= Phase 2 的真前置；B 层（quark+theory）= Phase 2 后段

---

## 3. P0 反假绿清单（立即执行，全部已核验）

> P0 = 阻塞下游 / 单点故障 / 违铁律。必须先做，每项配 ADR + 测试 + CI 三连。

### P0-1 反假绿基建（F1 + atomize DLQ + deletion_log）
- **F1 `.pyc 防御`**：`tests/conftest.py` 加 `sys.dont_write_bytecode = True`（防测试运行改 .pyc 污染源判断 + 反假绿核验指纹）。C4 回归测：断言导入后无新 .pyc。
- **atomize DLQ 兜底**：核验 `_spawn_atomize` 失败路径是否落 DLQ（C7），若 fail-open 静默 = 补 DLQ。
- **deletion_log 审计表**：软删（C2 cascade_soft_delete）已有，但**无独立审计表记录「谁/何时/为何删」**。补 `deletion_log` 表（id/user_id/table/record_id/reason/actor/deleted_at）+ sovereignty.py 写入。

### P0-2 sample-size 分级 confidence 门（F6）
- 提取 danao14 `compute.py:23-27 _grade_confidence` 为 V6 公共门 `plugins/memory/ptg/confidence_gate.py`：
  - `<10` 样本 → 不发结论（confidence=None）
  - `10-30` → confidence=0.6
  - `≥30` → confidence=0.9
- **接线**：所有写 `insight_aggregation` 的 LLM insight 输出（日报/周镜面/quark/theory）必须过此门。小样本 → 标 `data_sufficiency="insufficient"` + 不发强结论。
- 这直接堵 F6（V6 最大假绿源）。

### P0-3 G1 RAG 硬强制引用（F2）
- 对齐 danao13 生产范式（`rag_service.py:711-731`，比 danao14 regex 严）：
  1. 合成 1-based 索引 `[idx]` 喂 LLM（chunk_lines）
  2. LLM `response_format={"type":"json_object"}` 返回 `citation_indices`
  3. **越界裁剪**：`[i for i in citation_indices if 1 <= i <= n_chunks]`
  4. 映射回真实 atom_id/memo_id
  5. **C5 schema gate**（pydantic）+ **C7 DLQ 兜底**（LLM 失败/无引用 → DLQ，不静默）
  6. **硬强制**：回答无有效引用 = 拒绝渲染（返回「无法从记忆中找到依据」），**绝不**渲染无引用回答
- C4 回归测：合成 memo + mock LLM 幻觉引用 → 断言被裁剪/拒绝。

### P0-4 store 层基建（F3 + F4）
- **F3**：`atomizer.py:520-521` R9 trigger_source 加 `entity` key（写 `{"trigger":..., "entity": <entity_name_or_null>, "atom":"R9_Emotion"}`）—— 解锁 K域 compute.py 移植
- **F4 store 基建**：
  - `store.transaction()` 上下文管理器（原子写多表）
  - `upsert_relation(..., delta: Optional[float]=None)` —— 增量更新（K域相关性 delta 累积）
  - `relations` 表加 `stale_at` 列（K域失效标记）+ 迁移
  - `store.mark_k_correlation_stale(user_id, subject_id)` —— 失效 K 缓存
- C4 回归测 + alembic 实跑 upgrade head 自证（[[alembic-upgrade-head-lesson]]）

---

## 4. 全量完成执行路线（分阶段，按依赖排序）

### Phase 2-A：消费层闭环（先 A，~1-2 周）
**目标**：让已落库的 atoms/entities/relations「有人读、读得准、读得可信」。

| 步 | 任务 | 依赖 | ADR |
|---|---|---|---|
| A0 | P0-1~P0-4 反假绿基建 | — | ADR-V6-040（本规划） |
| A1 | G1 RAG 硬强制引用（P0-3） | A0 | ADR-V6-041 |
| A2 | K域 K_Correlation compute（Tier2 适配移植） | P0-4 F3+F4 | ADR-V6-042 |
| A3 | R12 显式通路（`/完成` record_outcome） | A0 | ADR-V6-043 |
| A4 | 纠错反馈修复链（strategy02 G2 broken link） | A0 | ADR-V6-044 |
| A5 | M域人物画像（strategy02 G3 unbuilt） | A2 | ADR-V6-045 |

**完成标准（每步）**：ADR + 实现 + C4 回归测 + ruff/tsc 全量 + CI 三连核验（headSha 精确匹配 + step 级非 skip + 真实运行）。

### Phase 2-B：合成层（后 B，~2-3 周，ADR-V6-039 推迟到此后）
**目标**：在消费层可信基础上，扩展洞察广度。

| 步 | 任务 | 依赖 | ADR |
|---|---|---|---|
| B1 | Batch1 quark 提取（3 文本 kind，ADR-V6-039） | A 层 + P0-2 confidence 门 | ADR-V6-046 |
| B2 | Batch2 theory LLM 近似（**诚实降级**：只 Time/Emotion/Execution 可信，Cognition 降级，Energy/Social/Environment 标 defer） | B1 + P0-2 | ADR-V6-047 |
| B3 | Batch3 调度接线 + UI 读取 | B2 | ADR-V6-048 |

**诚实降级铁律**（Agent ④）：theory 输出必须标每个 PC 维的 `basis`（如 `"R1.fatigue 自述"`）+ `degraded`（如 `["audio_unavailable"]`）。**绝不**把 None 渲染成「平稳」或省略降级标 —— 那是假绿。

### Phase 2-C：可选借鉴（按 ROI，穿插或后置）
| 任务 | 时机 | ROI |
|---|---|---|
| plugin-package-contract 直移植 | A 层后任意 | Tier2 纯收益 |
| memory-host-sdk facade（hermes.ts 抽层） | B 层后 | Tier2 可维护性 |
| bundled-backend（首发失败根治） | 外部用户上量前 | Tier1 但 CI 重 |

### 不做（Phase 2 范围外，诚实延迟）
- Phase 2.5 声学（SER/prosody/voiceprint/scene）—— ADR-V6-011/031 降级
- theory 统计公式（PC-Energy σ 等）—— 需 R10 节律连续值 + 音频
- memory-graph（@supermemory）—— 语义鸿沟，V6 已有 starmap
- 桶 D 运营 KR —— 需外部用户上量

---

## 5. 反假绿守则（执行期间持续应用）

1. **动手前实地核验**：任何「已有/已实现」声明，grep + 读行号确认（本规划 8 个缺口全部已核验）
2. **不建空壳**：schema-only stub（如 F5 task_suggestions）= 假绿，要么真做实要么删 CHECK
3. **不可验证不写**：theory 降级维标 `defer` 而非伪造数值
4. **sample-size 硬门**：所有 LLM insight 过 P0-2 confidence 门
5. **CI 三连核验**：headSha 精确匹配 + step 级非 skip + 真实运行（绝不信管道 `$?`/`gh run watch`）
6. **每步 ADR + C4 测试**：铁律 C1/C4
7. **生产现实**（C3）：用合成 memo 测，不碰真实创始人 memo（PIPL/PII）

---

## 6. 立即开工

从 **P0-1（.pyc 防御 + atomize DLQ 核验 + deletion_log）** 开始，逐项推进，不回头问。每完成一项写 ADR + 测试 + push + CI 核验，进记忆，再下一项。
