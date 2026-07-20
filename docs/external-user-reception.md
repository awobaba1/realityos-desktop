# 外部用户接待 SOP + 桶 D 真机冒烟清单

> **关联**：ADR-V6-038(外部用户接待前置)、ADR-V6-037(发布管线)、ADR-V6-022(母纲桶 D)。
> **适用版本**：v2026.7.21 及以后(ADR-V6-027~037 反假绿收口版)。
> **受众**：创始人/运营者邀请外部用户前的自检 + 外部用户首次使用引导。

---

## 0. 诚实边界(先读)

V6 当前是**纯本地桌面拓扑**:每个用户在自己机器跑 hermes + 自己的 LLM key + 自己的 bot token,**无中心服务端**。所以本 SOP 不含「服务端健康」项。

**桶 D 真机链不可全自动**:CI runner 不是 mac 桌面真机,无法验证「真实安装体验」。下文冒烟清单**必须由真人在真机跑**——这是堵死「门禁过≠生产过」最后假绿的唯一方式(母纲 ADR-V6-022 镜头 8:「200 样本比生产简单,门禁过≠生产过」)。

---

## 1. 邀请前 Pre-flight(创始人侧,必跑)

```bash
cd /path/to/realityos-desktop
.venv/bin/python scripts/reception_preflight.py --tag v2026.7.21 --probe-download
```

**全绿(exit 0)才邀请**。任一红项修复后再邀请。检查项:

1. Release v2026.7.21 资产(dmg+exe)存在
2. 资产 size 非空非畸(对照 138MB/118MB 量级,反 20B 假文件)
3. `install.sh@<tag-commit>` GitHub raw 可达(HTTP 200)——首启 bootstrap 的源头
4. `--probe-download`:公开下载 HTTP 200(跟 302 到 release-assets CDN 终态)
5. 本地 hermes 运行时就绪(state.db 可读 + config 有效 + LLM key 配了)

> 纯分发物门禁(不查本地)用 `--skip-local`;CI/网差时省掉 `--probe-download`。

---

## 2. 分发物(发给外部用户的内容)

| 平台                  | 文件                             | 下载                                                                  |
| --------------------- | -------------------------------- | --------------------------------------------------------------------- |
| macOS (Apple Silicon) | `RealityOS-0.17.0-mac-arm64.dmg` | https://github.com/awobaba1/realityos-desktop/releases/tag/v2026.7.21 |
| Windows x64           | `RealityOS-0.17.0-win-x64.exe`   | 同上                                                                  |

**代码签名 = 无(D4 内部测试)**:mac 首次打开需右键→打开(绕过 Gatekeeper);Windows 可能弹 SmartScreen→「仍要运行」。**邀请前务必告知用户这点**,否则首启卡在系统警告。

---

## 3. 真机冒烟清单(外部用户跑 / 创始人对照)

> 每步:操作 → 预期 → 失败排查。勾选确认。

### 3.1 安装

- [ ] **mac**:双击 dmg → 拖 RealityOS 到 Applications → 启动(右键→打开首次)
- [ ] **win**:双击 exe → 按向导安装 → 启动
- **预期**:app 窗口出现,显示 bootstrap/进度界面(拉 `install.sh@<commit>` 跑各 stage)
- **排查**:若 bootstrap 卡住/失败 → 看 `~/.hermes/logs/desktop.log`(A:bootstrap 进度;D3 崩溃兜底后任何异常都落盘这里)。常见:`install.sh@commit` 拉取失败(网络)→ Pre-flight §1 CHECK 3 没跑或网变了。

### 3.2 首启 onboarding(配 LLM provider)

- [ ] onboarding 流程出现 → 选 provider(DeepSeek / 智谱 GLM / OpenAI / Anthropic)→ 填 API key → 选模型
- **预期**:onboarding 完成,进入主界面;`hermes doctor` 全绿(可选:`~/.hermes/.venv/bin/hermes doctor`)
- **排查**:key 无效 → provider 报 401 → 换 key;模型不可用 → 换模型(ADR-093 智谱主/DeepSeek 备)

### 3.3 建第一条 atom(捕获心脏)

- [ ] 在聊天框发一条 memo,如「我打算下周二和小王开会讨论 Q3 预算」
- **预期**:Atomizer 双 pass 落库(R3 事件 + R1 状态 + R2 人 R7 事 等);**记忆浏览器可见**
- [ ] 打开 `/memory` 页 → 确认出现「小王」「Q3 预算」相关原子
- **排查**:atom 未落库 → 查 `~/.hermes/ptg.db`(`SELECT COUNT(*) FROM atoms`)> 0);0 行 → 看捕获层 invoke_hook 是否 fail-open(查 `errors.log`),或 confidence 未过门(R3>0.8 等,母纲 C5)

### 3.4 出日报(洞察引擎)

- [ ] 方式 A:当天有 atom 后,等调度跑日报(startup-lazy 调度);方式 B:洞察页 `/insights` 触发
- **预期**:日报含当天 atom 的 LLM 摘要(端点 `GET /api/insights/daily-report`)
- **排查**:日报空 → 看当天是否有 atom(§3.3);日报报错 → 看 `gateway.log` + `errors.log`;冷启动门控可能抑制(ADR-V6 1b-2)

### 3.5 周镜面(满 7 天后)

- [ ] 用满 7 天(或手动触发)→ 周镜面页/端点产出本周回顾
- **预期**:周镜面含本周原子聚合,非空
- **排查**:节律相关内容标 deferred(time_trend_daily Phase 2 才建,ADR-V6-027 诚实降级)——这是**有意设计**,非 bug

### 3.6 洞察页(非空状态)

- [ ] 打开 `/insights` → 确认空状态正确(无数据时显示进度公式,非假数据)
- **预期**:有数据展示真实洞察;无数据展示空状态(ADR-V6-064)
- **排查**:若展示假/陈旧数据 → 缓存键含 prompt_version(ADR-V6-026),升 prompt 后旧报告应失效

---

## 4. 邀请后追踪(创始人侧)

### 4.1 D1/D7 留存(本地自看)

```bash
.venv/bin/python scripts/retention_local.py
```

**诚实**:这是创始人**本地自看**「我作为用户是否 D1/D7 回访」,非跨用户运营库(桶 D 运营留存须外部用户上量,本脚本不伪装)。

### 4.2 用户侧崩溃追溯

外部用户那端崩溃 → `desktop.log` 结构化落盘(ADR-V6-038 D3:`[CRASH <iso>] kind=... msg=... stack=...`)。让用户跑 `hermes debug share` **主动 opt-in 上报**(不自动外发,主权)。

### 4.3 提取质量指标

```bash
.venv/bin/python tests/benchmark/run_eval.py --ptg-db ~/.hermes/ptg.db --ptg-user-id <uid>
```

写入本地 `quality_metrics`(ADR-V6-027),看 atom precision/recall/f1。

---

## 5. 桶 D 待真实用户填补(诚实延迟,非本 SOP 能完成)

- [ ] §8 Phase-Gate KR(Day-7 留存>40% / 纠错率连续 2 周下降 / 回测>75%)——需外部用户上量
- [ ] 跨用户运营库 ETL——V6 无中心库,须先有运营拓扑决策(独立 ADR)
- [ ] 200 样本扩到真实分布(median 18 字单句 vs 生产 ASR 多句口语)——需真实用户 memo

这些是**天然需真实用户**的桶 D 项,代码侧已铺好 telemetry 入口(本 SOP §4 + ADR-V6-038),剩余由真实使用填补。
