# RealityOS V6 数据分类清单（PIPL Phase 0 合规）

> 架构设计文档 §6.8 明确 Phase 0 三件套：**DPIA（PIPL §55）· 数据分类清单 · net-policy**。
> 本文件是其中的「数据分类清单」——把 PTG 每一张表、每一个字段映射到《个人信息保护
> 法》(PIPL) 的敏感度层级，给出合法性基础、保留策略与控制措施。它同时是 DPIA（见
> `dpia.md`）与 `realityos_sovereignty`（§6.1 数据宪法）的输入。
>
> **维护规则**：每张 PTG 表都必须在此登记。新增字段未登记 = 合规缺口。字段敏感度层级
> 一律按「最坏内容」判定（例如 `memos.source_text` 内容可能含任何敏感信息，按敏感处理）。

---

## 1. 分类层级定义

参照 PIPL §28（敏感个人信息）与 §13（合法性基础）：

| 层级 | 定义 | PIPL 依据 | V6 典型字段 |
|---|---|---|---|
| **L3 敏感个人信息** | 一旦泄露或非法使用，导致人格尊严、人身/财产安全受害 | §28 | 声纹、位置、健康/心理状态、未成年人信息、原始音频 |
| **L2 一般个人信息** | 以电子方式记录的与自然人有关的信息，非 L3 | §4(1) | 姓名（本人及第三方）、昵称、邮箱、关系链、任务内容 |
| **L1 鉴权凭证** | 非 PIPL「个人信息」，但泄露即账号失陷 | — | 密码哈希 |
| **L0 派生/审计** | 系统派生的非识别性数据或运行日志 | — | schema_version、计数、质量指标 |

**判定原则**：
- 内容自由文本字段（`source_text`/`corrected_text`/`summary`/`prompt_input` 等）一律按 **L3**
  处理，因为用户文本可能随时出现密码、身份证、健康、财务等任何敏感内容；按最坏内容保守判定。
- 同一表内字段敏感度不同时，**表级敏感度取最高字段**（便于全表加密/访问控制）。

---

## 2. 合法性基础（PIPL §13）

V6 单租户、自部署、数据不出设备的形态下，默认合法性基础为：

- **§13(1) 取得同意**：安装时勾选 + 数据宪法 `consent_tag` 逐 atom 标注（默认 `local_only=true`）。
- **§13(2) 订立/履行合同所必需**：用户主动发来的对话（流经即捕获）属履行「记住/越聊越懂」服务所必需。
- **§13(6) 法律规定**：`llm_call_logs`/`dlq_messages` 的留存属 C6 可重放铁律与 §6.9 灾备的工程必需。

**未成年人**（§31）：年龄门触发后，L3 生物/心理类字段（声纹、R1 情绪）**降级不采集**，
删除权一键默认，监护人确认（详见 `dpia.md` §6 与 `realityos_sovereignty` Phase 1 骨架）。

---

## 3. PTG 表级分类清单

> 字段名严格对应 `plugins/memory/ptg/schema.py`（SCHEMA_VERSION 4，14 表）。
> 所有用户数据表均已满足 C2 铁律（`deleted_at` + `version`），见 `C2_USER_TABLES`。

### 3.1 `realityos_users`（创始人账户，表级 L2）

| 字段 | 层级 | 说明 |
|---|---|---|
| `email` | L2 | 一般个人，识别用 |
| `phone` | L2 | 可选；接近 L3 但未达敏感阈值 |
| `password_hash` | **L1** | 鉴权凭证，非 PIPL 个人信息，按高敏运维 |
| `nickname` / `avatar_url` | L2 | 一般个人 |
| `timezone` / `settings` | L0 | 派生/配置 |
| `data_consent` | L0 | 同意状态（默认 `{"local_only": true, "shareable": false}`） |
| `last_active_at` / `created_at` | L0 | 时间戳 |

**控制**：单行表（单创始人）；本地 SQLite，不出设备。

### 3.2 `memos`（原始捕获面，表级 **L3**）

