"""owlready2 공통 로더(``tools.owl_reasoner._ttl_to_owlready``)의 프로세스 경계 회귀 테스트.

로컬 import 사본이 선언한 하위 import 는 로컬에서 해석하거나 빈 온톨로지로 채우고,
입력 TTL 의 python_module annotation 은 모듈 import 로 이어지지 않는다. rdflib 그래프에는
없고 RDF/XML 직렬화본에만 나타나는 import 와 annotation 도 owlready2 에 넘기기 전에 거부한다.
네트워크 요청은 ``urllib.request.urlopen`` 을, 모듈 import 는 ``importlib.__import__`` 를
기록한 뒤 거부해 센다. owlready2 는 두 함수를 모듈 속성으로 찾아 호출한다.
"""

import importlib
import json
import urllib.request
from types import SimpleNamespace

import pytest
from rdflib import URIRef

_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_OWL_NS = "http://www.w3.org/2002/07/owl#"
_OWLREADY_NS = "http://www.lesfleursdunormal.fr/static/_downloads/owlready_ontology.owl#"
_PYTHON_MODULE = _OWLREADY_NS + "python_module"
_PROBE_MODULE = "oa_owl_loader_probe_module"
# 같은 프로세스의 다른 로더 테스트와 IRI 가 겹치지 않게 전용 이름공간을 쓴다.
_BASE = "http://example.org/owl-loader-egress"
_LOCAL_IRI = f"{_BASE}/Local"
_LOCAL_THING = f"{_BASE}/Local#LocalThing"


@pytest.fixture
def loader(tmp_path, monkeypatch):
    """import 디렉터리를 tmp 로 바꾸고 HTTP 요청과 모듈 import 를 기록 후 거부한다."""
    import owlready2

    from tools import owl_reasoner

    local_dir = tmp_path / "imports"
    local_dir.mkdir()
    requests: list[str] = []
    modules: list[str] = []

    def _record_request(url, *args, **kwargs):
        requests.append(str(getattr(url, "full_url", url)))
        raise OSError("테스트는 네트워크 요청을 허용하지 않는다")

    def _record_import(name, *args, **kwargs):
        modules.append(name)
        raise ImportError(f"테스트는 모듈 import 를 허용하지 않는다: {name}")

    monkeypatch.setattr(urllib.request, "urlopen", _record_request)
    monkeypatch.setattr(importlib, "__import__", _record_import)
    saved = list(owlready2.onto_path)
    owlready2.onto_path[:] = [str(local_dir)]
    owl_reasoner._cleanup_world()
    yield SimpleNamespace(
        reasoner=owl_reasoner, dir=local_dir, requests=requests, modules=modules,
    )
    owl_reasoner._cleanup_world()
    owlready2.onto_path[:] = saved


def _ttl_importing(iri: str) -> str:
    return (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"<{_BASE}/Top> a owl:Ontology ; owl:imports <{iri}> .\n"
        f"<{_BASE}/Top#C> a owl:Class .\n"
    )


def _owlxml(ontology_iri: str, *body: str) -> str:
    return (
        '<?xml version="1.0"?>\n'
        f'<Ontology xmlns="{_OWL_NS}" ontologyIRI="{ontology_iri}">\n'
        + "".join(f"  {line}\n" for line in body)
        + "</Ontology>\n"
    )


def _rdfxml(ontology_iri: str, *body: str) -> str:
    return (
        '<?xml version="1.0"?>\n'
        f'<rdf:RDF xmlns:rdf="{_RDF_NS}" xmlns:owl="{_OWL_NS}">\n'
        f'  <owl:Ontology rdf:about="{ontology_iri}">\n'
        + "".join(f"    {line}\n" for line in body)
        + "  </owl:Ontology>\n"
        f'  <owl:Class rdf:about="{_LOCAL_THING}"/>\n'
        "</rdf:RDF>\n"
    )


def _load(env, iri: str):
    """``_ttl_to_owlready`` 로 ``iri`` 를 import 하는 TTL 을 읽고 World 의 클래스 IRI 를 돌려준다."""
    onto, tmp = env.reasoner._ttl_to_owlready(_ttl_importing(iri))
    try:
        return onto, {c.iri for c in env.reasoner.default_world.classes()}
    finally:
        env.reasoner._safe_unlink(tmp)


