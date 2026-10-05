"""Tests for domain/namespaces.py — pure unit tests."""

from rdflib import Graph

from domain.namespaces import (
    DOMAIN_INST_NS,
    DOMAIN_NS,
    IOF_CORE_NS,
    IOF_MAINT_NS,
    SPARQL_PREFIXES,
    bind_namespaces,
    sanitize_sparql_value,
)
from domain.tbox_utils import _new_graph


class TestSparqlPrefixes:
    def test_contains_domain_prefix(self):
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        assert f"PREFIX {NS_PREFIX}:" in SPARQL_PREFIXES
        assert DOMAIN_NS in SPARQL_PREFIXES

    def test_contains_instance_prefix(self):
        from domain.namespaces import DOMAIN_INST_NS, NS_INST_PREFIX
        assert f"PREFIX {NS_INST_PREFIX}:" in SPARQL_PREFIXES
        assert DOMAIN_INST_NS in SPARQL_PREFIXES

    def test_contains_rdf_prefix(self):
        assert "PREFIX rdf:" in SPARQL_PREFIXES

    def test_contains_rdfs_prefix(self):
        assert "PREFIX rdfs:" in SPARQL_PREFIXES

    def test_contains_owl_prefix(self):
        assert "PREFIX owl:" in SPARQL_PREFIXES

    def test_contains_xsd_prefix(self):
        assert "PREFIX xsd:" in SPARQL_PREFIXES

    def test_contains_iof_core_prefix(self):
        assert "PREFIX iof-core:" in SPARQL_PREFIXES

    def test_contains_iof_maint_prefix(self):
        assert "PREFIX iof-maint:" in SPARQL_PREFIXES


class TestBindNamespaces:
    def test_binds_to_graph(self):
        g = _new_graph()
        bind_namespaces(g)
        ns_dict = dict(g.namespaces())
        assert str(ns_dict.get("steel")) == DOMAIN_NS
        assert str(ns_dict.get("steel-inst")) == DOMAIN_INST_NS

    def test_binds_iof_namespaces(self):
        """IOF 네임스페이스가 그래프에 bind 된다.

        2026-08-18 정정: 예전에는 ``iof-core`` 와 ``iof-maint`` 가 **각각** bind
        되기를 요구했으나, IOF 는 세 모듈을 하나의 네임스페이스
        (``/ontology/construct/``) 에 선언한다 — 동봉 파일 실측. rdflib 은
        IRI→prefix 를 1:1 로만 들고 있어 같은 IRI 에 라벨은 하나만 남는다.

        이것이 기능을 깨지 않는다: TTL 파싱은 **문서 자체의 @prefix** 를 쓰고
        SPARQL 은 ``SPARQL_PREFIXES`` 를 쓴다(둘 다 iof-maint 를 계속 포함).
        graph bind 는 **직렬화 라벨** 결정에만 관여하므로, 정본 하나로 고정하는
        것이 오히려 산출물을 결정적으로 만든다.
        """
        g = _new_graph()
        bind_namespaces(g)
        ns_dict = dict(g.namespaces())
        assert str(ns_dict.get("iof-core")) == str(IOF_CORE_NS)
        # 세 모듈이 같은 IRI 이므로 iof-maint 도 같은 값으로 해석돼야 한다.
        assert str(IOF_MAINT_NS) == str(IOF_CORE_NS)


class TestSanitizeSparqlValue:
    def test_escapes_quotes(self):
        assert sanitize_sparql_value('hello "world"') == 'hello \\"world\\"'

    def test_escapes_backslashes(self):
        assert sanitize_sparql_value("path\\to") == "path\\\\to"

    def test_escapes_newlines(self):
        result = sanitize_sparql_value("line1\nline2\rline3")
        assert "\\n" in result
        assert "\\r" in result
        assert "\n" not in result
        assert "\r" not in result

    def test_leaves_angle_brackets_and_apostrophe_unescaped(self):
        """큰따옴표 리터럴 안의 ``<`` ``>`` ``'`` 는 그대로 둔다.

        rdflib 은 큰따옴표 리터럴의 ``\\'`` 를 거부하고, egress 가드는 ``\\uXXXX`` 를
        거부하므로 둘 중 어느 표기도 만들지 않는다.
        """
        raw = "<script>alert('xss')</script>"
        result = sanitize_sparql_value(raw)
        assert result == raw
        assert "\\u" not in result

    def test_backslash_before_quotes(self):
        # Backslash must be escaped before quotes to avoid double-escaping
        result = sanitize_sparql_value('\\"')
        assert result == '\\\\\\"'

    def test_plain_string_unchanged(self):
        assert sanitize_sparql_value("hello world") == "hello world"

    def test_empty_string(self):
        assert sanitize_sparql_value("") == ""