| 字段 | 层级 | 说明 |
|---|---|---|
| `source_text` / `corrected_text` | **L3** | 自由文本，可能含密码/身份证/健康/财务，按最坏内容判 L3 |
| `summary` | **L3** | assistant 摘要，同上 |
| `audio_clip_id` | **L3** | 原始音频引用 = 声纹生物识别载体（PIPL §28 生物识别） |
| `input_mode` (text/voice) | L0 | |
| `location_context` | **L3** | GPS = 行踪轨迹（PIPL §28 行踪轨迹），V6 仅 GPS 一种多模态 |
| `moderation_status` | L0 | 内容审核标记 |
| `timestamp` / `created_at` | L0 | |

**保留**：永久资产默认（§6.3），音频按用户选模式 A/B 清理（§6.2/§6.3），原子永留。

### 3.3 `identity_events`（R3 人物，表级 L2）

| 字段 | 层级 | 说明 |
|---|---|---|
| `person_name` | L2 | **第三方**姓名（非本人），仍为个人信息 |
| `mention_context` | **L3** | 自由文本提及上下文 |
| `sentiment` / `interaction_type` | L2 | 对第三方关系判断 |

### 3.4 `meaning_events`（R2 任务 + R7 表达，表级 **L3**）

| 字段 | 层级 | 说明 |
|---|---|---|
| `task_description` / `completion_note` / `topic_tags` | **L3** | 自由文本，可能含敏感任务内容 |
| `intent_class` / `urgency` / `task_status` / `is_overdue` | L0 | 派生枚举 |
| `deadline` / `completed_at` / `updated_at` | L0 | 时间戳 |

### 3.5 `entity_events`（R0 地点/机构/术语，表级 L2）

| 字段 | 层级 | 说明 |
|---|---|---|
| `entity_name` / `mention_context` | L2 / **L3** | 地点名一般；上下文自由文本 L3 |
| `entity_category` (place/organization/term) | L0 | |

### 3.6 `feeling_events`（R1 自我状态 + 情绪，表级 **L3**）

| 字段 | 层级 | 说明 |
|---|---|---|
| `state_type` (stress/fatigue/energy/mood) | **L3** | **健康/心理状态**（PIPL §28 健康） |
| `direction` / `intensity` / `emotion_vad` | **L3** | 同上，情绪 VAD 派生 |
| `ser_source` (llm_text/ser_audio/both) | L0 | 来源标记（Phase 2.5 声学 SER 才用 ser_audio） |
| `trigger_source` | L3 | 触发情境 JSON |

**未成年人**：降级不采集（§6.7），`realityos_sovereignty` 拦截。

### 3.7 `entities`（PTG 节点，表级 **L3**）

| 字段 | 层级 | 说明 |
|---|---|---|
| `entity_name` / `entity_name_normalized` | L2 | 人物姓名归一化 |
| `entity_type` (person/task/topic/context) | L0 | |
| `properties` | **L3** | JSON 属性，内容自由 |
| `voiceprint_samples` | **L3** | **声纹生物识别样本**（PIPL §28 生物识别，最高敏） |
| `voiceprint_confidence` | L3 | 派生置信度 |
| `mention_count` / `first_seen_at` / `last_seen_at` | L0 | |

### 3.8 `relations`（PTG 边，表级 L2）

| 字段 | 层级 | 说明 |
|---|---|---|
| `subject_id` / `object_id` / `relation_type` / `value` | L2 | 关系链 |
| `trend` / `evidence_count` / `delta` / `completeness` | L0 | 派生 |
| `consent_tag` | L0 | **数据宪法行使面**（NULL=本地默认 / `migrated`=V5 迁入 / `shareable` / `restricted`） |
| `last_updated` / `created_at` | L0 | |

### 3.9 `task_suggestions`（V 域主动建议，表级 L2）

| 字段 | 层级 | 说明 |
|---|---|---|
| `suggestion_text` / `task_description` / `dismissal_reason` | **L3** | 自由文本 |
| `suggestion_type` / `status` / `urgency` / `days_overdue` / `confidence` | L0 | 派生 |

**注**：V 域建议涉及 PIPL §24 自动化决策（详见 DPIA §5）。

### 3.10 `feedback`（用户反馈，表级 L2）

| 字段 | 层级 | 说明 |
|---|---|---|
| `rating` (thumbs_up/down) / `comment` | L2 / **L3** | comment 自由文本 |

### 3.11 `insight_aggregation`（洞察缓存，表级 **L3**）

| 字段 | 层级 | 说明 |
|---|---|---|
| `result_data` / `input_data` | **L3** | LLM 生成洞察，聚合了用户多日行为画像 |
| `aggregation_type` / `period_*` / `confidence` / `data_sufficiency` | L0 | 派生 |

