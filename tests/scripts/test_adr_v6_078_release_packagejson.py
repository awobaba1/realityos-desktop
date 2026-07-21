"""ADR-V6-078 C4 回归：release.py bump version 后必须提交 apps/desktop/package.json。

根因：release.py update_version_files 改写 apps/desktop/package.json 的 version
（与 pyproject lockstep），但 bump commit 的 add_files 不含它 → package.json 留在
工作区未提交 → tag/desktop-build 在 tag 点读到旧 version → desktop 制品版本滞后于
Python（v2026.7.26：desktop dmg/exe=0.18.3，Python wheel=0.18.4，版本错位）。
与 ADR-V6-077 uv.lock 同类（bump 写了文件却没提交 → 静默版本错位，C7）。

本守卫锚 binding 形态（package.json 路径构造 + add_files.append）防回归。
"""

import re
import sys
from pathlib import Path

RELEASE_PY = Path(__file__).resolve().parents[2] / "scripts" / "release.py"


def _source() -> str:
    return RELEASE_PY.read_text(encoding="utf-8")


def test_package_json_path_constructed():
    """release.py 须构造 apps/desktop/package.json 路径（确认它是被 bump 的文件）。"""
    src = _source()
    assert re.search(r'"apps"\s*/\s*"desktop"\s*/\s*"package\.json"', src), (
        "release.py 须引用 apps/desktop/package.json（update_version_files 改写它）"
    )


def test_package_json_in_bump_add_files():
    """bump commit 必须把 apps/desktop/package.json 加入 add_files。

    锚 binding：package.json 路径构造之后，存在 add_files.append 把它纳入提交。
    """
    src = _source()
    m = re.search(r'"apps"\s*/\s*"desktop"\s*/\s*"package\.json"', src)
    assert m is not None, "prerequisite: package.json 路径构造存在"
    after = src[m.start():]
    assert re.search(r"add_files\.append", after), (
        "apps/desktop/package.json 须 append 进 bump commit 的 add_files，"
        "否则 desktop 制品版本滞后于 Python（ADR-V6-078，C7 静默版本错位）"
    )


if __name__ == "__main__":
    test_package_json_path_constructed()
    test_package_json_in_bump_add_files()
    print("ADR-V6-078 package.json commit guards passed")
    sys.exit(0)
