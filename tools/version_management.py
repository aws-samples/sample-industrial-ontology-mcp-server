"""T-Box 버전 관리 — #6.

owl:priorVersion, owl:deprecated 주석, CHANGELOG.ttl 자동 증분.

주요 책임:
- T-Box 저장 전 현재 파일을 baseline으로 두고 diff 산출 (클래스/프로퍼티 add/remove)
- 새 T-Box에 `owl:priorVersion <이전-versionIRI>` 및 `owl:versionInfo` 증분
- 제거된 클래스/프로퍼티는 삭제하지 않고 `owl:deprecated true`로 표기 옵션
- 변경 내역을 CHANGELOG.ttl (PROV-O Activity 형식)에 append

이 모듈은 idempotent — 동일 T-Box로 두 번 호출해도 CHANGELOG 엔트리가
중복되지 않도록 commit_hash 기반 dedup.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal as _L

from rdflib import OWL, RDF, XSD, Literal, Namespace, URIRef

from config import TBOX_BASELINE_PATH, TBOX_PATH
from domain.namespaces import DOMAIN_NS, NS_PREFIX, ONTOLOGY_URI
from domain.tbox_utils import _new_graph
from tools.common import atomic_write, error_response, success_response

logger = logging.getLogger(__name__)

PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")


def _diff_tbox(old_ttl: str, new_ttl: str) -> dict:
    """두 T-Box TTL 간 클래스/프로퍼티 add/remove/changed 산출.

    steel: 네임스페이스만 고려 (외부 라이브러리 클래스는 제외).

    Returns:
        {added_classes, removed_classes, added_ops, removed_ops,
         added_dps, removed_dps}
    """
    def _extract(ttl: str) -> tuple[set[str], set[str], set[str]]:
        g = _new_graph()
        if not ttl.strip():
            return set(), set(), set()
        g.parse(data=ttl, format="turtle")
        steel = str(DOMAIN_NS)
        classes: set[str] = set()
        ops: set[str] = set()
        dps: set[str] = set()
        for c in g.subjects(RDF.type, OWL.Class):
            if isinstance(c, URIRef) and str(c).startswith(steel):
                classes.add(str(c))
        for p in g.subjects(RDF.type, OWL.ObjectProperty):
            if isinstance(p, URIRef) and str(p).startswith(steel):
                ops.add(str(p))
        for p in g.subjects(RDF.type, OWL.DatatypeProperty):
            if isinstance(p, URIRef) and str(p).startswith(steel):
                dps.add(str(p))
        return classes, ops, dps

    old_c, old_op, old_dp = _extract(old_ttl)
    new_c, new_op, new_dp = _extract(new_ttl)
    return {
        "added_classes": sorted(new_c - old_c),
        "removed_classes": sorted(old_c - new_c),
        "added_ops": sorted(new_op - old_op),
        "removed_ops": sorted(old_op - new_op),
        "added_dps": sorted(new_dp - old_dp),
        "removed_dps": sorted(old_dp - new_dp),
    }


def _compute_commit_hash(ttl: str) -> str:
    """T-Box TTL의 안정 해시. 공백/개행 정규화 후 SHA-256 12자리."""
    normalized = "\n".join(
        line.strip() for line in ttl.splitlines() if line.strip()
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _bump_patch(version: str) -> str:
    """semver patch increment. 예: "1.2.3" → "1.2.4". Non-semver는 그대로."""
    parts = version.split(".")
    try:
        if len(parts) == 3:
            return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    except ValueError:
        pass
    return version


def apply_version_metadata(
    ttl: str,
    *,
    old_ttl: str = "",
    strategy: _L["patch", "minor", "major", "keep"] = "patch",
    mark_deprecated: bool = True,
) -> tuple[str, dict]:
    """새 T-Box에 version metadata + owl:priorVersion을 추가한다.

    Args:
        ttl: 새로 생성될 T-Box TTL 문자열.
        old_ttl: 이전 T-Box (baseline). 비어있으면 prior version 없음.
        strategy: 버전 증분 방식. "keep"은 메타데이터만 보강.
        mark_deprecated: 제거된 클래스/프로퍼티를 삭제 대신 owl:deprecated로 표기.

    Returns:
        (수정된 TTL, metadata dict).
    """
    g = _new_graph()
    g.parse(data=ttl, format="turtle")
    ont = URIRef(ONTOLOGY_URI)

    # 현재 버전 조회 또는 초기화
    curr_versions = list(g.objects(ont, OWL.versionInfo))
    curr = str(curr_versions[0]) if curr_versions else "0.1.0"

    # 이전 버전 (baseline)
    prior_version = ""
    if old_ttl.strip():
        prior_g = _new_graph()
        try:
            prior_g.parse(data=old_ttl, format="turtle")
            prior_versions = list(prior_g.objects(ont, OWL.versionInfo))
            prior_version = str(prior_versions[0]) if prior_versions else ""
        except Exception as e:
            logger.warning("baseline T-Box 파싱 실패: %s", e)

    # 새 버전 계산
    if strategy == "patch":
        new_version = _bump_patch(curr) if prior_version == curr else curr
    elif strategy == "keep":
        new_version = curr
    else:
        # minor/major: patch만 자동 적용, 나머지는 사용자 결정
        new_version = curr

    # versionInfo 교체
    for _s, _p, _o in list(g.triples((ont, OWL.versionInfo, None))):
        g.remove((_s, _p, _o))
    g.add((ont, OWL.versionInfo, Literal(new_version)))

    # versionIRI
    for _s, _p, _o in list(g.triples((ont, OWL.versionIRI, None))):
        g.remove((_s, _p, _o))
    g.add((ont, OWL.versionIRI, URIRef(f"{ONTOLOGY_URI}/{new_version}")))

    # priorVersion
    if prior_version:
        prior_uri = URIRef(f"{ONTOLOGY_URI}/{prior_version}")
        g.add((ont, OWL.priorVersion, prior_uri))

    # Deprecated 표기 (제거된 것들)
    diff = _diff_tbox(old_ttl, ttl) if old_ttl.strip() else None
    deprecated_marks = 0
    if diff and mark_deprecated:
        for removed_uri in (
            diff["removed_classes"] + diff["removed_ops"] + diff["removed_dps"]
        ):
            u = URIRef(removed_uri)
            # 이전 T-Box에서 선언을 가져와 복원 + deprecated=true
            try:
                prior_g = _new_graph()
                prior_g.parse(data=old_ttl, format="turtle")
                for _p, _o in prior_g.predicate_objects(u):
                    g.add((u, _p, _o))
                g.add((u, OWL.deprecated, Literal(True)))
                deprecated_marks += 1
            except Exception as _re:
                logger.debug("deprecated 복원 실패 %s: %s", removed_uri, _re)

    # 변경 해시 태그 (commit hash). 기존 identifier 제거 후 교체.
    for _s, _p, _o in list(g.triples((ont, DCTERMS.identifier, None))):
        g.remove((_s, _p, _o))
    commit_hash = _compute_commit_hash(ttl)
    g.add((ont, DCTERMS.identifier, Literal(f"sha256:{commit_hash}")))

    # Provenance timestamps.
    # dcterms:issued is the first-published date — preserve if already set,
    # otherwise stamp now. dcterms:modified is always refreshed to reflect
    # the latest bump.
    now_ts = Literal(
        datetime.now(tz=UTC).isoformat(), datatype=XSD.dateTime,
    )
    existing_issued = list(g.objects(ont, DCTERMS.issued))
    if not existing_issued:
        g.add((ont, DCTERMS.issued, now_ts))
    for _s, _p, _o in list(g.triples((ont, DCTERMS.modified, None))):
        g.remove((_s, _p, _o))
    g.add((ont, DCTERMS.modified, now_ts))

    # prov:wasDerivedFrom — link this version's ontology node to its prior
    # versionIRI. Only meaningful when we actually had a baseline to diff
    # against, so the annotation is skipped on the very first bump.
    if prior_version:
        prior_uri = URIRef(f"{ONTOLOGY_URI}/{prior_version}")
        g.add((ont, PROV.wasDerivedFrom, prior_uri))

    metadata = {
        "prior_version": prior_version,
        "new_version": new_version,
        "commit_hash": commit_hash,
        "deprecated_marks": deprecated_marks,
        "diff": diff,
    }
    return g.serialize(format="turtle"), metadata


def _append_changelog(
    metadata: dict, *, changelog_path: str | None = None,
) -> str:
    """CHANGELOG.ttl에 버전 변경 Activity를 append한다. 중복은 commit_hash로 방지.

    Returns:
        changelog 경로.
    """
    path = changelog_path or os.path.join(
        os.path.dirname(TBOX_PATH), "CHANGELOG.ttl",
    )
    commit = metadata.get("commit_hash", "unknown")
    diff = metadata.get("diff") or {}

    # 기존 changelog 로드 및 중복 체크
    g = _new_graph()
    g.bind("prov", PROV)
    g.bind("dcterms", DCTERMS)
    g.bind(NS_PREFIX, str(DOMAIN_NS))
    if os.path.exists(path):
        try:
            g.parse(path, format="turtle")
        except Exception as _pe:
            logger.warning("기존 CHANGELOG 파싱 실패 (새로 시작): %s", _pe)
            g = _new_graph()
            g.bind("prov", PROV)
            g.bind("dcterms", DCTERMS)

    activity_uri = URIRef(f"{DOMAIN_NS}changelog/commit_{commit}")
    if (activity_uri, RDF.type, PROV.Activity) in g:
        return path  # 이미 기록된 commit

    # 신규 Activity 생성
    g.add((activity_uri, RDF.type, PROV.Activity))
    g.add((activity_uri, DCTERMS.identifier, Literal(f"sha256:{commit}")))
    g.add((activity_uri, PROV.startedAtTime,
           Literal(datetime.now(tz=UTC).isoformat())))
    g.add((activity_uri, DCTERMS.description,
           Literal(
               f"T-Box {metadata.get('prior_version', '-')} "
               f"→ {metadata.get('new_version', '-')}",
               lang="ko",
           )))
    # Diff 개수 요약
    for key in ("added_classes", "removed_classes",
                "added_ops", "removed_ops",
                "added_dps", "removed_dps"):
        cnt = len(diff.get(key, []))
        if cnt > 0:
            g.add((activity_uri, URIRef(f"{DOMAIN_NS}changelog/{key}"),
                   Literal(cnt)))

    atomic_write(path, g.serialize(format="turtle"))
    return path


def bump_tbox_version(strategy: str = "patch") -> str:
    """로컬 T-Box와 CHANGELOG 파일을 수정해 버전 증분을 적용한다 (#6).

    Args:
        strategy: "patch" (1.0.0 → 1.0.1), "keep" (메타데이터만 보강),
                  "minor"/"major" (수동 커밋).

    Side effects:
        - TBOX_PATH 파일에 새 메타데이터 inline 업데이트.
        - CHANGELOG.ttl에 PROV:Activity append (commit hash 중복 방지).
        - 제거된 클래스는 deprecated=true로 표기해 하위 호환 유지.
    """
    try:
        if not os.path.exists(TBOX_PATH):
            return error_response(
                f"T-Box 파일이 없습니다: {TBOX_PATH}",
                hint="generate_tbox로 먼저 생성하세요.",
                logger=logger,
            )
        new_ttl = Path(TBOX_PATH).read_text(encoding="utf-8")
        old_ttl = ""
        if os.path.exists(TBOX_BASELINE_PATH):
            old_ttl = Path(TBOX_BASELINE_PATH).read_text(encoding="utf-8")
        if strategy not in ("patch", "keep", "minor", "major"):
            return error_response(
                f"strategy는 patch|keep|minor|major 중 하나여야 합니다: {strategy}",
                logger=logger,
            )
        new_ttl2, meta = apply_version_metadata(
            new_ttl, old_ttl=old_ttl, strategy=strategy,  # type: ignore[arg-type]
        )
        atomic_write(TBOX_PATH, new_ttl2)
        cl_path = _append_changelog(meta)
        return success_response({
            **meta,
            "tbox_path": TBOX_PATH,
            "changelog_path": cl_path,
        })
    except Exception as e:
        return error_response(e, logger=logger)
