"""T-Box 품질 3개 수정의 회귀 고정 — 보존 방향 우선.

2026-08-11 T-Box 품질 평가에서 확정된 3건:

1. **``dcterms:source`` 미표기 147개** (커버리지 40.6%). A-Box 는 이 값을 CSV 헤더와
   직접 비교하므로 표기가 없으면 이름 추측 폴백으로 떨어지고, 실패하면 **해당 컬럼이
   조용히 적재되지 않는다**. 147개 전부 S2 초안 출처이고 ``tbox_generation.py`` 에는
   보강 단계가 아예 없었다 (프롬프트는 "필수" 라고 하지만 준수율 42%).
   → ``step_12d3`` 이 커버리지 판정(``resolve_column_owner``)을 역기록한다. 추측이
   아니라 **게이트가 이미 내린 판정의 전사** 다.

2. **중복 OP 42개**. ``step_15`` 가 이름만 보고 같은 ``(domain, range)`` 에 동의어를
   계속 만들었다. CSV FK 컬럼은 하나뿐이라 A-Box 는 그중 하나만 채우고 나머지는 값
   0건으로 남는다 — "빈 관계로 질의하면 0건이 정답처럼" 반환되는 함정.
   → ``graph_utils.ops_linking`` 공용 가드. ``jury_fixes`` 사본도 이 헬퍼에 위임.

3. **죽은 스텁 2개** (``GHGScope`` / ``ghgScopeEnum``). lazy_class 24개 중 22개는
   S3 가 만든 **의도된 추상 그룹** 이라 지우면 계층이 붕괴한다 — 그래서 5신호 OR.
   → ``step_21b``.

이 파일은 "고쳤다" 를 세지 않고 **정당한 입력이 보존되는가** 를 먼저 주장한다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from domain.graph_utils import ops_linking
from domain.namespaces import DOMAIN_NS, NS_PREFIX

_DCTERMS = Namespace("http://purl.org/dc/terms/")
_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    "@prefix dcterms: <http://purl.org/dc/terms/> .\n"
    "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
)


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _graph(body: str) -> Graph:
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    return g


# ── 1. 커버리지 사다리 단일화 (resolve_column_owner) ─────────────────


def test_coverage_match_still_delegates_to_the_single_ladder():
    """사다리를 두 곳에 복제하면 게이트와 기록자가 다른 답을 낸다.

    ``_coverage_match`` 는 ``resolve_column_owner`` 의 bool 래퍼여야 한다 — 로직이
    되돌아오면 이 테스트가 깨진다.
    """
    import inspect

    from tools.ontology_quality import _coverage_match

    src = inspect.getsource(_coverage_match)
    assert "resolve_column_owner" in src, "사다리가 복제됐다"


@pytest.mark.parametrize("column,cls,dps,expected_stage", [
    ("status", "equipmentstatus", {"status"}, 1),
    ("timestamp", "alarmevents", {"alarmeventstimestamp"}, 2),
])
def test_resolver_reports_which_dp_owns_the_column(column, cls, dps, expected_stage):
    """어느 DP 가 담당하는지 **이름과 단계** 를 돌려준다 (bool 이 아니다)."""
    from tools.ontology_quality import resolve_column_owner

    owner = resolve_column_owner(column, cls, dps)
    assert owner is not None
    dp_local, stage, candidates = owner
    assert dp_local in dps
    assert stage == expected_stage
    assert candidates == (dp_local,)


def test_resolver_marks_already_sourced_columns_as_stage_zero():
    """출처 표기가 있으면 이름 추측 없이 stage 0 — 기록자는 손대지 않아야 한다."""
    from tools.ontology_quality import resolve_column_owner

    owner = resolve_column_owner("event_id", "alarmevents", {"x"}, {"EVENT_ID"})
    assert owner == ("", 0, ())


def test_resolver_refuses_the_loose_stage_when_writing():
    """``max_stage`` 로 stage 6(contains)을 배제할 수 있다 (기록 시 필수)."""
    from tools.ontology_quality import resolve_column_owner

    # 'status' 를 포함하는 DP — contains 단계에서만 성립.
    dps = {"equipmentstatusdetailedstatusflag"}
    assert resolve_column_owner("status", "other", dps, max_stage=6) is not None
    assert resolve_column_owner("status", "other", dps, max_stage=5) is None


# ── 2. dcterms:source 역기록 (step_12d3) ─────────────────────────────


def _stub_csv(monkeypatch, table: str, columns: list[str]) -> None:
    """CSV 스캔이 픽스처만 보게 한다 (실제 40개 테이블 간섭 제거)."""
    import tempfile
    from pathlib import Path

    import config
    from tools import ontology_quality

    tmp = tempfile.mkdtemp()
    Path(tmp, f"{table}.csv").write_text(
        ",".join(columns) + "\n" + ",".join("1" for _ in columns) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)
    monkeypatch.setattr(ontology_quality, "_EXPECTED_COLUMNS_CACHE", None, raising=False)


def test_backfill_records_the_column_the_gate_already_matched(monkeypatch):
    """THE REGRESSION: 게이트가 커버로 판정한 컬럼을 ``dcterms:source`` 로 적는다."""
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d3_dp_source_backfill import apply

    _stub_csv(monkeypatch, "Alarm_Events", ["Event_ID", "Severity"])
    g = _graph(
        f"{NS_PREFIX}:AlarmEvents a owl:Class .\n"
        f"{NS_PREFIX}:alarmEventsSeverity a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:AlarmEvents ; rdfs:range xsd:string .\n"
    )
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    sources = [str(o) for o in g.objects(D("alarmEventsSeverity"), _DCTERMS.source)]
    assert sources == ["Severity"], sources
    assert res.stats["dp_source_backfilled"] == 1


def test_backfill_never_touches_an_already_sourced_dp(monkeypatch):
    """PRESERVATION: 기존 표기는 덮어쓰지 않는다 (멱등성도 여기서 온다)."""
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d3_dp_source_backfill import apply

    _stub_csv(monkeypatch, "Alarm_Events", ["Severity"])
    g = _graph(
        f"{NS_PREFIX}:AlarmEvents a owl:Class .\n"
        f'{NS_PREFIX}:alarmEventsSeverity a owl:DatatypeProperty ; '
        f'rdfs:domain {NS_PREFIX}:AlarmEvents ; dcterms:source "CURATED_COL" .\n'
    )
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    sources = [str(o) for o in g.objects(D("alarmEventsSeverity"), _DCTERMS.source)]
    assert sources == ["CURATED_COL"], sources


def test_backfill_refuses_when_two_dps_claim_one_column(monkeypatch):
    """모호하면 **기록하지 않는다**. 틀린 확신은 폴백보다 나쁘다.

    A-Box 출처 인덱스는 다른 모든 매칭 경로보다 먼저 짧은 회로로 확정되므로,
    잘못된 컬럼을 적으면 올바른 폴백조차 타지 못한다.
    """
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d3_dp_source_backfill import apply

    _stub_csv(monkeypatch, "Alarm_Events", ["Severity"])
    # 두 DP 가 같은 컬럼을 주장하도록 (direct + class-prefixed)
    g = _graph(
        f"{NS_PREFIX}:AlarmEvents a owl:Class .\n"
        f"{NS_PREFIX}:severity a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:AlarmEvents .\n"
        f"{NS_PREFIX}:alarmEventsSeverity a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:AlarmEvents .\n"
    )
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    # 가장 먼저 성립한 단계(direct)만 인정되므로 severity 하나만, 혹은 모호로 0건.
    recorded = [
        (str(s).split("#")[-1], str(o))
        for s, _, o in g.triples((None, _DCTERMS.source, None))
    ]
    assert len(recorded) <= 1, f"모호한데 둘 다 기록했다: {recorded}"


def test_backfill_refuses_when_the_ladder_itself_returns_two_candidates(monkeypatch):
    """사다리가 후보 2개를 돌려주면(``dp_lower == ""``) 기록하지 않는다.

    pass 1 의 짧은 회로는 **단계** 를 좁히지만 같은 단계 안의 동점은 남는다. 이
    경로가 열려 있으면 A-Box 가 엉뚱한 컬럼을 최우선으로 확정한다.
    """
    from tools.quality_steps import step_12d3_dp_source_backfill as step
    from tools.quality_steps._base import StepContext

    _stub_csv(monkeypatch, "Alarm_Events", ["Severity"])
    g = _graph(
        f"{NS_PREFIX}:AlarmEvents a owl:Class .\n"
        f"{NS_PREFIX}:alarmEventsSeverity a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:AlarmEvents .\n"
    )
    monkeypatch.setattr(
        "tools.ontology_quality.resolve_column_owner",
        lambda *a, **k: ("", 2, ("dpA", "dpB")),   # 동점 후보 2개
    )
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert list(g.triples((None, _DCTERMS.source, None))) == [], "동점인데 기록했다"
    assert res.stats["dp_source_backfill_ambiguous"] >= 1


def test_backfill_refuses_when_one_dp_claims_two_columns(monkeypatch):
    """한 DP 가 두 컬럼을 주장하면 어느 쪽인지 알 수 없다 — 기록하지 않는다.

    사다리를 직접 스텁한다: 실제 CSV 로는 짧은 회로가 먼저 걸려 이 경로에 닿지
    않지만, stage 0 소유자가 사라지는 순간 열리는 실경로다.
    """
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d3_dp_source_backfill import apply

    _stub_csv(monkeypatch, "Equipment_Status", ["Status", "Status_Value"])
    g = _graph(
        f"{NS_PREFIX}:EquipmentStatus a owl:Class .\n"
        f"{NS_PREFIX}:equipmentStatusValue a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:EquipmentStatus .\n"
    )
    # 두 컬럼 모두 같은 DP 를 지목하게 만든다.
    monkeypatch.setattr(
        "tools.ontology_quality.resolve_column_owner",
        lambda *a, **k: ("equipmentstatusvalue", 3, ("equipmentstatusvalue",)),
    )
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    recorded = [str(o) for _, _, o in g.triples((None, _DCTERMS.source, None))]
    assert recorded == [], f"한 DP 가 컬럼 2개를 주장하는데 기록했다: {recorded}"
    assert res.stats["dp_source_backfill_ambiguous"] >= 1


def test_backfill_refuses_when_two_dps_converge_on_the_same_column(monkeypatch):
    """한 컬럼을 두 DP 가 주장하면 기록하지 않는다 (컬럼 방향 경쟁).

    ``proposals`` 쪽만 검사하면 이 방향이 열린 채 남아 **두 DP 에 같은 컬럼** 이
    적힌다 — A-Box 출처 인덱스는 그중 하나를 임의로 집는다.
    """
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d3_dp_source_backfill import apply

    _stub_csv(monkeypatch, "Equipment_Status", ["Status"])
    g = _graph(
        f"{NS_PREFIX}:EquipmentStatus a owl:Class .\n"
        f"{NS_PREFIX}:equipmentStatusValue a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:EquipmentStatus .\n"
        f"{NS_PREFIX}:equipmentStatusCode a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:EquipmentStatus .\n"
    )
    # 호출 순서에 따라 서로 다른 DP 를 지목 → 두 DP 가 같은 'status' 를 주장.
    picks = iter(["equipmentstatusvalue", "equipmentstatuscode"])

    def _fake(csv_col_lower, class_camel_lower, declared, sourced=None, **kw):
        try:
            dp = next(picks)
        except StopIteration:
            return None
        return (dp, 3, (dp,))

    monkeypatch.setattr("tools.ontology_quality.resolve_column_owner", _fake)
    # 컬럼이 하나뿐이라 pass 1 이 한 번만 돌게 되므로, 컬럼 2개를 흉내내되
    # 둘 다 'STATUS' 로 정규화되도록 대문자 변형을 준다.
    monkeypatch.setattr(
        "tools.ontology_quality._collect_expected_columns_per_class",
        lambda: {
            "EquipmentStatus": {
                "table": "Equipment_Status",
                "expected_columns": {"status", "STATUS"},
                "all_columns": ["Status"],
                "pk_columns": set(),
                "fk_columns": set(),
            },
        },
    )
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    by_column: dict[str, list[str]] = {}
    for subj, _, obj in g.triples((None, _DCTERMS.source, None)):
        by_column.setdefault(str(obj).upper(), []).append(str(subj).split("#")[-1])
    clashes = {col: dps for col, dps in by_column.items() if len(dps) > 1}
    assert not clashes, f"한 컬럼을 여러 DP 에 적었다: {clashes}"
    assert res.stats["dp_source_backfill_ambiguous"] >= 1


def test_backfill_rejects_the_loose_contains_stage(monkeypatch):
    """stage 6(``contains``)로 매칭된 컬럼은 **적지 않는다**.

    판정(게이트)은 6까지 쓰지만 기록은 5까지다. ``contains`` 는 부분 문자열이라
    ``status`` 가 ``…detailedStatusFlag`` 를 물어오는 식의 과매칭이 생기고, 잘못
    적힌 출처는 A-Box 에서 **다른 모든 폴백보다 먼저** 확정된다.
    """
    from tools.quality_steps import step_12d3_dp_source_backfill as step
    from tools.quality_steps._base import StepContext

    _stub_csv(monkeypatch, "Equipment_Status", ["Status"])
    g = _graph(
        f"{NS_PREFIX}:EquipmentStatus a owl:Class .\n"
        f"{NS_PREFIX}:equipmentStatusDetailedStatusFlag a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:EquipmentStatus .\n"
    )
    seen: list[int | None] = []

    def _fake(csv_col_lower, class_camel_lower, declared, sourced=None, *, max_stage=6):
        seen.append(max_stage)
        if max_stage < 6:
            return None                     # stage 6 만 성립하는 컬럼
        return ("equipmentstatusdetailedstatusflag", 6, ("x",))

    monkeypatch.setattr("tools.ontology_quality.resolve_column_owner", _fake)
    step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert seen and max(s for s in seen if s is not None) <= 5, (
        f"기록 경로가 stage 6 을 허용했다: max_stage={seen}"
    )
    assert list(g.triples((None, _DCTERMS.source, None))) == []


def test_backfill_is_idempotent(monkeypatch):
    """두 번 돌려도 트리플이 변하지 않는다 (S3 는 멱등이어야 한다)."""
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d3_dp_source_backfill import apply

    _stub_csv(monkeypatch, "Alarm_Events", ["Severity"])
    g = _graph(
        f"{NS_PREFIX}:AlarmEvents a owl:Class .\n"
        f"{NS_PREFIX}:alarmEventsSeverity a owl:DatatypeProperty ; "
        f"rdfs:domain {NS_PREFIX}:AlarmEvents .\n"
    )
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    snapshot = set(g)
    res2 = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert set(g) == snapshot
    assert res2.stats["dp_source_backfilled"] == 0


# ── 3. 중복 OP 멱등 가드 (ops_linking / step_15) ─────────────────────


def test_ops_linking_is_directional_so_inverse_pairs_survive():
    """THE PRESERVATION CONTRACT: inverseOf 쌍을 막아선 안 된다.

    ``hasX(A→B)`` 가 있어도 ``isXOf(B→A)`` 는 다른 쌍이다. 방향을 무시하면 역방향
    OP 를 만들 수 없어 2-hop 경로가 끊긴다.
    """
    g = _graph(
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:hasB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .\n"
    )
    assert ops_linking(g, D("A"), D("B")) == {D("hasB")}
    assert ops_linking(g, D("B"), D("A")) == set(), "역방향을 같은 쌍으로 봤다"


def test_ops_linking_does_not_follow_subsumption():
    """``owl:Thing`` domain 62개가 있어 상위 클래스를 타면 거의 전부 겹친다."""
    g = _graph(
        f"{NS_PREFIX}:Parent a owl:Class .\n"
        f"{NS_PREFIX}:Child a owl:Class ; rdfs:subClassOf {NS_PREFIX}:Parent .\n"
        f"{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:op a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Parent ; rdfs:range {NS_PREFIX}:B .\n"
    )
    assert ops_linking(g, D("Child"), D("B")) == set(), "subsumption 을 따라갔다"


def test_jury_ops_linking_delegates_to_the_shared_helper():
    """사본이 갈라지지 않게 위임 형태를 고정한다."""
    import inspect

    from tools.jury_fixes import _ops_linking

    assert "ops_linking" in inspect.getsource(_ops_linking)


def _drive_step_15(monkeypatch, body: str, *, csv_dir=None):
    """step_15 를 **실제 로더 두 개** 를 구동해 실행한다.

    ``_load_cross_domain_ops`` (이름 목록) 와 ``_load_cross_domain_op_meta``
    (name → (domain, range, label_en, label_ko, comment_ko, inverse)) 를 모두
    패치해야 스텝이 후보를 본다. 존재하지 않는 함수를 ``raising=False`` 로
    패치하면 후보가 0이라 **가드를 지워도 테스트가 통과한다** — 실측으로 확인된
    공허 테스트이므로 이 헬퍼로 고정한다.

    FK 근거 게이트는 ``warn`` 으로 둔다. 이 헬퍼가 고정하는 것은 **쌍 단위 중복
    가드** 이고, 픽스처의 클래스 A/B 는 ``_FK_PATTERNS`` (고정 Master 테이블 표)
    로는 FK 타겟이 될 수 없다 — 게이트를 켜두면 후보가 진입 전에 걸러져 위 docstring
    이 경고하는 **공허 테스트** 가 된다. 게이트 자체는
    ``tests/test_step_15_fk_grounding_gate.py`` 와 아래
    ``test_step_15_fk_gate_blocks_ungrounded_pair`` 가 검증한다.
    """
    import tempfile

    from tools.quality_steps import step_15_cross_domain_ops as step15
    from tools.quality_steps._base import StepContext

    monkeypatch.setattr(
        "tools.ontology_quality._load_cross_domain_ops",
        lambda: ["synonymAtoB"],
    )
    monkeypatch.setattr(
        "tools.ontology_quality._load_cross_domain_op_meta",
        lambda: {
            "synonymAtoB": (
                "A", "B", "synonym a to b", "동의어", "동의어 관계", "isSynonymBOfA",
            ),
        },
    )
    # CSV-backed 필터가 픽스처 클래스를 걸러내지 않도록 A/B 테이블을 만든다.
    import config

    tmp = csv_dir or tempfile.mkdtemp()
    if csv_dir is None:
        from pathlib import Path

        for name in ("A", "B"):
            Path(tmp, f"{name}.csv").write_text("col\n1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)
    monkeypatch.setenv("TBOX_CROSS_DOMAIN_FK_GATE", "warn")

    g = _graph(body)
    return g, step15.apply(g, StepContext(domain_ns=DOMAIN_NS))


def test_step_15_creates_the_configured_pair_when_nothing_occupies_it(monkeypatch):
    """PRESERVATION 먼저: 빈 쌍이면 설정된 OP 를 **양쪽 다** 만든다.

    가드가 과도하게 막으면 설정이 요구한 관계가 조용히 사라지고, 그러면
    "빈 관계로 질의하면 0건" 이라는 원래 문제가 형태만 바꿔 재발한다.
    """
    g, res = _drive_step_15(
        monkeypatch,
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n",
    )
    assert (D("synonymAtoB"), RDF.type, OWL.ObjectProperty) in g, "정방향을 안 만들었다"
    assert (D("isSynonymBOfA"), RDF.type, OWL.ObjectProperty) in g, "역방향을 안 만들었다"
    assert res.stats["cross_domain_ops_skipped_duplicate"] == 0


def test_step_15_skips_the_whole_pair_when_the_forward_leg_is_taken(monkeypatch):
    """THE REGRESSION: 정방향 쌍이 점유돼 있으면 **쌍 전체** 를 건너뛴다.

    한쪽만 만들면 ``owl:inverseOf`` 가 미선언 이름을 가리킨다.
    """
    g, res = _drive_step_15(
        monkeypatch,
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:existingAtoB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .\n",
    )
    assert (D("synonymAtoB"), RDF.type, OWL.ObjectProperty) not in g, "동의어를 만들었다"
    assert (D("isSynonymBOfA"), RDF.type, OWL.ObjectProperty) not in g, (
        "정방향은 막고 역방향만 만들었다 — dangling inverseOf 가 된다"
    )
    assert res.stats["cross_domain_ops_skipped_duplicate"] == 1


def test_step_15_skips_the_whole_pair_when_only_the_reverse_leg_is_taken(monkeypatch):
    """역방향 쌍만 점유된 경우에도 쌍 전체를 건너뛴다.

    실측 (2026-08-11): 이 경우에 정방향만 만들면서 dangling inverseOf 가
    0 → 10 → 15 로 늘었고 어떤 validator 도 잡지 못했다.
    """
    g, res = _drive_step_15(
        monkeypatch,
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:existingBtoA a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:B ; rdfs:range {NS_PREFIX}:A .\n",
    )
    assert (D("synonymAtoB"), RDF.type, OWL.ObjectProperty) not in g
    assert res.stats["cross_domain_ops_skipped_duplicate"] == 1


def test_step_15_never_leaves_a_dangling_inverse_reference(monkeypatch):
    """어떤 경로로도 ``owl:inverseOf`` 가 미선언 OP 를 가리키면 안 된다.

    건너뛰는 쌍의 이름을 이미 누군가 ``inverseOf`` 로 참조하고 있으면 그 참조도
    함께 정리한다 (실측: hasGHGAirEmissionMonitoring →
    isAirEmissionMonitoringOfGHG 1건).
    """
    g, res = _drive_step_15(
        monkeypatch,
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:existingAtoB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B ; "
        f"owl:inverseOf {NS_PREFIX}:isSynonymBOfA .\n",
    )
    dangling = [
        (str(s).split("#")[-1], str(o).split("#")[-1])
        for s, _, o in g.triples((None, OWL.inverseOf, None))
        if (o, RDF.type, OWL.ObjectProperty) not in g
    ]
    assert not dangling, f"미선언 OP 를 가리키는 inverseOf 가 남았다: {dangling}"
    assert res.stats["cross_domain_dangling_inverse_cleaned"] >= 1


def test_step_15_handles_a_self_referential_pair_without_touching_others(monkeypatch):
    """``domain == range`` 인 쌍도 쌍 단위 판정으로 일관되게 처리된다.

    두 조회가 같은 집합을 돌려주므로 별도 예외가 필요 없다 (leg 단위 판정에서만
    필요했던 분기다). 여기서 고정하는 것은 **남의 inverseOf 를 건드리지 않는다** 는
    점이다 — 정리 로직이 self-ref 경로에서 과도하게 동작하면 안 된다.
    """
    import tempfile
    from pathlib import Path

    from tools.quality_steps import step_15_cross_domain_ops as step15
    from tools.quality_steps._base import StepContext

    monkeypatch.setattr(
        "tools.ontology_quality._load_cross_domain_ops", lambda: ["selfRef"],
    )
    monkeypatch.setattr(
        "tools.ontology_quality._load_cross_domain_op_meta",
        lambda: {"selfRef": ("A", "A", "self ref", "자기참조", "자기참조", "isSelfRefOf")},
    )
    import config

    tmp = tempfile.mkdtemp()
    Path(tmp, "A.csv").write_text("col\n1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)
    # 이 테스트가 고정하는 것은 **쌍 단위 중복 가드의 self-ref 처리** 다. 클래스 A 는
    # _FK_PATTERNS 로 FK 타겟이 될 수 없어 게이트를 켜두면 후보가 진입 전에 걸러지고
    # 공허 테스트가 된다 (_drive_step_15 docstring 과 같은 이유).
    monkeypatch.setenv("TBOX_CROSS_DOMAIN_FK_GATE", "warn")

    # (A→A) 를 잇는 OP 가 **이미 있어야** 예외가 의미를 갖는다. 없으면
    # forward_taken 이 비어 pair_taken=False 라 예외 유무와 무관하게 생성된다 —
    # 그 픽스처로는 예외를 지워도 테스트가 통과한다 (뮤테이션으로 확인).
    g = _graph(
        f"{NS_PREFIX}:A a owl:Class .\n"
        f"{NS_PREFIX}:existingSelf a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:A .\n"
    )
    res = step15.apply(g, StepContext(domain_ns=DOMAIN_NS))
    # 정방향 쌍은 점유됐지만 self-referential 이므로 역방향 판정을 하지 않는다.
    # 즉 pair_taken 은 forward_taken 만 보고 결정되며, 이 경우 skip 이 정답이다.
    # 예외가 사라지면 ops_linking(A,A) 가 두 번 걸려 **판정 근거가 달라진다** —
    # 그 차이를 stats 로 고정한다.
    assert res.stats["cross_domain_ops_skipped_duplicate"] == 1
    assert res.stats["cross_domain_dangling_inverse_cleaned"] == 0, (
        "self-ref 경로에서 남의 inverseOf 를 지웠다"
    )
    # 그리고 기존 self-loop OP 는 손상되지 않아야 한다.
    assert (D("existingSelf"), RDF.type, OWL.ObjectProperty) in g


def test_step_15b_skips_the_whole_fk_pair_when_the_reverse_is_taken(monkeypatch):
    """step_15b 도 쌍 단위다 — 정방향만 만들면 step_02 가 dangling 을 만든다.

    step_02 는 정방향에 ``owl:inverseOf is{Target}Of`` 를 붙이므로, 역방향 선언을
    막고 정방향을 만들면 그 참조가 허공을 가리킨다 (실측: 2차 실행에서 5건).
    """
    import tempfile
    from pathlib import Path

    import config
    from tools.quality_steps import step_15b_fk_op_autocreate as step15b

    # 리포의 실제 rules/contracts/fk_patterns.json 을 쓴다 (경로가 모듈에 고정돼 있다).
    # ItemMaster 로 해석되는 FK 컬럼을 가진 최소 CSV 두 장만 스텁한다.
    tmp = tempfile.mkdtemp()
    Path(tmp, "Order_Head.csv").write_text(
        "Order_ID,Item_Code\nO1,I1\n", encoding="utf-8",
    )
    Path(tmp, "Item_Master.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)
    # A-Box 사용 신호를 비운다 — 이 테스트가 검증하는 것은 **값이 없을 때** 의
    # 중복 억제다. 실제 배포 A-Box 에 hasItemMaster 값이 있으면 억제-취소가 걸려
    # 다른 계약을 재는 테스트가 된다 (그 계약은 별도 테스트로 고정돼 있다).
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda names: set(),
    )

    # (ItemMaster → OrderHead) 를 다른 이름이 이미 점유 → 쌍 전체 skip 이어야 한다.
    g = _graph(
        f"{NS_PREFIX}:OrderHead a owl:Class .\n{NS_PREFIX}:ItemMaster a owl:Class .\n"
        f"{NS_PREFIX}:itemAppearsInOrder a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:ItemMaster ; rdfs:range {NS_PREFIX}:OrderHead .\n"
    )
    stats = step15b._autocreate_fk_ops(g, DOMAIN_NS)

    # (1) 쌍이 점유돼 있으니 **아무것도 만들지 않아야** 한다. 두 다리를 다 만들면
    #     dangling 은 없지만 (ItemMaster→OrderHead) 에 동의어가 생겨 이 가드의
    #     목적(중복 방지) 자체가 무너진다.
    assert stats["fk_ops_autocreated"] == 0, (
        f"점유된 쌍에 OP 를 만들었다: {stats}"
    )
    assert stats["fk_inverse_skipped_duplicate"] == 1
    assert sorted(
        str(o).split("#")[-1] for o in ops_linking(g, D("ItemMaster"), D("OrderHead"))
    ) == ["itemAppearsInOrder"], "같은 쌍에 동의어가 추가됐다"

    # (2) 한쪽 다리만 만들지도 않았다 — step_02 가 붙이는 owl:inverseOf 가
    #     미선언 이름을 가리키는 dangling 을 만들기 때문이다.
    fwd = (D("hasItemMaster"), RDF.type, OWL.ObjectProperty) in g
    inv = (D("isItemMasterOf"), RDF.type, OWL.ObjectProperty) in g
    assert fwd == inv is False, f"한쪽 다리만 만들었다 (forward={fwd} inverse={inv})"
    dangling = [
        (str(s).split("#")[-1], str(o).split("#")[-1])
        for s, _, o in g.triples((None, OWL.inverseOf, None))
        if (o, RDF.type, OWL.ObjectProperty) not in g
    ]
    assert not dangling, f"미선언 OP 를 가리키는 inverseOf 가 남았다: {dangling}"


def test_step_15b_still_creates_both_legs_for_a_free_pair(monkeypatch):
    """PRESERVATION: 빈 쌍이면 FK OP 를 **양쪽 다** 만든다.

    가드가 과도하게 막으면 FK 관계가 사라져 A-Box FK 매칭이 끊기고 CW master 가
    100% 고립된다 (step_15b 의 원래 존재 이유).
    """
    import tempfile
    from pathlib import Path

    import config
    from tools.quality_steps import step_15b_fk_op_autocreate as step15b

    tmp = tempfile.mkdtemp()
    Path(tmp, "Order_Head.csv").write_text(
        "Order_ID,Item_Code\nO1,I1\n", encoding="utf-8",
    )
    Path(tmp, "Item_Master.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)

    g = _graph(
        f"{NS_PREFIX}:OrderHead a owl:Class .\n{NS_PREFIX}:ItemMaster a owl:Class .\n"
    )
    stats = step15b._autocreate_fk_ops(g, DOMAIN_NS)
    assert stats["fk_ops_autocreated"] == 1, f"빈 쌍인데 만들지 않았다: {stats}"
    assert (D("hasItemMaster"), RDF.type, OWL.ObjectProperty) in g
    assert (D("isItemMasterOf"), RDF.type, OWL.ObjectProperty) in g, (
        "역방향을 만들지 않았다 — step_02 의 inverseOf 가 허공을 가리킨다"
    )


# ── 4. 죽은 스텁 제거 (step_21b) ─────────────────────────────────────


def test_dead_stub_with_no_signal_is_pruned():
    """THE REGRESSION: 구조·설정·tacit·개체 어디에도 없는 클래스는 지운다."""
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    g = _graph(
        f"{NS_PREFIX}:DeadStub a owl:Class ; rdfs:label \"dead\"@en .\n"
        f"{NS_PREFIX}:Live a owl:Class .\n"
        f"{NS_PREFIX}:op a owl:ObjectProperty ; rdfs:domain {NS_PREFIX}:Live ; "
        f"rdfs:range {NS_PREFIX}:Live .\n"
    )
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("DeadStub"), RDF.type, OWL.Class) not in g
    assert (D("Live"), RDF.type, OWL.Class) in g, "산 클래스를 지웠다"
    assert res.stats["dead_stub_names"] == ["DeadStub"]


@pytest.mark.parametrize("signal_body,name", [
    # S1 — 구조 공리의 대상
    (f"{NS_PREFIX}:Other a owl:Class ; rdfs:subClassOf {NS_PREFIX}:Target .\n", "S1 자식 보유"),
    # S2 — 자신이 계층에 자리를 가짐
    (f"{NS_PREFIX}:Parent a owl:Class .\n"
     f"{NS_PREFIX}:Target rdfs:subClassOf {NS_PREFIX}:Parent .\n", "S2 부모 보유"),
    # S1 — domain/range 로 등장
    (f"{NS_PREFIX}:op a owl:ObjectProperty ; rdfs:domain {NS_PREFIX}:Target ; "
     f"rdfs:range {NS_PREFIX}:Target .\n", "S1 domain/range"),
])
def test_any_single_signal_preserves_the_class(signal_body, name):
    """PRESERVATION: 신호 하나만 있어도 보존한다 (5신호 OR).

    각 신호를 빼면 무엇이 잘못 지워지는지 실측했다 — 자식만 보면
    ``MaintenanceManagement``, comment 를 넣으면 ``EquipmentAsset``, 설정만 믿으면
    ``TransactionRecord``, 인스턴스 0건을 넣으면 **65개 중 64개** 가 지워진다.
    """
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    g = _graph(f"{NS_PREFIX}:Target a owl:Class .\n" + signal_body)
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("Target"), RDF.type, OWL.Class) in g, f"{name} 인데 지웠다"


def test_configured_domain_group_is_preserved_even_without_children():
    """설정이 선언한 도메인 그룹은 자식이 없어도 보존한다.

    실측: ``MaintenanceManagement`` 는 lazy class 24개 중 유일하게 자식이 0이다.
    자식 유무만 보는 기준이면 지워지고 정비 계층이 무너진다.
    """
    from tools.ontology_quality import _load_hierarchy_config
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    configured = [
        cfg["class"] for cfg in _load_hierarchy_config().values() if cfg.get("class")
    ]
    assert configured, "설정에 도메인 그룹이 없다 (픽스처 전제 실패)"
    target = configured[0]
    g = _graph(f"{NS_PREFIX}:{target} a owl:Class .\n")
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D(target), RDF.type, OWL.Class) in g, f"설정 선언 {target} 을 지웠다"


def test_class_with_used_individuals_is_preserved():
    """개체가 **다른 트리플에서 쓰이면** 보존한다."""
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    g = _graph(
        f"{NS_PREFIX}:Enum a owl:Class .\n"
        f"{NS_PREFIX}:ValueA a {NS_PREFIX}:Enum .\n"
        f"{NS_PREFIX}:Other a owl:Class .\n"
        f"{NS_PREFIX}:Other {NS_PREFIX}:hasValue {NS_PREFIX}:ValueA .\n"
    )
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("Enum"), RDF.type, OWL.Class) in g, "쓰이는 개체를 가진 클래스를 지웠다"


def test_unreferenced_individuals_do_not_rescue_a_dead_stub():
    """THE ``GHGScope`` CASE: 아무도 참조하지 않는 개체는 스텁의 일부다.

    ``Scope1/2/3`` 은 ``a GHGScope`` + 라벨만 있고 A-Box·추론·딕셔너리·tacit 어디에도
    참조가 없었다. "개체가 있으면 보존" 으로만 판정하면 그 자기참조가 클래스를 영구히
    살려둔다 — 실측으로 스텁 1개가 제거되지 않고 남았다.
    """
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    g = _graph(
        f"{NS_PREFIX}:GHGScope a owl:Class ; rdfs:label \"GHG scope\"@en .\n"
        f"{NS_PREFIX}:Scope1 a {NS_PREFIX}:GHGScope ; rdfs:label \"Scope 1\"@en .\n"
        f"{NS_PREFIX}:Scope2 a {NS_PREFIX}:GHGScope ; rdfs:label \"Scope 2\"@en .\n"
    )
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("GHGScope"), RDF.type, OWL.Class) not in g, "참조 없는 개체가 클래스를 살렸다"
    # 개체도 함께 정리돼야 한다 — 타입이 사라진 고아 개체를 남기면 안 된다.
    assert list(g.predicate_objects(D("Scope1"))) == [], "개체 Scope1 이 고아로 남았다"
    assert list(g.predicate_objects(D("Scope2"))) == [], "개체 Scope2 이 고아로 남았다"
    assert res.stats["dead_stub_names"] == ["GHGScope"]


def test_individual_with_its_own_data_property_keeps_the_class():
    """PRESERVATION: 개체가 주석 아닌 프로퍼티를 가지면 실질 내용이 있다 — 보존."""
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    g = _graph(
        f"{NS_PREFIX}:Enum a owl:Class .\n"
        f"{NS_PREFIX}:ValueA a {NS_PREFIX}:Enum ; {NS_PREFIX}:factor 2.5 .\n"
    )
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("Enum"), RDF.type, OWL.Class) in g, "값을 가진 개체의 클래스를 지웠다"


def test_broken_string_literal_reference_does_not_keep_a_stub_alive():
    """``owl:equivalentClass "_:x"^^xsd:string`` 은 IRI 가 아니라 의미가 없다.

    실측: 이 문자열 리터럴을 "자리" 로 세면 ``GHGScope`` 가 영구히 살아남는다.
    """
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    g = _graph(f"{NS_PREFIX}:Stub a owl:Class .\n")
    g.add((D("Stub"), OWL.equivalentClass, Literal("_:phantomEnum")))
    apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("Stub"), RDF.type, OWL.Class) not in g, "문자열 리터럴을 자리로 인정했다"


# ── 5. jury_fixes 우회 경로 차단 (D-1) ───────────────────────────────


def test_add_inverse_property_cannot_resurrect_a_refused_synonym():
    """THE BYPASS: ``add_object_property`` 가 거부한 쌍을 inverse 핸들러로 되살릴 수 없다.

    ``_swap_domain_range`` 가 상대의 domain/range 를 뒤집어 채우므로, 신규
    프로퍼티가 결국 기존 OP 와 같은 ``(domain, range)`` 를 갖게 된다. 실측
    (2026-08-11): ``isBOf(B→A)`` 가 있는 그래프에 ``(synonymAtoB, inverseOf=isBOf)``
    를 주면 ``(A→B)`` 가 ``{hasB, synonymAtoB}`` 로 늘어났다.
    """
    from tools.jury_fixes import apply_jury_fixes

    ttl = _HDR + (
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:hasB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .\n"
        f"{NS_PREFIX}:isBOf a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:B ; rdfs:range {NS_PREFIX}:A .\n"
    )
    res = apply_jury_fixes(
        ttl,
        [{"action": "add_inverse_property",
          "property": "synonymAtoB", "inverseOf": "isBOf"}],
    )
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    assert sorted(
        str(o).split("#")[-1] for o in ops_linking(g, D("A"), D("B"))
    ) == ["hasB"], "우회 경로로 동의어가 생겼다"
    assert len(res.get("applied", [])) == 0
    assert len(res.get("noop", [])) == 1, "차단은 noop 으로 보고돼야 한다"


def test_add_inverse_property_still_completes_an_existing_pair():
    """PRESERVATION: 이미 선언된 두 OP 를 inverseOf 로 잇는 것은 정당한 보강이다.

    가드가 이것까지 막으면 ``check_quality_rules`` 의 missing_domain/range HIGH 를
    방지하려던 이 핸들러의 본래 목적이 사라진다.
    """
    from tools.jury_fixes import apply_jury_fixes

    # 케이스 1 — 상대의 domain/range 가 비어 있어 쌍 조회가 성립하지 않는 경로.
    ttl = _HDR + (
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:hasB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .\n"
        f"{NS_PREFIX}:isBOf a owl:ObjectProperty .\n"     # domain/range 누락
    )
    res = apply_jury_fixes(
        ttl,
        [{"action": "add_inverse_property", "property": "isBOf", "inverseOf": "hasB"}],
    )
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    assert len(res.get("applied", [])) == 1, f"정당한 보강을 막았다: {res.get('noop')}"
    assert D("B") in set(g.objects(D("isBOf"), RDFS.domain))
    assert D("A") in set(g.objects(D("isBOf"), RDFS.range))

    # 케이스 2 — **양쪽이 이미 선언되고 각자 쌍을 점유한** 상태에서 inverseOf 만
    # 잇는다. 여기서 "기존 OP 는 통과" 예외가 없으면 자기 자신이 점유한 쌍을
    # 근거로 스스로를 거부한다 (자기 마스킹). 이 경로가 이 예외의 존재 이유다.
    ttl2 = _HDR + (
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:hasB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .\n"
        f"{NS_PREFIX}:isBOf a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:B ; rdfs:range {NS_PREFIX}:A .\n"
    )
    res2 = apply_jury_fixes(
        ttl2,
        [{"action": "add_inverse_property", "property": "isBOf", "inverseOf": "hasB"}],
    )
    g2 = Graph()
    g2.parse(data=res2["ttl"], format="turtle")
    assert len(res2.get("applied", [])) == 1, (
        f"이미 선언된 두 OP 를 잇는 정당한 보강을 막았다: {res2.get('noop')}"
    )
    assert (D("hasB"), OWL.inverseOf, D("isBOf")) in g2
    assert (D("isBOf"), OWL.inverseOf, D("hasB")) in g2

    # 케이스 3 — 대상 쌍에 **다른 OP 도** 함께 있는 상태에서 기존 두 OP 를 잇는다.
    # 여기서 "기존 OP 는 통과" 예외가 없으면 그 다른 OP 를 근거로 정당한 inverseOf
    # 보강이 거부된다 (중복은 이미 존재하고, 이 액션은 중복을 늘리지 않는다).
    ttl3 = _HDR + (
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:hasB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .\n"
        f"{NS_PREFIX}:isBOf a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:B ; rdfs:range {NS_PREFIX}:A .\n"
        f"{NS_PREFIX}:altBtoA a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:B ; rdfs:range {NS_PREFIX}:A .\n"
    )
    res3 = apply_jury_fixes(
        ttl3,
        [{"action": "add_inverse_property", "property": "isBOf", "inverseOf": "hasB"}],
    )
    g3 = Graph()
    g3.parse(data=res3["ttl"], format="turtle")
    assert len(res3.get("applied", [])) == 1, (
        f"같은 쌍의 다른 OP 를 근거로 기존 OP 간 inverseOf 보강을 막았다: "
        f"{res3.get('noop')}"
    )
    assert (D("isBOf"), OWL.inverseOf, D("hasB")) in g3


def test_split_property_does_not_hand_one_column_to_two_same_domain_splits():
    """같은 domain 의 split 두 개가 같은 ``dcterms:source`` 를 물려받으면 안 된다.

    A-Box 출처 인덱스는 ``(domain_class, UPPER(column))`` 키라 어느 DP 가 값을
    받을지 비결정적이 된다. 조용한 오적재보다 이름 근사 폴백이 낫다.
    """
    from tools.jury_fixes import _apply_split_property

    g = _graph(
        f"{NS_PREFIX}:A a owl:Class .\n"
        f'{NS_PREFIX}:sampleId a owl:DatatypeProperty ; '
        f'rdfs:domain {NS_PREFIX}:A ; dcterms:source "COL_X" .\n'
    )
    _apply_split_property(g, {"target": "sampleId", "splits": [
        {"name": "aSampleId", "domain": "A", "type": "DatatypeProperty"},
        {"name": "a2SampleId", "domain": "A", "type": "DatatypeProperty"},
    ]})
    sources = [
        (str(s).split("#")[-1], str(o))
        for s, _, o in g.triples((None, _DCTERMS.source, None))
    ]
    assert sources == [], f"같은 domain 두 split 에 같은 컬럼을 물려줬다: {sources}"


def test_split_property_still_inherits_source_across_distinct_domains():
    """PRESERVATION: domain 이 다르면 상속이 정답이다 (원본이 삭제되므로).

    상속하지 않으면 출처가 통째로 사라지고 A-Box 는 그 컬럼을 이름 추측으로만
    찾는다 — split 의 원래 목적(PK/FK 혼용 분리)이 무의미해진다.
    """
    from tools.jury_fixes import _apply_split_property

    g = _graph(
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f'{NS_PREFIX}:sampleId a owl:DatatypeProperty ; '
        f'rdfs:domain {NS_PREFIX}:A ; dcterms:source "COL_X" .\n'
    )
    _apply_split_property(g, {"target": "sampleId", "splits": [
        {"name": "aSampleId", "domain": "A", "type": "DatatypeProperty"},
        {"name": "bSampleId", "domain": "B", "type": "DatatypeProperty"},
    ]})
    for local in ("aSampleId", "bSampleId"):
        assert [str(o) for o in g.objects(D(local), _DCTERMS.source)] == ["COL_X"], (
            f"{local} 이 출처를 잃었다"
        )


def test_split_property_prefers_an_explicit_per_split_source():
    """split 별 명시 출처는 의도된 값이므로 같은 domain 이어도 그대로 쓴다."""
    from tools.jury_fixes import _apply_split_property

    g = _graph(
        f"{NS_PREFIX}:A a owl:Class .\n"
        f'{NS_PREFIX}:sampleId a owl:DatatypeProperty ; '
        f'rdfs:domain {NS_PREFIX}:A ; dcterms:source "COL_X" .\n'
    )
    _apply_split_property(g, {"target": "sampleId", "splits": [
        {"name": "aSampleId", "domain": "A", "type": "DatatypeProperty",
         "source": "COL_A"},
        {"name": "a2SampleId", "domain": "A", "type": "DatatypeProperty",
         "source": "COL_B"},
    ]})
    assert [str(o) for o in g.objects(D("aSampleId"), _DCTERMS.source)] == ["COL_A"]
    assert [str(o) for o in g.objects(D("a2SampleId"), _DCTERMS.source)] == ["COL_B"]


def test_directional_token_matching_uses_word_boundaries():
    """``to`` 가 ``inventory`` / ``transportation`` 에 부분일치하면 안 된다.

    방향 대립 면제는 중복 OP 게이트를 **통과시키는** 판정이라, 오탐은 진짜 중복을
    숨긴다. 한글 토큰은 복합어로 붙어 쓰이므로 부분 문자열 매칭을 유지한다.
    """
    from tools.ontology_quality import _directional_tokens_in_labels

    g = _graph(
        f"{NS_PREFIX}:invOp a owl:ObjectProperty ; "
        f'rdfs:label "inventory transportation record"@en .\n'
        f"{NS_PREFIX}:realTo a owl:ObjectProperty ; rdfs:label "
        f'"ships to warehouse"@en .\n'
        f"{NS_PREFIX}:koOp a owl:ObjectProperty ; rdfs:label "
        f'"출발지 창고"@ko .\n'
    )
    assert "to" not in _directional_tokens_in_labels(g, D("invOp")), (
        "'inventory'/'transportation' 안의 'to' 를 방향 토큰으로 읽었다"
    )
    assert "to" in _directional_tokens_in_labels(g, D("realTo")), (
        "단어로 등장한 'to' 를 놓쳤다"
    )
    assert "출발" in _directional_tokens_in_labels(g, D("koOp")), (
        "한글 복합어(출발지) 안의 토큰을 놓쳤다"
    )


# ── 6. step_21b 의 A-Box 신호 (S6, fail-closed) ──────────────────────


def test_dead_stub_prune_keeps_a_class_the_abox_instantiated(monkeypatch, tmp_path):
    """S6: T-Box 구조 신호가 없어도 A-Box 인스턴스가 있으면 보존한다.

    지웠다면 그 인스턴스들이 타입 없는 고아가 된다.
    """
    import config
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        f"<{DOMAIN_NS}inst1> a <{DOMAIN_NS}LiveByAbox> .\n", encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g = _graph(
        f"{NS_PREFIX}:LiveByAbox a owl:Class .\n"
        f"{NS_PREFIX}:TrulyDead a owl:Class .\n"
    )
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("LiveByAbox"), RDF.type, OWL.Class) in g, "A-Box 가 쓰는 클래스를 지웠다"
    assert (D("TrulyDead"), RDF.type, OWL.Class) not in g, "진짜 죽은 스텁은 지워야 한다"
    assert res.stats["dead_stub_kept_by_abox"] == ["LiveByAbox"]


def test_dead_stub_prune_is_fail_closed_when_the_namespace_is_unknown(
    monkeypatch, tmp_path,
):
    """FAIL-CLOSED: 판정 불가면 **아무것도 지우지 않는다** (step_12f 와 같은 계약).

    네임스페이스 설정이 비면 텍스트 스캔이 0건을 돌려주는데, 그 0 을 "미사용" 으로
    읽으면 살아있는 클래스를 전부 지운다 — 이 리포에서 두 번 재발한 버그 유형이다.
    """
    import config
    from tools.quality_steps import step_21b_dead_stub_prune as step

    abox = tmp_path / "a_box.ttl"
    abox.write_text("# empty\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    monkeypatch.setattr(
        "domain.graph_utils.domain_predicate_pattern", lambda names=None: None,
    )

    from tools.quality_steps._base import StepContext

    g = _graph(f"{NS_PREFIX}:WouldBePruned a owl:Class .\n")
    res = step.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("WouldBePruned"), RDF.type, OWL.Class) in g, "판정 불가인데 지웠다"
    assert res.stats["dead_stubs_pruned"] == 0
    assert res.stats["dead_stub_withheld_abox_unknown"] == 1


def test_dead_stub_prune_still_works_before_the_abox_exists(monkeypatch, tmp_path):
    """PRESERVATION of purpose: A-Box 가 없는 것은 정상이다 (S3 는 S7 앞).

    없는 파일을 근거로 전면 보류하면 스텝이 영구 no-op 이 된다.
    """
    import config
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    monkeypatch.setattr(config, "ABOX_PATH", str(tmp_path / "does_not_exist.ttl"))

    g = _graph(f"{NS_PREFIX}:DeadStub a owl:Class .\n")
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("DeadStub"), RDF.type, OWL.Class) not in g, (
        "A-Box 부재를 이유로 스텝이 no-op 이 됐다"
    )
    assert res.stats["dead_stub_names"] == ["DeadStub"]


def test_dead_stub_prune_is_fail_closed_when_the_abox_cannot_be_read(
    monkeypatch, tmp_path,
):
    """읽기 실패도 판정 불가다 — 권한/디스크 오류로 살아있는 클래스를 지우면 안 된다.

    파일이 **존재하는데** 열 수 없는 경우와, 애초에 없는 경우는 다르다. 후자는
    A-Box 생성 전이라는 정상 상태이고, 전자는 확인 불가다.
    """
    import builtins

    import config
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_21b_dead_stub_prune import apply

    abox = tmp_path / "a_box.ttl"
    abox.write_text(f"<{DOMAIN_NS}inst1> a <{DOMAIN_NS}Something> .\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    real_open = builtins.open

    def _failing_open(path, *args, **kwargs):
        if str(path) == str(abox):
            raise OSError("simulated read failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _failing_open)

    g = _graph(f"{NS_PREFIX}:WouldBePruned a owl:Class .\n")
    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert (D("WouldBePruned"), RDF.type, OWL.Class) in g, "읽기 실패인데 지웠다"
    assert res.stats["dead_stubs_pruned"] == 0
    assert res.stats["dead_stub_withheld_abox_unknown"] == 1


# ── 7. A-Box 사용 신호 (적대적 검증이 찾아낸 손실) ───────────────────


def test_abox_used_local_names_scans_tacit_too(monkeypatch, tmp_path):
    """tacit 이 쓰는 이름을 '미사용' 으로 읽으면 산 데이터를 지운다.

    실측 (2026-08-11): ``hasSoilImpact`` 는 object term 4,128건이 tacit 에 있는데
    A-Box 본문만 보는 판정은 0 을 반환했고, step_22e 는 그 0 을 근거로 값 0건인
    동의어를 정본으로 골랐다.
    """
    import config
    from domain import graph_utils

    abox = tmp_path / "abox" / "a_box.ttl"
    abox.parent.mkdir()
    abox.write_text(f"<{DOMAIN_NS}i1> <{DOMAIN_NS}inAbox> <{DOMAIN_NS}i2> .\n",
                    encoding="utf-8")
    tacit_dir = tmp_path / "src" / "tacit"
    tacit_dir.mkdir(parents=True)
    (tacit_dir / "k.ttl").write_text(
        f"<{DOMAIN_NS}i1> <{DOMAIN_NS}inTacitOnly> <{DOMAIN_NS}i3> .\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    monkeypatch.setattr(config, "SOURCE_DIR", str(tmp_path / "src"))
    graph_utils._ABOX_USED_CACHE.clear()

    used = graph_utils.abox_used_local_names({"inAbox", "inTacitOnly", "nowhere"})
    assert used == {"inAbox", "inTacitOnly"}, f"tacit 을 놓쳤다: {used}"


def test_abox_used_local_names_is_fail_closed(monkeypatch, tmp_path):
    """판정 불가는 ``None`` — 0건과 구분해야 한다.

    구분하지 않으면 호출부가 "미사용" 으로 오판해 산 데이터를 지운다 (이 리포에서
    하드코딩 prefix 때문에 두 번 재발한 사고 유형).
    """
    import config
    from domain import graph_utils

    abox = tmp_path / "a_box.ttl"
    abox.write_text("# x\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    monkeypatch.setattr(config, "SOURCE_DIR", str(tmp_path))
    graph_utils._ABOX_USED_CACHE.clear()
    monkeypatch.setattr(graph_utils, "domain_predicate_pattern", lambda names=None: None)

    assert graph_utils.abox_used_local_names({"x"}) is None, "판정 불가를 0건으로 봤다"


def test_step_15_does_not_suppress_an_op_the_abox_uses(monkeypatch):
    """THE LOSS: 값 있는 OP 를 억제하면 T-Box 미선언 술어가 되어 중복보다 나쁘다.

    실측: 억제 대상 43개 중 8개가 A-Box 값 보유(``realTimeDataOf`` 36,000 등)였고,
    전부 자기 쌍에서 값을 가진 유일하거나 최다인 OP 였다. 전체 재실행 시 미선언
    트리플이 52,471 → 91,117 로 늘어난다.
    """
    from tools.quality_steps import step_15_cross_domain_ops as step15

    monkeypatch.setattr(
        step15, "abox_used_local_names", lambda names: {"synonymAtoB"},
    )
    g, res = _drive_step_15(
        monkeypatch,
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:existingAtoB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .\n",
    )
    assert (D("synonymAtoB"), RDF.type, OWL.ObjectProperty) in g, (
        "A-Box 가 쓰는 OP 를 억제했다 — 미선언 술어가 된다"
    )
    assert res.stats["cross_domain_ops_kept_abox_used"] == 1
    assert res.stats["cross_domain_ops_skipped_duplicate"] == 0
    # 억제 취소 시에도 dangling 을 만들지 않는다 (양쪽 다 선언).
    assert (D("isSynonymBOfA"), RDF.type, OWL.ObjectProperty) in g


def test_step_15_still_suppresses_when_neither_leg_has_data(monkeypatch):
    """PRESERVATION of purpose: 둘 다 값 0건이면 여전히 억제한다.

    이 예외가 무조건 통과로 바뀌면 중복 가드 자체가 무력해진다.
    """
    from tools.quality_steps import step_15_cross_domain_ops as step15

    monkeypatch.setattr(step15, "abox_used_local_names", lambda names: set())
    g, res = _drive_step_15(
        monkeypatch,
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:existingAtoB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .\n",
    )
    assert (D("synonymAtoB"), RDF.type, OWL.ObjectProperty) not in g
    assert res.stats["cross_domain_ops_skipped_duplicate"] == 1
    assert res.stats["cross_domain_ops_kept_abox_used"] == 0


def test_step_15_fk_gate_blocks_ungrounded_pair(monkeypatch):
    """기본 모드에서는 FK 근거 없는 쌍을 만들지 않는다.

    위 테스트들은 ``_drive_step_15`` 가 게이트를 ``warn`` 으로 두므로 중복 가드만
    검증한다. 그 ``warn`` 이 게이트를 **영구히 끄는 것으로 오해되지 않도록**, 같은
    픽스처로 기본 모드의 차단을 여기서 고정한다.

    실측 (2026-08-14): 게이트 없이 S3 를 돌리면 신규 OP 53개 중 39개가 CSV FK 근거도
    A-Box·tacit 데이터도 없었다 (OP 사용률 39.1% → 35.1% 악화, RR 은 0.323 → 0.412 상승).
    """
    import tempfile
    from pathlib import Path

    import config
    from tools.quality_steps import step_15_cross_domain_ops as step15
    from tools.quality_steps._base import StepContext

    monkeypatch.setattr(
        "tools.ontology_quality._load_cross_domain_ops", lambda: ["synonymAtoB"],
    )
    monkeypatch.setattr(
        "tools.ontology_quality._load_cross_domain_op_meta",
        lambda: {
            "synonymAtoB": (
                "A", "B", "synonym a to b", "동의어", "동의어 관계", "isSynonymBOfA",
            ),
        },
    )
    tmp = tempfile.mkdtemp()
    for name in ("A", "B"):
        Path(tmp, f"{name}.csv").write_text("col\n1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)
    monkeypatch.delenv("TBOX_CROSS_DOMAIN_FK_GATE", raising=False)
    monkeypatch.setattr(step15, "abox_used_local_names", lambda names: set())

    g = _graph(f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n")
    res = step15.apply(g, StepContext(domain_ns=DOMAIN_NS))

    assert (D("synonymAtoB"), RDF.type, OWL.ObjectProperty) not in g, (
        "FK 근거가 없는데 주입됐다 — 게이트가 꺼졌다"
    )
    assert res.stats["cross_domain_ops_skipped_no_fk"] >= 1
    assert res.stats["cross_domain_fk_gate_mode"] == "skip"


def test_step_15_fk_skip_also_cleans_dangling_inverse(monkeypatch):
    """FK 게이트가 건너뛴 쌍의 dangling inverseOf 도 정리한다.

    실측 회귀 (2026-08-15): FK 게이트를 신설할 때 ``continue`` 가 중복 skip 경로의
    dangling 정리보다 **앞에** 있어서, 게이트가 건너뛴 쌍을 가리키는 기존
    ``owl:inverseOf`` 가 허공을 가리킨 채 남았다. 두 skip 경로가 같은 정리를
    해야 한다.
    """
    import tempfile
    from pathlib import Path

    import config
    from tools.quality_steps import step_15_cross_domain_ops as step15
    from tools.quality_steps._base import StepContext

    monkeypatch.setattr(
        "tools.ontology_quality._load_cross_domain_ops", lambda: ["synonymAtoB"],
    )
    monkeypatch.setattr(
        "tools.ontology_quality._load_cross_domain_op_meta",
        lambda: {
            "synonymAtoB": (
                "A", "B", "synonym a to b", "동의어", "동의어 관계", "isSynonymBOfA",
            ),
        },
    )
    tmp = tempfile.mkdtemp()
    for name in ("A", "B"):
        Path(tmp, f"{name}.csv").write_text("col\n1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)
    monkeypatch.delenv("TBOX_CROSS_DOMAIN_FK_GATE", raising=False)
    monkeypatch.setattr(step15, "abox_used_local_names", lambda names: set())

    # 기존 OP 가 게이트에 걸릴 이름을 inverseOf 로 참조하고 있다.
    g = _graph(
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
        f"{NS_PREFIX}:existingAtoB a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B ; "
        f"owl:inverseOf {NS_PREFIX}:isSynonymBOfA .\n",
    )
    res = step15.apply(g, StepContext(domain_ns=DOMAIN_NS))

    dangling = [
        (str(s).split("#")[-1], str(o).split("#")[-1])
        for s, _, o in g.triples((None, OWL.inverseOf, None))
        if (o, RDF.type, OWL.ObjectProperty) not in g
    ]
    assert not dangling, (
        f"FK skip 경로가 dangling inverseOf 를 남겼다: {dangling}"
    )
    assert res.stats["cross_domain_dangling_inverse_cleaned"] >= 1


def test_step_15_cleans_dangling_inverse_in_both_directions():
    """orphan 이 subject 인 ``inverseOf`` 도 지운다.

    object 쪽만 지우면 ``orphan owl:inverseOf X`` 가 남는데, 그 subject 는 OP 로
    선언되지 않아 step_22d 가 세지 않는다 — 게이트에 보이지 않는 dangling 이
    영구히 남는다 (실측 1건, 3/3 재현).
    """
    import inspect

    from tools.quality_steps import step_15_cross_domain_ops as step15

    src = inspect.getsource(step15.apply)
    assert "g.triples((orphan, OWL.inverseOf, None))" in src, (
        "orphan 이 subject 인 방향을 지우지 않는다"
    )


def test_duplicate_op_prune_keeps_the_property_that_has_data():
    """정본 선택은 **실측 사용량이 이름 점수를 이겨야** 한다.

    실측 (2026-08-11): ``hasSoilImpact`` (점수 -6, A-Box+tacit 4,228건) 가
    ``hasSoilMonitoringReport`` (점수 +14, 0건) 에게 져서 삭제 대상이 됐다. 그
    결과가 이 스텝이 막으려던 "빈 관계로 질의하면 0건" 이다.
    """
    from tools.quality_steps import step_22e_duplicate_op_prune as step22e

    # 이름 점수가 낮지만 값이 있는 쪽 vs 점수가 높지만 값 0건인 쪽.
    # hasSoilImpact 는 range 이름(SoilMonitoring)을 담지 않아 점수가 낮고,
    # hasSoilMonitoringReport 는 담아서 높다 — 실제 배포본의 구도다.
    g = _graph(
        f"{NS_PREFIX}:EquipmentMaster a owl:Class .\n"
        f"{NS_PREFIX}:SoilMonitoring a owl:Class .\n"
        f"{NS_PREFIX}:hasSoilImpact a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:EquipmentMaster ; rdfs:range {NS_PREFIX}:SoilMonitoring .\n"
        f"{NS_PREFIX}:hasSoilMonitoringReport a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:EquipmentMaster ; rdfs:range {NS_PREFIX}:SoilMonitoring ; "
        f'rdfs:comment "보고서"@ko .\n'
    )
    plan = step22e._build_prune_plan(g, DOMAIN_NS, {"hasSoilImpact": 4228}) if hasattr(
        step22e, "_build_prune_plan",
    ) else None
    if plan is None:                       # 내부 함수명이 다르면 스텝으로 구동

        import pytest as _pytest

        from tools.quality_steps._base import StepContext

        mp = _pytest.MonkeyPatch()
        try:
            mp.setattr(
                "tools.ontology_quality._load_abox_op_usage",
                lambda abox_path=None: {"hasSoilImpact": 4228},
            )
            mp.setenv("TBOX_DUP_OP_PRUNE", "on")
            res = step22e.apply(g, StepContext(domain_ns=DOMAIN_NS))
        finally:
            mp.undo()
        plan = res.stats.get("plan") or []
    keeps = {e["keep"] for e in plan}
    removes = {r for e in plan for r in e.get("remove", [])}
    assert "hasSoilImpact" in keeps, (
        f"값 4,228건인 프로퍼티를 정본으로 고르지 않았다 (keep={keeps})"
    )
    assert "hasSoilImpact" not in removes, "값 있는 프로퍼티를 삭제 대상으로 뒀다"


def test_abox_used_local_names_withholds_on_read_failure(monkeypatch, tmp_path):
    """파일이 있는데 열 수 없으면 판정 불가다 — 부분 결과를 돌려주면 안 된다.

    일부 파일만 읽고 "이건 미사용" 이라고 답하면 호출부가 산 데이터를 지운다.
    (파일이 애초에 없는 것은 다른 상황이고, 그때는 A-Box 생성 전이라는 정상 상태다.)
    """
    import builtins

    import config
    from domain import graph_utils

    abox = tmp_path / "a_box.ttl"
    abox.write_text(f"<{DOMAIN_NS}i> <{DOMAIN_NS}used> <{DOMAIN_NS}j> .\n",
                    encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    monkeypatch.setattr(config, "SOURCE_DIR", str(tmp_path))
    graph_utils._ABOX_USED_CACHE.clear()

    real_open = builtins.open

    def _failing_open(path, *args, **kwargs):
        if str(path) == str(abox):
            raise OSError("simulated read failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _failing_open)
    assert graph_utils.abox_used_local_names({"used"}) is None, (
        "읽기 실패인데 부분 결과를 돌려줬다 — 호출부가 '미사용' 으로 오판한다"
    )
