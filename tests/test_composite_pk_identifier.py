"""composite PK 가 ``dcterms:identifier`` 에 합성 조인키를 심지 않는가.

2026-08-28 실측. ``dcterms:identifier`` 59,280건(인스턴스의 84%)이 전부
``"TAG001|2025-09-01 00:00:00"`` 형태 **파이프 합성 조인키**였다. 6개 클래스가
그 폴백을 탔고 전부 composite PK 테이블이다::

    RealTimeData 36,000 / EquipmentStatus 6,000 / Process_* 4×4,320

## 왜 문제인가

``dcterms:identifier`` 는 "이 개체의 식별자" 라는 **표준 어휘**이므로 외부 시스템
(MES/ERP)이 그 값으로 조회한다. 파이프 합성값은 어느 시스템에도 없어 조인이
불가능하고, LLM 도 그것을 조회 키로 오인한다. 개체 식별은 IRI 가 이미 한다
(``RealTimeData_TAG001_2025-09-01_00_00_00``).

게다가 값이 **중복**이었다 — 구성 요소가 이미 따로 있다::

    realTimeDataTagIdRef     "TAG001"                       ← 외부 조회 가능
    realTimeDataTimestamp    "2025-09-01T00:00:00"          ← 타입 있는 dateTime
    hasRealTimeTag           TagMaster_TAG001               ← OP 링크
    identifier               "TAG001|2025-09-01 00:00:00"   ← 위 둘을 뭉갠 중복

## 왜 폴백 제거만으로는 부족했나

RealTimeData 만 온전했던 이유가 답을 줬다: 그 클래스에는 T-Box 에
``realTimeDataTagIdRef`` (``dcterms:source`` = Tag_ID) 가 선언돼 있어 A-Box 가 FK 값을
리터럴로 채운다. 나머지 5개는 **그 DP 가 T-Box 에 없어** FK 값이 리터럴로 전혀 남지
않았다 (실측: EQ001/P001 이 IRI 와 OP 링크에만 존재). 폴백만 지우면 리터럴 조회 경로를
잃으므로 ``tbox_manual_additions.ttl`` 에 5개 DP 를 함께 추가했다.

## 이 테스트의 방향

"identifier 가 없다" 만 주장하면 식별자를 통째로 잃어도 통과한다. 세 축을 고정한다:

* 제거 — composite PK 는 합성값을 심지 않는다
* 보존 — **단일 컬럼 PK 는 그대로 유지한다** (그 값은 CSV 에 실재하는 조회 키다)
* 대체 — composite PK 클래스가 FK ID 를 리터럴로 갖는다 (조회 경로 유지)
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import Graph, Namespace, URIRef

from tools.abox_generation import _emit_pk_identifier, _is_composite_pk

DCTERMS = Namespace("http://purl.org/dc/terms/")
ABOX = pathlib.Path("data/generated/abox/a_box.ttl")
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")
NS = "http://example.com/steel-ontology#"

#: composite PK 라서 폴백을 타던 클래스 → 대체 FK ID DP.
_COMPOSITE_CLASSES = {
    "EquipmentStatus": "equipmentStatusEquipmentIdRef",
    "ProcessBlastFurnace": "blastFurnaceProductIdRef",
    "RealTimeData": "realTimeDataTagIdRef",
}

#: 처음엔 이 3개도 패치로 선언했는데 2026-08-29 재실행에서 S2 가 같은 컬럼에
#: ``*ProductId`` 를 스스로 만들어 ``dcterms:source`` 충돌이 생겼고, A-Box 생성기가
#: **양쪽 모두 버려** FK 리터럴이 0건이 됐다. 패치에서 제거하고 병합 시점 충돌 회피를
#: 넣었다 (step_30 의 ``_conflicting_patch_dps`` / tests/test_patch_dp_source_conflict).
#: 이 클래스들의 FK 리터럴은 S2 가 만드는 DP 가 채운다.
_S2_PROVIDED_CLASSES = {
    "ProcessContinuousCasting": "continuousCastingProductId",
    "ProcessRolling": "rollingProductId",
    "ProcessSteelmakingFurnace": "steelmakingProductId",
}


class _Writer:
    def __init__(self) -> None:
        self.triples: list[tuple] = []

    def add(self, triple: tuple) -> None:
        self.triples.append(triple)


def _emit(pk_column, row: dict) -> list[str]:
    """``_emit_pk_identifier`` 를 돌려 기록된 identifier 값 목록을 준다."""
    writer = _Writer()
    _emit_pk_identifier(
        None, pk_column, row, "SomeClass", URIRef("http://ex.org/i"), {}, writer,
    )
    return [
        str(o) for _, p, o in writer.triples
        if str(p) == str(DCTERMS.identifier)
    ]


def _emit_all(pk_column, row: dict, tbox_info: dict | None = None) -> list[str]:
    """기록된 **모든** 리터럴 값 — 술어를 가리지 않는다.

    ``_emit`` 은 ``dcterms:identifier`` 만 보므로 합성값이 class-specific ID DP 로
    옮겨가면 통과한다 (2026-08-30 실측: 그 경로로 15,180건이 새어나갔다).
    """
    writer = _Writer()
    _emit_pk_identifier(
        None, pk_column, row, "SomeClass", URIRef("http://ex.org/i"),
        tbox_info or {}, writer,
    )
    return [str(o) for _, _p, o in writer.triples]


# ── composite 판정 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pk", "expected"),
    [
        ("tag_id", False),                      # 단일 컬럼 (문자열)
        (["tag_id"], False),                    # 단일 원소 리스트도 composite 아님
        (["tag_id", "timestamp"], True),
        (("a", "b", "c"), True),
        (None, False),
        ([], False),
    ],
)
def test_composite_detection(pk, expected):
    """단일 원소 리스트를 composite 로 보면 정상 PK 까지 식별자를 잃는다."""
    assert _is_composite_pk(pk) is expected


# ── 제거: composite 는 합성값을 심지 않는다 ─────────────────────────────


def test_composite_pk_emits_no_identifier():
    """THE REGRESSION: composite PK 는 파이프 합성값을 만들지 않는다."""
    values = _emit(["tag_id", "timestamp"],
                   {"Tag_ID": "TAG001", "Timestamp": "2025-09-01 00:00:00"})

    assert values == [], f"합성 조인키가 기록됐다: {values}"


def test_three_column_composite_also_skipped():
    values = _emit(["a", "b", "c"], {"A": "1", "B": "2", "C": "3"})
    assert values == []


# ── 보존: 단일 PK 는 유지한다 (NEGATIVE 방향) ───────────────────────────


def test_single_column_pk_keeps_identifier():
    """단일 컬럼 PK 값은 CSV 에 실재하는 조회 키다 — 지우면 식별자를 잃는다."""
    assert _emit("tag_id", {"Tag_ID": "TAG001"}) == ["TAG001"]


def test_single_element_list_keeps_identifier():
    """리스트로 감싸였을 뿐인 단일 PK 도 유지된다."""
    assert _emit(["tag_id"], {"Tag_ID": "TAG001"}) == ["TAG001"]


def test_identifier_value_has_no_pipe():
    """유지되는 값에 파이프가 섞이지 않는다."""
    values = _emit("po_id", {"PO_ID": "PO-2025-001"})
    assert values == ["PO-2025-001"]
    assert "|" not in values[0]


# ── 제거 2: class-specific ID DP 로도 합성값이 새지 않는다 ──────────────
#
# 위 dcterms:identifier 폴백을 막은 뒤 **같은 합성값이 한 칸 옆으로 옮겨갔다**.
# _class_specific_id_dp 가 이름을 찾아주면 그 DP 에 파이프값이 박혀, 한 개체가
# 같은 DP 에 모순된 값 2개를 갖는다 (컬럼 경로의 "P015" + 이 경로의 "P015|...").


def _tbox_with_id_dp() -> dict:
    """``_class_specific_id_dp`` Candidate 1 이 적중하는 최소 tbox_info.

    ``someClassId`` 를 선언해 두면 이름 해석이 성공하므로, 합성값이 그 DP 로
    새는지 검사할 수 있다. 이 픽스처가 없으면 id_dp=None 폴백만 타서
    회귀가 재현되지 않는다.
    """
    return {
        "class_props": {"SomeClass": {"someclassid": "someClassId"}},
        "dp_ranges": {},
        "dp_by_source": {},
    }


def test_composite_pk_does_not_leak_into_class_specific_dp():
    """THE REGRESSION (2026-08-30): ID DP 가 해석되더라도 합성값을 쓰지 않는다."""
    values = _emit_all(
        ["product_id", "timestamp"],
        {"Product_ID": "P015", "Timestamp": "2025-09-30 23:50:00"},
        _tbox_with_id_dp(),
    )

    assert values == [], f"합성값이 class-specific DP 로 새어나갔다: {values}"
    assert not any("|" in v for v in values)


def test_single_pk_still_reaches_class_specific_dp():
    """NEGATIVE 방향 — 단일 PK 는 여전히 ID DP 에 적재된다.

    위 수정이 composite 뿐 아니라 단일 PK 까지 막으면 식별자를 통째로 잃는다.
    """
    values = _emit_all("product_id", {"Product_ID": "P015"}, _tbox_with_id_dp())

    assert values == ["P015"], f"단일 PK 값이 사라졌다: {values}"


def test_deployed_abox_has_no_pipe_in_any_literal():
    """배포 A-Box 의 **모든** 술어에 파이프 합성값이 없는가.

    이전 테스트는 ``dcterms:identifier`` 만 봤다. 술어를 가리지 않고 보면
    합성값이 어느 DP 로 옮겨가도 잡힌다.
    """
    if not ABOX.exists():
        pytest.skip("A-Box 없음")

    import re

    # `"값|값"` 형태 리터럴만 센다 — 주석/URI 의 파이프는 무시.
    pat = re.compile(r'"[^"\n]*\|[^"\n]*"')
    offenders: dict[str, int] = {}
    with ABOX.open(encoding="utf-8") as fh:
        for line in fh:
            if not pat.search(line):
                continue
            m = re.search(r"(steel:[A-Za-z0-9_]+|dcterms:identifier)\s+\"", line)
            key = m.group(1) if m else "<unknown>"
            offenders[key] = offenders.get(key, 0) + 1

    assert not offenders, (
        f"파이프 합성 리터럴이 남아 있다 (술어별 건수): {offenders}"
    )


# ── 배포 산출물 실측 ────────────────────────────────────────────────────


def test_deployed_abox_has_no_synthetic_identifier():
    """배포 A-Box 에 파이프 합성 identifier 가 없는가.

    grep 으로 본다 — 729k 트리플 rdflib 파싱은 느리고, 이 검사는 문자열 존재
    여부라 텍스트 스캔으로 충분하다 (533MB A-Box 파싱으로 step 22 가 정지한 이력).
    """
    if not ABOX.exists():
        pytest.skip("A-Box 없음")

    synthetic = 0
    with ABOX.open(encoding="utf-8") as fh:
        for line in fh:
            if "dcterms:identifier" in line and "|" in line:
                synthetic += 1

    assert synthetic == 0, f"파이프 합성 identifier {synthetic}건이 남아 있다"


def test_deployed_abox_fills_fk_id_literals():
    """composite PK 클래스가 FK ID 를 리터럴로 갖는가 — 조회 경로 대체 확인.

    폴백만 지우고 이것이 없으면 식별 정보를 잃는다 (실측: 5개 클래스가 그 상태였다).
    """
    if not ABOX.exists():
        pytest.skip("A-Box 없음")

    # 패치가 선언하는 것과 S2 가 선언하는 것을 함께 본다 — 어느 쪽이 담당하든
    # **FK 값이 리터럴로 남아 있는지**가 이 테스트의 관심사다.
    expected = {**_COMPOSITE_CLASSES, **_S2_PROVIDED_CLASSES}
    counts = dict.fromkeys(expected.values(), 0)
    with ABOX.open(encoding="utf-8") as fh:
        for line in fh:
            for dp in counts:
                if f"steel:{dp} " in line:
                    counts[dp] += 1

    missing = [dp for dp, n in counts.items() if n == 0]
    assert not missing, (
        f"FK ID 리터럴이 채워지지 않은 DP: {missing} — dcterms:source 충돌 의심"
    )
    assert sum(counts.values()) >= 50000, (
        f"FK ID 리터럴 합계가 {sum(counts.values())}건 — 6만 근처여야 한다"
    )


def test_fk_id_dps_are_declared_in_tbox():
    """FK ID DP 가 T-Box 에 선언되고 컬럼 매핑을 갖는가 (step_30 병합 결과).

    선언이 없으면 A-Box 가 컬럼을 매핑할 대상이 없어 조용히 값을 버린다. 어느 쪽이
    선언했는지(패치 vs S2)는 묻지 않는다 — 실행마다 달라지고, 중요한 것은 **결과**다.
    """
    if not TBOX.exists():
        pytest.skip("T-Box 없음")

    from rdflib import OWL, RDF, RDFS

    tbox = Graph()
    tbox.parse(str(TBOX), format="turtle")
    for cls, dp in {**_COMPOSITE_CLASSES, **_S2_PROVIDED_CLASSES}.items():
        uri = URIRef(NS + dp)
        assert (uri, RDF.type, OWL.DatatypeProperty) in tbox, f"{dp} 미선언"
        domains = {str(o) for o in tbox.objects(uri, RDFS.domain)}
        assert NS + cls in domains, f"{dp} 의 domain 이 {cls} 가 아니다: {domains}"
        sources = {str(o) for o in tbox.objects(uri, DCTERMS.source)}
        assert sources, f"{dp} 에 dcterms:source 가 없다 — 컬럼 매핑이 안 된다"


def test_no_duplicate_source_claim(tmp_path):
    """한 (클래스, 컬럼) 을 두 DP 가 주장하지 않는가.

    주장이 겹치면 A-Box 생성기가 **양쪽 모두 버리고** 폴백으로 떨어진다 — 실측
    2026-08-29: 그 상태에서 FK 리터럴 3개가 0건이 됐다. 원인은 패치와 S2 산출물의
    이름이 달라(``*ProductIdRef`` vs ``*ProductId``) 중복 선언이 된 것이다.
    """
    if not TBOX.exists():
        pytest.skip("T-Box 없음")

    from collections import defaultdict

    from rdflib import OWL, RDF, RDFS

    tbox = Graph()
    tbox.parse(str(TBOX), format="turtle")
    claims: dict[tuple[str, str], set[str]] = defaultdict(set)
    for prop in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(prop).startswith(NS):
            continue
        cols = {str(o).strip().upper() for o in tbox.objects(prop, DCTERMS.source)
                if str(o).strip()}
        doms = {str(d) for d in tbox.objects(prop, RDFS.domain)}
        for dom in doms:
            for col in cols:
                claims[(dom, col)].add(str(prop)[len(NS):])

    duplicated = {k: sorted(v) for k, v in claims.items() if len(v) > 1}
    assert not duplicated, (
        f"중복 dcterms:source 주장 {len(duplicated)}건 — 해당 컬럼이 폴백으로 "
        f"떨어져 FK 리터럴을 잃는다: {list(duplicated.items())[:3]}"
    )


def test_manual_additions_carries_the_dps():
    """패치가 담당하는 DP 가 tbox_manual_additions.ttl 에 있는가.

    T-Box 는 gitignore 이고 S2 가 덮어쓰므로, 여기에 없으면 다음 파이프라인 실행에서
    조용히 사라진다 (step_30 이 이 파일을 병합한다).

    S2 가 스스로 만드는 3개(``*ProductId``)는 여기 있으면 **오히려 해롭다** —
    dcterms:source 충돌로 양쪽이 폐기된다 (tests/test_patch_dp_source_conflict).
    """
    patch = pathlib.Path("rules/domain/tbox_manual_additions.ttl")
    if not patch.exists():
        pytest.skip("수동 추가분 파일 없음")

    text = patch.read_text(encoding="utf-8")
    for dp in _COMPOSITE_CLASSES.values():
        assert f"steel:{dp} a owl:DatatypeProperty" in text, (
            f"{dp} 가 수동 추가분에 없다 — S2 재실행 시 소실된다"
        )
    for dp in _S2_PROVIDED_CLASSES.values():
        assert f"steel:{dp} a owl:DatatypeProperty" not in text, (
            f"{dp} 는 S2 가 만든다 — 패치에 두면 충돌로 양쪽이 폐기된다"
        )
