"""T-Box mutation runner — applies SPARQL UPDATE defects, reruns validators."""
# ruff: noqa: I001, SIM105, SIM115
#
# Pre-existing debt, not introduced here (verified identical on a stashed
# baseline: same 4 findings, only line numbers shifted). Suppressed at file
# scope because the pre-commit ruff hook would otherwise block unrelated
# commits, and `--fix` rewrites lines this module did not touch:
#   I001   — the deferred `import tools.kg_validation as kgv` block inside
#            `_run_kg_validators` is intentionally local (import-time cycle).
#   SIM105 — temp-file unlink in `finally`; `contextlib.suppress` here would
#            hide the same OSError with more indirection.
#   SIM115 — mkstemp/fdopen pairing for the serialized mutant T-Box.
from __future__ import annotations

import glob
import itertools
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass

from rdflib import Graph, URIRef

from config import ABOX_PATH, GENERATED_DIR, TBOX_PATH
from domain.sparql_templates import _require_iri, reject_sparql_egress

logger = logging.getLogger(__name__)


def _is_bindable_iri(value: object) -> bool:
    """placeholder 자리에 ``<...>`` 로 넣을 수 있는 IRI 인지 판정한다.

    IRIREF 본문에 올 수 없는 문자 (``<`` ``>`` ``"`` ``{`` ``}`` 공백 등) 가 있는
    IRI 는 치환 결과의 질의 구조를 바꿀 수 있으므로 대상 후보에서 뺀다.
    """
    if not isinstance(value, URIRef):
        return False
    try:
        _require_iri(str(value))
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Mutator:
    name: str        # e.g. "M1_domain_range/delete_domain"
    category: str    # e.g. "M1"
    path: str        # absolute filesystem path
    sparql: str      # file contents


def discover_mutators(catalog_dir: str) -> list[Mutator]:
    """Return all .sparql files under catalog_dir sorted by name. Deterministic."""
    out: list[Mutator] = []
    pattern = os.path.join(catalog_dir, "*", "*.sparql")
    for path in sorted(glob.glob(pattern)):
        rel = os.path.relpath(path, catalog_dir)
        name, _ = os.path.splitext(rel)
        name = name.replace(os.sep, "/")
        category = name.split("_", 1)[0]  # "M1_domain_range/..." → "M1"
        with open(path, encoding="utf-8") as f:
            sparql = f.read()
        out.append(Mutator(name=name, category=category, path=path, sparql=sparql))
    return out


_PLACEHOLDER_QUERIES = {
    "?TARGET_PROP": """
        SELECT DISTINCT ?p WHERE {
            { ?p a owl:ObjectProperty }
            UNION { ?p a owl:DatatypeProperty }
        } ORDER BY ?p
    """,
    "?TARGET_CLASS": """
        SELECT DISTINCT ?c WHERE {
            ?c a owl:Class . FILTER (!isBlank(?c))
        } ORDER BY ?c
    """,
    "?FOREIGN_CLASS": """
        SELECT DISTINCT ?c WHERE {
            ?c a owl:Class . FILTER (!isBlank(?c))
        } ORDER BY ?c
    """,
}


def resolve_targets(graph: Graph, placeholder: str, limit: int = 3) -> list[URIRef]:
    query = _PLACEHOLDER_QUERIES.get(placeholder)
    if query is None:
        return []
    prefix = "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
    rows = list(graph.query(prefix + query))
    uris = [row[0] for row in rows if _is_bindable_iri(row[0])]
    return sorted(uris, key=str)[:limit]


_PLACEHOLDER_RE = re.compile(r"\?(TARGET_\w+|FOREIGN_CLASS)")


#: 한 mutator 가 시도할 후보 조합 상한.
#:
#: 예전에는 ``limit=1`` 로 **알파벳 첫 후보 하나만** 시도했다. 그 후보가 mutator 의
#: WHERE 절을 만족하지 못하면 ``no_effect`` 로 기록되고 그 mutant 는 아무것도
#: 검증하지 못한다. 실측 (2026-08-24, 배포 T-Box): S9.5 KG 감사 8개 중 **7개가
#: ``no_effect``** 였다 — ``?TARGET_PROP`` 의 첫 후보가
#: ``airEmissionMonitoringConcentration`` (DatatypeProperty) 인데
#: ``delete_inverse`` 등은 ``owl:inverseOf`` 보유 프로퍼티를 요구한다. T-Box 에
#: ``owl:inverseOf`` 는 120개나 있었다.
#:
#: 즉 감사가 "결함을 못 잡았다" 가 아니라 **결함을 심지도 못했다.** 그 상태로
#: ``caught_at_least_once: 0`` 이 보고돼 검출률 0% 가 검증 체계의 실패로 읽혔다.
#:
#: placeholder 가 2개인 mutator (``?TARGET_PROP`` × ``?FOREIGN_CLASS``) 는 조합이
#: 곱으로 늘어난다. 총 조합 수를 상한으로 두면 **첫 프로퍼티의 후보만 소진하고
#: 끝난다** — 그래서 placeholder 당 후보 수를 제한하고, 총 시도는 그 곱까지
#: 허용한다.
#:
#: 후보를 훑는 것은 **폴백** 이다. 주 경로는 :func:`_targets_from_where` 로 mutator
#: 자신의 WHERE 절에게 "네 전제를 만족하는 대상" 을 물어보는 것이다. 눈먼 스캔은
#: 상한에 민감하다 — 실측: ``owl:inverseOf`` 보유 프로퍼티의 첫 등장이 정렬 순서
#: **17번째** 라 상한 12 로는 도달하지 못했다.
_MAX_CANDIDATES_PER_PLACEHOLDER = 40
_MAX_TARGET_ATTEMPTS = 200

#: ``DELETE {...} INSERT {...} WHERE {...}`` 에서 WHERE 블록만 떼어내는 패턴.
#: 그 블록을 SELECT 로 감싸면 mutator 의 전제를 만족하는 바인딩을 직접 얻는다.
_WHERE_RE = re.compile(r"\bWHERE\b\s*(\{.*)", re.IGNORECASE | re.DOTALL)

