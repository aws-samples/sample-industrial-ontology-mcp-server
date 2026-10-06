"""SWRL 규칙 추론 모듈 (작업코드 I4 — docs/reference/task-glossary.md).

Pellet (via owlready2) 로 SWRL 규칙을 실행. opt-in (SWRL_ENABLED=true).
기본 S8 OWL RL 추론 이후 추가 실행되는 S8.5 단계.

설계 메모
========

원래 plan 의 "verbose TTL + swrl:Imp bnode" 접근은 Pellet 이 Jena N-Triples
변환 단계에서 "is not a URI node" 로 거부한다 (bnode 를 변수로 쓰기 때문).

그래서 pivot:
- 사용자는 `.swrl` 텍스트 파일에 **owlready2 DL 문법** 으로 규칙을 쓴다.
  예: `steel:Equipment(?e) ^ steel:hasAlarm(?e, ?a) -> steel:Warning(?e)`
- `Imp().set_as_rule(line)` 으로 로드 → Pellet 이 정상 처리.

장점: 1) 읽기/쓰기 쉬움, 2) bnode 직렬화 함정 회피, 3) owlready2 네이티브 API.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from rdflib import Graph

from config import INFERRED_PATH, PROJECT_ROOT
from tools.common import atomic_write, error_response, resolve_path_within, success_response

logger = logging.getLogger(__name__)


SWRL_DIR_DEFAULT = os.path.join(PROJECT_ROOT, "rules", "swrl")
SWRL_EXT = ".swrl"


# ── SWRL rule 텍스트 파서 ────────────────────────────


def _parse_swrl_line(line: str, fallback_label: str) -> dict | None:
    """한 줄 텍스트 → {label, dl, source} dict. 실패 시 None.

    지원 포맷:
        "label: body -> head"
        "body -> head"  (label 은 fallback_label)

    '->' 가 없으면 None 반환 (graceful skip).
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "->" not in line:
        return None

    if ":" in line.split("->")[0]:
        # label: body -> head 형태 (단, ':' 이 URI prefix 일 수도 있으므로 주의)
        # "label :" 뒤 공백으로 식별 — 규칙 body 내부의 prefix 는 '<단어>:<단어>' 로 연속.
        # 간단 휴리스틱: 줄 맨 앞부터 첫 ': ' (콜론+공백) 을 라벨 구분자로 간주.
        idx = line.find(": ")
        if idx > 0 and idx < line.find("->"):
            candidate_label = line[:idx].strip()
            # label 은 [A-Za-z0-9_] 만 허용. prefix URI (steel:...) 면 label 아님.
            if candidate_label.replace("_", "").replace("-", "").isalnum():
                return {
                    "label": candidate_label,
                    "dl": line[idx + 2:].strip(),
                    "source": fallback_label,
                }
    # fallback: 라벨 없음
    return {"label": fallback_label, "dl": line, "source": fallback_label}


def _resolve_swrl_dir(swrl_dir: str) -> str:
    """공개 도구가 받은 SWRL 디렉터리를 SWRL_DIR_DEFAULT 자신 또는 그 하위로 해석한다.

    symlink 해석 후 기준 디렉터리 밖이면 ValueError 를 낸다. ``resolve_path_within``
    은 기준 디렉터리 자체를 거부하므로 그 경우만 먼저 받는다.
    """
    if not isinstance(swrl_dir, str) or not swrl_dir.strip():
        raise ValueError("swrl_dir 는 비어 있을 수 없습니다.")
    base = os.path.realpath(SWRL_DIR_DEFAULT)
    if os.path.realpath(swrl_dir.strip()) == base:
        return base
    return resolve_path_within(SWRL_DIR_DEFAULT, swrl_dir)


