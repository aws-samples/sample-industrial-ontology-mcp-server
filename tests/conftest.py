"""Shared fixtures for the ontology-agent test suite.

Fixture scope 정책 (#10):
- 모든 TTL 문자열/SHACL shape 픽스처는 `scope="function"`으로 명시한다.
- 테스트가 픽스처 값을 in-place 변형해도 다음 테스트 호출 시 새 값이 공급된다.
- rdflib Graph는 픽스처로 반환하지 않는다. 테스트마다
  `_new_graph()` + `g.parse(data=<ttl 문자열>)` 로 독립 그래프를 만든다.
  (Graph는 mutable이라 session-scope로 공유하면 이전 테스트의
  add()/remove()가 다음 테스트에 누수됨.)
- 아래 pytest_collection_modifyitems 훅이 Graph 타입을 session/module scope로
  등록한 픽스처를 자동 거부한다.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, NS_INST_PREFIX, NS_PREFIX


def pytest_collection_modifyitems(config, items):
    """rdflib Graph 픽스처가 session/module scope로 등록되면 수집 단계에서 실패.

    이렇게 하면 향후 기여자가 실수로 `scope="session"` Graph fixture를 추가하더라도
    조용히 상태가 새지 않고 즉시 감지된다.
    """
    try:
        from rdflib import Graph as _RdfGraph
    except Exception:  # pragma: no cover — rdflib 미설치 환경 방어
        return
    seen: set[str] = set()
    for item in items:
        if not hasattr(item, "_fixtureinfo"):
            continue
        fm = item.session._fixturemanager
        for fixture_name in getattr(item._fixtureinfo, "argnames", ()):
            if fixture_name in seen:
                continue
            seen.add(fixture_name)
            fixtures = fm._arg2fixturedefs.get(fixture_name, [])
            for fixdef in fixtures:
                scope = getattr(fixdef, "scope", "function")
                if scope == "function":
                    continue
                # 픽스처 함수 어노테이션이 Graph인 경우만 차단 (하드 차단 대신 경고로 시작)
                fn = getattr(fixdef, "func", None)
                ann = getattr(fn, "__annotations__", {}) if fn else {}
                ret = ann.get("return") if ann else None
                if ret is _RdfGraph:
                    raise pytest.UsageError(
                        f"Fixture '{fixture_name}'는 rdflib.Graph를 반환하는데 "
                        f"scope='{scope}'입니다. Graph는 mutable이므로 반드시 "
                        f"scope='function'이어야 합니다."
                    )


@pytest.fixture(scope="session", name="pytest_collected_node_ids")
def collected_node_ids_fixture():
    """공개 도구 계약 파일의 실제 pytest 수집 node id를 반환한다."""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/public/test_tool_contracts.py",
            "--collect-only",
            "-q",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "공개 도구 계약 테스트 수집에 실패했습니다:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return frozenset(
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/public/test_tool_contracts.py::")
    )

# 테스트 픽스처용 PREFIX 블록 (domain_config.json 기반)
_PFX = NS_PREFIX
_PFX_INST = NS_INST_PREFIX
_NS = DOMAIN_NS
_NS_INST = DOMAIN_INST_NS


@pytest.fixture(scope="function")
def sample_tbox_ttl():
    """Minimal valid T-Box with 2 classes, 1 ObjectProperty, 2 DatatypeProperties."""
    return f"""\
