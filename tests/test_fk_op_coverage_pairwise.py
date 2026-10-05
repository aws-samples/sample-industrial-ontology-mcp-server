"""FK-OP 커버리지 게이트가 ``(source, target)`` 쌍으로 판정하는지 고정.

## 왜

이 게이트는 "CSV FK 컬럼마다 그것을 표현할 ObjectProperty 가 있는가" 를 묻는다.
그런데 판정이 **range 만** 보고 있었다:

    has_op = expected_op in tbox_ops or any(
        (d.get("range") or "").lower() == target_class.lower()
        for d in op_details.values()          # ← domain 무시, 전체 OP 스캔
    )

그래서 어떤 클래스에서든 그 타겟을 가리키는 OP 가 **하나만** 있으면 그 타겟을
참조하는 **모든 테이블의 FK** 가 커버로 셌다. 실측 (2026-08-12 배포 T-Box):
``ItemMaster`` 를 range 로 갖는 OP 를 9개 → 1개로 줄여도
``coverage=100.0% / passed=True`` 였다 — ``Item_Code`` FK 를 쓰는 테이블이 5개인데
OP 1개로 전부 커버가 됐다.

**게이트가 깨진 축에 건강을 보고하면 게이트가 없는 것보다 나쁘다** — 운영자가
문제를 다른 곳에서 찾는다.

``owl:Thing`` domain 도 커버로 세지 않는다. universal 이라 "어떤 source 도 허용"
이지만 A-Box 생성기가 그런 OP 를 후보로 쓰지 못하고(``load_object_properties`` 가
domain 을 도메인 NS 로 필터해 ``None``), 실측상 그런 OP 62개는 값이 0건이었다.
"""
from __future__ import annotations

import json

import pytest
from rdflib import Graph

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.validation_support.checks.referential import check_fk_op_coverage

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)


@pytest.fixture
def fk_env(tmp_path):
    """``Item_Code`` FK 를 쓰는 테이블 2개 + 타겟 테이블 1개."""
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "fk_patterns.json").write_text(
        json.dumps({"patterns": {"itemcode": "ItemMaster"}, "suffix_rules": []}),
        encoding="utf-8",
    )
    csvs = tmp_path / "rawdata"
    csvs.mkdir()
    for table in ("Order_Head", "Shipment"):
        (csvs / f"{table}.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    (csvs / "Item_Master.csv").write_text("Item_Code\nI1\n", encoding="utf-8")
    return {"rules_dir": str(rules), "source_rawdata_dir": str(csvs)}


def _tbox(body: str) -> Graph:
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    return g


_CLASSES = (
    f"{NS_PREFIX}:OrderHead a owl:Class .\n"
    f"{NS_PREFIX}:Shipment a owl:Class .\n"
    f"{NS_PREFIX}:ItemMaster a owl:Class .\n"
)


def _op(name: str, dom: str, rng: str) -> str:
    return (
        f"{NS_PREFIX}:{name} a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:{dom} ; rdfs:range {NS_PREFIX}:{rng} .\n"
    )


# ── THE REGRESSION: 한 OP 로 여러 테이블을 커버하면 안 된다 ───────────


def test_one_op_does_not_cover_every_table_referencing_the_target(fk_env):
    """핵심 회귀: ``OrderHead→ItemMaster`` OP 하나가 ``Shipment`` FK 까지 커버할 수 없다."""
    tbox = _tbox(_CLASSES + _op("orderHasItem", "OrderHead", "ItemMaster"))
    res = check_fk_op_coverage(tbox, **fk_env)
    missing = res["missing_ops"]
    tables = {m["table"] for m in missing}
    assert "Shipment" in tables, (
        f"Shipment 의 FK 를 다른 테이블의 OP 로 커버했다: coverage="
        f"{res.get('coverage_pct', res.get('coverage'))}% missing={missing}"
    )
    assert res["passed"] is False


def test_both_tables_covered_when_each_has_its_own_op(fk_env):
    """PRESERVATION: 테이블마다 OP 가 있으면 100% 다."""
    tbox = _tbox(
        _CLASSES
        + _op("orderHasItem", "OrderHead", "ItemMaster")
        + _op("shipmentHasItem", "Shipment", "ItemMaster")
    )
    res = check_fk_op_coverage(tbox, **fk_env)
    assert res["missing_ops"] == [], f"정상인데 미커버로 셌다: {res['missing_ops']}"
    assert res["passed"] is True
    assert res.get("coverage_pct", res.get("coverage")) == 100.0


def test_name_match_path_also_requires_the_pair(fk_env):
    """``has{Target}`` 이름만으로 커버 판정하면 안 된다.

    옛 코드의 첫 조건 ``expected_op in tbox_ops`` 는 이름만 봤다 — 그 이름이
    어딘가 있으면 모든 테이블이 통과했다.
    """
    # hasItemMaster 가 있지만 domain 이 Shipment 뿐 → OrderHead 는 미커버여야 한다.
    tbox = _tbox(_CLASSES + _op("hasItemMaster", "Shipment", "ItemMaster"))
    res = check_fk_op_coverage(tbox, **fk_env)
    tables = {m["table"] for m in res["missing_ops"]}
    assert "Order_Head" in tables, (
        f"이름만 보고 OrderHead 를 커버로 셌다: missing={res['missing_ops']}"
    )


# ── owl:Thing domain 은 커버가 아니다 ─────────────────────────────────


def test_owl_thing_domain_does_not_count_as_coverage(fk_env):
    """``owl:Thing`` domain OP 는 A-Box 가 쓰지 못하므로 커버로 세지 않는다.

    실측: 그런 OP 62개는 전부 값 0건이었고, 커버로 세면 뮤테이션(range OP 9→1)이
    통과해 게이트가 무력해진다.
    """
    tbox = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:hasItemMaster a owl:ObjectProperty ; "
          f"rdfs:domain owl:Thing ; rdfs:range {NS_PREFIX}:ItemMaster .\n"
    )
    res = check_fk_op_coverage(tbox, **fk_env)
    assert res["passed"] is False, "owl:Thing domain 을 커버로 셌다"
    assert len(res["missing_ops"]) == 2, (
        f"두 테이블 모두 미커버여야 한다: {res['missing_ops']}"
    )