def _load_swrl_rules(swrl_dir: str) -> list[dict]:
    """swrl_dir 의 *.swrl 파일 전부 → [{label, dl, source}, ...].

    symlink 해석 후 swrl_dir 밖을 가리키는 파일은 읽지 않는다.

    Returns:
        빈 리스트 if dir 이 없거나 .swrl 파일이 없음.
    """
    rules: list[dict] = []
    if not swrl_dir or not os.path.isdir(swrl_dir):
        return rules

    for ttl in sorted(glob.glob(os.path.join(swrl_dir, f"*{SWRL_EXT}"))):
        try:
            resolve_path_within(swrl_dir, ttl, allowed_suffixes=(SWRL_EXT,))
        except ValueError:
            logger.warning("SWRL 디렉터리 밖을 가리키는 파일을 건너뛴다: %s", ttl)
            continue
        try:
            fname = Path(ttl).stem
            with open(ttl, encoding="utf-8") as f:
                for i, raw in enumerate(f, start=1):
                    fallback = f"{fname}_L{i}"
                    parsed = _parse_swrl_line(raw, fallback)
                    if parsed is not None:
                        rules.append(parsed)
        except OSError as exc:
            logger.warning("SWRL 파일 읽기 실패 %s: %s", ttl, exc)
    return rules


def _get_swrl_mtime(swrl_dir: str = "") -> float:
    """swrl_dir 의 모든 .swrl 파일 중 가장 최신 mtime 반환. 없으면 0."""
    swrl_dir = swrl_dir or SWRL_DIR_DEFAULT
    if not os.path.isdir(swrl_dir):
        return 0.0
    try:
        paths = glob.glob(os.path.join(swrl_dir, f"*{SWRL_EXT}"))
        if not paths:
            return 0.0
        return max(os.path.getmtime(p) for p in paths)
    except OSError:
        return 0.0


# ── owlready2 + Pellet 실행 ─────────────────────────


def _normalize_dl_for_owlready(dl: str, domain_prefixes: dict[str, str]) -> str:
    """DL 문법 내 "prefix:localname" 을 owlready2 가 인식하도록 정규화한다.

    owlready2 의 `Imp.set_as_rule()` 내부 `_find_entity` 는 prefix: 문법을
    world 의 `_prefix_2_base_iri` 맵으로 해석하려 하는데, rdflib 로 로드한
    ontology 는 prefix 정보를 owlready2 world 에 등록하지 않는다.

    해결: **도메인 prefix (default 는 onto.base_iri 와 동일) 를 벗긴 local
    name 만 남긴다.** owlready2 는 `namespace[name]` = base_iri + name 으로
    lookup 하므로 prefix 없는 이름이 정상 해석된다.

    Args:
        dl: DL 문법 rule 문자열.
        domain_prefixes: {"steel": "http://example.com/steel-ontology#", ...} —
                         값이 ontology base_iri 와 같으면 prefix 를 제거한다.

    Returns:
        prefix 가 정규화된 rule 문자열.
    """
    import re
    out = dl
    for prefix in domain_prefixes:
        # "steel:LocalName" → "LocalName"
        pattern = re.compile(rf"\b{re.escape(prefix)}:([A-Za-z_][A-Za-z0-9_]*)\b")
        out = pattern.sub(r"\1", out)
    return out


#: SWRL built-ins are not ontology entities, so they never indicate a namespace.
_SWRL_BUILTINS = frozenset({
    "DifferentFrom", "SameAs", "greaterThan", "greaterThanOrEqual",
    "lessThan", "lessThanOrEqual", "equal", "notEqual", "add", "subtract",
    "multiply", "divide", "abs", "stringConcat", "contains", "matches",
})


def _referenced_names(normalized_rules: list[str]) -> tuple[str, ...]:
    """규칙이 술어로 쓰는 로컬명을 빌트인을 빼고 등장 순서대로 돌려준다.

    :func:`_rule_context` 가 이 이름으로 어느 네임스페이스가 실제로 해석하는지 프로브한다.
    IRI 문자열만 보고 네임스페이스를 고르면 다른 NS 를 쓰는 그래프를 깨기 때문이다.
    """
    import re
    seen: list[str] = []
    for dl in normalized_rules:
        for name in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", dl):
            if name not in _SWRL_BUILTINS and name not in seen:
                seen.append(name)
    return tuple(seen)