#: placeholder 별 **종류 제약** — WHERE 기반 해상 시 함께 강제한다.
#:
#: mutator 의 WHERE 는 자기 전제만 적고 대상 종류는 placeholder 이름에 맡긴다.
#: 그래서 ``?TARGET_CLASS rdfs:label ?l`` 은 ``owl:Ontology`` 도 매칭했다 (실측) —
#: 클래스가 아닌 대상에 결함을 심으면, 클래스만 보는 검증기가 정당하게 무반응인데도
#: 사각지대로 집계된다.
#:
#: ``?__VAR__`` 는 호출부가 실제 변수명으로 치환한다.
_PLACEHOLDER_TYPE_PATTERNS = {
    "?TARGET_CLASS": "?__VAR__ a owl:Class .",
    "?FOREIGN_CLASS": "?__VAR__ a owl:Class .",
    "?TARGET_PROP": (
        "{ ?__VAR__ a owl:ObjectProperty } "
        "UNION { ?__VAR__ a owl:DatatypeProperty }"
    ),
}
_PREFIX_RE = re.compile(r"^\s*PREFIX\b[^\n]*$", re.IGNORECASE | re.MULTILINE)


def _targets_from_where(
    graph: Graph, sparql: str, placeholders: list[str],
) -> dict[str, URIRef] | None:
    """mutator 의 WHERE 절이 **직접 고른** placeholder 바인딩. 실패 시 ``None``.

    눈먼 후보 스캔보다 이것이 정본이다. mutator 는 자기 전제를 WHERE 에 이미
    적어 뒀으므로 (``?p owl:inverseOf ?inv`` 처럼), 그 블록을 ``SELECT`` 로 감싸
    질의하면 **효과가 보장된 대상** 을 한 번에 얻는다.

    ``ORDER BY`` 로 결정성을 유지한다 — 같은 T-Box 면 항상 같은 mutant 가 나온다.
    """
    match = _WHERE_RE.search(sparql)
    if not match:
        return None
    where_block = match.group(1).strip()
    if not where_block.startswith("{"):
        return None
    # WHERE 에 등장하지 않는 placeholder 는 SELECT 로 묶을 수 없다 (unbound).
    # ``add_spurious_inverse`` 의 ``?FOREIGN_CLASS`` 처럼 INSERT 전용인 경우가
    # 있으므로, WHERE 가 실제로 바인딩할 수 있는 것만 물어본다. 나머지는 호출부가
    # 후보 목록에서 채운다.
    placeholders = [p for p in placeholders if f"?{p}" in where_block]
    if not placeholders:
        return None
    prefixes = "\n".join(_PREFIX_RE.findall(sparql))
    vars_ = " ".join(f"?{p}" for p in placeholders)
    order = " ".join(f"?{p}" for p in placeholders)
    # **placeholder 의 자기 정의도 함께 강제한다.** mutator 의 WHERE 는 자기 전제만
    # 적고 대상 종류는 placeholder 이름에 맡긴다 — ``delete_label`` 의 WHERE 는
    # ``?TARGET_CLASS rdfs:label ?l`` 뿐이라 **label 을 가진 무엇이든** 매칭한다.
    # 실측 (2026-08-24): 그래서 ``owl:Ontology`` (온톨로지 메타데이터) 가 선택돼
    # "클래스 label 삭제" 가 아닌 결함이 심어졌고, 클래스 label 만 보는
    # ``check_quality_rules`` 가 정당하게 무반응이라 **검증 사각지대로 오집계** 됐다.
    #
    # ``_PLACEHOLDER_QUERIES`` 의 정의를 패턴으로 주입해 종류를 고정한다.
    # 원래 WHERE 블록을 **그대로 중괄호 그룹으로 감싸** 가드와 나란히 둔다.
    # 블록 안에 문자열을 끼워 넣으면 ``UNION`` 으로 끝나는 패턴 뒤에 ``.`` 이 붙어
    # 파싱이 깨진다 (실측: ``delete_inverse`` 가 SelectQuery 파싱 실패로 폴백).
    type_guards = [
        f"{{ {guard.replace('?__VAR__', f'?{p}')} }}"
        for p in placeholders
        if (guard := _PLACEHOLDER_TYPE_PATTERNS.get("?" + p))
    ]
    inner = where_block.strip()
    if type_guards:
        inner = "{ " + inner + "\n" + "\n".join(type_guards) + "\n}"
    query = (
        f"{prefixes}\nPREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
        f"SELECT DISTINCT {vars_} WHERE {inner}\n"
        f"ORDER BY {order}\nLIMIT 1"
    )
    try:
        # 실행할 최종 SELECT 를 그대로 가드한다. 거부되면 실행 없이 후보 스캔으로
        # 넘어가고, 그 경로의 UPDATE 도 ``_bind_and_update`` 가 다시 가드한다.
        reject_sparql_egress(query)
        rows = list(graph.query(query))
    except Exception as exc:  # noqa: BLE001 — WHERE 가 SELECT 로 안 되는 형태면 폴백
        logger.debug("WHERE-기반 타깃 해상 실패 (후보 스캔으로 폴백): %s", exc)
        return None
    for row in rows:
        binding = {}
        for i, p in enumerate(placeholders):
            value = row[i]
            if not _is_bindable_iri(value):
                return None
            binding["?" + p] = value
        return binding
    return None


def _bind_and_update(
    graph: Graph, sparql: str, bindings: dict[str, URIRef],
) -> tuple[Graph, int, int]:
    """치환한 SPARQL 을 사본에 적용하고 ``(사본, 삭제수, 추가수)`` 반환.

    **집합 차분으로 센다.** 예전에는 ``len()`` 차이만 봤는데, ``DELETE {…}
    INSERT {…}`` 형태는 같은 개수를 지우고 넣으므로 길이가 변하지 않는다. 실측
    (2026-08-24): ``some_to_all`` 이 ``owl:someValuesFrom`` **101개를
    ``allValuesFrom`` 으로 전부 바꿨는데** ``len`` 이 4972 → 4972 라
    ``no_effect`` 로 기록됐다 — 결함을 심었는데 심지 않은 것으로 집계됐다.

    치환(replace) 계열 mutator 가 카탈로그의 절반이므로 이 오판이 검출률을
    구조적으로 깎았다.

    바인딩 값은 IRIREF 형식을 확인한 뒤 ``<...>`` 로 치환하고, 치환을 끝낸 UPDATE
    문자열을 실행 직전 egress 가드에 넣는다.

    Raises:
        ValueError: 바인딩 값이 IRIREF 형식이 아니거나 최종 UPDATE 가 가드에 거부될 때.
    """
    bound = sparql
    for ph_key, uri in bindings.items():
        bound = bound.replace(ph_key, f"<{_require_iri(str(uri))}>")
    reject_sparql_egress(bound)
    mutated = Graph()
    mutated += graph  # shallow copy of triples; Graph.__iadd__ works
    before = set(mutated)
    mutated.update(bound)
    after = set(mutated)
    return mutated, len(before - after), len(after - before)