def test_python_module_iri_matches_owlready2():
    """로더가 지우는 annotation IRI 는 owlready2 가 모듈 import 에 쓰는 IRI 와 같다."""
    import owlready2

    from tools import owl_reasoner

    actual = owlready2.default_world._unabbreviate(owlready2.base.owlready_python_module)
    assert actual == owl_reasoner._OWLREADY_PYTHON_MODULE == _PYTHON_MODULE


def test_owlxml_local_copy_nested_import_is_not_requested(loader):
    """OWL/XML 로컬 사본의 ``<Import>`` 는 요청 없이 빈 온톨로지가 되고 사본 내용은 읽힌다."""
    nested = "http://127.0.0.1:65530/nested-owlxml"
    (loader.dir / "Local.owl").write_text(
        _owlxml(
            _LOCAL_IRI,
            f"<Import>{nested}</Import>",
            f'<Declaration><Class IRI="{_LOCAL_THING}"/></Declaration>',
        ),
        encoding="utf-8",
    )

    _onto, classes = _load(loader, _LOCAL_IRI)

    assert loader.requests == []
    assert _LOCAL_THING in classes
    stub = loader.reasoner.default_world.get_ontology(nested)
    assert stub.loaded
    assert list(stub.classes()) == []


def test_owlxml_nested_import_with_local_copy_is_loaded(loader):
    """OWL/XML 하위 import 에 로컬 사본이 있으면 그 사본을 요청 없이 읽는다."""
    nested_iri = f"{_BASE}/Nested"
    nested_thing = f"{_BASE}/Nested#NestedThing"
    (loader.dir / "Local.owl").write_text(
        _owlxml(_LOCAL_IRI, f"<Import>{nested_iri}</Import>"), encoding="utf-8",
    )
    (loader.dir / "Nested.owl").write_text(
        _owlxml(nested_iri, f'<Declaration><Class IRI="{nested_thing}"/></Declaration>'),
        encoding="utf-8",
    )

    _onto, classes = _load(loader, _LOCAL_IRI)

    assert loader.requests == []
    assert nested_thing in classes


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ("nested-rel", "http://127.0.0.1:65530/onto/nested-rel"),
        ("/abs-path", "http://127.0.0.1:65530/onto/abs-path"),
        ("#frag", "http://127.0.0.1:65530/onto/Rel#frag"),
        ("../up", "http://127.0.0.1:65530/up"),
    ],
)
def test_rdfxml_relative_nested_import_is_not_requested(loader, resource, expected):
    """RDF/XML 상대 IRI import 는 owlready2 가 푸는 IRI 그대로 빈 온톨로지로 채운다.

    owlready2 는 상대 IRI 를 온톨로지 IRI 기준의 자체 규칙으로 풀므로, 파일 URI 를 기준으로
    푸는 파서의 결과로 채우면 owlready2 는 다른 IRI 를 HTTP 로 요청한다.
    """
    onto_iri = "http://127.0.0.1:65530/onto/Rel/"
    (loader.dir / "Rel.rdf").write_text(
        _rdfxml(onto_iri, f'<owl:imports rdf:resource="{resource}"/>'), encoding="utf-8",
    )

    _onto, classes = _load(loader, onto_iri)

    assert loader.requests == []
    assert _LOCAL_THING in classes
    assert loader.reasoner.default_world.get_ontology(expected).loaded


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            f"<{_LOCAL_IRI}> a owl:Ontology ; "
            "owl:imports <http://127.0.0.1:65530/turtle> .\n",
            id="turtle",
        ),
        pytest.param("<html><body>http://127.0.0.1:65530/html</body></html>\n", id="other-xml-root"),
        pytest.param(
            '<?xml version="1.0"?>\n'
            f'<owl:Ontology xmlns:rdf="{_RDF_NS}" xmlns:owl="{_OWL_NS}" rdf:about="{_LOCAL_IRI}">\n'
            '  <owl:imports rdf:resource="http://127.0.0.1:65530/unwrapped"/>\n'
            "</owl:Ontology>\n",
            id="rdfxml-without-rdf-root",
        ),
        pytest.param(
            _owlxml(_LOCAL_IRI, "<Import>http://127.0.0.1:65530/truncated</Import>").replace(
                "</Ontology>\n", "<Declaration>",
            ),
            id="truncated-owlxml",
        ),
        pytest.param("", id="empty"),
    ],
)
def test_unreadable_local_copy_is_rejected_without_request(loader, body):
    """형식을 판별하지 못하거나 owlready2 가 끝까지 읽지 못하는 로컬 사본은 요청 없이 거부한다."""
    (loader.dir / "Local.owl").write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match="로컬 import 파일"):
        loader.reasoner._ttl_to_owlready(_ttl_importing(_LOCAL_IRI))
    result = json.loads(
        loader.reasoner.validate_owl_consistency(ttl_content=_ttl_importing(_LOCAL_IRI)),
    )

    assert result["success"] is False
    assert loader.requests == []