class TestIOFNamespaceCorrection:
    """IOF 는 Core/Maintenance/SupplyChain 을 **하나의** 네임스페이스에 선언한다.

    실측 (2026-08-18): 동봉된 ``Core.rdf`` / ``Maintenance.rdf`` /
    ``SupplyChain.rdf`` 의 subject 가 전부 ``/ontology/construct/`` 다
    (Core 143 / Maintenance 23 / SupplyChain 127). 모듈별 IRI
    (``/ontology/core/Core/`` 등) 로는 T-Box 가 참조하는 IOF 용어 24개 중
    **3개만** 해소됐고 그 3개도 온톨로지 헤더 IRI였다 — 즉 ``owl:imports`` 3건이
    한 트리플도 로드하지 못하고, HermiT 의 침묵은 "일관됨" 이 아니라 **공리가
    없어서 얻은 침묵** 이었다.

    교정 후: 참조 해소 3/24 → 18/24, IOF 병합 시 7,529 트리플 로드,
    HermiT consistent=true / unsat 0.
    """

    def test_three_iof_constants_share_one_iri(self):
        """세 상수가 같은 IRI 를 가리킨다 (RDF 상 동일 네임스페이스)."""
        from domain.namespaces import IOF_CORE, IOF_MAINT, IOF_SCRO

        assert IOF_CORE == IOF_MAINT == IOF_SCRO
        assert IOF_CORE.endswith("/ontology/construct/"), IOF_CORE

    def test_iri_matches_the_shipped_reference_files(self):
        """상수가 **동봉 파일이 실제로 쓰는** 네임스페이스와 일치한다.

        상수만 바꾸고 파일과 어긋나면 다시 고립 리프가 된다 — 값을 하드코딩으로
        고정하는 대신 파일에서 읽어 대조한다.
        """
        import os

        import pytest
        from rdflib import URIRef

        from domain.namespaces import IOF_CORE

        path = os.path.join("data", "source", "reference", "Core.rdf")
        if not os.path.exists(path):
            pytest.skip("IOF reference 파일 없음")
        g = Graph()
        g.parse(path)
        subjects = [str(s) for s in g.subjects() if isinstance(s, URIRef)]
        hits = sum(1 for s in subjects if s.startswith(IOF_CORE))
        assert hits > 50, (
            f"상수 {IOF_CORE} 로 시작하는 subject 가 {hits}개뿐 — 동봉 파일과 "
            "어긋나 참조가 고립 리프가 된다"
        )

    def test_prefix_labels_are_still_distinct(self):
        """NEGATIVE: prefix 세 개는 유지된다 (SPARQL·프롬프트·설정에 퍼져 있다).

        IRI 만 통일하고 라벨은 의미 구분용으로 남긴다. 라벨까지 합치면 36곳을
        고쳐야 하고 기존 SPARQL 이 깨진다.
        """
        from domain.namespaces import SPARQL_PREFIXES

        for p in ("iof-core:", "iof-maint:", "iof-scro:"):
            assert f"PREFIX {p}" in SPARQL_PREFIXES

    def test_iof_maint_authored_ttl_still_parses(self):
        """``iof-maint:`` 로 저작된 TTL·SPARQL 이 계속 동작한다.

        직렬화 라벨을 정본 하나로 고정하는 변경이 **기존 저작물을 깨지 않는다**는
        것을 고정한다 — 이것이 원래 ``test_binds_iof_namespaces`` 가 지키려던
        기능이다. 파싱은 문서의 ``@prefix`` 를, SPARQL 은 ``SPARQL_PREFIXES`` 를
        쓰므로 graph bind 유무와 무관하다.
        """
        from rdflib import RDFS, URIRef

        from domain.namespaces import IOF_MAINT, SPARQL_PREFIXES, bind_namespaces

        ttl = (
            f"@prefix iof-maint: <{IOF_MAINT}> .\n"
            "@prefix steel: <http://example.com/steel-ontology#> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            "steel:X a owl:Class ; rdfs:subClassOf iof-maint:MaintenanceProcess .\n"
        )
        g = Graph()
        bind_namespaces(g)
        g.parse(data=ttl, format="turtle")
        assert (
            URIRef("http://example.com/steel-ontology#X"),
            RDFS.subClassOf,
            URIRef(IOF_MAINT + "MaintenanceProcess"),
        ) in g
        assert "PREFIX iof-maint:" in SPARQL_PREFIXES

    def test_serialization_label_is_deterministic(self):
        """같은 IRI 에 prefix 셋이 붙어도 직렬화 라벨이 결정적이다.

        rdflib 는 ``replace=True`` 순차 bind 에서 **마지막** 것만 남긴다. 그대로
        두면 ``MaterialArtifact`` 가 ``iof-scro:`` 로 직렬화돼 의미가 오해되고,
        bind 순서(dict 순서)가 바뀌면 산출물 diff 가 흔들린다.
        """
        from rdflib import URIRef

        from domain.namespaces import IOF_CORE, bind_namespaces

        g = Graph()
        bind_namespaces(g)
        qname = g.namespace_manager.normalizeUri(URIRef(IOF_CORE + "MaterialArtifact"))
        assert qname.startswith("iof-core:"), (
            f"직렬화 라벨이 iof-core: 가 아니다: {qname}"
        )


