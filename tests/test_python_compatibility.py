"""실제 Python 3.11로 소스 문법을 검사해 상위 버전의 parser 차이를 피한다."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_PATHS = sorted(
    [*_ROOT.glob("*.py")]
    + [
        path
        for directory in ("domain", "tools", "scripts", "tests")
        for path in (_ROOT / directory).rglob("*.py")
    ],
)


def test_sources_compile_on_python_311():
    """새 문법이 섞이면 실패하며 검사할 3.11이 없을 때도 조용히 통과하지 않는다."""
    interpreter = (
        sys.executable if sys.version_info[:2] == (3, 11)
        else os.environ.get("PYTHON311") or shutil.which("python3.11")
    )
    if not interpreter:
        pytest.fail(
            "Python 3.11이 필요합니다. 상위 버전에서 검사하려면 "
            "PYTHON311=/path/to/python3.11을 지정하세요. 이 검사는 표준 라이브러리만 사용합니다.",
        )
    assert _SOURCE_PATHS, "검사할 Python 소스가 없습니다."
    result = subprocess.run(
        [
            interpreter, "-I", "-B", "-c",
            """
import json
import sys
from pathlib import Path

if sys.version_info[:2] != (3, 11):
    raise SystemExit("PYTHON311 must point to Python 3.11")
errors = []
for filename in json.load(sys.stdin):
    path = Path(filename)
    try:
        compile(path.read_bytes(), str(path), "exec")
    except SyntaxError as exc:
        errors.append(f"{path}:{exc.lineno}: {exc.msg}")
print("\\n".join(errors))
raise SystemExit(bool(errors))
""",
        ],
        input=json.dumps([str(path) for path in _SOURCE_PATHS]),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
