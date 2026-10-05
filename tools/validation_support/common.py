"""Validation 공용 유틸 — kg_validation 분해의 기반.

- `_local`, `_validate_prop_name`, `_query`: URI/이름/SPARQL 실행 헬퍼
- `_detect_domain_ns`, `_build_superclass_map`: T-Box 구조 분석
- `SharedCheckContext`: 21 check가 공유하는 계산 캐시

원래 tools/kg_validation.py 안에 있던 것을 이전. 기존 호출자 호환을
위해 kg_validation.py가 얇은 re-export wrapper를 유지한다.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict

from rdflib import OWL, RDF, RDFS, XSD, Graph, URIRef
from rdflib.collection import Collection

from domain.namespaces import DOMAIN_NS
from domain.sparql_templates import execute_local_sparql
from domain.uri_conventions import local_name as _local_name

logger = logging.getLogger(__name__)


# ── 네임 검증 ─────────────────────────────────────

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def validate_prop_name(name: str) -> str:
    """SPARQL 문자열 보간에 안전한 프로퍼티 이름인지 검증한다.

    Raises ValueError if the name contains unexpected characters.
    """
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid property name for SPARQL interpolation: {name!r}"
        )
    return name


# ── URI / 이름 유틸 ────────────────────────────────


def local(uri: str | None) -> str | None:
    """URI에서 local name 추출. None/빈 문자열은 그대로 반환."""
    if uri:
        return _local_name(uri)
    return uri


def query(g: Graph, sparql: str) -> list[dict[str, str | None]]:
    """SPARQL 실행 + format_sparql_results dict 리스트로 반환."""
    return execute_local_sparql(g, sparql)


# ── 도메인 네임스페이스 감지 ──────────────────────

_STANDARD_NS_PREFIXES = (
    str(RDF),
    str(RDFS),
    str(OWL),
    str(XSD),
    "http://www.w3.org/",
)


def detect_domain_ns(tbox: Graph) -> str:
    """T-Box 그래프에서 도메인 네임스페이스를 자동 감지한다.

    OWL Class 선언의 URI에서 가장 빈번한 네임스페이스를 반환.
    감지 실패 시 프로젝트 기본 DOMAIN_NS 상수를 반환.
    """
    ns_counts: Counter[str] = Counter()
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue
        uri = str(cls)
        if any(uri.startswith(p) for p in _STANDARD_NS_PREFIXES):
            continue
        if "#" in uri:
            ns = uri[:uri.rindex("#") + 1]
        else:
            ns = uri[:uri.rindex("/") + 1]
        ns_counts[ns] += 1
    if ns_counts:
        return ns_counts.most_common(1)[0][0]
    return DOMAIN_NS


# ── Superclass map ─────────────────────────────────


#: 클래스당 값이 하나여야 하는 주석 술어. 클래스 병합 시 두 클래스가 서로 다른
#: 값을 갖고 있었다면 병합 후 상충 값이 공존해 검증이 오작동한다 (실측 2026-07-25:
#: FacilityOperation identity ['+I','-I'] → OntoClean C3 오탐).
#: OntoClean 메타는 `<domain-ns>-ontoclean#` 네임스페이스를 쓰므로 local name 으로
#: 매칭한다.
SINGLE_VALUED_ANNOTATION_LOCALS: tuple[str, ...] = (
    "identity", "rigidity", "unity", "dependence",
)


def resolve_single_valued_annotations(
    g: Graph,
    subjects: set[URIRef] | None = None,
    *,
    locals_to_check: tuple[str, ...] = SINGLE_VALUED_ANNOTATION_LOCALS,
) -> list[dict]:
    """단일값 주석이 여러 값을 갖는 경우를 하나로 정리한다.

    해소 규칙 — **상위 클래스와 정합한 값을 남긴다.** OntoClean C1~C3 규칙은
    ``rdfs:subClassOf`` 상하 관계에서 메타값이 모순되지 않아야 한다고 요구한다.
    예: ``+I`` 자식 + ``-I`` 부모는 C3 위반이다. 그래서 부모가 선언한 값이 있으면
    그것과 같은 값을 남긴다. 부모 선언이 없거나 부모끼리도 갈리면 ``-`` (약한
    주장) 를 남긴다 — 없는 기준을 있다고 주장하는 것보다 안전하다.

    Args:
        g: 대상 그래프 (in-place 수정).
        subjects: 검사할 주어 집합. None 이면 상충이 있는 모든 주어.
        locals_to_check: 검사할 술어 local name.

    Returns:
        ``[{"subject", "predicate", "kept", "removed", "reason"}]`` 목록.
    """
    resolved: list[dict] = []
    targets = subjects if subjects is not None else set(g.subjects())
    for subject in targets:
        if not isinstance(subject, URIRef):
            continue
        for pred in set(g.predicates(subject, None)):
            if not isinstance(pred, URIRef):
                continue
            if str(pred).split("#")[-1].split("/")[-1] not in locals_to_check:
                continue
            values = list(g.objects(subject, pred))
            if len(values) < 2:
                continue
            texts = sorted({str(v) for v in values})
            if len(texts) < 2:
                continue

            # 부모가 선언한 값과 맞추면 C1~C3 위반이 생기지 않는다.
            parent_values = {
                str(v)
                for parent in g.objects(subject, RDFS.subClassOf)
                if isinstance(parent, URIRef)
                for v in g.objects(parent, pred)
            }
            aligned = sorted(parent_values & set(texts))
            if aligned:
                keep_text, reason = aligned[0], "parent_aligned"
            else:
                negative = sorted(t for t in texts if t.startswith("-"))
                keep_text = negative[0] if negative else texts[0]
                reason = "weaker_claim" if negative else "lexical"

            for value in values:
                if str(value) != keep_text:
                    g.remove((subject, pred, value))
            resolved.append({
                "subject": str(subject).split("#")[-1],
                "predicate": str(pred).split("#")[-1],
                "kept": keep_text,
                "removed": [t for t in texts if t != keep_text],
                "reason": reason,
            })
    if resolved:
        logger.warning(
            "단일값 주석 상충 %d건 해소 — %s",
            len(resolved),
            [f"{r['subject']}.{r['predicate']}={r['kept']} (버림 {r['removed']})"
             for r in resolved[:5]],
        )
    return resolved


def find_abstract_parent_classes(
    tbox: Graph, candidates: set[str] | None = None, ns: str | None = None,
) -> set[str]:
    """CSV 소스/인스턴스가 없는 것이 **정상** 인 추상 상위 클래스를 식별한다.

    판정: 자식 클래스가 있고 자기 DatatypeProperty 가 없으면 추상으로 본다.
    파이프라인이 스스로 만들라고 요구하는 계층(``design_patterns.json`` 의
    domain_hierarchy, ``step_12`` 중간 추상 클래스) 은 CSV 테이블도 인스턴스도
    가지지 않는 것이 설계 의도다. 이를 결함으로 세면 지표가 구조적으로 달성
    불가능해진다 (2026-07-25 실측: T-Box 34개 중 14개가 추상 — MasterData 12자식,
    ProcessActivity 6자식 등. T-Box Fitness 2.9% / 클래스별 인스턴스 수
    truly_orphan 12개가 모두 이 오탐이었다).

    자기 DP 를 가진 클래스는 자식이 있어도 실체로 취급한다 (예: ``MaterialA`` 은
    ``MaterialB`` 을 자식으로 두지만 자기 컬럼 184개를 갖는다).

    Args:
        tbox: T-Box 그래프.
        candidates: 검사 대상 클래스 local name 집합. None 이면 T-Box 전체.
        ns: 도메인 네임스페이스. None 이면 ``DOMAIN_NS``.

    Returns:
        추상 상위 클래스 local name 집합.
    """
    ns_filter = ns if ns is not None else DOMAIN_NS
    if candidates is None:
        candidates = {
            local(str(cls))
            for cls in tbox.subjects(RDF.type, OWL.Class)
            if isinstance(cls, URIRef) and str(cls).startswith(ns_filter)
        }

    dp_domains: set[str] = set()
    for dp in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        for domain in tbox.objects(dp, RDFS.domain):
            if isinstance(domain, URIRef) and str(domain).startswith(ns_filter):
                dp_domains.add(local(str(domain)))

    abstract: set[str] = set()
    for cls_name in candidates:
        if not cls_name or cls_name in dp_domains:
            continue
        cls_uri = URIRef(ns_filter + cls_name)
        has_children = any(
            True for child in tbox.subjects(RDFS.subClassOf, cls_uri)
            if isinstance(child, URIRef) and str(child).startswith(ns_filter)
        )
        if has_children:
            abstract.add(cls_name)
    return abstract


def build_superclass_map(tbox: Graph, ns: str | None = None) -> dict[str, set[str]]:
    """T-Box에서 클래스별 모든 상위 클래스(transitive) 집합을 구축한다.

    ns가 지정되면 해당 네임스페이스로 필터링, None이면 프로젝트 기본 DOMAIN_NS 사용.
    """
    ns_filter = ns if ns is not None else DOMAIN_NS
    direct: dict[str, set[str]] = defaultdict(set)
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef) or not str(cls).startswith(ns_filter):
            continue
        name = local(str(cls))
        for parent in tbox.objects(cls, RDFS.subClassOf):
            if isinstance(parent, URIRef) and str(parent).startswith(ns_filter):
                direct[name].add(local(str(parent)))

    # equivalentClass의 unionOf 멤버도 부모로 간주
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef) or not str(cls).startswith(ns_filter):
            continue
        parent_name = local(str(cls))
        for eq in tbox.objects(cls, OWL.equivalentClass):
            union_list = tbox.value(eq, OWL.unionOf)
            if union_list:
                try:
                    for member in Collection(tbox, union_list):
                        if isinstance(member, URIRef) and str(member).startswith(ns_filter):
                            direct[local(str(member))].add(parent_name)
                except Exception as e:
                    logger.debug("equivalentClass unionOf 수집 실패: %s", e)

    # transitive closure with cycle detection
    superclasses: dict[str, set[str]] = {}
    for name in direct:
        visited: set[str] = set()
        stack = list(direct[name])
        while stack:
            p = stack.pop()
            if p == name:
                logger.warning(
                    "subClassOf 순환 참조 감지: %s 가 자신의 상위 클래스 경로에 포함됨",
                    name,
                )
                continue
            if p in visited:
                continue
            visited.add(p)
            stack.extend(direct.get(p, set()))
        superclasses[name] = visited

    return superclasses


# ── SharedCheckContext ────────────────────────────


class SharedCheckContext:
    """여러 check가 공유하는 계산 결과 캐시 — 전수 재계산 방지.

    instance_types: rdf:type 역색인. 수십만~수백만 트리플에서 3회 재구축 시
    각 CPU 30초+ 낭비 + GC 압박. 한 번만 구축하고 공유한다.
    """

    def __init__(self, g: Graph, tbox: Graph):
        self.g = g
        self.tbox = tbox
        self._instance_types: dict[str, set[str]] | None = None
        self._superclass_map: dict[str, set[str]] | None = None
        self._typed_subjects: set[URIRef] | None = None
        self._ns: str | None = None
        self._steel_classes: set[str] | None = None
        self._inverse_pairs: list[tuple[str, str]] | None = None
        self._op_uris: set[URIRef] | None = None

    @property
    def ns(self) -> str:
        if self._ns is None:
            self._ns = detect_domain_ns(self.tbox)
        return self._ns

    @property
    def superclass_map(self) -> dict[str, set[str]]:
        if self._superclass_map is None:
            self._superclass_map = build_superclass_map(self.tbox, ns=self.ns)
        return self._superclass_map

    @property
    def instance_types(self) -> dict[str, set[str]]:
        """rdf:type 역색인.

        수백만 트리플 그래프에서 rdflib Python 루프로 30초+ 걸리던 것을
        Oxigraph SPARQL GROUP_CONCAT 집계로 교체 — steel 네임스페이스 타입은
        엔진이 필터하고 Python 은 결과 행만 파싱한다.
        typed_subjects 는 같은 패스에서 URIRef subject 전체를 별도 쿼리로.
        """
        if self._instance_types is None:
            cache: dict[str, set[str]] = defaultdict(set)
            ns = self.ns
            # 도메인 네임스페이스 타입만 한 줄로 집계
            q = (
                "SELECT ?s (GROUP_CONCAT(STR(?o); SEPARATOR=\"|\") AS ?types) WHERE { "
                "?s a ?o . "
                "FILTER(isIRI(?s)) "
                f"FILTER(STRSTARTS(STR(?o), \"{ns}\")) "
                "} GROUP BY ?s"
            )
            for row in self.g.query(q):
                s_str = str(row[0])
                types_str = str(row[1]) if row[1] is not None else ""
                if not types_str:
                    continue
                for t in types_str.split("|"):
                    if t.startswith(ns):
                        cache[s_str].add(local(t))
            self._instance_types = cache
            # typed_subjects 는 별도 집계: 타입 네임스페이스와 무관하게 모든
            # URIRef subject 를 수집 (dangling_references 에서 O 검사용).
            typed: set[URIRef] = set()
            for row in self.g.query(
                "SELECT DISTINCT ?s WHERE { ?s a ?o . FILTER(isIRI(?s)) }"
            ):
                typed.add(row[0])
            self._typed_subjects = typed
        return self._instance_types

    @property
    def typed_subjects(self) -> set[URIRef]:
        if self._typed_subjects is None:
            typed: set[URIRef] = set()
            for row in self.g.query(
                "SELECT DISTINCT ?s WHERE { ?s a ?o . FILTER(isIRI(?s)) }"
            ):
                typed.add(row[0])
            self._typed_subjects = typed
        return self._typed_subjects

    @property
    def steel_classes(self) -> set[str]:
        """T-Box에 선언된 steel 네임스페이스 클래스 집합 (여러 check가 공유)."""
        if self._steel_classes is None:
            self._steel_classes = {
                _local_name(str(cls))
                for cls in self.tbox.subjects(RDF.type, OWL.Class)
                if isinstance(cls, URIRef) and str(cls).startswith(DOMAIN_NS)
            }
        return self._steel_classes

    @property
    def inverse_pairs(self) -> list[tuple[str, str]]:
        """T-Box owl:inverseOf 쌍 (fwd_local, inv_local)."""
        if self._inverse_pairs is None:
            pairs: list[tuple[str, str]] = []
            for prop in self.tbox.subjects(RDF.type, OWL.ObjectProperty):
                if not str(prop).startswith(DOMAIN_NS):
                    continue
                for inv in self.tbox.objects(prop, OWL.inverseOf):
                    if str(inv).startswith(DOMAIN_NS):
                        fwd_local = local(str(prop))
                        inv_local = local(str(inv))
                        if fwd_local and inv_local:
                            pairs.append((fwd_local, inv_local))
            self._inverse_pairs = pairs
        return self._inverse_pairs

    @property
    def op_uris(self) -> set[URIRef]:
        """T-Box에 선언된 steel ObjectProperty URI 집합."""
        if self._op_uris is None:
            self._op_uris = {
                p for p in self.tbox.subjects(RDF.type, OWL.ObjectProperty)
                if isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS)
            }
        return self._op_uris

    def release(self) -> None:
        """대형 캐시 수동 해제 — validate_kg가 끝에 호출하여 즉시 GC."""
        self._instance_types = None
        self._typed_subjects = None
        self._superclass_map = None
        self._steel_classes = None
        self._inverse_pairs = None
        self._op_uris = None

def summarize_measurability(checks: list[dict]) -> dict:
    """체크 목록에서 **측정된 축과 미측정 축을 분리**한다.

    ## 무엇이 틀렸었나 (실측 2026-09-02)

    점수 분자는 ``passed = sum(1 for c in checks if c["passed"])`` 였고
    ``applicable`` 을 보지 않았다. 그런데 ``applicable: False`` 인 체크는
    ``passed: True`` 와 **쌍으로** 온다 (판정 불가를 FAIL 로 내면 게이트가 영구
    빨간불이 되므로 그렇게 설계됐다). 그래서 "23/25" 의 분자에 미판정 축이 섞였고,
    ``checks[]`` 를 직접 열지 않으면 구분되지 않았다.

    ## 왜 ``score`` 문자열을 바꾸지 않는가

    ``quality_history.parse_total`` 은 ``int(score.split("/")[1])`` 이다.
    ``"23/25 (measured 24)"`` 처럼 뒤에 무엇이든 붙이면 ``ValueError`` → 0 →
    ``score_entries`` 가 그 항목을 **통째로 버려** 회귀 탐지가 죽는다. 그래서
    측정 회계는 **별 필드**로만 낸다.

    Returns:
        ``measured_total`` / ``measured_passed`` / ``score_measured`` /
        ``unmeasured_checks`` (이름과 사유). ``applicable`` 키가 없는 체크는
        측정된 것으로 센다 (기존 23개 체크의 기본값).
    """
    measured: list[dict] = []
    unmeasured: list[dict] = []
    for check in checks:
        if check.get("applicable", True):
            measured.append(check)
            continue
        unmeasured.append({
            "name": check.get("name", "(이름 없음)"),
            # 사유 없는 미판정은 세탁과 구분되지 않는다. 없으면 그 사실을 적는다.
            "reason": check.get("reason") or "사유 미기재 — 체크가 reason 을 내지 않았다",
        })
    measured_passed = sum(1 for c in measured if c["passed"])
    return {
        "measured_total": len(measured),
        "measured_passed": measured_passed,
        "score_measured": f"{measured_passed}/{len(measured)}",
        "unmeasured_checks": unmeasured,
    }


def measurability_violations(checks: list[dict]) -> list[str]:
    """미판정 선언이 **세탁**으로 쓰이는 경우를 찾는다 (NEGATIVE 방향 불변식).

    상시 빨간불인 체크를 ``applicable: False`` 로 재분류하면 분모가 줄어 점수가
    오른다. 그것이 이 리포에서 가장 짧은 지표 매수 경로다. 다음 두 형태를 막는다:

    1. ``applicable: False`` 인데 위반 목록이 비어있지 않다 → 실제 발견을 미판정으로
       내보내는 것이다.
    2. ``applicable: False`` 인데 ``passed: False`` 다 → 판정을 했다는 뜻이므로
       미판정 선언과 모순이다.
    """
    problems: list[str] = []
    for check in checks:
        if check.get("applicable", True):
            continue
        name = check.get("name", "(이름 없음)")
        found = check.get("violations") or check.get("missing") or []
        if found:
            problems.append(
                f"{name}: applicable=false 인데 위반 {len(found)}건을 함께 보고했다",
            )
        if check.get("passed") is False:
            problems.append(
                f"{name}: applicable=false 인데 passed=false 다 (판정과 미판정이 모순)",
            )
    return problems
