"""ADR-V6-079 C4 回归：release.py changelog 须解析全部提交 + 链接指向 fork。

两个静默缺陷（C7 静默失败）：

1. **changelog 截断到 1 条**：get_commits 跑 `git log --format=...%x00%b%x00`
   （无 `-z`）后按 `\\0\\0` 切分。但 git 用 `\\0\\n`（NUL+换行）分隔 log 条目，
   于是整个范围塌缩成**一**块 → 只解析到首个提交 → 每个 release 的自动
   changelog 都被静默截断到 1 条。修：加 `-z`，git 用 NUL 终止每条记录。
2. **changelog 链接 404**：generate_changelog 默认 repo_url 指向 NousResearch
   上游，提交在 awobaba1 fork 上 → 上游链接 404。修：get_repo_url 从 origin 派生。

守卫策略（不依赖真实 git 历史 —— CI 是浅克隆，任意 tag 如 v2026.7.26 不在场，
`git rev-list v2026.7.26..HEAD` 会 exit 128）：
- `test_parse_log_splits_all_z_separated_records`：喂合成 \0\0 分隔多记录串给
  `_parse_log`，断言全解析（parse 逻辑守卫）。
- `test_get_commits_source_uses_z_flag`：源码 binding，release.py 的 git log
  调用必须含 `-z`（防 -z 被删后回归）。
- `test_get_commits_runs_and_parses_head`：CI 安全冒烟，range HEAD（无需 tag）。
- `_normalize_repo_url` × 4 + get_repo_url 集成校验（指向 fork 非上游）。
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release  # noqa: E402

RELEASE_PY = REPO_ROOT / "scripts" / "release.py"


def _record(sha: str, name: str, email: str, subject: str, body: str) -> str:
    """一条 `git log -z --format=%H%x1f%an%x1f%ae%x1f%s%x00%b%x00` 记录 + -z 终止符。

    末尾两个 NUL = 格式串的 trailing %x00 + -z 记录终止符（合起来形成 \\0\\0
    记录分隔）。body 必填 —— 真实提交均带 Co-Authored-By trailer，非空。
    """
    return f"{sha}\x1f{name}\x1f{email}\x1f{subject}\x00{body}\x00\x00"


def test_parse_log_splits_all_z_separated_records():
    """_parse_log 须把 -z 产生的 \0\0 分隔多记录全部解析，而非塌缩成 1 块。

    无 -z 时 git log 用 \0\n 分隔，split("\\0\\0") 把整段塌缩成 1 块 → 只解析
    首记录（ADR-V6-079 根因）。合成 3 条 + 末尾空块，断言 3 条全解析。
    """
    blob = (
        _record("a" * 40, "alice", "a@x", "feat: one", "Co-Authored-By: x")
        + _record("b" * 40, "bob", "b@x", "fix: two", "body two")
        + _record("c" * 40, "cara", "c@x", "chore: three", "body three")
    )
    commits = release._parse_log(blob)
    assert len(commits) == 3, f"expected 3 records, got {len(commits)}"
    assert [c["sha"] for c in commits] == ["a" * 40, "b" * 40, "c" * 40]
    assert [c["subject"] for c in commits] == ["feat: one", "fix: two", "chore: three"]
    # body 解析没污染 header 字段
    assert commits[1]["author_name"] == "bob"
    assert commits[1]["author_email"] == "b@x"


def test_get_commits_source_uses_z_flag():
    """release.py 的 get_commits git log 调用必须含 -z（源码 binding 守卫）。

    -z 丢失 → 记录用 \0\n 分隔 → split("\\0\\0") 塌缩成 1 块 → changelog 截断。
    锚定格式串后断言 -z 在其后（同 git 调用内）。
    """
    src = RELEASE_PY.read_text(encoding="utf-8")
    m = re.search(r'"--format=%H%x1f%an%x1f%ae%x1f%s%x00%b%x00"', src)
    assert m, "get_commits 的 --format 串未找到"
    after = src[m.start() :]
    assert re.search(r'"-z"', after), (
        "get_commits 必须给 git log 传 -z（ADR-V6-079）：无 -z 则记录用 \\0\\n 分隔，"
        "split('\\0\\0') 塌缩成 1 块 → changelog 只解析首提交"
    )


def test_get_commits_runs_and_parses_head():
    """CI 安全冒烟：get_commits(since_tag=None) 用 range HEAD（无需 tag）。

    浅克隆里 HEAD 恒在，至少解析出 1 条（首记录即便 body 为空也解析成首块）。
    验证 git 调用 + _parse_log 端到端不抛错。
    """
    commits = release.get_commits(since_tag=None)
    assert len(commits) >= 1, "get_commits(HEAD) 应至少解析 1 条提交"


def test_normalize_repo_url_ssh_form():
    assert (
        release._normalize_repo_url("git@github.com:awobaba1/realityos-desktop.git")
        == "https://github.com/awobaba1/realityos-desktop"
    )


def test_normalize_repo_url_ssh_no_dotgit():
    assert (
        release._normalize_repo_url("git@github.com:awobaba1/realityos-desktop")
        == "https://github.com/awobaba1/realityos-desktop"
    )


def test_normalize_repo_url_https_with_dotgit():
    assert (
        release._normalize_repo_url("https://github.com/awobaba1/realityos-desktop.git")
        == "https://github.com/awobaba1/realityos-desktop"
    )


def test_normalize_repo_url_ssh_scheme():
    assert (
        release._normalize_repo_url(
            "ssh://git@github.com/awobaba1/realityos-desktop.git"
        )
        == "https://github.com/awobaba1/realityos-desktop"
    )


def test_get_repo_url_resolves_to_fork_not_upstream():
    """get_repo_url 须指向 fork (awobaba1)，不是 NousResearch 上游。"""
    url = release.get_repo_url()
    assert "NousResearch" not in url, f"repo_url 仍指向上游: {url}"
    assert url.startswith("https://github.com/"), f"非 HTTPS github URL: {url}"
    assert url.endswith("realityos-desktop"), f"非预期仓库: {url}"


if __name__ == "__main__":
    test_parse_log_splits_all_z_separated_records()
    test_get_commits_source_uses_z_flag()
    test_get_commits_runs_and_parses_head()
    test_normalize_repo_url_ssh_form()
    test_normalize_repo_url_ssh_no_dotgit()
    test_normalize_repo_url_https_with_dotgit()
    test_normalize_repo_url_ssh_scheme()
    test_get_repo_url_resolves_to_fork_not_upstream()
    print("ADR-V6-079 changelog parse + repo_url guards passed")
    sys.exit(0)