def _rule_context(loaded, domain_ns: str, probe_names: tuple[str, ...] = ()):
    """규칙의 맨 로컬명을 해석할 컨텍스트를 돌려준다.

    ``set_as_rule`` 은 맨 로컬명을 감싸는 컨텍스트의 네임스페이스에서 찾는다. 로드한
    온톨로지를 컨텍스트로 쓰면 그 ``base_iri`` 가 기준이 되므로, ``base_iri`` 가 엔티티가
    실제로 속한 네임스페이스와 다르면 규칙이 첫 원자에서 "Cannot find entity" 로 실패한다.

    배포 설정의 T-Box 는 ``owl:sameAs`` alias 를 선언하지 않는다. 그래도 이 교정이 필요한
    이유는 추론 그래프에 owl:Ontology 노드가 여럿이기 때문이다. T-Box 의 ``ONTOLOGY_URI``
    와 A-Box 생성기가 선언하는 ``{ONTOLOGY_URI}/abox`` 가 함께 있고, owlready2 로더는
    그중 하나를 ``base_iri`` 로 잡는다. 두 노드를 함께 로드하면 ``…/abox#`` 가 잡힐 수
    있고, 그러면 ``EquipmentMaster`` 같은 T-Box 클래스가 그 온톨로지 속성으로 조회되지
    않는다. 도메인 설정이 ``metadata.persistent_iri`` 를 선언하면 step_07 이
    ``owl:sameAs`` 를 추가하고, OWL RL 이 이를 전파해 alias IRI 도 owl:Ontology 노드가
    되므로 후보가 하나 더 는다. 온톨로지 노드를 지워 로더의 선택을 바꾸는 방식은 남은
    노드 중 다른 하나가 잡힐 뿐이다.

    owlready2 는 엔티티를 온톨로지가 아니라 **네임스페이스**에 담고, 그 네임스페이스는
    엔티티 IRI 를 따른다. 그래서 온톨로지를 갈아타는 대신 **네임스페이스를 컨텍스트로
    준다**. 다음 두 방식은 쓰지 않는다.

    * ``get_ontology(DOMAIN_NS)``: 새 빈 온톨로지라 ``classes()`` 가 0 이어서 판별할 수 없다.
    * 무조건 ``get_namespace(DOMAIN_NS)``: 다른 NS 를 쓰는 그래프(``http://test.org/onto#``
      테스트 픽스처, 타 도메인 이식본)의 이름 해석을 깬다.

    그러므로 **엔티티가 실제로 어디서 해석되는지 확인하고** 고른다. 로더의 ``base_iri``
    로 풀리면 그대로 두고, 안 풀리는데 DOMAIN_NS 로 풀리면 그쪽 네임스페이스를 쓴다.
    """
    if str(loaded.base_iri).rstrip("#/") == domain_ns.rstrip("#/"):
        return loaded

    domain_ns_obj = loaded.get_namespace(domain_ns)
    for probe in probe_names:
        if not probe:
            continue
        # 로더의 네임스페이스가 이미 푸는 이름이면 옮기지 않는다.
        if getattr(loaded, probe, None) is not None:
            return loaded
        if getattr(domain_ns_obj, probe, None) is not None:
            return domain_ns_obj
    return loaded