def apply_mutator(graph: Graph, mutator: Mutator) -> tuple[Graph, dict]:
    """Apply one mutator to a deep copy of graph.

    Returns (mutated_graph, info) where info = {applied, target, triples_removed,
    triples_added, reason?}. If no target can be resolved, returns graph unchanged
    with applied=False.

    **후보를 실제로 효과가 날 때까지 훑는다.** mutator 의 WHERE 절은 자기만의
    전제를 갖는다 (``owl:inverseOf`` 보유, cardinality 제약 보유 등). 첫 후보만
    시도하면 그 전제를 만족하지 못해 ``no_effect`` 가 되고, 결함이 **심어지지도
    않은 채** 검출 실패로 집계된다 (:data:`_MAX_TARGET_ATTEMPTS` 주석의 실측 참조).

    후보 순서는 ``resolve_targets`` 의 정렬을 그대로 쓰므로 **결정적** 이다 — 같은
    T-Box 에 대해 항상 같은 mutant 가 나온다.

    카탈로그 원문이 egress 가드에 거부되면 (SERVICE·LOAD·USING 등, 또는 해석 불가)
    아무 질의도 실행하지 않고 ``reason="sparql_guard_rejected"`` 로 돌려준다.
    """
    try:
        reject_sparql_egress(mutator.sparql)
    except ValueError as exc:
        logger.warning("mutator %s 를 SPARQL 가드가 거부했다: %s", mutator.name, exc)
        return graph, {"applied": False, "reason": "sparql_guard_rejected",
                       "error": str(exc)[:200], "target": None,
                       "triples_removed": 0, "triples_added": 0}

    placeholders = sorted(set(_PLACEHOLDER_RE.findall(mutator.sparql)))
    if not placeholders:
        mutated, removed, added = _bind_and_update(graph, mutator.sparql, {})
        if removed or added:
            return mutated, {"applied": True, "target": {},
                             "triples_removed": removed, "triples_added": added}
        return graph, {"applied": False, "reason": "no_effect", "target": {},
                       "triples_removed": 0, "triples_added": 0}

    # 1) 주 경로: mutator 의 WHERE 절이 자기 전제를 만족하는 대상을 직접 고른다.
    #    WHERE 가 못 채우는 placeholder (INSERT 전용) 는 후보 목록으로 보충한다.
    from_where = _targets_from_where(graph, mutator.sparql, placeholders)
    if from_where:
        missing = [p for p in placeholders if ("?" + p) not in from_where]
        extra_lists = [
            resolve_targets(graph, "?" + p, limit=_MAX_CANDIDATES_PER_PLACEHOLDER)
            for p in missing
        ]
        if all(extra_lists):
            for combo in itertools.product(*extra_lists) if missing else [()]:
                bindings = dict(from_where)
                for p, uri in zip(missing, combo):
                    bindings["?" + p] = uri
                mutated, removed, added = _bind_and_update(
                    graph, mutator.sparql, bindings,
                )
                if removed or added:
                    return mutated, {
                        "applied": True,
                        "target": {k: str(v) for k, v in bindings.items()},
                        "triples_removed": removed,
                        "triples_added": added,
                        "target_source": "where_clause",
                    }

    # 2) 폴백: 후보를 정렬 순서로 훑는다 (WHERE 를 SELECT 로 못 감싸는 형태 등).
    candidate_lists: list[tuple[str, list[URIRef]]] = []
    for ph in placeholders:
        ph_key = "?" + ph
        candidates = resolve_targets(
            graph, ph_key, limit=_MAX_CANDIDATES_PER_PLACEHOLDER,
        )
        if not candidates:
            return graph, {"applied": False, "reason": f"no_target_{ph}",
                           "target": None, "triples_removed": 0, "triples_added": 0}
        candidate_lists.append((ph_key, candidates))

    # 조합을 순서대로 시도한다 (첫 placeholder 가 가장 바깥 = 정렬 순서 우선).
    last_target: dict[str, str] = {}
    attempts = 0
    for combo in itertools.product(*(c for _, c in candidate_lists)):
        if attempts >= _MAX_TARGET_ATTEMPTS:
            break
        attempts += 1
        bindings = {
            key: uri for (key, _), uri in zip(candidate_lists, combo)
        }
        last_target = {k: str(v) for k, v in bindings.items()}
        mutated, removed, added = _bind_and_update(graph, mutator.sparql, bindings)
        if removed or added:
            return mutated, {
                "applied": True,
                "target": last_target,
                "triples_removed": removed,
                "triples_added": added,
                "target_attempts": attempts,
            }

    return graph, {
        "applied": False,
        "reason": "no_effect",
        "target": last_target,
        "triples_removed": 0,
        "triples_added": 0,
        "target_attempts": attempts,
    }


# ---------------------------------------------------------------------------
# S4 validator adapter
# ---------------------------------------------------------------------------

# Status ranks used when comparing baseline vs mutant per check.
_STATUS_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _classify_validate_ttl_syntax(payload: dict) -> str:
    """PASS when parsing succeeded, FAIL otherwise."""
    return "PASS" if payload.get("success") is True else "FAIL"


def _classify_check_quality_rules(payload: dict) -> str:
    """Map quality rule counts to PASS/WARN/FAIL.

    FAIL when any critical or high severity issue exists.
    WARN when only warnings/infos exist.
    PASS when zero issues.
    """
    if payload.get("success") is not True:
        return "FAIL"
    critical = int(payload.get("critical", 0) or 0)
    high = int(payload.get("high", 0) or 0)
    warning = int(payload.get("warning", 0) or 0)
    if critical > 0 or high > 0:
        return "FAIL"
    if warning > 0:
        return "WARN"
    return "PASS"


def _classify_validate_owl_consistency(payload: dict) -> str:
    """FAIL on error or inconsistent; WARN on unsatisfiable classes; PASS otherwise."""
    if payload.get("success") is not True:
        return "FAIL"
    if payload.get("consistent") is False:
        return "FAIL"
    if int(payload.get("unsatisfiable_count", 0) or 0) > 0:
        return "WARN"
    return "PASS"


def _classify_classify_tbox(payload: dict) -> str:
    """FAIL on reasoner/consistency error, PASS otherwise.

    classify_tbox aborts with success=False when the ontology is inconsistent.
    """
    if payload.get("success") is not True:
        return "FAIL"
    if payload.get("unsatisfiable_classes"):
        return "WARN"
    return "PASS"


