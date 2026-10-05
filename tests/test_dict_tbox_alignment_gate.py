"""딕셔너리↔T-Box 정합 게이트 — 유령 항목을 통과시키지 않는지 고정.

## 왜

``validate_semantic_dictionary`` 는 **단방향 검사만** 했다:

- ``tbox - dict`` (누락) → ``high`` → ``passed=False``
- ``dict - tbox`` (유령) → ``warning`` → **``passed`` 에 영향 없음**
- **DatatypeProperty 는 아예 대조하지 않았다**
- **유령 ObjectProperty 도 규칙이 없었다**

실측 (2026-08-11): S3 가 ``GHGScope`` / ``ghgScopeEnum`` 을 지운 뒤에도 딕셔너리는
그 두 클래스를 선언한 채 ``passed=True`` 를 냈다. 낡은 딕셔너리로 재현하면 유령
OP 125개 / 유령 DP 33개 / 누락 DP 29개가 있는데 검출은 **0건**이었다.

딕셔너리는 LLM NL→SPARQL 레퍼런스다 — 존재하지 않는 이름을 제시하면 질의가
조용히 0건을 반환한다. 그래서 누락과 유령은 **대칭 심각도** 여야 한다.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from tools.semantic_dict_validation import _validate

_NS = None


def _ns() -> str:
    global _NS
    if _NS is None:
        from domain.namespaces import DOMAIN_NS
        _NS = DOMAIN_NS
    return _NS


def _tbox_file(body: str) -> str:
    from domain.namespaces import NS_PREFIX

    hdr = (
        f"@prefix {NS_PREFIX}: <{_ns()}> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
    )
    # mkstemp 는 파일을 원자적으로 만들고 fd 를 함께 준다. mktemp 는 이름만
    # 만들어 돌려주므로 그 이름을 열기까지의 틈에 다른 프로세스가 끼어들 수 있다.
    fd, path = tempfile.mkstemp(suffix=".ttl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(hdr + body)
    return path


def _minimal_dict(classes: dict, ops: dict | None = None) -> dict:
    """필수 섹션을 채운 최소 딕셔너리 — 정합 검사 외 노이즈를 없앤다."""
    return {
        "metadata": {"version": "test"},
        "classes": classes,
        "object_properties": ops or {},
        "sparql_guide": {
            "engine_compatibility": {"x": 1},
            "anti_patterns": [{"x": 1}],
            "common_patterns": [{"x": 1}],
        },
        "common_mistakes": [{"x": 1}],
        "question_templates": [{"x": 1}],
        "class_quick_reference": {"Alpha": "요약"},
        "process_flow": [{"x": 1}],
    }


_TBOX_BODY = """
{p}:Alpha a owl:Class ; rdfs:label "알파"@ko .
{p}:alphaName a owl:DatatypeProperty ; rdfs:domain {p}:Alpha ; rdfs:range xsd:string .
"""


@pytest.fixture
def tbox_path():
    from domain.namespaces import NS_PREFIX

    path = _tbox_file(_TBOX_BODY.format(p=NS_PREFIX))
    yield path
    os.unlink(path)


def _aligned_dict() -> dict:
    return _minimal_dict({
        "Alpha": {
            "label_ko": "알파", "label_en": "alpha", "description_ko": "설명",
            "datatype_properties": {"alphaName": {"range": "string"}},
        },
    })


def _rules(res: dict) -> set[str]:
    return {i["rule"] for i in res["issues"]}


# ── PRESERVATION 먼저: 정합 딕셔너리는 통과한다 ──────────────────────


def test_aligned_dictionary_passes(tbox_path):
    """거짓 양성 없음 — 완전 정합이면 high 0 / passed True."""
    res = _validate(_aligned_dict(), tbox_path, "")
    high = [i for i in res["issues"] if i["severity"] == "high"]
    assert res["passed"] is True, f"정합인데 막았다: {high}"
    assert high == []
    assert res["coverage"]["dp_match_rate"] == 100.0


# ── THE REGRESSION: 유령을 잡는다 ─────────────────────────────────────


def test_ghost_class_fails_the_gate(tbox_path):
    """핵심 회귀: T-Box 에 없는 클래스를 선언하면 통과시켜선 안 된다.

    예전에는 ``warning`` 이라 ``passed=True`` 였다 — 삭제된 GHGScope 가 그렇게
    딕셔너리에 살아남았다.
    """
    d = _aligned_dict()
    d["classes"]["GhostClass"] = {
        "label_ko": "유령", "label_en": "ghost", "description_ko": "x",
        "datatype_properties": {},
    }
    res = _validate(d, tbox_path, "")
    assert res["passed"] is False, "유령 클래스를 통과시켰다"
    assert "class_extra_in_dict" in _rules(res)
    assert any(
        i["severity"] == "high" and i["rule"] == "class_extra_in_dict"
        for i in res["issues"]
    ), "유령 클래스가 high 가 아니다 (passed 에 영향 없음)"


def test_ghost_object_property_fails_the_gate(tbox_path):
    """유령 OP — 예전에는 규칙이 아예 없어 125개가 통과했다."""
    d = _aligned_dict()
    d["object_properties"]["ghostOp"] = {"label_ko": "유령", "domain": [], "range": []}
    res = _validate(d, tbox_path, "")
    assert res["passed"] is False, "유령 OP 를 통과시켰다"
    assert "objprop_extra_in_dict" in _rules(res)


def test_ghost_datatype_property_fails_the_gate(tbox_path):
    """유령 DP — 예전에는 DP 를 전혀 대조하지 않았다."""
    d = _aligned_dict()
    d["classes"]["Alpha"]["datatype_properties"]["ghostDp"] = {"range": "string"}
    res = _validate(d, tbox_path, "")
    assert res["passed"] is False, "유령 DP 를 통과시켰다"
    assert "dp_extra_in_dict" in _rules(res)


def test_missing_datatype_property_fails_the_gate(tbox_path):
    """누락 DP 도 잡는다 (양방향)."""
    d = _aligned_dict()
    d["classes"]["Alpha"]["datatype_properties"] = {}
    res = _validate(d, tbox_path, "")
    assert res["passed"] is False, "누락 DP 를 통과시켰다"
    assert "dp_missing_in_dict" in _rules(res)


# ── PRESERVATION: DP 대조 스코프가 비대칭이면 안 된다 ─────────────────


def test_dp_of_a_class_absent_from_the_dictionary_is_not_reported_missing():
    """딕셔너리에 없는 클래스의 DP 를 "누락" 으로 세면 안 된다.

    딕셔너리 DP 는 ``classes[*]`` 아래에만 있으므로, 그 클래스가 딕셔너리에
    없으면 DP 도 있을 자리가 없다. 그것을 누락으로 세면 클래스 누락 1건이 DP
    누락 N건으로 증폭돼 실패 원인을 가린다.
    """
    from domain.namespaces import NS_PREFIX

    path = _tbox_file(
        _TBOX_BODY.format(p=NS_PREFIX)
        + f"{NS_PREFIX}:Beta a owl:Class ; rdfs:label \"베타\"@ko .\n"
        + f"{NS_PREFIX}:betaCode a owl:DatatypeProperty ; "
          f"rdfs:domain {NS_PREFIX}:Beta ; rdfs:range xsd:string .\n"
    )
    try:
        res = _validate(_aligned_dict(), path, "")
        dp_missing = [
            i for i in res["issues"] if i["rule"] == "dp_missing_in_dict"
        ]
        assert dp_missing == [], (
            f"딕셔너리에 없는 클래스(Beta)의 DP 를 누락으로 셌다: {dp_missing}"
        )
        # 클래스 누락 자체는 여전히 보고돼야 한다.
        assert "class_missing_in_dict" in _rules(res)
    finally:
        os.unlink(path)


def test_dp_without_domain_is_not_reported_missing():
    """``rdfs:domain`` 이 없는 DP 는 딕셔너리에 실릴 자리가 없다 — 누락 아님."""
    from domain.namespaces import NS_PREFIX

    path = _tbox_file(
        _TBOX_BODY.format(p=NS_PREFIX)
        + f"{NS_PREFIX}:orphanDp a owl:DatatypeProperty ; rdfs:range xsd:string .\n"
    )
    try:
        res = _validate(_aligned_dict(), path, "")
        assert res["passed"] is True, (
            f"domain 없는 DP 를 누락으로 셌다: "
            f"{[i for i in res['issues'] if i['severity'] == 'high']}"
        )
    finally:
        os.unlink(path)


def test_deployed_dictionary_is_aligned_with_the_deployed_tbox():
    """실측 고정: 배포 딕셔너리와 배포 T-Box 가 정합이어야 한다.

    이 게이트가 켜진 뒤로는 어긋난 상태로 파이프라인이 진행되면 S11 이 막는다.
    """
    import config

    if not (os.path.exists(config.SEMANTIC_DICT_PATH)
            and os.path.exists(config.TBOX_PATH)):
        pytest.skip("배포 산출물 없음")
    with open(config.SEMANTIC_DICT_PATH, encoding="utf-8") as fh:
        d = json.load(fh)
    res = _validate(d, config.TBOX_PATH, "")
    align = [
        i for i in res["issues"]
        if i["rule"].endswith(("_extra_in_dict", "_missing_in_dict"))
    ]
    assert align == [], f"배포 딕셔너리가 T-Box 와 어긋난다: {align[:5]}"