def _run_pellet_on_graph(
    base_graph: Graph,
    rules: list[dict],
) -> tuple[list[tuple], list[dict]]:
    """base_graph 에 rules 를 적용, Pellet 실행 후 new triples + errors 반환.

    **주의** — owlready2 의 default_world 는 mutable 전역. 본 함수는 실행 전
    기존 상태를 cleanup 하고, 실행 후도 다른 호출에 영향 없도록 cleanup 한다.

    Args:
        base_graph: 기존 추론 결과 (all_inferred.ttl) 로드된 rdflib Graph.
        rules: _load_swrl_rules 반환값.

    Returns:
        (new_triples, errors):
            new_triples: [(s, p, o), ...] Pellet 이 추가로 도출한 triple.
            errors: [{"label": ..., "error": ...}] 규칙별 로드 실패.
    """
    # 늦은 import — owl_reasoner 의 _cleanup_world / _ttl_to_owlready 재사용.
    from tools.owl_reasoner import _cleanup_world, _safe_unlink, _ttl_to_owlready

    _cleanup_world()
    tmp_path = None
    try:
        from owlready2 import Imp, default_world, sync_reasoner_pellet

        # base graph → owlready2 Ontology
        ttl_str = base_graph.serialize(format="turtle")
        onto, tmp_path = _ttl_to_owlready(ttl_str)

        # Domain prefix map — 도메인 IRI prefix 벗김용 (set_as_rule 이 local name 을 기대).
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        domain_prefixes = {NS_PREFIX: DOMAIN_NS}

        # 로컬명은 엔티티가 실제로 있는 네임스페이스에서 푼다. 추론 그래프의 owl:Ontology
        # 노드가 여럿이라 로더 base_iri 가 DOMAIN_NS 와 다를 수 있다 (_rule_context 참조).
        # 규칙이 참조하는 이름으로 프로브해 IRI 문자열만으로 고르지 않는다.
        normalized_rules = [
            _normalize_dl_for_owlready(rule["dl"], domain_prefixes) for rule in rules
        ]
        rule_ctx = _rule_context(onto, DOMAIN_NS, _referenced_names(normalized_rules))
        if rule_ctx is not onto:
            logger.info(
                "SWRL 규칙 이름해석 컨텍스트 교정: 로더 base_iri=%s → 도메인 NS=%s",
                onto.base_iri, DOMAIN_NS,
            )

        # Load each SWRL rule via set_as_rule (prefix-normalized)
        errors: list[dict] = []
        loaded = 0
        with rule_ctx:
            for rule, normalized in zip(rules, normalized_rules, strict=True):
                try:
                    imp = Imp()
                    imp.set_as_rule(normalized)
                    loaded += 1
                except Exception as exc:  # set_as_rule raises ValueError for unknown entities
                    errors.append({
                        "label": rule.get("label"),
                        "error": str(exc),
                        "dl": rule.get("dl"),
                        "normalized": normalized,
                    })

        if loaded == 0:
            logger.warning("로드된 SWRL 규칙 0개 (errors=%d), Pellet 건너뜀", len(errors))
            return [], errors

        # Snapshot pre-Pellet graph
        pre_g = default_world.as_rdflib_graph()
        pre_triples = set(pre_g)

        # Run Pellet — same ontology the rules were attached to, so the Imp
        # individuals are in scope.
        with rule_ctx:
            sync_reasoner_pellet(infer_property_values=True, debug=0)

        # Snapshot post-Pellet graph
        post_g = default_world.as_rdflib_graph()
        new_triples = [t for t in post_g if t not in pre_triples]

        return new_triples, errors
    finally:
        _safe_unlink(tmp_path)
        # cleanup_world defense — ensure shared default_world doesn't leak state.
        try:  # noqa: SIM105  (pre-existing; suppress() would hide the failure site)
            _cleanup_world()
        except Exception:
            pass


def _filter_domain_triples(new_triples: list[tuple], ontology_iri: str) -> list[tuple]:
    """Pellet 결과에서 도메인 온톨로지 IRI 에 속하는 triple 만 필터링.

    owlready2 + Pellet 은 owl/rdf 스키마 triple 도 함께 생성하므로,
    사용자가 의미 있다고 생각하는 '도메인 파생' 만 골라내려면 필터가 필요.

    Note: startswith 사용 (substring match 아님) — 유사 prefix IRI
    (예: `steel-ontology-extra#X`) 의 오탐 방지.
    """
    filtered = []
    for s, p, o in new_triples:
        s_str, p_str = str(s), str(p)
        # s 또는 p 가 도메인 IRI prefix 로 시작하면 포함
        if s_str.startswith(ontology_iri) or p_str.startswith(ontology_iri):
            filtered.append((s, p, o))
    return filtered


# ── I2 justification 통합 ───────────────────────────