def _classify_validate_tbox_shacl(payload: dict) -> str:
    """PASS on conformance; FAIL if any llm_required violation; WARN otherwise."""
    if payload.get("success") is not True:
        return "FAIL"
    if payload.get("conforms") is True:
        return "PASS"
    counts = payload.get("counts", {}) or {}
    if int(counts.get("llm_required", 0) or 0) > 0:
        return "FAIL"
    if int(counts.get("deterministic_fixable", 0) or 0) > 0:
        return "WARN"
    # Non-conforming with zero classified violations — treat as WARN.
    return "WARN"


# Registry: (check_name, callable_factory, status_classifier).
# factory는 실제 validator import를 호출 시점까지 늦춘다. catalog 조회나 mutator
# 테스트만 할 때 `tools.owl_reasoner`의 JVM/HermiT 초기화 비용을 내지 않기 위함이다.
def _validator_registry():
    from tools.owl_reasoner import classify_tbox, validate_owl_consistency
    from tools.validation_core import (
        check_quality_rules,
        validate_tbox_shacl,
        validate_ttl_syntax,
    )

    # Keys use spec short aliases (syntax/quality/hermit/classify/shacl) so 관련 단계
    # sensitivity matrix and 관련 단계 report render consistently. Do NOT rename back
    # to MCP tool names — every downstream consumer keys on these short aliases.
    return [
        ("syntax", validate_ttl_syntax, _classify_validate_ttl_syntax),
        ("quality", check_quality_rules, _classify_check_quality_rules),
        (
            "hermit",
            validate_owl_consistency,
            _classify_validate_owl_consistency,
        ),
        ("classify", classify_tbox, _classify_classify_tbox),
        ("shacl", validate_tbox_shacl, _classify_validate_tbox_shacl),
    ]


def _run_tbox_validators(tbox_path: str) -> dict[str, str]:
    """모든 S4 검증기를 tbox_path 내용에 실행해 ``{check: status}``를 반환한다.

    변조 실행기가 소유한 임시 파일을 한 번 읽고 각 검증기에 ``ttl_content`` 로
    전달한다. 공개 ``ttl_path`` 파라미터는 배포 T-Box 디렉터리의 파일명만
    허용하므로 임시 절대경로를 넘기지 않는다.

    각 검증기 예외는 해당 검사를 FAIL로 기록한다. HermiT/JVM 또는 SHACL 엔진
    하나가 실패해도 전체 변조 실행은 다음 검사와 mutant를 계속 처리한다.
    """
    results: dict[str, str] = {}
    try:
        with open(tbox_path, encoding="utf-8") as handle:
            ttl_content = handle.read()
    except Exception:
        return {
            check_name: "FAIL"
            for check_name, _fn, _classifier in _validator_registry()
        }

    for check_name, fn, classifier in _validator_registry():
        try:
            raw = fn(ttl_content=ttl_content)
            payload = json.loads(raw) if isinstance(raw, str) else raw
            results[check_name] = classifier(payload)
        except Exception:
            results[check_name] = "FAIL"
    return results


def _serialize_to_temp(graph: Graph) -> str:
    """Write graph to a named temp .ttl file and return its path."""
    tf = tempfile.NamedTemporaryFile(suffix=".ttl", delete=False)
    tf.close()
    graph.serialize(destination=tf.name, format="turtle")
    return tf.name


def _caught_by(baseline: dict[str, str], mutant: dict[str, str]) -> list[str]:
    """Checks whose status degraded PASS→(WARN|FAIL) or WARN→FAIL."""
    out: list[str] = []
    for check, m_status in mutant.items():
        b_status = baseline.get(check, "PASS")
        if _STATUS_RANK.get(m_status, 0) > _STATUS_RANK.get(b_status, 0):
            out.append(check)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_tbox_mutations(
    tbox_path: str,
    catalog_dir: str = "rules/mutations",
    out_dir: str | None = None,
    timeout_s_per_mutant: float = 30.0,
) -> dict:
    """Apply every mutator, rerun S4 validators, write delta JSON.

    Returns summary dict; writes {out_dir}/tbox.json if out_dir is provided.
    """
    start = time.monotonic()
    mutators = discover_mutators(catalog_dir)

    baseline_graph = Graph().parse(tbox_path, format="turtle")
    baseline_summary = _run_tbox_validators(tbox_path)

    mutants_out: list[dict] = []
    applied = 0
    skipped_no_target = 0
    skipped_no_effect = 0
    skipped_rejected = 0

    for m in mutators:
        t0 = time.monotonic()
        mutated_graph, info = apply_mutator(baseline_graph, m)
        if not info.get("applied"):
            reason = info.get("reason", "unknown")
            if reason == "no_effect":
                skipped_no_effect += 1
            elif reason == "sparql_guard_rejected":
                skipped_rejected += 1
            else:
                skipped_no_target += 1
            mutants_out.append({
                "mutant_id": m.name,
                "category": m.category,
                "stage": "S4.5",
                "applied": False,
                "reason": reason,
                "target": info.get("target"),
                "triples_removed": info.get("triples_removed", 0),
                "triples_added": info.get("triples_added", 0),
                "duration_s": round(time.monotonic() - t0, 3),
            })
            continue

        mut_path = _serialize_to_temp(mutated_graph)
        try:
            mutant_summary = _run_tbox_validators(mut_path)
        finally:
            try:
                os.unlink(mut_path)
            except OSError:
                pass

        caught = _caught_by(baseline_summary, mutant_summary)
        mutants_out.append({
            "mutant_id": m.name,
            "category": m.category,
            "stage": "S4.5",
            "applied": True,
            "target": info["target"],
            "triples_removed": info["triples_removed"],
            "triples_added": info["triples_added"],
            "baseline_summary": baseline_summary,
            "mutant_summary": mutant_summary,
            "caught_by": caught,
            "duration_s": round(time.monotonic() - t0, 3),
        })
        applied += 1

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tbox_path": tbox_path,
        "total_mutants": len(mutators),
        "applied": applied,
        "skipped_no_target": skipped_no_target,
        "skipped_no_effect": skipped_no_effect,
        "skipped_rejected": skipped_rejected,
        "duration_s": round(time.monotonic() - start, 3),
        "mutants": mutants_out,
    }

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "tbox.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)

    return summary


# ---------------------------------------------------------------------------
# S9.5 KG-stage mutation runner
# ---------------------------------------------------------------------------