def test_undeclared_domain_does_not_count_as_coverage(fk_env):
    """``rdfs:domain`` 이 없으면 판정 근거가 없다 — 커버 아님."""
    tbox = _tbox(
        _CLASSES
        + f"{NS_PREFIX}:hasItemMaster a owl:ObjectProperty ; "
          f"rdfs:range {NS_PREFIX}:ItemMaster .\n"
    )
    res = check_fk_op_coverage(tbox, **fk_env)
    assert res["passed"] is False, "domain 미선언을 커버로 셌다"


# ── PRESERVATION: 오탐을 만들지 않는다 ───────────────────────────────


def test_self_referencing_pk_is_not_counted_as_a_gap(fk_env):
    """자기 테이블의 PK 는 FK 가 아니다 — ``Item_Master.Item_Code`` 는 제외한다.

    self-loop OP 는 ``step_15b`` 가 오히려 제거하므로, 이것을 커버 미달로 세면
    게이트가 영구히 도달 불가능한 목표를 요구한다.
    """
    tbox = _tbox(
        _CLASSES
        + _op("orderHasItem", "OrderHead", "ItemMaster")
        + _op("shipmentHasItem", "Shipment", "ItemMaster")
    )
    res = check_fk_op_coverage(tbox, **fk_env)
    tables = {m["table"] for m in res["missing_ops"]}
    assert "Item_Master" not in tables, (
        "자기 PK(Item_Master.Item_Code)를 FK 갭으로 셌다"
    )
    assert res["passed"] is True


def test_missing_rules_file_skips_cleanly(tmp_path):
    """``fk_patterns.json`` 이 없으면 건너뛴다 (도메인-중립 기본값)."""
    res = check_fk_op_coverage(
        _tbox(_CLASSES), rules_dir=str(tmp_path), source_rawdata_dir=str(tmp_path),
    )
    assert res["passed"] is True
    assert res["missing_ops"] == []


def test_current_pipeline_output_reaches_full_coverage(s3_output_ttl):
    """실측 고정: 현재 코드의 S3 산출물은 FK-OP 커버리지 100% 여야 한다.

    ``step_10`` read-only 화로 ``owl:Thing`` domain 이 0 이 되면서 모든 FK 쌍이
    구체 domain OP 로 판정 가능해졌다. 이 수치가 내려가면 어떤 CSV FK 가 T-Box 에
    표현되지 못한다는 뜻이다.
    """
    if s3_output_ttl is None:
        pytest.skip("S2 초안 픽스처 없음")
    g = Graph()
    g.parse(data=s3_output_ttl, format="turtle")
    res = check_fk_op_coverage(g)
    assert res["missing_ops"] == [], (
        f"FK 를 표현하지 못하는 쌍이 있다: {res['missing_ops'][:5]}"
    )