def _write_swrl_justification(
    new_triples: list[tuple],
    rules: list[dict],
    inferred_dir: str,
) -> dict:
    """SWRL 로 도출된 triple 을 inference_provenance.ttl 에 reify 하고,
    inference_justifications.json 에도 append.

    Non-blocking: I2 파일이 없어도 실패하지 않음 (빈 dict 반환).

    Returns:
        {"provenance_triples": N, "justifications_added": M}
    """
    result = {"provenance_triples": 0, "justifications_added": 0}
    if not new_triples or not rules:
        return result

    try:
        from rdflib import URIRef

        from tools.provenance import build_per_triple_reification

        # SWRL 은 규칙별 precise matching 이 어려움 — 단일 "swrl:all" 태그로 처리하거나
        # 규칙 라벨을 derivedByRule 에 넣는 정책 중 선택. 현재는 단일 규칙 집계.
        rule_label = f"swrl:{','.join(r['label'] for r in rules[:3])}{'...' if len(rules) > 3 else ''}"
        justifications = [{
            "triple": t,
            "rule": rule_label,
            "confidence": "swrl_derived",
            "prerequisites": [f"SWRL rule: {r['label']}" for r in rules],
        } for t in new_triples]

        activity = URIRef(
            f"urn:activity:swrl_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
        )
        reif_g = build_per_triple_reification(
            justifications, pre_graph=None, tbox=None, activity_uri=activity,
        )
        result["provenance_triples"] = len(reif_g)

        # Merge with existing inference_provenance.ttl
        prov_path = os.path.join(inferred_dir, "inference_provenance.ttl")
        existing_prov = Graph()
        if os.path.exists(prov_path):
            try:
                existing_prov.parse(prov_path, format="turtle")
            except Exception as exc:
                logger.warning("기존 inference_provenance.ttl 파싱 실패: %s", exc)

        existing_prov += reif_g
        # Atomic write (concurrent-safe, 다른 sidecar 패턴과 일관)
        from tools.common import atomic_write
        atomic_write(prov_path, existing_prov.serialize(format="turtle"))

        # Append to inference_justifications.json if exists
        just_path = os.path.join(inferred_dir, "inference_justifications.json")
        if os.path.exists(just_path):
            try:
                with open(just_path, encoding="utf-8") as f:
                    data = json.load(f)
                # minimal: add swrl block
                swrl_entry = {
                    "rule_label": rule_label,
                    "triples_count": len(new_triples),
                    "sample": [
                        {"s": str(t[0]), "p": str(t[1]), "o": str(t[2])}
                        for t in new_triples[:10]
                    ],
                    "generated_at": datetime.now().isoformat(),
                }
                data.setdefault("swrl", []).append(swrl_entry)
                atomic_write(just_path, json.dumps(data, ensure_ascii=False, indent=2))
                result["justifications_added"] = len(new_triples)
            except Exception as exc:
                logger.warning("inference_justifications.json append 실패: %s", exc)

    except Exception as exc:
        # I2 = 추론 근거 reification. 용어: docs/reference/task-glossary.md
        logger.warning("추론 근거(I2) justification 통합 실패 (non-blocking): %s", exc)

    return result


# ── MCP 도구: run_swrl_inference ────────────────────