# M7 (consistency) is excluded per spec §3.2 — HermiT timeout + S4.5's
# validate_owl_consistency already covers it, and rerunning the reasoner under
# validate_kg would blow past the S9.5 budget. M4/M5 (label/comment) are
# ignored at the KG stage because annotation changes don't propagate to A-Box
# instance validation — they stay inside the S4.5 sensitivity matrix.
#: S9.5 가 표본을 뽑는 카테고리 = "KG 로 전파되는" 변조.
#
# 2026-09-05: M4 / M7 을 추가했다. 판정 기준은 "validate_kg 에 그 축을 보는 체크가
# 있는가" 다:
#
#   M4 (disjoint/계층)  → ``AllDisjointClasses 위반`` / 계층을 읽는 여러 체크
#   M7 (restriction)    → ``필수참여 공리 충족`` / ``카디널리티 제약 위반``
#
# 왜 지금 추가하는가: ``M7/some_to_all`` 은 someValuesFrom 40건을 전부
# allValuesFrom 으로 바꿔 ``필수참여 공리 충족`` 을 **판정 불가로 만든다**. 그런데
# 그 mutant 는 S4.5 만 적용했고 S4.5 는 validate_kg 를 돌리지 않는다 — 즉 KG 체크를
# 눈멀게 하는 변조가 KG 체크를 돌리는 단계에서 한 번도 적용되지 않았다 (실측:
# meta_audit 15 runs 에서 ``some_to_all`` 의 seen_stages = ['S4.5'] 뿐).
#
# 대가: ``per_cat = sample_size // len(_KG_CATEGORIES)`` 이므로 카테고리가 늘면 기본
# ``sample_size=8`` 에서 카테고리당 1개만 뽑힌다 (총 6개). 카탈로그가 카테고리당 3개
# 이므로 **전수 노출은 sample_size=18** 이다. 부분 표본으로도 결정적이다 (이름 정렬).
_KG_CATEGORIES = ("M1", "M2", "M3", "M4", "M6", "M7")

# Multiplier applied to (baseline cost x mutant count) when deriving the S9.5
# budget. Covers mutation/serialize overhead and per-mutant variance without
# letting a pathological mutant stall the audit indefinitely.
_BUDGET_SLACK = 1.5


def _classify_uncaught(mutants_out: list[dict]) -> dict[str, int]:
    """주입됐지만 못 잡힌 mutant 에 **왜 못 잡았는가** 를 각인하고 갈래를 센다.

    ## 왜 분모를 줄이지 않는가

    2026-09-04 S9.5 실측은 applied 10 / caught 2 였고, 못 잡은 8건 중 절반이
    "구조적으로 인스턴스 위반을 만들 수 없는" 것이었다 (표적 술어가 A-Box 0건이거나,
    변조가 공리를 **제거**하기만 해서 위반할 대상이 사라진 경우). 그것들을 분모에서
    빼면 catch_rate 가 20% → 57% 로 뛴다.

    **그렇게 하지 않는다.** "현재 검사 집합으로는 관측 불가" 는 곧 사각지대이고, 이
    감사의 존재 이유가 그 사각지대를 드러내는 것이다. 분모를 줄이면 탐지력을 하나도
    올리지 않고 숫자만 좋아진다 — 이 리포가 반복해서 기각한 지표 매수다
    (``0회 발동 게이트는 0회로 보고하라``).

    대신 **이유를 각인**해 "고칠 수 있는 miss" 와 "구조적으로 못 보는 miss" 를
    구분한다. ``detector_gap`` 이 곧 조치 대상이다 — 2026-09-04 기준 그 갈래는
    DP 축 3건(``domain_range_conformance`` 가 OP 만 순회)과 inverseOf 타입 오류
    1건이었고 둘 다 수정했다.

    사용량 판정은 ``abox_used_local_names`` 정본만 쓴다 (A-Box + master + tacit 을
    함께 본다). 그 헬퍼가 ``None`` 을 내면 **판정 불가** 이므로 "미사용" 으로 읽지
    않는다 — 0건과 판정불가를 섞으면 산 데이터를 미사용으로 오판한다.

    Args:
        mutants_out: 각 mutant 기록. 이 함수가 ``uncaught_reasons`` 를 **제자리에서**
            추가한다.

    Returns:
        갈래별 개수. 한 mutant 가 두 이유를 가질 수 있어 합계가 miss 수보다 클 수 있다.
    """
    from domain.graph_utils import abox_used_local_names

    uncaught = [
        m for m in mutants_out
        if m.get("applied") and not m.get("caught_by")
    ]
    if not uncaught:
        return {}

    # 표적 술어 local name 을 모아 한 번에 묻는다 (헬퍼는 파일 서명으로 캐시한다).
    candidates: set[str] = set()
    for m in uncaught:
        for value in (m.get("target") or {}).values():
            text = str(value)
            if "#" in text:
                candidates.add(text.rsplit("#", 1)[-1])
    used = abox_used_local_names(candidates) if candidates else set()

    breakdown: dict[str, int] = {}
    for m in uncaught:
        reasons: list[str] = []
        targets = [
            str(v).rsplit("#", 1)[-1] for v in (m.get("target") or {}).values()
            if "#" in str(v)
        ]
        if used is None:
            reasons.append("abox_usage_undecidable")
        elif targets and not any(t in used for t in targets):
            # 표적 술어가 A-Box·master·tacit 어디에도 없다 → 인스턴스 축은
            # 원리상 위반을 만들 수 없다. 스키마 축은 여전히 잡을 수 있으므로
            # 이것을 "면제" 로 쓰지 않는다 (분모 불변).
            reasons.append("target_unused_in_abox")
        if m.get("blinded_checks"):
            # 감사가 이미 다른 채널로 포착했다 — 검증기가 위반을 못 본 이유가
            # "가드가 돌지 못했다" 로 규명된 것이다. ``detector_gap`` 은 "설명되지
            # 않은 miss" 를 뜻하므로 이 경우를 거기 남기면 조치 목록이 오염된다.
            #
            # 실측 2026-09-05 (전수 노출 S9.5): ``M7/some_to_all`` 이 유일한 사례이고
            # ``필수참여 공리 충족`` 을 판정 불가로 만든다. 그것을 detector_gap 으로
            # 두면 "고칠 탐지기" 를 찾게 되는데, 실제로 필요한 것은 그 축이 왜 판정
            # 불가가 됐는지(공리가 사라졌다)를 보는 것이다.
            reasons.append("blinded_guard")
        if m.get("triples_added", 0) == 0 and m.get("triples_removed", 0) > 0:
            # 공리를 **제거**하는 변조. **위반** 검사로는 못 잡는다 (제약이 사라지면
            # 위반할 대상도 없다). 다만 "선언이 있어야 한다" 를 요구하는 **완전성**
            # 축은 잡을 수 있다 — 실제로 ``delete_domain`` 은 이 갈래로 분류되지만
            # 2026-09-04 에 추가한 ``dp_used_without_domain`` 이 잡는다. 그래서 이
            # 라벨은 "포기" 가 아니라 "위반 축에서는 안 보인다" 는 뜻이다.
            reasons.append("weakening_only")
        if not reasons:
            reasons.append("detector_gap")    # 조치 대상
        m["uncaught_reasons"] = reasons
        for reason in reasons:
            breakdown[reason] = breakdown.get(reason, 0) + 1
    return breakdown