@pytest.mark.parametrize(
    ("filename", "body"),
    [
        pytest.param(
            "Local.owl",
            _owlxml(
                _LOCAL_IRI,
                "<Annotation>"
                f'<AnnotationProperty IRI="{_PYTHON_MODULE}"/><Literal>{_PROBE_MODULE}</Literal>'
                "</Annotation>",
            ),
            id="owlxml",
        ),
        pytest.param(
            "Local.rdf",
            _rdfxml(
                _LOCAL_IRI,
                f'<python_module xmlns="{_OWLREADY_NS}">{_PROBE_MODULE}</python_module>',
            ),
            id="rdfxml",
        ),
    ],
)
def test_local_copy_declaring_python_module_is_rejected(loader, filename, body):
    """python_module annotation 을 선언한 로컬 사본은 읽지 않으므로 모듈 import 가 없다."""
    (loader.dir / filename).write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match="python_module"):
        loader.reasoner._ttl_to_owlready(_ttl_importing(_LOCAL_IRI))

    assert loader.modules == []
    assert loader.requests == []


@pytest.mark.parametrize(
    "value",
    [f'"{_PROBE_MODULE}"', f'"{_PROBE_MODULE}"^^<http://www.w3.org/2001/XMLSchema#string>'],
)
def test_ttl_python_module_annotation_is_not_imported(loader, value):
    """입력 TTL 의 python_module annotation 은 지워져 모듈 import 가 없고, 나머지 내용은 읽힌다."""
    ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"<{_BASE}/T> a owl:Ontology ; <{_PYTHON_MODULE}> {value} .\n"
        f"<{_BASE}/T#C> a owl:Class .\n"
    )

    _onto, tmp = loader.reasoner._ttl_to_owlready(ttl)
    try:
        world = loader.reasoner.default_world
        classes = {c.iri for c in world.classes()}
        annotations = list(world.as_rdflib_graph().triples((None, URIRef(_PYTHON_MODULE), None)))
    finally:
        loader.reasoner._safe_unlink(tmp)

    assert loader.modules == []
    assert loader.requests == []
    assert annotations == []
    assert f"{_BASE}/T#C" in classes


def _datatype_breakout_ttl(injected_xml: str) -> str:
    """RDF/XML 직렬화본에만 ``injected_xml`` 이 나타나는 TTL 을 만든다.

    rdflib XMLSerializer 는 ``rdf:datatype`` 값을 이스케이프하지 않고, ``default`` 스토어의
    Turtle 파서는 UCHAR 로 쓴 ``"`` ``<`` ``>`` 공백을 IRI 에 받아들인다. 그래서 이 datatype
    IRI 는 앞 술어 요소를 닫고 ``injected_xml`` 을 넣은 뒤 같은 술어 요소를 다시 연다.
    주입한 요소는 rdflib 그래프에는 없고 owlready2 가 읽는 파일에만 있다.
    """
    raw = f'http://x/a">x</ex:p>{injected_xml}<ex:p rdf:datatype="http://x/b'
    escaped = "".join(f"\\u{ord(c):04X}" if c in '<>" ' else c for c in raw)
    return (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"@prefix ex: <{_BASE}/> .\n"
        f'<{_BASE}/T> a owl:Ontology ; ex:p "x"^^<{escaped}> .\n'
        f"<{_BASE}/T#C> a owl:Class .\n"
    )


