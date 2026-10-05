"""Interlinking 게이트가 owl:imports 를 **개수가 아니라 해석 여부** 로 채점하는지 고정.

## 배경 (2026-08-17 실측)

``linked_data.py`` 는 ``if imports_list: score += 30`` — imports 를 **선언만** 하면
30점을 줬다. 해석 검증이 없었다.

그 결과 배포 T-Box 가 이 상태로 만점 근처를 받았다:

  owl:imports 3건 선언 (IOF Core / Maintenance / SupplyChain)
  그 네임스페이스 아래 IRI 21개 참조
  **그중 공리를 가진 것 0개**

원인은 네임스페이스 드리프트다 — 번들 ``Core.rdf`` 는 클래스를
``/ontology/construct/`` 로 선언하는데 T-Box 는 ``/ontology/core/Core/`` 를 참조한다.

## 왜 이것이 중요한가

imports 가 로드되지 않으면 상위 클래스의 disjointness 가 없다. 그래서
"HermiT consistent / unsatisfiable 0" 은 **건전성이 아니라 공리가 없어서 얻은 침묵**
이었다. 네임스페이스를 맞춰 병합하자 ``SupplierMaster`` 가 unsatisfiable 로 드러났다
(``Organization`` ⊥ ``MaterialArtifact``).

즉 이 게이트의 침묵이 step_12 의 IOF 부모 오정렬 9건을 덮고 있었다.
"""
from __future__ import annotations

import pytest
from rdflib import RDFS, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.validation_support.checks.linked_data import check_interlinking

CORE = "https://spec.industrialontologies.org/ontology/core/Core/"


def _tbox(*, imports: bool, resolved: bool) -> Graph:
    """imports 선언 / 그 IRI 의 공리 보유를 독립적으로 켜는 픽스처."""
    ttl = (
        f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        f"@prefix iof: <{CORE}> .\n"
        f"<{DOMAIN_NS}> a owl:Ontology"
        + (f" ;\n    owl:imports <{CORE}>" if imports else "")
        + " .\n"
        # 외부 참조를 충분히 둬서 ratio/subsumption 점수는 상수로 유지한다.
        f"{NS_PREFIX}:A a owl:Class ; rdfs:subClassOf iof:MaterialArtifact .\n"
        f"{NS_PREFIX}:B a owl:Class ; rdfs:subClassOf iof:PlannedProcess .\n"
    )
    if resolved:
        # imports 가 실제로 해석된 상태 = 그 IRI 들이 그래프 안에서 subject 로 등장.
        ttl += (
            "iof:MaterialArtifact a owl:Class .\n"
            "iof:PlannedProcess a owl:Class .\n"
        )
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


class TestImportsScoredByResolution:
    def test_inert_imports_lose_the_import_points(self):
        """선언만 있고 공리가 0 이면 30점을 주지 않는다."""
        r = check_interlinking(_tbox(imports=True, resolved=False))
        assert r["imports_inert"] is True
        assert r["import_count"] == 1
        assert r["imported_ns_resolved_iris"] == 0
        assert r["import_resolution_pct"] == 0.0
        assert any("공리를 가진 것이 0개" in x for x in r["recommendations"]), (
            "inert 상태를 사용자에게 보고하지 않았다"
        )

    def test_resolved_imports_get_the_points(self):
        """**정당한 입력 보존** (NEGATIVE 방향): 해석되면 감점하지 않는다.

        게이트를 엄격하게 만들 때 정상 상태까지 깎으면 아무도 신뢰하지 않는다.
        """
        inert = check_interlinking(_tbox(imports=True, resolved=False))
        ok = check_interlinking(_tbox(imports=True, resolved=True))
        assert ok["imports_inert"] is False
        assert ok["import_resolution_pct"] == 100.0
        assert ok["score"] == inert["score"] + 30, (
            f"해석된 imports 가 30점을 못 받았다: {ok['score']} vs {inert['score']}"
        )
        assert not any("공리를 가진 것이 0개" in x for x in ok["recommendations"])

    def test_no_imports_is_reported_separately_from_inert(self):
        """imports 자체가 없는 것과 inert 는 다른 진단이다."""
        r = check_interlinking(_tbox(imports=False, resolved=False))
        assert r["import_count"] == 0
        assert r["imports_inert"] is False, "선언이 없으면 inert 가 아니다"
        assert any("owl:imports 를 통해" in x for x in r["recommendations"])

    def test_meta_vocabulary_does_not_pollute_the_denominator(self):
        """분모는 **imports 네임스페이스 아래** 로만 한정한다.

        ``owl:Class``/``rdfs:label`` 같은 메타 용어는 T-Box 안에 공리가 없는 것이
        정상이다. 외부 IRI 전체를 세면 (실측 40개) 정상 그래프도 영구히 inert 로
        보고돼 게이트가 무의미해진다.
        """
        r = check_interlinking(_tbox(imports=True, resolved=True))
        # iof:MaterialArtifact + iof:PlannedProcess 2개만 세야 한다.
        assert r["imported_ns_referenced_iris"] == 2, (
            f"메타 용어가 분모에 섞였다: {r['imported_ns_referenced_iris']}"
        )

    def test_unreferenced_imports_flagged_distinctly(self):
        """선언했는데 그 네임스페이스 IRI 를 아무것도 안 쓰면 별도 경고."""
        g = Graph()
        g.parse(data=(
            f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            f"<{DOMAIN_NS}> a owl:Ontology ; owl:imports <{CORE}> .\n"
            f"{NS_PREFIX}:A a owl:Class .\n"
        ), format="turtle")
        r = check_interlinking(g)
        assert r["imports_unreferenced"] is True
        assert r["imports_inert"] is False, "참조가 0 이면 inert 판정 대상이 아니다"
        assert any("하나도 참조하지 않습니다" in x for x in r["recommendations"])