@prefix {_PFX}: <{_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

{_PFX}:EquipmentMaster a owl:Class ;
    rdfs:label "Equipment Master"@en, "설비 마스터"@ko ;
    rdfs:comment "모든 설비의 기본 정의"@ko .

{_PFX}:EquipmentStatus a owl:Class ;
    rdfs:label "Equipment Status"@en, "설비 상태"@ko ;
    rdfs:comment "설비의 현재 상태 정보"@ko .

{_PFX}:hasEquipmentStatus a owl:ObjectProperty ;
    rdfs:domain {_PFX}:EquipmentMaster ;
    rdfs:range {_PFX}:EquipmentStatus ;
    rdfs:label "has equipment status"@en, "설비 상태 관계"@ko ;
    rdfs:comment "설비의 현재 상태를 나타냄"@ko .

{_PFX}:equipmentID a owl:DatatypeProperty ;
    rdfs:domain {_PFX}:EquipmentMaster ;
    rdfs:range xsd:string ;
    rdfs:label "equipment ID"@en, "설비 ID"@ko ;
    rdfs:comment "설비 고유 식별자"@ko .

{_PFX}:status a owl:DatatypeProperty ;
    rdfs:domain {_PFX}:EquipmentStatus ;
    rdfs:range xsd:string ;
    rdfs:label "status"@en, "상태"@ko ;
    rdfs:comment "설비 가동 상태 값"@ko .
"""


@pytest.fixture(scope="function")
def sample_abox_ttl():
    """Minimal A-Box with 2 instances."""
    return f"""\
@prefix {_PFX}: <{_NS}> .
@prefix {_PFX_INST}: <{_NS_INST}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

{_PFX_INST}:EquipmentMaster_EQ001 a {_PFX}:EquipmentMaster ;
    {_PFX}:equipmentID "EQ001" .

{_PFX_INST}:EquipmentStatus_EQ001 a {_PFX}:EquipmentStatus ;
    {_PFX}:status "Running" .
"""


@pytest.fixture(scope="function")
def invalid_ttl():
    """An invalid TTL string."""
    return "this is not valid turtle @@@[[[{"


@pytest.fixture(scope="function")
def sample_tbox_circular():
    """T-Box with TransitiveProperty + inverseOf (circular pattern)."""
    return f"""\
@prefix {_PFX}: <{_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

{_PFX}:EquipmentMaster a owl:Class ;
    rdfs:label "Equipment Master"@en, "설비 마스터"@ko ;
    rdfs:comment "설비 마스터"@ko .

{_PFX}:isPartOf a owl:ObjectProperty, owl:TransitiveProperty ;
    rdfs:domain {_PFX}:EquipmentMaster ;
    rdfs:range {_PFX}:EquipmentMaster ;
    owl:inverseOf {_PFX}:hasPart ;
    rdfs:label "is part of"@en, "부분"@ko ;
    rdfs:comment "부분-전체 관계"@ko .

{_PFX}:hasPart a owl:ObjectProperty ;
    rdfs:domain {_PFX}:EquipmentMaster ;
    rdfs:range {_PFX}:EquipmentMaster ;
    owl:inverseOf {_PFX}:isPartOf ;
    rdfs:label "has part"@en, "구성"@ko ;
    rdfs:comment "전체-부분 관계"@ko .
"""


@pytest.fixture(scope="function")
def sample_tbox_missing_labels():
    """T-Box with a class missing @en label."""
    return f"""\
@prefix {_PFX}: <{_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{_PFX}:EquipmentMaster a owl:Class ;
    rdfs:label "설비 마스터"@ko ;
    rdfs:comment "설비 마스터"@ko .
"""


@pytest.fixture(scope="function")
def sample_tbox_missing_domain_range():
    """T-Box with an ObjectProperty missing domain and range."""
    return f"""\
@prefix {_PFX}: <{_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{_PFX}:EquipmentMaster a owl:Class ;
    rdfs:label "Equipment Master"@en, "설비 마스터"@ko ;
    rdfs:comment "설비 마스터"@ko .

{_PFX}:hasEquipmentStatus a owl:ObjectProperty ;
    rdfs:label "has equipment status"@en, "설비 상태"@ko ;
    rdfs:comment "설비 상태"@ko .
"""


@pytest.fixture(scope="function")
def sample_shacl_shapes():
    """Minimal SHACL shapes for testing."""
    return f"""\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix {_PFX}: <{_NS}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

{_PFX}:EquipmentMasterShape a sh:NodeShape ;
    sh:targetClass {_PFX}:EquipmentMaster ;
    sh:property [
        sh:path {_PFX}:equipmentID ;
        sh:datatype xsd:string ;
        sh:minCount 1 ;
    ] .
"""

@pytest.fixture(scope="session")
def s3_output_ttl() -> str | None:
    """S2 초안에 S3 를 2회 적용한 산출물 **TTL 문자열** (세션 1회 계산).

    S3 전체 실행이 약 3분이라, 파이프라인 산출물을 검증하는 통합 테스트가 각자
    돌리면 비용이 곱해진다 (실측: 10회 반복 → 30분). 정책상 ``Graph`` 는 세션
    스코프로 공유하지 않지만 **문자열은 불변** 이라 안전하다 — 테스트는 이 문자열을
    받아 각자 ``Graph`` 를 만든다.

    2회인 이유: S3 는 2차부터 멱등이고 1차 산출물에는 아직 반영되지 않는 스텝
    상호작용이 있다 (실측: 1차 5,546 → 2차 5,814 고정).

    Returns:
        TTL 문자열. 픽스처 파일이 없으면 ``None`` — 테스트는 skip 해야 한다.
    """
    import os

    src = "/tmp/t_box.before_S3.ttl"
    if not os.path.exists(src):
        return None
    from tools.ontology_quality import improve_tbox

    with open(src, encoding="utf-8") as fh:
        ttl = fh.read()
    for _ in range(2):
        ttl, _ = improve_tbox(ttl)
    return ttl


@pytest.fixture(scope="session")
def s3_output_stats(s3_output_ttl) -> dict | None:
    """``s3_output_ttl`` 과 **같은 실행** 의 마지막 stats.

    stats 를 얻으려고 S3 를 또 돌리면 fixture 의 목적(1회 계산)이 무의미해진다.
    그래서 TTL fixture 가 만든 산출물에 3회차를 한 번 더 적용해 stats 를 받는다 —
    2차부터 멱등이므로 3회차 stats 는 2회차와 동일하고 그래프도 변하지 않는다.
    """
    if s3_output_ttl is None:
        return None
    from tools.ontology_quality import improve_tbox

    _, stats = improve_tbox(s3_output_ttl)
    return stats


@pytest.fixture(autouse=True)
def _isolate_compromise_audit(tmp_path, monkeypatch):
    """테스트가 **프로덕션** compromise_audit.json 을 건드리지 못하게 한다.

    실측 (2026-08-18): ``tests/test_t5_decision_criteria.py`` 의 두 테스트가
    ``_architect_compromise`` 를 patch 없이 호출한다. 그 함수는 path 인자 없이
    ``append_compromise_audit(artifact)`` 를 부르고(multi_agent_tbox.py), 그러면
    ``COMPROMISE_AUDIT_PATH`` 로 폴백해 **배포 파일에 쓴다**.

    ``_MAX_ITERATIONS = 10`` (compromise_audit.py) 이므로 피해는 append 가 아니라
    **실측 S2 레코드 축출** 이다 — 확인 시점에 배포 파일 10건이 전부 테스트
    픽스처(``overall_rationale: "no issues"``)로 교체돼 있었고, 실제 S2 절충 이력은
    이미 사라졌다.

    개별 테스트에 patch 를 추가하는 방식으로는 새 테스트가 또 오염시킨다. autouse
    로 전역 차단하고, 경로 자체를 검증하려는 테스트는 이 fixture 를 override 하거나
    명시적으로 실제 경로를 다시 patch 하면 된다.
    """
    try:
        import tools.compromise_audit as _ca
    except Exception:  # pragma: no cover — 모듈 없는 환경 방어
        return
    monkeypatch.setattr(
        _ca, "COMPROMISE_AUDIT_PATH", str(tmp_path / "compromise_audit.json"),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _isolate_debate_log(tmp_path, monkeypatch):
    """테스트가 **프로덕션** debate_log.json 을 건드리지 못하게 한다.

    ``_finalize_and_save`` 가 ``append_debate_run`` 을 path 인자 없이 부르므로
    모듈 상수 ``DEBATE_LOG_PATH`` 로 폴백한다 — ``compromise_audit`` 과 **같은
    형태이고 같은 사고가 가능하다** (2026-08-18: 테스트 2개가 배포 감사 파일에 써서
    실측 10건이 픽스처로 축출됐다. ``_MAX_RUNS=10`` 이라 여기도 동일하다).

    개별 테스트에 patch 를 추가하는 방식은 새 테스트가 또 오염시키므로 autouse 로
    전역 차단한다. 경로 자체를 검증하려는 테스트는 명시적으로 다시 patch 하거나
    ``path=`` 인자를 넘기면 된다.
    """
    try:
        import tools.debate_log_store as _dls
    except Exception:  # pragma: no cover — 모듈 없는 환경 방어
        return
    monkeypatch.setattr(
        _dls, "DEBATE_LOG_PATH", str(tmp_path / "debate_log.json"),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _isolate_quality_history(tmp_path, monkeypatch):
    """테스트가 **프로덕션** quality_history.json 을 건드리지 못하게 한다.

    ``_finalize_and_save`` 가 ``_record_s2_quality_history`` 를 부르게 되면서
    (S2 항목이 이력에 0건이던 문제 수정) 그 함수를 직접 호출하는 테스트 4개
    (``test_s2_infra_abort.py`` / ``test_multi_agent_tbox.py`` /
    ``test_s2_integrity_guards.py`` / ``test_debate_log_persistence.py``) 가
    배포 이력 파일에 쓸 수 있다. ``_append_quality_history`` 는
    ``dirname(_STATE_PATH)`` 로 경로를 파생하므로 ``_STATE_PATH`` 를 tmp 로
    돌리면 차단된다.

    ``debate_log`` / ``compromise_audit`` 과 같은 사고 형태다 (2026-08-18:
    테스트 2개가 배포 감사 파일에 써서 실측 10건이 픽스처로 축출됐다). 이력은
    ``[-100:]`` 로 잘리므로 여기서도 실측 항목이 밀려날 수 있다.

    ``_STATE_PATH`` 를 직접 patch 하는 기존 테스트
    (``test_golden_queries.py`` / ``test_checkpoint_per_step_snapshot.py``) 는
    이 fixture 이후에 자기 값으로 다시 설정하므로 영향받지 않는다.
    """
    try:
        import tools.kg_validation as _kgv
        import tools.pipeline_state as _ps
    except Exception:  # pragma: no cover — 모듈 없는 환경 방어
        return
    monkeypatch.setattr(
        _ps, "_STATE_PATH", str(tmp_path / "pipeline_state.json"),
        raising=False,
    )

    # ── 두 번째 writer 도 막는다 ────────────────────────────────────
    # 이 파일에는 writer 가 **둘** 있고 경로를 서로 다른 상수에서 파생한다:
    #   tools/pipeline_state.py          → dirname(_STATE_PATH)
    #   tools/validation_support/…       → dirname(GENERATED_ABOX_DIR)
    # 위에서 _STATE_PATH 만 돌려도 ``validate_kg`` 경로는 그대로 배포 파일에 쓴다.
    #
    # 실측 (2026-08-25): ``validate_kg`` 를 부르면서 ``GENERATED_ABOX_DIR`` 를
    # patch 하지 않는 테스트가 5개 파일에 있고, 그 테스트들은 ``_load_graph`` 를
    # 빈 그래프로 mock 하므로 ``triples: 0`` 엔트리를 배포 이력에 남긴다. 이력은
    # ``[-100:]`` 로 잘리지만 문제는 잘림이 아니라 **실측 항목이 mock 항목으로
    # 교체된다** 는 것이다 — 테스트 1건 실행으로 배포 파일 md5 가 바뀌는 것을 확인했다.
    #
    # 그래서 배포 이력이 계속 1건에 머물렀고, ``min_history=5`` 게이트를 넘지
    # 못했다. debate_log / compromise_audit 과 같은 사고 형태다 (2026-08-18).
    # 경로 상수가 **세 곳** 에 있다. 세 번째는 writer 자기 모듈의 상수이고,
    # ``append_quality_history(base_dir=None)`` 는 그것을 쓴다 — 호출부(kg_validation)
    # 만 막으면 그 writer 를 직접 부르는 경로가 배포 파일에 쓴다 (실측: 내가 추가한
    # 회귀 테스트가 배포 파일을 오염시켜 그 구멍을 드러냈다).
    monkeypatch.setattr(
        _kgv, "GENERATED_ABOX_DIR", str(tmp_path / "abox"), raising=False,
    )
    try:
        from tools.validation_support import quality_history as _qh
    except Exception:  # pragma: no cover
        return
    monkeypatch.setattr(
        _qh, "GENERATED_ABOX_DIR", str(tmp_path / "abox"), raising=False,
    )


@pytest.fixture(autouse=True)
def _isolate_semantic_dictionary(tmp_path, monkeypatch):
    """테스트가 **프로덕션** semantic_dictionary.json 을 건드리지 못하게 한다.

    실측 (2026-08-28): 전체 스위트를 돌린 뒤 배포 딕셔너리의 정의 클래스 통계가
    전부 사라졌다 — 파이프라인이 만든 v2 가 v1 수준으로 교체됐다::

        파이프라인 v2 (S10)   RunningEquipmentStatus 2998 / Scope1Emission 50 / 통계 76 클래스
        스위트 실행 후        0 / 0 / 41 클래스

    범인은 ``tests/test_semantic_dictionary_value_range.py`` 로, ``monkeypatch`` 없이
    ``generate_semantic_dictionary()`` 를 인자 없이 3회 부른다. 기본값이
    ``use_inferred=False`` 이므로 추론 그래프를 읽지 않고 배포 파일을 덮어쓴다.

    피해가 조용하다: 파일은 유효한 v2 이고 검증도 통과한다. 다만 ``instance_count``
    가 0 이 되어 **LLM 이 "그 클래스는 비어있다" 고 읽는다** — 딕셔너리의 소비자는
    코드가 아니라 LLM 이므로 게이트가 잡지 못한다.

    ``compromise_audit`` / ``debate_log`` / ``quality_history`` 와 같은 사고 형태다
    (2026-08-18, 2026-08-25). 개별 테스트에 patch 를 추가하는 방식은 새 테스트가 또
    오염시키므로 autouse 로 막는다. 경로 상수는 **두 곳** 이다 — 모듈 자기 상수와
    config 원본. 전자만 막으면 config 에서 다시 읽는 경로가 배포 파일에 쓴다.
    """
    try:
        import tools.semantic_dictionary as _sd
    except Exception:  # pragma: no cover — 모듈 없는 환경 방어
        return
    monkeypatch.setattr(
        _sd, "SEMANTIC_DICT_PATH", str(tmp_path / "semantic_dictionary.json"),
        raising=False,
    )


# ── 배포 산출물 쓰기 차단 (경로 상수 열거를 대신한다) ──────────────


def _deployed_artifact_root() -> str | None:
    """Absolute ``data/generated`` of **this checkout**, or None if unavailable."""
    try:
        from config import GENERATED_DIR
    except Exception:  # pragma: no cover — config 없는 환경 방어
        return None
    return os.path.realpath(GENERATED_DIR)


def _is_deployed_artifact(path: object, root: str | None) -> bool:
    """True if ``path`` resolves inside the deployed artifact tree.

    ``realpath`` on both sides so that a tmp_path redirect that happens to contain the
    string ``data/generated`` is not mistaken for the real tree, and so that a symlink
    into it *is* caught.
    """
    if not root or not isinstance(path, str | os.PathLike):
        return False
    try:
        resolved = os.path.realpath(os.fspath(path))
    except (TypeError, ValueError):  # pragma: no cover — 비정상 경로 방어
        return False
    return resolved == root or resolved.startswith(root + os.sep)


@pytest.fixture(autouse=True)
def _block_deployed_artifact_writes(request, monkeypatch):
    """테스트가 ``data/generated`` 아래 **배포 산출물**을 덮어쓰지 못하게 한다.

    ## 왜 경로 상수 리다이렉트가 아니라 쓰기 차단인가

    위의 격리 4개는 경로 상수를 tmp 로 돌리는 방식이다. 그 방식은 **상수 사본을 전부
    열거해야** 하고, 한 곳이라도 빠지면 조용히 뚫린다 — 실제로 세 번 재발했다
    (2026-08-18 compromise_audit / 2026-08-25 quality_history 는 상수가 3곳이었다 /
    2026-08-28 semantic_dictionary 는 2곳). 남은 표면을 세어 보면 이 방식으로는
    유지가 불가능하다: ``INFERRED_PATH`` 를 import 하는 모듈이 **19개**,
    ``GENERATED_REPORTS_DIR`` 이 **17개** 다.

    그래서 상수를 쫓지 않고 **쓰기 지점**을 막는다. 어떤 모듈이 어떤 상수로 경로를
    조립했든, 그 경로가 실제로 배포 트리를 향하면 예외가 난다. 새 writer 와 새 테스트도
    자동으로 잡힌다.

    ## 실측 피해 (2026-09-01, 이 가드가 없던 동안)

    전체 스위트 1회 실행으로 ``data/generated`` 54개 파일 중 **12개가 테스트 픽스처로
    교체**됐다::

        inference_loss_manifest.json   input {tbox:4922, abox:-4921} ← 산술 불성립
        inference_justifications.json  total_inferred 0
        inference_provenance.ttl       23 lines
        reports/ontoclean_report.json  {"error": "T-Box not found: /nonexistent/t.ttl"}
        reports/cq_feedback.json       테스트 유래 iteration append
        reports/canonical_compare.json pytest tmpdir 경로가 각인
        + fair_score / farber_dimensions / dqv_sidecar / adversarial_report /
          query_test_report.html / table_class_map.html

    ``cq_feedback.json`` 오염이 특히 나쁘다 — 이 파일은 다음 S2 Architect 프롬프트에
    주입되므로(T3) **테스트 픽스처가 T-Box 생성 입력**이 된다.

    배포 경로 자체를 검증하는 것이 목적인 테스트는
    ``@pytest.mark.writes_deployed_artifacts`` 로 opt-out 한다.
    """
    if request.node.get_closest_marker("writes_deployed_artifacts"):
        return

    root = _deployed_artifact_root()
    if not root:
        return

    def _fail(path: object, primitive: str) -> None:
        raise AssertionError(
            f"테스트가 배포 산출물에 쓰려 했다 ({primitive}): {path}\n"
            f"이 파일은 파이프라인 실행 결과이고 git 에 추적되지 않는다 — 덮어쓰면 "
            f"실측값이 픽스처로 교체되고 되돌릴 수 없다.\n"
            f"해결: 경로를 tmp_path 로 patch 하라. 배포 경로를 쓰는 것이 테스트의 "
            f"목적이면 @pytest.mark.writes_deployed_artifacts 를 붙여라."
        )

    # ── (1) atomic_write / atomic_write_json ────────────────────────
    # 모듈 15개가 ``from tools.common import atomic_write`` 로 **이름을 복사**하므로
    # tools.common 만 patch 하면 그 사본들은 원본을 계속 부른다. 각 모듈의 사본까지
    # 갈아야 한다 — 이것이 상수 리다이렉트와 같은 함정이라, 여기서는 이미 import 된
    # 모듈을 sys.modules 로 훑어 **가진 쪽 전부**를 덮는다.
    #
    # 모듈 이름으로 대상을 좁히지 않는다. 처음엔 ``tools.``/``domain.`` prefix 로
    # 걸렀는데 그 필터가 곧바로 구멍을 만들었다 — 테스트 모듈 자신이 module-level
    # import 로 원본 이름을 붙잡고 있으면 제외돼 가드가 발화하지 않았다 (실측:
    # ``tests/test_deployed_artifact_write_guard.py`` 3건 DID NOT RAISE). 판정 기준은
    # "그 이름이 원본 함수를 가리키는가" 하나로 충분하다.
    import sys

    import tools.common as _common

    for name in ("atomic_write", "atomic_write_json"):
        real = getattr(_common, name, None)
        if real is None:  # pragma: no cover
            continue

        def _guarded(*args, _real=real, _name=name, **kwargs):
            path = args[0] if args else kwargs.get("path")
            if _is_deployed_artifact(path, root):
                _fail(path, _name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(_common, name, _guarded, raising=False)
        for module in list(sys.modules.values()):
            if module is None or module is _common:
                continue
            if getattr(module, name, None) is real:
                monkeypatch.setattr(module, name, _guarded, raising=False)

    # ── (2) rdflib Graph.serialize(destination=...) ─────────────────
    # ``tools/provenance.py`` / ``tools/inference.py`` / ``tools/triple_confidence.py``
    # 는 atomic_write 를 거치지 않고 직접 직렬화한다 (실측: 오염된
    # inference_provenance.ttl 이 이 경로다).
    try:
        from rdflib import Graph as _Graph
    except Exception:  # pragma: no cover
        _Graph = None
    if _Graph is not None:
        _real_serialize = _Graph.serialize

        def _guarded_serialize(self, destination=None, *args, **kwargs):
            if _is_deployed_artifact(destination, root):
                _fail(destination, "Graph.serialize(destination=)")
            return _real_serialize(self, destination, *args, **kwargs)

        monkeypatch.setattr(_Graph, "serialize", _guarded_serialize, raising=False)

    # ── (3) 내장 open() 의 쓰기 모드 ────────────────────────────────
    # ``semantic_dictionary.py`` / ``remote/neo4j.py`` 는 ``open(..., "w")`` 로 직접
    # 쓴다. 읽기는 건드리지 않는다 — 배포 산출물을 **읽는** 테스트는 정당하다.
    import builtins

    _real_open = builtins.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")) and \
                _is_deployed_artifact(file, root):
            _fail(file, f"open(mode={mode!r})")
        return _real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open, raising=False)

    # ── (4) builtins.open 을 거치지 않는 경로 ───────────────────────
    #
    # 2026-09-02 실측: 위 (1)~(3) 을 전부 켠 상태에서 프로브 6종이 통과했다.
    # ``pathlib`` 은 ``io.open`` 을 부르므로 ``builtins.open`` 재바인딩이 닿지 않고,
    # ``os.replace``/``os.rename`` 은 tmp 를 배포 트리 **밖**에 만든 뒤 이동하면
    # 어느 쓰기 지점도 거치지 않는다. 그리고 디렉터리 생성은 아예 축이 없었다 —
    # 그래서 ``data/generated/brand_new_subdir/`` 가 남아 있었다 (가드 자신의
    # 테스트가 만든 것이다).
    import pathlib

    _real_write_text = pathlib.Path.write_text
    _real_write_bytes = pathlib.Path.write_bytes
    _real_path_open = pathlib.Path.open

    def _guarded_write_text(self, *args, **kwargs):
        if _is_deployed_artifact(self, root):
            _fail(self, "Path.write_text")
        return _real_write_text(self, *args, **kwargs)

    def _guarded_write_bytes(self, *args, **kwargs):
        if _is_deployed_artifact(self, root):
            _fail(self, "Path.write_bytes")
        return _real_write_bytes(self, *args, **kwargs)

    def _guarded_path_open(self, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")) and \
                _is_deployed_artifact(self, root):
            _fail(self, f"Path.open(mode={mode!r})")
        return _real_path_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", _guarded_write_text, raising=False)
    monkeypatch.setattr(pathlib.Path, "write_bytes", _guarded_write_bytes, raising=False)
    monkeypatch.setattr(pathlib.Path, "open", _guarded_path_open, raising=False)

    # os.replace / os.rename — **목적지**만 본다. tmp 를 어디에 만들었는지는 무관하다.
    _real_replace = os.replace
    _real_rename = os.rename

    def _guarded_replace(src, dst, **kwargs):
        if _is_deployed_artifact(dst, root):
            _fail(dst, "os.replace")
        return _real_replace(src, dst, **kwargs)

    def _guarded_rename(src, dst, **kwargs):
        if _is_deployed_artifact(dst, root):
            _fail(dst, "os.rename")
        return _real_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", _guarded_replace, raising=False)
    monkeypatch.setattr(os, "rename", _guarded_rename, raising=False)

    # 디렉터리 생성은 **없는 경로일 때만** 막는다.
    #
    # 과잉 차단 방지: 호출부 46곳이 ``os.makedirs(GENERATED_REPORTS_DIR,
    # exist_ok=True)`` 처럼 이미 있는 폴더를 방어적으로 보장한다. 그것까지 막으면
    # 배포 산출물을 **읽는** 정당한 테스트가 깨진다. 새 폴더 생성만이 오염 축이다.
    _real_makedirs = os.makedirs
    _real_mkdir = os.mkdir

    def _guarded_makedirs(name, *args, **kwargs):
        if _is_deployed_artifact(name, root) and not os.path.isdir(name):
            _fail(name, "os.makedirs (신규 디렉터리)")
        return _real_makedirs(name, *args, **kwargs)

    def _guarded_mkdir(path, *args, **kwargs):
        if _is_deployed_artifact(path, root) and not os.path.isdir(path):
            _fail(path, "os.mkdir (신규 디렉터리)")
        return _real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", _guarded_makedirs, raising=False)
    monkeypatch.setattr(os, "mkdir", _guarded_mkdir, raising=False)
