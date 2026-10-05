"""``owl:imports`` 가 **해석 가능한** 온톨로지 IRI 만 가리키는지 고정.

## 왜 이 테스트가 필요한가

IOF 네임스페이스를 정본(``…/ontology/construct/``) 하나로 통일할 때, ``step_07`` 이
그 상수를 ``owl:imports`` 에도 쓰고 있던 것을 놓쳤다. 용어 네임스페이스는 어떤
파일의 ``owl:Ontology`` 선언도 아니므로 owlready2 가 네트워크로 나가고, IOF 서버는
HTML 리다이렉트(307)를 준다 → **HermiT 이 온톨로지를 열지 못해 검증이 통째로
사라졌다**:

    validate_owl_consistency → {"success": false,
      "error": "Cannot download 'https://…/ontology/construct/'!"}

실측 (2026-08-18): 그 한 줄을 제거하자 즉시 ``consistent=true`` 로 돌아오고 **unsat
4건** (``AlarmEvents`` / ``TagMaster`` / ``EquipmentMaster`` / ``EquipmentStatus``) 이
드러났다 — 검증기가 죽어 있던 동안 숨어 있던 실제 결함이다.

**교훈**: 검증기가 실패하면 "결함 없음" 이 아니라 "검증 없음" 이다. 그 구분을
테스트로 고정한다.
"""
from __future__ import annotations

import os

import pytest
from rdflib import OWL, RDF, Graph, URIRef

from tools.quality_steps import step_07b_imports_resolvable as step
from tools.quality_steps._base import StepContext

S = "http://example.com/steel-ontology#"
_TERM_NS = "https://spec.industrialontologies.org/ontology/construct/"
_ONT_CORE = "https://spec.industrialontologies.org/ontology/core/Core/"


def _reference_dir_available() -> bool:
    import config

    base = getattr(config, "SOURCE_REFERENCE_DIR", None) or os.path.join(
        "data", "source", "reference",
    )
    return os.path.isdir(base) and any(
        f.lower().endswith((".rdf", ".owl", ".ttl")) for f in os.listdir(base)
    )


class TestImportsSeparateFromTermNamespace:
    def test_ontology_iris_are_not_the_term_namespace(self):
        """상수 분리가 유지된다 — 둘이 같아지면 회귀가 되살아난다."""
        from domain.namespaces import IOF_CORE, IOF_ONTOLOGY_IRIS

        assert IOF_CORE == _TERM_NS
        for iri in IOF_ONTOLOGY_IRIS:
            assert iri != IOF_CORE, (
                "owl:imports 대상이 용어 네임스페이스와 같아졌다 — 추론기가 "
                "네트워크로 나가 HermiT 검증이 사라진다"
            )

    def test_ontology_iris_match_the_shipped_files(self):
        """imports 값이 동봉 파일의 ``owl:Ontology`` 선언과 일치한다.

        값을 하드코딩으로만 고정하면 파일과 어긋나도 통과한다 — 파일에서 읽어
        대조한다.
        """
        import config
        from domain.namespaces import IOF_ONTOLOGY_IRIS

        if not _reference_dir_available():
            pytest.skip("reference 파일 없음")
        base = config.SOURCE_REFERENCE_DIR
        shipped: set[str] = set()
        for fname in os.listdir(base):
            if not fname.lower().endswith((".rdf", ".owl", ".ttl")):
                continue
            g = Graph()
            try:
                g.parse(os.path.join(base, fname))
            except Exception:  # noqa: BLE001
                continue
            shipped |= {str(s) for s in g.subjects(RDF.type, OWL.Ontology)}
        for iri in IOF_ONTOLOGY_IRIS:
            assert iri in shipped, (
                f"{iri} 가 동봉 파일의 owl:Ontology 집합에 없다 — onto_path 로 "
                f"해석되지 않아 다운로드로 빠진다. shipped={sorted(shipped)}"
            )