class TestLegacyIOFNamespaceMigration:
    """파일에 문자열로 박힌 **구 IOF IRI** 를 정본으로 마이그레이션한다.

    ``namespaces.py`` 상수만 고치면 **앞으로 만들 IRI** 에만 적용된다 — 실측
    (2026-08-18): 상수 교정 + S3 재실행 후에도 배포 T-Box 의 IOF 참조 해소율이
    12% → 14% 로 거의 그대로였다. 파일에 ``/ontology/core/Core/…`` 문자열이 47건
    박혀 있었기 때문이다.

    이번 세션에 반복 확인한 패턴이다: 생성 지점 가드만으로는 이미 파일에 있는
    것이 사라지지 않는다 → 정리(마이그레이션) 스텝이 함께 필요하다.
    """

    def test_migrates_legacy_module_iris(self):
        from rdflib import RDFS, URIRef

        from domain.namespaces import IOF_CORE
        from tools.quality_steps import step_02b_iof_namespace_migrate as step
        from tools.quality_steps._base import StepContext

        S = "http://example.com/steel-ontology#"
        OLD = "https://spec.industrialontologies.org/ontology/core/Core/"
        g = Graph()
        g.add((URIRef(S + "X"), RDFS.subClassOf, URIRef(OLD + "MaterialArtifact")))
        g.add((URIRef(S + "Y"), RDFS.subClassOf,
               URIRef("https://spec.industrialontologies.org/ontology/"
                      "maintenance/Maintenance/MaintenanceProcess")))

        result = step.apply(g, StepContext(domain_ns=S))

        assert (URIRef(S + "X"), RDFS.subClassOf,
                URIRef(IOF_CORE + "MaterialArtifact")) in g
        assert (URIRef(S + "Y"), RDFS.subClassOf,
                URIRef(IOF_CORE + "MaintenanceProcess")) in g
        assert result.stats["iof_legacy_iris_migrated"] >= 2

    def test_is_idempotent(self):
        from rdflib import RDFS, URIRef

        from domain.namespaces import IOF_CORE
        from tools.quality_steps import step_02b_iof_namespace_migrate as step
        from tools.quality_steps._base import StepContext

        S = "http://example.com/steel-ontology#"
        g = Graph()
        g.add((URIRef(S + "X"), RDFS.subClassOf, URIRef(IOF_CORE + "MaterialArtifact")))
        first = step.apply(g, StepContext(domain_ns=S)).stats
        snapshot = set(g)
        second = step.apply(g, StepContext(domain_ns=S)).stats
        assert first["iof_legacy_iris_migrated"] == 0
        assert second["iof_legacy_iris_migrated"] == 0
        assert set(g) == snapshot

    def test_leaves_non_iof_iris_alone(self):
        """NEGATIVE: IOF 밖 IRI 는 건드리지 않는다."""
        from rdflib import RDFS, URIRef

        from tools.quality_steps import step_02b_iof_namespace_migrate as step
        from tools.quality_steps._base import StepContext

        S = "http://example.com/steel-ontology#"
        bfo = URIRef("http://purl.obolibrary.org/obo/BFO_0000004")
        g = Graph()
        g.add((URIRef(S + "X"), RDFS.subClassOf, bfo))
        step.apply(g, StepContext(domain_ns=S))
        assert (URIRef(S + "X"), RDFS.subClassOf, bfo) in g

    def test_deployed_tbox_has_no_legacy_term_iris(self):
        """실측 회귀: 배포 T-Box 의 **용어** IRI 에 구 네임스페이스가 없다.

        ``owl:imports`` 대상인 **온톨로지 헤더** IRI 는 예외다 — 동봉 파일의
        ``owl:Ontology`` 선언이 정확히 그 세 값이다 (실측:
        ``Core.rdf`` → ``/ontology/core/Core/`` 등). 즉 IOF 는 **용어는 하나의
        construct 네임스페이스, 온톨로지 IRI 는 모듈별** 로 둔다. imports 를
        정본으로 바꾸면 파일과 대응이 끊긴다.
        """
        import os
        import re

        import pytest
        from rdflib import OWL

        from config import TBOX_PATH

        if not os.path.exists(TBOX_PATH):
            pytest.skip("배포 T-Box 없음")
        g = Graph()
        g.parse(TBOX_PATH, format="turtle")
        imports = {str(o) for o in g.objects(None, OWL.imports)}
        with open(TBOX_PATH, encoding="utf-8") as fh:
            body = fh.read()
        # 구 네임스페이스로 시작하고 **local name 이 있는** IRI = 용어 IRI
        bad = set()
        for m in re.finditer(
            r"https://spec\.industrialontologies\.org/ontology/"
            r"(?:core/Core|maintenance/Maintenance|supplychain/SupplyChain)/"
            r"([A-Za-z][\w-]*)",
            body,
        ):
            if m.group(0) not in imports:
                bad.add(m.group(0))
        assert bad == set(), (
            f"구 네임스페이스 용어 IRI 가 남아 있다 (해소되지 않는 고립 리프): "
            f"{sorted(bad)[:5]}"
        )

    def test_ontology_header_imports_match_shipped_files(self):
        """``owl:imports`` 가 동봉 파일의 ``owl:Ontology`` IRI 와 일치한다.

        용어 IRI 를 정본으로 옮기면서 imports 까지 바꾸면 **파일과 대응이 끊긴다**.
        이 방향을 주장하지 않으면 마이그레이션이 과하게 적용된다.
        """
        import os

        import pytest
        from rdflib import OWL, RDF

        from config import TBOX_PATH

        base = os.path.join("data", "source", "reference")
        if not (os.path.exists(TBOX_PATH) and os.path.isdir(base)):
            pytest.skip("T-Box 또는 reference 파일 없음")
        shipped = set()
        for fname in ("Core.rdf", "Maintenance.rdf", "SupplyChain.rdf"):
            path = os.path.join(base, fname)
            if not os.path.exists(path):
                continue
            ref = Graph()
            ref.parse(path)
            shipped |= {str(s) for s in ref.subjects(RDF.type, OWL.Ontology)}
        g = Graph()
        g.parse(TBOX_PATH, format="turtle")
        imports = {str(o) for o in g.objects(None, OWL.imports)}
        assert shipped & imports, (
            f"imports 가 동봉 파일 온톨로지 IRI 와 겹치지 않는다: "
            f"imports={sorted(imports)[:3]} shipped={sorted(shipped)}"
        )

    def test_registered_in_the_pipeline(self):
        from tools.quality_steps import (
            _MAIN_PRE_STEP9,
            step_02b_iof_namespace_migrate,
        )

        assert step_02b_iof_namespace_migrate.apply in _MAIN_PRE_STEP9