def run_swrl_inference(
    swrl_dir: str = "",
    append_to_inferred: bool = True,
) -> str:
    """SWRL 규칙을 Pellet 으로 실행하는 opt-in S8.5 단계.

    예상 소요시간: 30초 ~ 3분 (A-Box 규모 + 규칙 수에 비례)

    전제 조건:
    - run_owl_rl_inference 가 먼저 실행되어 all_inferred.ttl 존재.
    - rules/swrl/ 디렉토리에 .swrl 텍스트 파일이 존재.
    - (선택) rules/swrl/tbox_extensions.ttl 에서 파생 클래스/프로퍼티 선언.

    활성화: export SWRL_ENABLED=true

    동작:
    1. rules/swrl/*.swrl 전부 로드 (owlready2 DL 문법)
    2. all_inferred.ttl 로드
    3. 각 규칙을 Imp().set_as_rule() 로 온톨로지에 추가
    4. sync_reasoner_pellet 실행
    5. 새 triple 추출 (도메인 IRI 필터링)
    6. (append_to_inferred=True 면) all_inferred.ttl 에 append
    7. I2 reification 및 justifications.json 통합 (non-blocking)

    Args:
        swrl_dir: SWRL 규칙 디렉토리 (기본: rules/swrl/). rules/swrl 자신이나 그
            하위 디렉터리만 받으며, symlink 해석 후 밖이면 거부한다.
        append_to_inferred: True 면 all_inferred.ttl 에 append (기본).

    Returns:
        {
          "success": bool,
          "skipped": bool,            # SWRL_ENABLED 미설정 시 True
          "rules_loaded": int,
          "rules_errors": [...],      # set_as_rule 실패 규칙
          "triples_inferred": int,
          "new_triples_sample": [...],# 최대 20
          "duration_seconds": float,
          "justifications_added": int,# I2 통합 결과
        }
    """
    # Early skip: opt-in 가드
    if os.getenv("SWRL_ENABLED", "false").lower() != "true":
        return success_response({
            "skipped": True,
            "reason": (
                "SWRL_ENABLED=false (default). 활성화: export SWRL_ENABLED=true"
            ),
        })

    try:
        start = time.monotonic()
        swrl_dir = _resolve_swrl_dir(swrl_dir) if swrl_dir else SWRL_DIR_DEFAULT

        # 1. Load SWRL rules
        rules = _load_swrl_rules(swrl_dir)
        if not rules:
            return success_response({
                "skipped": False,
                "rules_loaded": 0,
                "message": f"SWRL 규칙 없음 in {swrl_dir}",
            })

        # 2. Load existing inferred graph
        base_graph = Graph()
        if os.path.exists(INFERRED_PATH):
            try:
                base_graph.parse(INFERRED_PATH, format="turtle")
            except Exception as exc:
                return error_response(
                    f"INFERRED_PATH 로드 실패: {exc}",
                    hint="run_owl_rl_inference 를 먼저 실행하세요.",
                    logger=logger,
                )

        # Merge tbox_extensions.ttl if exists (for PotentialFailure/QualityViolation)
        ext_path = os.path.join(swrl_dir, "tbox_extensions.ttl")
        if os.path.exists(ext_path):
            try:
                ext_path = resolve_path_within(
                    swrl_dir, ext_path, allowed_suffixes=(".ttl",),
                )
            except ValueError:
                logger.warning("SWRL 디렉터리 밖을 가리키는 파일을 건너뛴다: %s", ext_path)
                ext_path = ""
        if ext_path and os.path.exists(ext_path):
            try:
                base_graph.parse(ext_path, format="turtle")
            except Exception as exc:
                logger.warning("tbox_extensions.ttl 로드 실패: %s", exc)

        # 3. Run Pellet
        try:
            new_triples, rule_errors = _run_pellet_on_graph(base_graph, rules)
        except Exception as exc:
            return error_response(
                f"Pellet 실행 실패: {exc}",
                hint=(
                    "Java 25+ + JAVA_EXE 환경변수 확인. "
                    "SWRL 문법 오류 가능성도 검토 (rules/swrl/*.swrl)."
                ),
                logger=logger,
            )

        # 4. Filter to domain triples
        from domain.namespaces import DOMAIN_NS
        ontology_iri = DOMAIN_NS.rstrip("#")
        domain_new = _filter_domain_triples(new_triples, ontology_iri)

        # 5. Append to all_inferred.ttl
        justification_info: dict = {}
        if append_to_inferred and domain_new:
            append_g = Graph()
            for t in domain_new:
                append_g.add(t)
            append_ttl = append_g.serialize(format="turtle")

            existing = ""
            if os.path.exists(INFERRED_PATH):
                with open(INFERRED_PATH, encoding="utf-8") as f:
                    existing = f.read()
            combined = existing + (
                "\n\n# --- SWRL I4 derived triples ---\n" + append_ttl
            )
            atomic_write(INFERRED_PATH, combined)

            # 6. I2 justification (non-blocking)
            justification_info = _write_swrl_justification(
                domain_new, rules,
                inferred_dir=os.path.dirname(INFERRED_PATH),
            )

        duration = round(time.monotonic() - start, 2)
        return success_response({
            "skipped": False,
            "rules_loaded": len(rules) - len(rule_errors),
            "rules_errors": rule_errors[:10],
            "rules_errors_count": len(rule_errors),
            "triples_inferred": len(domain_new),
            "triples_inferred_raw": len(new_triples),
            "new_triples_sample": [
                {"s": str(t[0]), "p": str(t[1]), "o": str(t[2])}
                for t in domain_new[:20]
            ],
            "duration_seconds": duration,
            "justifications_added": justification_info.get("justifications_added", 0),
            "provenance_triples": justification_info.get("provenance_triples", 0),
        })
    except Exception as exc:
        return error_response(exc, logger=logger)