class TestStep07bCleanup:
    def test_removes_term_namespace_import(self):
        if not _reference_dir_available():
            pytest.skip("reference 파일 없음")
        g = Graph()
        ont = URIRef("http://example.com/steel-ontology")
        g.add((ont, RDF.type, OWL.Ontology))
        g.add((ont, OWL.imports, URIRef(_TERM_NS)))

        result = step.apply(g, StepContext(domain_ns=S))

        assert (ont, OWL.imports, URIRef(_TERM_NS)) not in g
        assert result.stats["imports_unresolvable_removed"] >= 1

    def test_preserves_resolvable_ontology_iri(self):
        """NEGATIVE: 동봉 파일에 대응하는 정당한 imports 는 보존한다.

        "카운터 ≥ 1" 만 주장하면 전부 지우는 구현도 통과한다.
        """
        if not _reference_dir_available():
            pytest.skip("reference 파일 없음")
        g = Graph()
        ont = URIRef("http://example.com/steel-ontology")
        g.add((ont, RDF.type, OWL.Ontology))
        g.add((ont, OWL.imports, URIRef(_ONT_CORE)))
        g.add((ont, OWL.imports, URIRef(_TERM_NS)))

        step.apply(g, StepContext(domain_ns=S))

        assert (ont, OWL.imports, URIRef(_ONT_CORE)) in g
        assert (ont, OWL.imports, URIRef(_TERM_NS)) not in g

    def test_preserves_unknown_private_imports(self):
        """NEGATIVE: 로컬 사본이 없는 사설 imports 는 판정 대상이 아니다."""
        if not _reference_dir_available():
            pytest.skip("reference 파일 없음")
        private = URIRef("http://internal.example.org/ontology/plant/")
        g = Graph()
        ont = URIRef("http://example.com/steel-ontology")
        g.add((ont, RDF.type, OWL.Ontology))
        g.add((ont, OWL.imports, private))

        step.apply(g, StepContext(domain_ns=S))

        assert (ont, OWL.imports, private) in g

    def test_is_idempotent(self):
        if not _reference_dir_available():
            pytest.skip("reference 파일 없음")
        g = Graph()
        ont = URIRef("http://example.com/steel-ontology")
        g.add((ont, RDF.type, OWL.Ontology))
        g.add((ont, OWL.imports, URIRef(_TERM_NS)))
        step.apply(g, StepContext(domain_ns=S))
        snapshot = set(g)
        second = step.apply(g, StepContext(domain_ns=S))
        assert set(g) == snapshot
        assert second.stats["imports_unresolvable_removed"] == 0

    def test_registered_in_the_pipeline(self):
        from tools.quality_steps import _MAIN_PRE_STEP9

        assert step.apply in _MAIN_PRE_STEP9


class TestDeployedTboxImportsResolve:
    def test_deployed_tbox_has_no_term_namespace_import(self):
        """실측 회귀: 배포 T-Box 가 용어 네임스페이스를 import 하지 않는다."""
        import config

        if not os.path.exists(config.TBOX_PATH):
            pytest.skip("배포 T-Box 없음")
        g = Graph()
        g.parse(config.TBOX_PATH, format="turtle")
        imports = {str(o) for o in g.objects(None, OWL.imports)}
        assert _TERM_NS not in imports, (
            "용어 네임스페이스가 owl:imports 에 남아 있다 — HermiT 이 "
            "'Cannot download' 로 검증을 포기한다"
        )
        assert imports, "imports 가 통째로 비었다 — IOF 공리를 못 얻는다"


class TestLocalMirrorRegistration:
    def test_reference_dir_is_an_onto_path_candidate(self):
        """동봉 reference 디렉토리가 ``onto_path`` 후보에 있다.

        ``data/external`` 은 fresh checkout 에 없어 ``onto_path`` 가 비어 있었고,
        모든 imports 가 네트워크로 나갔다 (실측: ``onto_path == []``). 동봉 파일이
        바로 그 imports 대상이므로 등록하면 오프라인에서도 해석된다.
        """
        import inspect

        from tools import owl_reasoner

        src = inspect.getsource(owl_reasoner._register_local_imports_dir)
        assert '"reference"' in src, (
            "동봉 reference 디렉토리가 onto_path 후보에 없다 — data/external 이 "
            "없는 환경에서 imports 가 전부 다운로드로 빠진다"
        )