def _run_kg_validators(tbox_path: str, abox_path: str) -> dict[str, str]:
    """Rerun validate_kg's 25 checks against the given T-Box + A-Box.

    Path override strategy (Option C): patch the module-level ``TBOX_PATH`` /
    ``ABOX_PATH`` attributes on both ``tools.kg_validation`` (used directly by
    wrapper functions + raw_triple_count) and ``domain.tbox_utils`` (which
    ``load_graph`` reads as its default when no explicit path is passed).

    Why not env vars: both modules do ``from config import TBOX_PATH, ...`` at
    import time, so the constants are bound into each module's namespace once
    and never re-read from ``os.environ``. Env-var overrides would be silent
    no-ops.

    Why not ``load_graph(tbox_path=..., abox_path=...)``: ``validate_kg`` calls
    ``_load_graph(use_inferred)`` which does not forward path args, and it also
    reads ``TBOX_PATH`` directly for ``_check_class_instance_count``. Patching the
    module attributes covers all call sites with one mechanism.

    (2026-08-31: ``raw_triple_count`` no longer comes from
    ``_count_triples_in_file`` — it reads the inferer's own measurement in
    ``inference_loss_manifest.json``, because a line-count approximation cannot
    include the 58,993 inverse triples that ``ensure_inverse_triples`` materialises
    at load time. That path is not affected by these module-attribute patches, so
    a mutant that only edits the T-Box leaves the manifest untouched and the
    inference-sanity axis reports ``applicable: false`` rather than a false FAIL.)

    The ``load_graph`` cache key includes ``(use_inferred, tbox_path, abox_path)``
    — since we pass empty strings (the defaults), it falls through to the
    patched module globals, so each (tbox, abox) pair gets its own cache entry
    and stale data is not reused across mutants.

    Returns ``{check_name: status}`` where status is ``PASS`` / ``FAIL`` derived
    from ``passed`` (no WARN at the KG stage — individual checks are binary).
    On any exception (e.g. corrupt T-Box, missing file, HermiT crash) returns
    ``{"validate_kg": "FAIL"}`` so the runner continues with the next mutant.

    Side-effect isolation: ``validate_kg`` appends to ``quality_history.json``
    under ``GENERATED_ABOX_DIR``. We redirect it to a per-call temp dir so
    mutation runs never pollute production quality history.

    ``tools.ontoclean.TBOX_PATH`` is patched too: ``validate_kg`` invokes
    ``analyze_ontoclean()`` without forwarding ``tbox_path``, so without this
    patch ontoclean would parse the baseline T-Box and overwrite
    ``ontoclean_report.json`` on every mutant (side-effect pollution + stale
    results masquerading as the mutant's).
    """
    import tools.kg_validation as kgv
    import tools.ontoclean as onto
    import domain.tbox_utils as tbu

    orig = {
        "kgv_tbox": kgv.TBOX_PATH,
        "kgv_abox": kgv.ABOX_PATH,
        "kgv_abox_dir": kgv.GENERATED_ABOX_DIR,
        "tbu_tbox": tbu.TBOX_PATH,
        "tbu_abox": tbu.ABOX_PATH,
        "onto_tbox": onto.TBOX_PATH,
    }
    # Invalidate graph cache before + after: stale cache entries keyed to the
    # original paths would mask mutations. Cheaper than reasoning about which
    # cache key is active at call time.
    tbu.invalidate_graph_cache()
    history_tmp = tempfile.mkdtemp(prefix="mutation_kg_history_")
    try:
        kgv.TBOX_PATH = tbox_path
        kgv.ABOX_PATH = abox_path
        kgv.GENERATED_ABOX_DIR = os.path.join(history_tmp, "abox")
        tbu.TBOX_PATH = tbox_path
        tbu.ABOX_PATH = abox_path
        onto.TBOX_PATH = tbox_path
        try:
            raw = kgv.validate_kg(use_inferred=False)
        except Exception:
            return {"validate_kg": "FAIL"}
    finally:
        kgv.TBOX_PATH = orig["kgv_tbox"]
        kgv.ABOX_PATH = orig["kgv_abox"]
        kgv.GENERATED_ABOX_DIR = orig["kgv_abox_dir"]
        tbu.TBOX_PATH = orig["tbu_tbox"]
        tbu.ABOX_PATH = orig["tbu_abox"]
        onto.TBOX_PATH = orig["onto_tbox"]
        tbu.invalidate_graph_cache()
        shutil.rmtree(history_tmp, ignore_errors=True)

    # validate_kg returns a JSON string; error path returns {"success": False}.
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {"validate_kg": "FAIL"}

    if not isinstance(payload, dict) or payload.get("success") is not True:
        return {"validate_kg": "FAIL"}

    result: dict[str, str] = {}
    for check in payload.get("checks", []) or []:
        if not isinstance(check, dict):
            continue
        name = check.get("name") or "<unnamed>"
        if check.get("applicable") is False:
            # 미판정을 PASS 로 접지 않는다. ``applicable: false`` 는 "위반이 없다"
            # 가 아니라 "판정할 수 없다" 다 — 이 리포는 그 둘을 섞어 축을 통째로 잃은
            # 이력이 있다. ``N/A`` 는 ``_STATUS_RANK`` 에 없으므로 ``_caught_by``
            # (등급 악화 비교) 에서 catch 로 세지 않는다. 대신 baseline 에서
            # 측정되던 체크가 N/A 로 바뀐 것을 ``blinded_checks`` 로 따로 낸다.
            result[name] = "N/A"
            continue
        result[name] = "PASS" if check.get("passed") else "FAIL"
    return result or {"validate_kg": "PASS"}


