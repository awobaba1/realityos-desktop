"""ADR-V6-077 C4 回归：release.py bump version 后必须 uv lock + 提交 uv.lock。

根因：release.py update_version_files 改 pyproject version 但不更新 uv.lock
→ CI ``uv sync --locked`` 失败（lockfile out of sync）→ 所有 Python CI 静默红
（install 阶段挂，测试根本没跑）。与 ADR-V6-076 P0-2 同类（silent CI red）。

本守卫锚 binding 形态（subprocess 调 uv lock / add_files 含 uv.lock / 失败 return 1）
防回归；空白规范化防绕过（同 ADR-V6-075 publish 守卫思路）。
"""

import re
import sys
from pathlib import Path

RELEASE_PY = Path(__file__).resolve().parents[2] / "scripts" / "release.py"


def _source() -> str:
    return RELEASE_PY.read_text(encoding="utf-8")


def test_bump_runs_uv_lock():
    """bump 流程必须 subprocess.run(["uv", "lock"]) 重新生成 uv.lock。"""
    src = _source()
    # 锚 binding：subprocess.run 的参数列表含 "uv" 与 "lock"（容忍空白）
    assert re.search(r'\[\s*"uv"\s*,\s*"lock"\s*\]', src), (
        "release.py bump 流程必须 subprocess.run(['uv', 'lock']) 更新 uv.lock，"
        "否则 CI uv sync --locked 静默红（ADR-V6-077）"
    )


def test_uvlock_failure_returns_nonzero():
    """uv lock 失败必须 return 1（不静默 exit 0，类 ADR-V6-076 P0-2）。"""
    src = _source()
    m = re.search(r'\[\s*"uv"\s*,\s*"lock"\s*\]', src)
    assert m is not None, "prerequisite: uv lock 调用存在"
    after = src[m.end():]
    # uv lock 调用之后必须存在 return 1 失败分支
    assert re.search(r"return\s+1", after), "uv lock 失败/超时必须 return 1"


def test_uvlock_in_add_files():
    """bump commit add_files 必须包含 uv.lock。"""
    src = _source()
    # 锚 binding：add_files 列表含 "uv.lock"（容忍中间元素与空白）
    assert re.search(r'add_files\s*=\s*\[[^\]]*"uv\.lock"', src, re.DOTALL), (
        "release.py bump commit add_files 必须含 uv.lock，否则 uv.lock 不随 bump 提交"
    )


if __name__ == "__main__":
    test_bump_runs_uv_lock()
    test_uvlock_failure_returns_nonzero()
    test_uvlock_in_add_files()
    print("ADR-V6-077 uvlock guards passed")
    sys.exit(0)
