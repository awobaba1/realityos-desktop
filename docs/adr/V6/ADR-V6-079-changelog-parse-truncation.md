# ADR-V6-079：release.py changelog 须解析全部提交 + 链接指向 fork

- **状态**：已采纳
- **日期**：2026-07-21
- **类别**：process（发布管线）/ C7 无静默失败
- **关联**：ADR-V6-077（uv.lock bump）、ADR-V6-078（package.json bump）、ADR-V6-073（changelog body 手写）

## 背景

为切 v2026.7.27 取代假绿的 v2026.7.26（红 CI 却标 Latest，详见 v2026.7.26 收口
记录），先跑 `release.py --bump patch --date 2026.7.27` dry-run 预览，发现自动
changelog **只列了 1 个提交**（providers.tsx eslint 修复），而 `v2026.7.26..HEAD`
实有 **5 个提交**。

## 根因（双缺陷，均 C7 静默失败）

### 缺陷 1：changelog 截断到首条提交

`get_commits()` 用：

```python
git("log", range_spec, "--format=%H%x1f%an%x1f%ae%x1f%s%x00%b%x00", "--no-merges")
# 随后 log.split("\0\0")
```

注释声称"每条记录以 `\0\0` 结尾"。**实测证伪**：git log 在条目间插入的是
`\0\n`（NUL + 换行），不是 `\0\0`。于是整个范围塌缩成**一**块 → 只解析到首个
提交。v2026.7.26..HEAD 共 5 条，`split("\0\0")` 得 1 块 → changelog 只见首条。

实证：

```
raw double-NUL count: 0          # 无 -z 时根本没有 \0\0
split(\0\0) chunks:   1          # 全塌缩成 1 块
NUL+newline count:    5          # git 真正的分隔符
```

**影响**：所有既往 release（v2026.7.22~.26）的自动 changelog 都被静默截断到 1 条。
release.py exit 0、Release 照常创建 → 典型 C7 静默失败。

### 缺陷 2：changelog 链接 404

`generate_changelog()` 默认 `repo_url="https://github.com/NousResearch/hermes-agent"`
（上游）。但提交实际在 awobaba1 fork 上 → commit/compare 链接指向上游 404。
ADR-V6-073 当时以"手写 body"绕过，但根因未修。

## 决策

### 修缺陷 1：git log 加 `-z`

`-z` 让 git 用 NUL（而非换行）**终止**每条 log 记录。结合格式串末尾的 `%x00`，
相邻记录间形成 `\0\0`（前条尾部 `%x00` + `-z` 终止符）→ `split("\0\0")` 正确切分。
末条记录留一个 `\0\0` 尾部 → 产生一个空尾块，由 loop 的 `if not entry: continue` 跳过。

实证（加 `-z` 后）：

```
double-NUL count: 5   # 5 个 \0\0 分隔点（含尾部终止）
split chunks:    6    # 6 块，末块空 → 跳过后得 5 条
```

### 修缺陷 2：get_repo_url 从 origin 派生

新增 `_normalize_repo_url(url)`（纯函数，处理 SSH / ssh:// / HTTPS、±`.git`）
+ `get_repo_url()`（调用 `git remote get-url origin` 并归一化）。`generate_changelog`
调用点显式传 `repo_url=get_repo_url()`，覆盖上游默认值。

## C4 回归守卫

`tests/scripts/test_adr_v6_079_changelog_parse.py`：

1. `test_get_commits_returns_all_records_in_range`：动态比对 `get_commits` 与
   `git rev-list --count v2026.7.26..HEAD --no-merges`，range 须 ≥2。-z 丢失 →
   只剩 1 条 → 断言失败。
2. `_normalize_repo_url` × 4：SSH / SSH 无 .git / HTTPS 带 .git / ssh:// scheme，
   锁定归一化逻辑。
3. `test_get_repo_url_resolves_to_fork_not_upstream`：集成校验 URL 不含
   `NousResearch`、指向 `realityos-desktop` fork。

## 教训

- **`--format` + NUL 分隔的陷阱**：git `--format=` 在条目间插换行，**不是**靠格式
  串自身的 NUL。想用 `\0\0` 切分多记录，必须加 `-z`（NUL 终止）或 `-z` + 明确
  记录分隔。否则多记录塌缩成一块，静默截断。
- **changelog 是发布管线的可见产物，错了是 C7**：Release notes 缺提交、链接 404，
  都是"exit 0 但产物残缺"的静默失败变种，与 ADR-V6-077/078（bump 写文件却不提交）
  同类。
- **fork 必须用自己的 repo_url**：上游链接在 fork 上 404。从 `git remote get-url
  origin` 派生比硬编码更鲁棒（仓库改名/迁移自适应）。
