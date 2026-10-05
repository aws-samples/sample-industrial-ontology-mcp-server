"""JVM heap 노브가 owlready2 기본값을 실제로 올리는가.

2026-08-28 실측. owlready2 의 ``JAVA_MEMORY`` 기본값은 2000(MB) 이고, 이 프로젝트의
추론 그래프(1.37M 트리플)에서 Pellet 이 죽었다::

    Exception: java.lang.OutOfMemoryError thrown from the UncaughtExceptionHandler
    in thread "main"

24000 으로 올리면 통과한다. 이 프로젝트는 그 값을 설정하지 않았으므로 ``S8.5 SWRL``
과 ``validate_owl_realisation`` 은 큰 KG 에서 항상 OOM 이었다.

## 이 테스트가 고정하는 세 축

* 반영   — 값을 주면 owlready2 모듈 전역에 실제로 대입된다 (설정만 읽고 버리지 않는가)
* 보존   — 미설정이면 owlready2 기본값을 건드리지 않는다
* 방어   — 잘못된 값이 **모듈 import 를 깨지 않는다**

세 번째가 중요하다: 이 대입은 import 시점에 돌기 때문에 ``ValueError`` 가 나면 MCP
서버 기동 자체가 실패한다. 실제로 첫 구현이 ``JAVA_MEMORY_MB=abc`` 에서 그렇게 깨졌다.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

_PROBE = (
    "import sys; sys.path.insert(0, '.');"
    "import tools.owl_reasoner, owlready2.reasoning as r;"
    "from config import JAVA_MEMORY_MB;"
    "print(f'{JAVA_MEMORY_MB}|{r.JAVA_MEMORY}')"
)

#: owlready2's own default — the value the knob must not disturb when unset.
_OWLREADY_DEFAULT = 2000


def _probe(value: str | None) -> tuple[int, int]:
    """Run a subprocess with JAVA_MEMORY_MB=value, return (config, owlready2)."""
    import os

    env = dict(os.environ)
    if value is None:
        env.pop("JAVA_MEMORY_MB", None)
    else:
        env["JAVA_MEMORY_MB"] = value

    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert result.returncode == 0, (
        f"JAVA_MEMORY_MB={value!r} 로 모듈 import 가 실패했다 — "
        f"서버 기동이 막힌다:\n{result.stderr[-500:]}"
    )
    for line in reversed(result.stdout.strip().splitlines()):
        if "|" in line:
            cfg, owl = line.rsplit("|", 1)
            return int(cfg), int(owl)
    raise AssertionError(f"프로브 출력을 못 읽었다: {result.stdout[-300:]}")


# ── 반영 ────────────────────────────────────────────────────────────────


def test_value_reaches_owlready():
    """THE REGRESSION: 설정한 heap 이 owlready2 에 실제로 대입된다.

    config 만 읽고 owlready2 에 전달하지 않으면 노브가 no-op 이 된다 — 이 리포가
    반복 겪은 "설정은 켜져 있는데 효과는 0" 유형이다.
    """
    _, owlready = _probe("24000")
    assert owlready == 24000


def test_config_exposes_the_value():
    config, _ = _probe("8000")
    assert config == 8000


# ── 보존: 미설정이면 기본값을 건드리지 않는다 ───────────────────────────


def test_unset_leaves_owlready_default():
    """미설정 → owlready2 기본값 유지. 임의 값을 강요하지 않는다."""
    config, owlready = _probe(None)
    assert config == 0
    assert owlready == _OWLREADY_DEFAULT


@pytest.mark.parametrize("value", ["0", "", "-5"])
def test_non_positive_values_leave_default(value):
    """0/빈값/음수는 "설정 안 함" 이다 — 음수 heap 을 JVM 에 넘기면 죽는다."""
    _, owlready = _probe(value)
    assert owlready == _OWLREADY_DEFAULT


# ── 방어: 잘못된 값이 서버 기동을 막지 않는다 ───────────────────────────


@pytest.mark.parametrize("value", ["abc", "24_000MB", "1e9", "  "])
def test_malformed_values_do_not_break_import(value):
    """비수치 값이 import 를 깨지 않는다.

    이 대입은 import 시점에 돌기 때문에 예외가 나면 MCP 서버가 기동하지 못한다.
    첫 구현이 실제로 ``JAVA_MEMORY_MB=abc`` 에서 ValueError 로 깨졌다.
    ``_probe`` 가 returncode 를 검사하므로 import 실패면 여기서 잡힌다.
    """
    _, owlready = _probe(value)
    assert owlready == _OWLREADY_DEFAULT, "잘못된 값이 heap 을 바꿨다"


# ── 배선: 두 곳이 같은 환경변수를 본다 ─────────────────────────────────


def test_parsing_is_not_duplicated():
    """파싱은 config 한 곳에서만 한다 — 사본을 두면 방어 수준이 어긋난다.

    config 에 이미 ``_safe_int`` 가 있으므로 owl_reasoner 는 그 결과를 import 한다.
    자체 ``getenv`` + ``int()`` 를 두면 한쪽만 잘못된 값에 방어하는 상태가 생긴다.
    """
    import pathlib

    config_src = pathlib.Path("config.py").read_text(encoding="utf-8")
    assert '_safe_int("JAVA_MEMORY_MB"' in config_src, "config 가 공용 파서를 안 쓴다"

    reasoner_src = pathlib.Path("tools/owl_reasoner.py").read_text(encoding="utf-8")
    assert "JAVA_MEMORY_MB" in reasoner_src, "owl_reasoner 가 값을 참조하지 않는다"
    assert 'getenv("JAVA_MEMORY_MB"' not in reasoner_src, (
        "owl_reasoner 가 파싱 사본을 만들었다 — config 의 값을 import 하라"
    )


def test_documented_in_env_example():
    """``.env.example`` 에 노출됐는가 — 없으면 아무도 존재를 모른다."""
    import pathlib

    assert "JAVA_MEMORY_MB" in pathlib.Path(".env.example").read_text(encoding="utf-8")
