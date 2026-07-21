# ADR-V6-078：release bump 必须提交 apps/desktop/package.json（修 desktop/python 版本错位）

- **状态**：已采纳
- **日期**：2026-07-21
- **类别**：process（发布管线）/ C7 无静默失败
- **关联**：ADR-V6-077（uv.lock 同类）、ADR-V6-076（release.py P0 失败路径）

## 背景

`scripts/release.py` 的 `update_version_files()` 改写 4 个文件以 bump 版本：

1. `hermes_cli/__init__.py`（`VERSION_FILE`）
2. `pyproject.toml`（`PYPROJECT_FILE`）
3. `apps/desktop/package.json`（与 pyproject lockstep）
4. ACP Registry manifest（`_update_acp_registry_versions`）

但 bump commit 的 `add_files` 只含前三类中的 1、2、4 + `uv.lock`（ADR-V6-077 补），
**唯独漏了 `apps/desktop/package.json`**。结果：package.json 的 version 改写留在工作区
未提交，tag 指向的提交里 package.json 仍是旧版本。

## 现象（v2026.7.26 实证）

Release `v2026.7.26`（SemVer `0.18.4`）：

- Python：`hermes_agent-0.18.4-py3-none-any.whl` ✓（pyproject 已 bump+提交）
- Desktop：`RealityOS-0.18.3-mac-arm64.dmg` / `RealityOS-0.18.3-win-x64.exe` ✗
  （package.json 未提交 → tag 点仍是 0.18.3 → desktop-build 产出 0.18.3）

desktop 制品版本滞后于 Python 一个 patch，版本错位。属 **C7 静默失败**：发布流程
没报错（exit 0），但产物版本不一致。与 ADR-V6-077（uv.lock bump 不提交致 CI 静默红）
完全同类：**bump 写了文件，commit 却没纳它**。

## 决策

`release.py` bump commit 的 `add_files` 增加 `apps/desktop/package.json`（存在则追加，
同 ACP manifest 的条件追加模式）：

```python
add_files = [str(VERSION_FILE), str(PYPROJECT_FILE), str(REPO_ROOT / "uv.lock")]
desktop_pkg = REPO_ROOT / "apps" / "desktop" / "package.json"
if desktop_pkg.exists():
    add_files.append(str(desktop_pkg))
if ACP_REGISTRY_MANIFEST.exists():
    add_files.append(str(ACP_REGISTRY_MANIFEST))
```

并将工作区遗留的 package.json `0.18.4`（上次 release.py 运行的未提交结果）提交入 HEAD，
使 HEAD 与已发布的 Python `0.18.4` 对齐。

## C4 回归守卫

`tests/scripts/test_adr_v6_078_release_packagejson.py`：锚 binding（package.json 路径构造
+ `add_files.append`）断言该文件被纳入 bump commit。空白规范化防绕过（同 ADR-V6-075/077
守卫思路）。

## 已知遗留（诚实记录）

- Release `v2026.7.26` 的 desktop 制品仍是 `0.18.3`（历史错位，tag 不可变）。本次修复
  保证**后续** release 的 desktop/python 版本一致；不回溯重切 v2026.7.26（无用户可见
  差异，版本号纯展示）。
- 下次 release 起 desktop 与 Python 将同版本。

## 教训

**bump 写文件 ≠ 提交文件。** 每当 `update_version_files` 改写一个文件，bump commit 的
`add_files` 必须显式纳它。`update_version_files` 改了几个文件，`add_files` 就该有几个
（条件存在的用 `.exists()` 守卫追加）。否则静默版本错位——exit 0 但产物不一致，是假绿的
发布管线变种。
