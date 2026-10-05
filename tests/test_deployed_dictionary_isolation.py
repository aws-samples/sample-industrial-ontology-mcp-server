"""테스트가 배포 semantic_dictionary.json 을 오염시키지 못하는가.

2026-08-28 실측. 전체 스위트를 돌린 뒤 배포 딕셔너리의 정의 클래스 통계가 사라졌다 —
파이프라인이 만든 v2 가 v1 수준으로 조용히 교체됐다::

    파이프라인 v2 (S10)  RunningEquipmentStatus 2998 / Scope1Emission 50 / 통계 76 클래스
    스위트 실행 후        0 / 0 / 41 클래스

범인은 ``tests/test_semantic_dictionary_value_range.py`` 로, ``monkeypatch`` 없이
``generate_semantic_dictionary()`` 를 인자 없이 3회 부른다. 기본값이
``use_inferred=False`` 라 추론 그래프를 읽지 않고 배포 파일을 덮어쓴다.

## 왜 게이트가 못 잡나

산출물은 **유효한 v2 로 남는다.** version 필드도 맞고 ``validate_semantic_dictionary``
도 통과한다 (class/OP/DP 매칭률 100%). 달라진 건 ``instance_count`` 뿐인데, 딕셔너리의
소비자는 코드가 아니라 **LLM** 이다 — LLM 은 그것을 "그 클래스는 비어있다" 로 읽고
NL→SPARQL 에서 그 클래스를 회피한다. 어느 스키마 검사로도 잡히지 않는다.

``compromise_audit`` / ``debate_log`` / ``quality_history`` 와 같은 사고 형태다
(2026-08-18, 2026-08-25). 그때마다 개별 테스트에 patch 를 추가했지만 새 테스트가 또
오염시켰기 때문에 conftest autouse 로 막는 것이 정본이다.

## 이 테스트의 방향

"픽스처가 있다" 만 주장하면 픽스처가 아무것도 안 해도 통과한다. 세 축을 고정한다:

* 차단 — ``generate_semantic_dictionary`` 를 부르면 배포 파일이 **안** 바뀐다
* 기능 — 그러면서 테스트는 정상 동작한다 (tmp 에 쓰고 읽을 수 있다)
* 범위 — 다른 배포 산출물(T-Box/A-Box)도 쓰기 대상이 아니다
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

DEPLOYED = pathlib.Path("data/generated/semantic_dictionary.json")


def _digest(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 차단: 배포 파일이 바뀌지 않는다 ─────────────────────────────────────


def test_generation_does_not_touch_deployed_file():
    """THE REGRESSION: 딕셔너리를 생성해도 배포 파일은 그대로다.

    autouse ``_isolate_semantic_dictionary`` 가 SEMANTIC_DICT_PATH 를 tmp 로 돌린다.
    이 테스트 자체가 그 픽스처의 보호를 받으므로, 픽스처가 없으면 실패한다.
    """
    before = _digest(DEPLOYED)
    if before is None:
        pytest.skip("배포 딕셔너리 없음")

    from tools.semantic_dictionary import generate_semantic_dictionary

    generate_semantic_dictionary(include_stats=False)

    assert _digest(DEPLOYED) == before, (
        "배포 딕셔너리가 변경됐다 — conftest 의 격리 픽스처가 작동하지 않는다"
    )


def test_module_path_is_redirected():
    """모듈 상수가 tmp 를 가리키는가 — 배선 확인."""
    import tools.semantic_dictionary as sd

    assert "semantic_dictionary.json" in sd.SEMANTIC_DICT_PATH
    assert not sd.SEMANTIC_DICT_PATH.startswith("data/generated"), (
        f"배포 경로를 그대로 쓴다: {sd.SEMANTIC_DICT_PATH}"
    )
    assert str(DEPLOYED.resolve()) != str(pathlib.Path(sd.SEMANTIC_DICT_PATH).resolve())


# ── 기능: 격리해도 테스트가 정상 동작한다 ───────────────────────────────


def test_generated_output_is_readable_from_tmp():
    """리다이렉트된 경로에 실제로 써지고 다시 읽을 수 있는가.

    격리가 "아무 곳에도 안 쓴다" 가 되면 딕셔너리 테스트들이 전부 무의미해진다.
    """
    import tools.semantic_dictionary as sd

    raw = sd.generate_semantic_dictionary(include_stats=False)
    result = json.loads(raw) if isinstance(raw, str) else raw
    assert result.get("success") is True

    written = pathlib.Path(sd.SEMANTIC_DICT_PATH)
    assert written.exists(), "리다이렉트된 경로에 파일이 없다"
    assert json.loads(written.read_text(encoding="utf-8")).get("classes"), (
        "써진 딕셔너리에 classes 가 없다"
    )


# ── 범위: 다른 배포 산출물도 안전한가 ───────────────────────────────────


@pytest.mark.parametrize(
    "artifact",
    [
        "data/generated/tbox/t_box.ttl",
        "data/generated/abox/a_box.ttl",
        "data/generated/inferred/all_inferred.ttl",
    ],
)
def test_dictionary_generation_reads_but_does_not_write_other_artifacts(artifact):
    """딕셔너리 생성은 입력을 **읽기만** 한다.

    generate_semantic_dictionary 가 T-Box/A-Box 를 건드리면 세대 불일치의 원인이 된다.
    """
    path = pathlib.Path(artifact)
    if not path.exists():
        pytest.skip(f"{artifact} 없음")

    before = _digest(path)
    from tools.semantic_dictionary import generate_semantic_dictionary

    generate_semantic_dictionary(include_stats=False)

    assert _digest(path) == before, f"{artifact} 가 변경됐다"


# ── 회귀 원인이 재발하지 않는가 (소스 검사) ────────────────────────────


def test_conftest_declares_the_isolation_fixture():
    """격리가 conftest autouse 로 있는가 — 개별 patch 방식은 재발한다.

    실측 이력: compromise_audit / debate_log / quality_history 셋 다 개별 테스트에
    patch 를 추가했다가 새 테스트가 또 오염시켜 autouse 로 옮겼다.
    """
    src = pathlib.Path("tests/conftest.py").read_text(encoding="utf-8")
    assert "_isolate_semantic_dictionary" in src, "격리 픽스처가 없다"
    idx = src.index("_isolate_semantic_dictionary")
    assert "autouse=True" in src[max(0, idx - 200):idx], "autouse 가 아니다"
    assert "SEMANTIC_DICT_PATH" in src[idx:idx + 2000], "경로 상수를 patch 하지 않는다"