### 3.12 `quality_metrics`（质量时序，表级 L0）

| 字段 | 层级 | 说明 |
|---|---|---|
| `metric_type` / `atom_type` / `value` / `sample_size` / `note` | L0 | **派生质量指标**，非识别个人 |

**控制**：仅质量度量；若 `note` 含样本文本需提审，当前仅存指标值。

### 3.13 `dlq_messages`（死信队列，表级 **L3**）

| 字段 | 层级 | 说明 |
|---|---|---|
| `original_data` | **L3** | 被拒 LLM 输出的原始 payload，可能含任何用户文本 |
| `source` / `error_type` / `error_msg` / `retry_count` / `resolved` | L0 | |

**C2 豁免**：append-only，无 `deleted_at`/`version`（基础设施日志，V5 同口径）。

### 3.14 `llm_call_logs`（LLM 调用日志，表级 **L3**）

| 字段 | 层级 | 说明 |
|---|---|---|
| `prompt_input` | **L3** | **完整原始 prompt = 用户完整文本**，最高敏，可能含任何内容 |
| `response` | **L3** | LLM 原始输出 |
| `model` / `provider` / `prompt_template_version` | L0 | |
| `input_tokens` / `output_tokens` / `latency_ms` / `cost_cny` | L0 | 派生 |
| `schema_valid` / `success` / `error_*` | L0 | C5/C7 状态 |

**控制**：C6 可重放铁律，完整留存；本地 SQLite，仅 LLM API 出网（§6.6 唯一数据处理出网口）。

### 3.15 `ptg_meta`（系统元数据，表级 L0）

| 字段 | 层级 |
|---|---|
| `schema_version` / `founder_user_id` / `last_backup_at` / `last_drill_at` | L0 |

---

## 4. 跨表控制矩阵

| 控制项 | 状态 | 落地位置 |
|---|---|---|
| C2 软删 + version（所有用户表） | ✅ 已实现 | `schema.py C2_USER_TABLES` + `test_ptg_store` 锁定 |
| 本地存储不出设备 | ✅ 默认 | 单一本地 SQLite，`<HERMES_HOME>/ptg.db` |
| consent_tag 行使面 | 🟡 schema 列就位（v4），UI 行使面 Phase 1 `realityos_sovereignty` | `relations.consent_tag` |
| 两模式删除（A/B） | 🟡 Phase 1 骨架（`realityos_sovereignty`，P1-3 待建） | §6.2 |
| 一键导出 JSON | 🟡 Phase 1（`realityos_sovereignty`，P1-3 待建） | §6.8 |
| 未成年人模式 | 🟡 Phase 1 骨架（P1-3 待建） | §6.7 |
| net-policy 出网口硬阻断 | 🟡 Phase 0 固化中（`realityos_security`，P1-2 待建） | §6.5 |
| 本地灾备 | ✅ 引擎已实现 | `backup.py`（P0-2 调度已补） |
| LLM API 出网 = 唯一数据处理出网 | 🟡 受 `realityos_security` fetch-guard 守护（P1-2） | §6.6 |

**图例**：✅ 已实现并测试 · 🟡 已规划/骨架在途 · ❌ 未开始

---

## 5. 出境（PIPL §38/§55）数据流

V6 数据处理的**唯一出网口是 LLM API**（§6.6）。受影响字段（出境到 LLM provider）：

- `memos.source_text` / `corrected_text`（抽取 + RAG 检索时上送）
- `prompt_input`（即上述文本的封装）
- `insight_aggregation`/`task_suggestions` 生成的中间上下文

**出境路径**：DeepSeek（主）/ 智谱（备）→ 受托处理者（PIPL §21/§22 受托关系）。
**合规口径**：Phase 4 端侧迁移为终极解（§6.6 回滚条款：泄露或投诉>5% 启动端侧迁移）。
当前阶段：PIPL §38 出境的合法化路径在 DPIA §7 单独评估（**这是 V6 最大的合规张力**，
诚实标注为未完全闭环）。

---

## 6. 变更登记

| 日期 | 变更 | 触发 |
|---|---|---|
| 2026-07-19 | 初版建表（14 表全登记，对应 SCHEMA_VERSION 4） | Phase 0 DPIA 先行（§6.8），补此前遗漏 |