class TestDeployedTBoxImportsAreInert:
    """**산출물 기반**: 배포 T-Box 의 실제 상태를 기록으로 고정한다.

    이 테스트는 "고쳐졌다" 를 주장하지 않는다 — imports 를 실제로 로드되게 만드는
    작업(네임스페이스 정규화 + BFO 2020 확보)은 아직 안 됐다. 대신 **게이트가 그
    상태를 정직하게 보고하는지** 를 고정한다. 나중에 정규화가 끝나면 이 테스트가
    실패하며 그 사실을 알려준다.
    """

    def test_gate_reports_the_drift_instead_of_scoring_full_marks(self):
        import os

        import config as _cfg
        if not os.path.exists(_cfg.TBOX_PATH):
            pytest.skip("배포 T-Box 없음")
        g = Graph()
        g.parse(_cfg.TBOX_PATH, format="turtle")
        r = check_interlinking(g)
        if not r["imports_inert"]:
            pytest.skip(
                "imports 가 해석된다 — 네임스페이스 정규화가 완료된 상태. "
                "이 테스트의 전제(드리프트)가 사라졌으므로 갱신 대상."
            )
        assert r["import_count"] >= 1
        assert r["imported_ns_referenced_iris"] > 0
        assert r["imported_ns_resolved_iris"] == 0
        assert r["score"] < 100.0, (
            "imports 가 한 트리플도 로드하지 않는데 만점이다 — 개수만 세고 있다"
        )


def test_mutation_count_only_scoring_would_fail_these(monkeypatch):
    """옛 채점식(``if imports_list: score += 30``)이 되살아나면 잡히는지 확인.

    inert 그래프와 해석된 그래프의 점수가 **같아지면** 게이트는 해석 여부를 보지
    않는 것이다.
    """
    inert = check_interlinking(_tbox(imports=True, resolved=False))
    ok = check_interlinking(_tbox(imports=True, resolved=True))
    assert inert["score"] != ok["score"], (
        "inert 와 해석된 imports 가 동점이다 — 개수만 세는 채점으로 회귀했다"
    )


def _mk_uri(local_name: str) -> URIRef:
    return URIRef(CORE + local_name)


def test_literal_axioms_count_as_resolution():
    """공리 판정은 ``a owl:Class`` 로 한정하지 않는다 — subject 로 등장하면 충분.

    로컬 사본을 병합하는 방식은 여러 가지다 (label 만 있는 stub, 완전한 공리 등).
    판정을 특정 술어에 묶으면 정당한 병합을 inert 로 오판한다.
    """
    g = _tbox(imports=True, resolved=False)
    g.add((_mk_uri("MaterialArtifact"), RDFS.comment, Literal("merged")))
    r = check_interlinking(g)
    assert r["imported_ns_resolved_iris"] == 1
    assert r["imports_inert"] is False