def _blinded_checks(baseline: dict[str, str], mutant: dict[str, str]) -> list[str]:
    """baseline 에서 **측정되던** 체크가 mutant 에서 판정 불가로 바뀐 것.

    ## 왜 catch 로 세지 않는가 (2026-09-05)

    S4.5/S9.5 양쪽이 놓친 유일한 순수 갭이 ``M7/some_to_all`` 이었다 —
    ``someValuesFrom`` 40건을 전부 ``allValuesFrom`` 으로 바꾼다. 그러면
    ``필수참여 공리 충족`` 이 검사할 공리가 0건이 되어 **공허하게 통과한다**
    (실측: ``axioms_checked 40 → 0``, ``passed: true``, 그리고 체크 자신은 이미
    메시지에 "vacuous pass" 라고 적고 있었다).

    이것을 catch 로 세면 **비-탐지를 검출률에 부풀리는** 것이 된다 — 검증기가 위반을
    본 것이 아니라 볼 능력을 잃은 것이다. 그래서 ``caught_by`` 와 분리해 별 신호로
    낸다. 가드가 눈먼 것은 위반보다 나쁠 수 있으므로 보고 자체는 필요하다.

    Returns:
        눈먼 체크 이름 목록 (baseline 이 PASS/WARN/FAIL 이었고 mutant 가 N/A).
    """
    out: list[str] = []
    for check, m_status in mutant.items():
        if m_status != "N/A":
            continue
        if baseline.get(check, "N/A") != "N/A":
            out.append(check)
    return sorted(out)


def run_kg_mutations(
    tbox_path: str,
    abox_path: str,
    catalog_dir: str = "rules/mutations",
    sample_size: int = 8,
    out_dir: str | None = None,
    budget_s: float | None = None,
) -> dict:
    """Apply sampled KG-propagating mutants, rerun validate_kg.

    Sampling: up to ``sample_size // len(_KG_CATEGORIES)`` mutants per category
    (M1/M2/M3/M6), sorted by mutator name for determinism. M7 is excluded per
    spec §3.2 (HermiT + validate_owl_consistency already cover it at S4.5).

    Budget: ``budget_s`` caps total wall-clock. Mutants exceeding the budget
    are recorded with ``applied=False`` and ``reason="skipped_timeout"`` so
    the sensitivity matrix sees them as skipped, not as false negatives.

    ``budget_s=None`` (default) **derives** the cap from the measured baseline
    cost: one ``validate_kg`` pass per mutant plus slack. A fixed default cannot
    work here because the cost scales with A-Box size — 2026-08-29 measured 65s
    per pass on a 250k-triple A-Box, so the old fixed ``90.0`` was spent by the
    baseline alone and **7 of 8 mutants were skipped before injection**. The
    audit then reported zero catches, which reads as "no detector fires" when
    the truth was "no defect was planted" (the same failure mode as
    ``mutation_audit_never_planted_defects``). Pass an explicit float to impose
    a hard ceiling instead.

    Writes ``{out_dir}/kg.json`` with ``sort_keys=True, indent=2,
    ensure_ascii=False``.
    """
    start = time.monotonic()
    all_mutators = discover_mutators(catalog_dir)
    kg_mutators = [m for m in all_mutators if m.category in _KG_CATEGORIES]

    by_cat: dict[str, list[Mutator]] = {}
    for m in kg_mutators:
        by_cat.setdefault(m.category, []).append(m)
    per_cat = max(1, sample_size // len(_KG_CATEGORIES))
    sampled: list[Mutator] = []
    for cat in _KG_CATEGORIES:
        # Sort by name for deterministic selection; slice to per_cat.
        sampled.extend(sorted(by_cat.get(cat, []), key=lambda x: x.name)[:per_cat])
    sampled = sampled[:sample_size]

    baseline_graph = Graph().parse(tbox_path, format="turtle")
    baseline_started = time.monotonic()
    baseline_summary = _run_kg_validators(tbox_path, abox_path)
    baseline_cost_s = time.monotonic() - baseline_started

    # Derive the cap from the measured cost of one pass so the sample is
    # actually reachable. ``_BUDGET_SLACK`` absorbs mutation/serialize overhead
    # and per-mutant variance (heavy checks are cache-sensitive).
    derived_budget = False
    if budget_s is None:
        derived_budget = True
        budget_s = baseline_cost_s * (len(sampled) + 1) * _BUDGET_SLACK
        logger.info(
            "S9.5: baseline validate_kg %.1fs → derived budget %.0fs for %d mutants",
            baseline_cost_s, budget_s, len(sampled),
        )

    mutants_out: list[dict] = []
    for m in sampled:
        t0 = time.monotonic()
        if time.monotonic() - start > budget_s:
            mutants_out.append({
                "mutant_id": m.name,
                "category": m.category,
                "stage": "S9.5",
                "applied": False,
                "reason": "skipped_timeout",
                "target": None,
                "duration_s": round(time.monotonic() - t0, 3),
            })
            continue

        mutated, info = apply_mutator(baseline_graph, m)
        if not info.get("applied"):
            mutants_out.append({
                "mutant_id": m.name,
                "category": m.category,
                "stage": "S9.5",
                "applied": False,
                "reason": info.get("reason", "unknown"),
                "target": info.get("target"),
                "triples_removed": info.get("triples_removed", 0),
                "triples_added": info.get("triples_added", 0),
                "duration_s": round(time.monotonic() - t0, 3),
            })
            continue

        mut_tbox = _serialize_to_temp(mutated)
        try:
            mutant_summary = _run_kg_validators(mut_tbox, abox_path)
        finally:
            try:
                os.unlink(mut_tbox)
            except OSError:
                pass

        mutants_out.append({
            "mutant_id": m.name,
            "category": m.category,
            "stage": "S9.5",
            "applied": True,
            "target": info["target"],
            "triples_removed": info["triples_removed"],
            "triples_added": info["triples_added"],
            "baseline_summary": baseline_summary,
            "mutant_summary": mutant_summary,
            "caught_by": _caught_by(baseline_summary, mutant_summary),
            # 잡은 것과 **눈멀게 한 것** 을 분리한다 (``_blinded_checks`` docstring).
            "blinded_checks": _blinded_checks(baseline_summary, mutant_summary),
            "duration_s": round(time.monotonic() - t0, 3),
        })

    # Injection accounting BEFORE detection accounting. A bare "caught 0" is
    # ambiguous: it means either "no detector fires" (a real blind spot) or "no
    # defect was planted" (an audit bug). S4.5 has counted applied /
    # skipped_no_effect since inception; S9.5 did not, so the 2026-08-29 run
    # reporting caught_at_least_once=0 looked like total detector failure when 7
    # of 8 mutants had been skipped on budget. Detection rate is expressed over
    # ``applied``, never over ``sample_size``.
    applied = sum(1 for m in mutants_out if m.get("applied"))
    skipped: dict[str, int] = {}
    for m in mutants_out:
        if not m.get("applied"):
            skipped[m.get("reason", "unknown")] = skipped.get(m.get("reason", "unknown"), 0) + 1
    caught = sum(1 for m in mutants_out if m.get("caught_by"))
    # 가드를 눈멀게 한 mutant 수. ``caught`` 와 겹칠 수 있다 (한 mutant 가 어떤 축은
    # 위반시키고 다른 축은 눈멀게 할 수 있다) — 그래서 별 카운터다.
    blinded = sum(1 for m in mutants_out if m.get("blinded_checks"))

    # Checks that are ALREADY FAIL at baseline cannot register a catch: the
    # PASS/WARN/FAIL rank comparison sees FAIL→FAIL as "no change". 2026-08-29
    # measured 8 of 25 checks red at baseline, and the audit log showed
    # ``schema_reference_integrity`` naming the injected ``DOES_NOT_EXIST``
    # range — it saw the defect and still scored 0. Reporting a bare "0%"
    # attributes that to the detectors when the instrument is blind. Expose the
    # blind set so the rate is read with its own limits attached.
    baseline_failed = sorted(k for k, v in baseline_summary.items() if v == "FAIL")

    uncaught_breakdown = _classify_uncaught(mutants_out)

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tbox_path": tbox_path,
        "abox_path": abox_path,
        "sample_size": len(sampled),
        "applied": applied,
        "skipped": skipped,
        "caught": caught,
        # None (not 0) when nothing was injected — a rate over an empty
        # denominator is not "0%", it is unmeasured.
        "catch_rate_pct": round(caught / applied * 100, 1) if applied else None,
        # 분모는 **applied 그대로다.** 구조적으로 관측 불가한 mutant 를 분모에서 빼면
        # 숫자는 좋아지지만 사각지대를 감춘다 — 그것이 바로 이 감사가 드러내야 하는
        # 대상이다. 대신 못 잡은 이유를 아래 breakdown 으로 노출한다
        # (``_classify_uncaught`` docstring 참조).
        "catch_rate_denominator": "applied",
        "uncaught_breakdown": uncaught_breakdown,
        # 가드가 눈먼 것은 검출이 아니므로 catch_rate 에 넣지 않는다. 다만 위반보다
        # 나쁠 수 있어 별 축으로 보고한다 (``_blinded_checks`` docstring).
        "blinded": blinded,
        "blinded_checks_union": sorted({
            name for m in mutants_out for name in (m.get("blinded_checks") or [])
        }),
        "baseline_failed_checks": baseline_failed,
        "baseline_failed_count": len(baseline_failed),
        "measurable_checks": len(baseline_summary) - len(baseline_failed),
        "baseline_cost_s": round(baseline_cost_s, 3),
        "budget_s": round(budget_s, 1),
        "budget_derived": derived_budget,
        "duration_s": round(time.monotonic() - start, 3),
        "mutants": mutants_out,
    }
    if not applied:
        logger.warning(
            "S9.5: %d개 mutant 중 0개만 주입됐다 — 검출률은 측정되지 않았다 (skipped: %s)",
            len(sampled), skipped,
        )
    elif baseline_failed:
        logger.warning(
            "S9.5: 체크 %d/%d 가 baseline FAIL 이라 검출을 등록할 수 없다 "
            "(FAIL→FAIL 은 변화 없음으로 읽힌다) — 검출률 %s%% 를 검증기 성능으로 "
            "읽지 말라. baseline FAIL: %s",
            len(baseline_failed), len(baseline_summary),
            summary["catch_rate_pct"], baseline_failed,
        )
    if uncaught_breakdown:
        logger.info(
            "S9.5: 못 잡은 mutant 갈래 %s — ``detector_gap`` 이 조치 대상이다 "
            "(나머지는 표적이 A-Box 에 없거나 공리를 제거만 해서 인스턴스 위반이 "
            "원리상 불가). 분모는 applied 그대로 둔다 — 빼면 사각지대를 감춘다",
            uncaught_breakdown,
        )
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "kg.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
    return summary


