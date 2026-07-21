"""ADR-V6-079 C4 回归：release.py changelog 必须解析全部提交 + 链接指向 fork。

两个静默缺陷（C7 静默失败）：

1. **changelog 截断到 1 条**：get_commits 跑 `git log --format=...%x00%b%x00`
   （无 `-z`）后按 `\\0\\0` 切分。但 git 用 `\\0\\n`（NUL+换行）分隔 log 条目，
   于是整个范围塌缩成**一**块 → 只解析到 tag 后的首个提交 → 每个 release 的
   自动 changelog 都被静默截断到 1 条。修：加 `-z`，git 用 NUL 终止每条记录，
   相邻记录间形成 `\\0\\0`。
2. **changelog 链接 404**：generate_changelog 默认 repo_url 指向 NousResearch
   上游，但提交在 awobaba1 fork 上 → 上游 commit/compare 链接 404。修：
   get_repo_url 从 `git remote get-url origin` 派生 HTTPS URL。

本守卫：
- 动态比对 get_commits 与 `git rev-list --count` 防回归（-z 丢失 → 只剩 1 条）。
- 锁定 _normalize_repo_url 三种 remote 形式（SSH / ssh:// / HTTPS，±.git）。
- 集成校验 get_repo_url 实际指向 fork 而非上游。
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release  # noqa: E402


def _revlist_count(since_tag: str) -> int:
    return int(
        subprocess.run(
            ["git", "rev-list", "--count", f"{since_tag}..HEAD", "--no-merges"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(REPO_ROOT),
        ).stdout.strip()
    )


def test_get_commits_returns_all_records_in_range():
    """-z 修复后 get_commits 须解析 tag 后的全部提交，而非仅首条。

    无 -z 时 git log 用 \\0\\n 分隔条目，split("\\0\\0") 把整段塌缩成 1 块 →
    只解析首提交。守卫动态比对 rev-list 计数，range 须 ≥2 条才有意义。
    """
    since_tag = "v2026.7.26"
    expected = _revlist_count(since_tag)
    assert expected >= 2, (
        f"test 需要 {since_tag}..HEAD 至少 2 条提交才有回归意义，实际 {expected}"
    )
    commits = release.get_commits(since_tag=since_tag)
    assert len(commits) == expected, (
        f"get_commits 解析到 {len(commits)} 条，期望 {expected} 条"
        f"（回归：-z 丢失 → 只解析首提交，ADR-V6-079）"
    )
    shas = {c["sha"] for c in commits}
    assert len(shas) == expected, "解析出的提交存在重复 sha"


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
    test_get_commits_returns_all_records_in_range()
    test_normalize_repo_url_ssh_form()
    test_normalize_repo_url_ssh_no_dotgit()
    test_normalize_repo_url_https_with_dotgit()
    test_normalize_repo_url_ssh_scheme()
    test_get_repo_url_resolves_to_fork_not_upstream()
    print("ADR-V6-079 changelog parse + repo_url guards passed")
    sys.exit(0)
