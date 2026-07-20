#!/usr/bin/env python3
"""Reception pre-flight — 创始人邀请外部用户前的分发物硬门禁。

ADR-V6-038 D1。把「Explore 核实的散件」(gh release / install.sh@commit 可达性 /
本地运行时就绪)拼成一个 exit-code 门禁:全绿才「可邀请外部用户」,任一红 exit 1。

边界(诚实):
- 这是「创始人/运营者视角的分发物门禁」,不是「外部用户客户端就绪」——后者由
  桌面 onboarding 的 evaluateRuntimeReadiness 已覆盖(C8)。
- V6 当前是纯本地桌面拓扑(每用户自己 hermes + 自己的 LLM key + 自己的 bot token,
  无中心服务端),故不含「服务端健康」项。

C7(无静默失败):每个 CHECK 用 try/except,失败给明确原因,不静默放行。
反假绿:资产 size 对照历史量级(真资产 138MB/118MB),反 20B 假文件教训。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "awobaba1/realityos-desktop"
# 真资产 ~118-138MB;50MB 下限反「20B 假备份」式空/畸文件(见 v5-backup-broken 教训)。
MIN_ASSET_BYTES = 50_000_000
INSTALL_SH_RAW_FMT = (
    "https://raw.githubusercontent.com/{repo}/{commit}/scripts/install.sh"
)
# raw.githubusercontent.com 直连可达(代理规则:GitHub 系直连有效,不套代理)。
REQUEST_TIMEOUT = 20


class CheckResult:
    __slots__ = ("name", "ok", "detail")

    def __init__(self, name: str, ok: bool, detail: str = "") -> None:
        self.name = name
        self.ok = ok
        self.detail = detail


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(text: str) -> str:
    return _color(text, "32")


def _red(text: str) -> str:
    return _color(text, "31")


def _cyan(text: str) -> str:
    return _color(text, "36")


def _gh_json(args: list[str]) -> tuple[bool, object, str]:
    """Run a gh subcommand expecting JSON. Returns (ok, parsed_or_None, stderr)."""
    if shutil.which("gh") is None:
        return False, None, "gh CLI not installed (https://cli.github.com)"
    try:
        proc = subprocess.run(
            ["gh", *args, "--repo", REPO],
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT * 2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, None, "gh timed out"
    if proc.returncode != 0:
        return False, None, (proc.stderr or proc.stdout or "gh failed").strip()
    try:
        return True, json.loads(proc.stdout), ""
    except json.JSONDecodeError as exc:
        return False, None, f"gh returned non-JSON: {exc}"


def resolve_tag(tag: str | None) -> tuple[bool, str | None, str]:
    """Resolve the target tag (latest if None). Returns (ok, tag, detail)."""
    if tag:
        return True, tag, ""
    ok, data, err = _gh_json(["release", "view", "--json", "tagName"])
    if not ok:
        return False, None, err
    assert isinstance(data, dict)
    return True, str(data.get("tagName") or ""), ""


def check_release_assets(tag: str) -> CheckResult:
    ok, data, err = _gh_json(["release", "view", tag, "--json", "tagName,assets,targetCommitish"])
    if not ok:
        return CheckResult(f"Release {tag} 存在", False, err)
    assert isinstance(data, dict)
    assets = data.get("assets") or []
    if len(assets) < 2:
        return CheckResult(
            f"Release {tag} 资产齐全(dmg+exe)",
            False,
            f"仅 {len(assets)} 个资产,期望 ≥2(dmg+exe)",
        )
    names = {str(a.get("name") or "") for a in assets}
    has_dmg = any(n.endswith(".dmg") for n in names)
    has_exe = any(n.endswith(".exe") for n in names)
    if not (has_dmg and has_exe):
        return CheckResult(
            "资产含 dmg+exe",
            False,
            f"资产名 {sorted(names)} 缺 dmg 或 exe",
        )
    return CheckResult(
        f"Release {tag} 资产齐全(dmg+exe)",
        True,
        f"{len(assets)} 资产,tag→{data.get('targetCommitish')}",
    )


def check_asset_sizes(tag: str) -> CheckResult:
    ok, data, err = _gh_json(["release", "view", tag, "--json", "assets"])
    if not ok:
        return CheckResult("资产 size 非空非畸", False, err)
    assert isinstance(data, dict)
    assets = data.get("assets") or []
    bad: list[str] = []
    sizes: list[str] = []
    for a in assets:
        name = str(a.get("name") or "")
        size = int(a.get("size") or 0)
        sizes.append(f"{name}={size}")
        if size < MIN_ASSET_BYTES:
            bad.append(f"{name}={size}B < {MIN_ASSET_BYTES}B 下限")
    if bad:
        return CheckResult("资产 size 非空非畸", False, "; ".join(bad))
    return CheckResult("资产 size 非空非畸", True, "; ".join(sizes))


def _release_commitish(tag: str) -> str | None:
    """Resolve a tag to the actual commit SHA it points at.

    `gh release view --json targetCommitish` returns the branch (e.g. 'main'),
    not the commit — useless for pinning install.sh@commit. Use the commits
    API, which derefs annotated tags automatically. Bypasses _gh_json's --repo
    appending (the api path is already absolute)."""
    if shutil.which("gh") is None:
        return None
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{REPO}/commits/{tag}", "--jq", ".sha"],
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT * 2,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def check_install_sh_reachable(tag: str) -> CheckResult:
    commit = _release_commitish(tag)
    if not commit:
        return CheckResult("install.sh@commit 可达", False, "无法解析 tag→commit")
    url = INSTALL_SH_RAW_FMT.format(repo=REPO, commit=commit)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "reception-preflight"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read(2048)
            status = resp.status
    except urllib.error.HTTPError as exc:
        return CheckResult("install.sh@commit 可达", False, f"HTTP {exc.code} {url}")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # OSError 涵盖 ConnectionError/RemoteDisconnected(网络断连);HTTPException 涵盖
        # HTTP 解析异常。实跑曾因 RemoteDisconnected 逃逸 URLError 而崩——C7:全捕,不静默。
        return CheckResult("install.sh@commit 可达", False, f"{type(exc).__name__}: {exc}")
    if status != 200:
        return CheckResult("install.sh@commit 可达", False, f"HTTP {status} {url}")
    if len(body) < 1000:
        return CheckResult("install.sh@commit 可达", False, f"install.sh 仅 {len(body)}B,疑似空/畸")
    return CheckResult(f"install.sh@{commit[:8]} 可达", True, f"HTTP 200, {len(body)}+ bytes")


def check_public_download(tag: str) -> CheckResult:
    """可选慢检查:公开下载 HTTP 200(跟 302 到 release-assets CDN 终态)。"""
    ok, data, err = _gh_json(["release", "view", tag, "--json", "assets"])
    if not ok:
        return CheckResult("公开下载 HTTP 200", False, err)
    assert isinstance(data, dict)
    assets = data.get("assets") or []
    problems: list[str] = []
    for a in assets:
        name = str(a.get("name") or "")
        url = str(a.get("url") or "")
        if not url:
            problems.append(f"{name} 无 url")
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "reception-preflight"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    problems.append(f"{name} HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            problems.append(f"{name} HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            problems.append(f"{name} {type(exc).__name__}")
    if problems:
        return CheckResult("公开下载 HTTP 200", False, "; ".join(problems))
    return CheckResult("公开下载 HTTP 200", True, f"{len(assets)} 资产全 200")


def check_local_readiness() -> CheckResult:
    """本地 hermes 运行时就绪子集(state.db 可读 + config 有效 + model 配了)。

    复用 gateway/readiness.py 的 probe 思路,但聚焦门禁需要的硬条件,不依赖
    gateway 运行(纯本地桌面拓扑)。"""
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    home_path = Path(home)
    problems: list[str] = []

    # state.db 可读
    state_db = home_path / "state.db"
    if state_db.exists():
        try:
            uri = f"file:{state_db.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
                conn.execute("PRAGMA query_only = ON")
                conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 — 门禁诊断必须给明确原因
            problems.append(f"state.db 不可读({type(exc).__name__})")
    # state.db 不存在不算红(全新安装未初始化),仅 info。

    # config 有效(yaml 可解析为 mapping,若存在)
    config = home_path / "config.yaml"
    if config.exists():
        try:
            import yaml  # type: ignore[import-untyped]

            raw = yaml.safe_load(config.read_text(encoding="utf-8"))
            if raw is not None and not isinstance(raw, dict):
                problems.append("config.yaml 顶层非 mapping")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"config.yaml 无效({type(exc).__name__})")

    # model 配了(.env DEEPSEEK_API_KEY / 智谱 / 或 config model)
    env_path = home_path / ".env"
    has_key = False
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8", errors="replace")
        for kw in ("DEEPSEEK_API_KEY", "ZHIPU", "OPENAI_API_KEY", "ANTHROPIC"):
            if kw in text:
                has_key = True
                break
    if not has_key:
        problems.append("未检测到 LLM provider key(.env 无 DEEPSEEK/ZHIPU/OPENAI/ANTHROPIC)")

    if problems:
        return CheckResult("本地 hermes 运行时就绪", False, "; ".join(problems))
    return CheckResult("本地 hermes 运行时就绪", True, f"home={home}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reception pre-flight(ADR-V6-038 D1):创始人邀请外部用户前的分发物硬门禁。",
    )
    parser.add_argument("--tag", default=None, help="目标 Release tag(默认最新)")
    parser.add_argument(
        "--probe-download",
        action="store_true",
        help="加跑公开下载 HTTP200 慢检查(默认跳过,CI/网差时省时)",
    )
    parser.add_argument(
        "--skip-local",
        action="store_true",
        help="跳过本地 hermes 就绪检查(纯分发物门禁时用)",
    )
    args = parser.parse_args(argv)

    print()
    print(_cyan("┌─────────────────────────────────────────────────────────┐"))
    print(_cyan("│        🛂 Reception Pre-flight (ADR-V6-038 D1)          │"))
    print(_cyan("└─────────────────────────────────────────────────────────┘"))

    ok, tag, err = resolve_tag(args.tag)
    if not ok or not tag:
        print(_red(f"  ✗ 无法解析目标 tag:{err}"))
        return 1
    print(_cyan(f"  目标 tag:{tag}"))
    print()

    results: list[CheckResult] = [
        check_release_assets(tag),
        check_asset_sizes(tag),
        check_install_sh_reachable(tag),
    ]
    if args.probe_download:
        results.append(check_public_download(tag))
    if not args.skip_local:
        results.append(check_local_readiness())

    all_ok = True
    for r in results:
        mark = _green("✓") if r.ok else _red("✗")
        line = f"  {mark} {r.name}"
        if r.detail:
            line += f"  — {r.detail}"
        print(line)
        if not r.ok:
            all_ok = False

    print()
    if all_ok:
        print(_green("  ✓ 全绿:可邀请外部用户。"))
        return 0
    print(_red("  ✗ 有红项:修复后再邀请(反假绿——分发物不可达不静默放行)。"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