@pytest.fixture
def default_store(monkeypatch):
    """rdflib ``default`` 스토어로 TTL 을 읽게 한다 (``RDFLIB_STORE=default`` 와 같다)."""
    from domain import tbox_utils

    monkeypatch.setattr(tbox_utils, "_RDFLIB_STORE", "default")


@pytest.mark.parametrize(
    ("injected_xml", "expected"),
    [
        pytest.param(
            f'<python_module xmlns="{_OWLREADY_NS}">{_PROBE_MODULE}</python_module>',
            "python_module",
            id="python_module",
        ),
        pytest.param(
            f'<owl:imports xmlns:owl="{_OWL_NS}" rdf:resource="http://127.0.0.1:65530/inj"/>',
            "로컬 사본이 없어",
            id="owl_imports",
        ),
    ],
)
def test_serialization_breakout_is_rejected_without_import_or_request(
    loader, default_store, injected_xml, expected,
):
    """직렬화본에만 나타나는 python_module 과 owl:imports 도 owlready2 에 넘기기 전에 거부한다."""
    from domain.tbox_utils import _new_graph

    ttl = _datatype_breakout_ttl(injected_xml)
    g = _new_graph()
    g.parse(data=ttl, format="turtle")
    assert injected_xml in g.serialize(format="xml")

    with pytest.raises(ValueError, match=expected):
        loader.reasoner._ttl_to_owlready(ttl)
    result = json.loads(loader.reasoner.validate_owl_consistency(ttl_content=ttl))

    assert result["success"] is False
    assert loader.modules == []
    assert loader.requests == []


def test_default_store_typed_literals_and_local_import_still_load(loader, default_store):
    """``default`` 스토어에서도 정상 typed literal 과 로컬 import 는 그대로 읽힌다."""
    (loader.dir / "Local.rdf").write_text(_rdfxml(_LOCAL_IRI), encoding="utf-8")
    ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
        f"@prefix ex: <{_BASE}/> .\n"
        f'<{_BASE}/T> a owl:Ontology ; owl:imports <{_LOCAL_IRI}> ; '
        'ex:issued "2024-01-01T00:00:00"^^xsd:dateTime ; ex:note "a<b & \\"c\\""@ko .\n'
        f"<{_BASE}/T#C> a owl:Class .\n"
    )

    onto, tmp = loader.reasoner._ttl_to_owlready(ttl)
    try:
        classes = {c.iri for c in loader.reasoner.default_world.classes()}
        imported = {o.base_iri for o in onto.imported_ontologies}
    finally:
        loader.reasoner._safe_unlink(tmp)

    assert loader.requests == []
    assert loader.modules == []
    assert {f"{_BASE}/T#C", _LOCAL_THING} <= classes
    assert any(iri.startswith(_LOCAL_IRI) for iri in imported), imported


@pytest.mark.parametrize(
    ("filename", "body"),
    [
        pytest.param("Local.rdf", _rdfxml(_LOCAL_IRI), id="rdfxml"),
        pytest.param(
            "Local.owl",
            _owlxml(_LOCAL_IRI, f'<Declaration><Class IRI="{_LOCAL_THING}"/></Declaration>'),
            id="owlxml",
        ),
        pytest.param(
            "Local.nt",
            f"<{_LOCAL_IRI}> <{_RDF_NS}type> <{_OWL_NS}Ontology> .\n"
            f"<{_LOCAL_THING}> <{_RDF_NS}type> <{_OWL_NS}Class> .\n",
            id="ntriples",
        ),
    ],
)
def test_supported_local_copy_loads_without_request(loader, filename, body):
    """지원 형식의 로컬 사본은 그대로 읽히고 요청이나 모듈 import 가 없다."""
    (loader.dir / filename).write_text(body, encoding="utf-8")

    onto, classes = _load(loader, _LOCAL_IRI)

    assert loader.requests == []
    assert loader.modules == []
    assert _LOCAL_THING in classes
    assert any(o.base_iri.startswith(_LOCAL_IRI) for o in onto.imported_ontologies)