# ---------------------------------------------------------------------------
# MCP tool surface (S4.5 / S9.5)
# ---------------------------------------------------------------------------


def run_tbox_mutation_audit() -> str:
    """S4.5: Apply all T-Box mutations and record validator-catch deltas.

    WARN-only. Never fails the pipeline. Output: data/generated/meta_audit/runs/<ts>/tbox.json
    Budget: 4-12 min, scales with the number of mutants actually applied
    (measured duration_s: 240-454 at 6-7 applied, 637-729 at 16-17).
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = os.path.join(GENERATED_DIR, "meta_audit", "runs", ts)
    try:
        summary = run_tbox_mutations(TBOX_PATH, out_dir=out_dir)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"status": "ERROR", "error": str(e)}, ensure_ascii=False)

    from tools.pipeline_state import save_step
    save_step("S4_5_MUTATION", {
        "total": summary["total_mutants"],
        "applied": summary["applied"],
        "caught_at_least_once": sum(1 for m in summary["mutants"] if m.get("caught_by")),
        "artifact": os.path.join(out_dir, "tbox.json"),
    }, duration_seconds=summary["duration_s"])
    return json.dumps({"status": "WARN_ONLY", **summary}, ensure_ascii=False, indent=2)


def run_kg_mutation_audit(sample_size: int = 8) -> str:
    """S9.5: Apply sampled KG-propagating mutations, rerun validate_kg.

    WARN-only. Budget: 7-8 min — one baseline validate_kg costs ~65s and the budget
    is derived from it, so wall-clock scales with mutant count (measured 448-450s).
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = os.path.join(GENERATED_DIR, "meta_audit", "runs", ts)
    try:
        summary = run_kg_mutations(TBOX_PATH, ABOX_PATH,
                                   sample_size=sample_size, out_dir=out_dir)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"status": "ERROR", "error": str(e)}, ensure_ascii=False)

    from tools.pipeline_state import save_step
    save_step("S9_5_KG_MUTATION", {
        "sample_size": summary["sample_size"],
        # applied first: a catch count without it cannot distinguish a blind
        # spot from an un-planted defect.
        "applied": summary["applied"],
        "skipped": summary["skipped"],
        "caught_at_least_once": summary["caught"],
        "catch_rate_pct": summary["catch_rate_pct"],
        # Without these the rate is unreadable: a check that is already FAIL
        # cannot register a catch, so the denominator overstates what the audit
        # could possibly have measured.
        "baseline_failed_count": summary["baseline_failed_count"],
        "measurable_checks": summary["measurable_checks"],
        "artifact": os.path.join(out_dir, "kg.json"),
    }, duration_seconds=summary["duration_s"])
    return json.dumps({"status": "WARN_ONLY", **summary}, ensure_ascii=False, indent=2)
