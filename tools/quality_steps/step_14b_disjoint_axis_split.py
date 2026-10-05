"""Step 14b — 직교 축이 섞인 AllDisjointClasses 그룹을 분리한다.

S2 가 **서로 직교하는 두 분류 축** 을 한 ``owl:AllDisjointClasses`` 에 섞어 넣으면
두 축에 동시 소속인 클래스가 전부 unsatisfiable 이 된다. 2026-08-19 실측
(S2 산출물 → S3 후):

    ['MasterData', 'TransactionRecord', 'EnvironmentalMonitoring',
     'EnergyManagement', 'QualityManagement', 'EquipmentManagement']

- 축 A (데이터 성격): ``MasterData`` / ``TransactionRecord``
- 축 B (업무 도메인): ``EquipmentManagement`` / ``QualityManagement`` /
  ``EnergyManagement`` / ``EnvironmentalMonitoring``

``EquipmentMaster`` 가 "설비 도메인의 마스터 데이터" 인 것은 **정당한 다중상속**
인데, 위 그룹이 그것을 금지해 HermiT 가 unsat 6건을 냈다 (``EquipmentMaster`` /
``TagMaster`` / ``EquipmentStatus`` / ``RealTimeData`` / ``FailureCause`` /
``ItemSupplierMap``). A-Box 는 unsat 클래스의 인스턴스를 만들 수 없으므로 해당
CSV 테이블이 KG 에서 통째로 사라진다.

**왜 기존 가드로 안 잡히나**: ``step_12`` / ``step_14`` 의
``would_violate_disjoint`` 는 **자기가 새로 추가하는 부모** 만 검사한다 (그 주석에
적힌 2026-08-11 사고의 교훈). S2 가 **이미 만들어 놓은** 충돌은 아무도 정리하지
않는다 — 생성 지점 가드만으로는 부족하고 사후 정리가 한 벌 더 필요하다는, 이
리포에서 반복된 패턴이다.

**무엇을 고치는가 (그룹 vs 계층)**: 다중상속을 끊지 않는다. 계층은 도메인적으로
옳고 (RR/DIT 개선의 근거이기도 하다), 잘못된 쪽은 두 축을 한 그룹에 넣은 disjoint
선언이다. 그래서 **그룹에서 멤버를 덜어내** 축을 분리한다.

**실행 위치**: ``step_14`` (서브그룹 생성) **뒤** 여야 한다. ``step_12`` /
``step_14`` 가 중간 추상 클래스와 서브그룹을 추가해 계층을 바꾸므로, 그 전에 돌면
확정되지 않은 계층을 보고 판정한다. 계층이 최종 상태일 때 남아 있는 충돌이 곧
HermiT 가 낼 unsat 이다.

**보수적 판정 3중 조건** — 아래를 모두 만족하는 멤버만 덜어낸다:
 1. 그 그룹이 실제로 unsat 을 유발한다 (두 멤버가 어떤 클래스의 공통 조상).
 2. 덜어낼 멤버가 config ``DISJOINT_GROUPS`` 에 **근거가 없다** (S2 자유 생성분).
 3. 덜어낸 뒤 그룹에 멤버가 2개 이상 남는다 (OWL 2 DL 은 최소 2개를 요구 —
    1개 이하로 줄면 노드째 제거).

config 에 근거가 있는 멤버는 절대 건드리지 않는다. SME 가 승인한 disjoint 를
"unsat 이 사라진다" 는 이유로 지우는 것은 지표 매수이고, 그러면 진짜 모델링 오류가
조용히 묻힌다.
"""
from __future__ import annotations

import logging

import rdflib
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def _local(node) -> str:
    return str(node).split("#")[-1].split("/")[-1]


def _ancestors(g: Graph, node, seen: frozenset = frozenset()) -> set[URIRef]:
    """``node`` 의 모든 조상 (자신 제외). 순환은 seen 으로 차단."""
    out: set[URIRef] = set()
    for parent in g.objects(node, RDFS.subClassOf):
        if not isinstance(parent, URIRef) or parent in seen:
            continue
        out.add(parent)
        out |= _ancestors(g, parent, seen | {parent})
    return out


def _read_members(g: Graph, bnode) -> tuple[object | None, list[URIRef]]:
    """``owl:members`` 리스트 노드와 멤버 URI 목록을 반환."""
    lists = list(g.objects(bnode, OWL.members))
    if not lists:
        return None, []
    try:
        members = [
            m for m in rdflib.collection.Collection(g, lists[0])
            if isinstance(m, URIRef)
        ]
    except Exception as exc:  # noqa: BLE001 — 판정 불가 시 손대지 않는다
        logger.debug("Step 14b: members 읽기 실패 (건너뜀): %s", exc)
        return lists[0], []
    return lists[0], members


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import DISJOINT_GROUPS

    before = len(g)

    # config 에 근거가 있는 클래스 — 절대 덜어내지 않는다.
    config_backed: set[str] = set()
    for group in DISJOINT_GROUPS:
        config_backed |= set(group)

    steel_str = ctx.domain_ns
    named_classes = [
        c for c in set(g.subjects(RDF.type, OWL.Class))
        if isinstance(c, URIRef) and str(c).startswith(steel_str)
    ]

    # 각 클래스의 조상 집합 (자신 포함) — 어떤 그룹이 unsat 을 만드는지 판정용.
    ancestry: dict[URIRef, set[URIRef]] = {
        c: _ancestors(g, c) | {c} for c in named_classes
    }

    members_removed = 0
    groups_split = 0
    groups_dropped = 0
    samples: list[str] = []
    unsat_classes_freed: set[str] = set()

    for bnode in list(g.subjects(RDF.type, OWL.AllDisjointClasses)):
        members_node, members = _read_members(g, bnode)
        if len(members) < 2:
            continue

        member_set = set(members)
        # 이 그룹 때문에 unsat 이 되는 클래스와, 그 클래스가 물고 있는 멤버쌍.
        offending: dict[URIRef, int] = {}   # 멤버 → 관여한 unsat 클래스 수
        victims: set[URIRef] = set()
        for cls, anc in ancestry.items():
            hit = anc & member_set
            if len(hit) >= 2:
                victims.add(cls)
                for m in hit:
                    offending[m] = offending.get(m, 0) + 1
        if not victims:
            continue

        # 덜어낼 후보: config 근거가 없는 멤버 (관여도 높은 순).
        removable = sorted(
            (m for m in offending if _local(m) not in config_backed),
            key=lambda m: (-offending[m], _local(m)),
        )
        if not removable:
            # 전부 config 근거가 있다 → 진짜 모델링 충돌이다. 손대지 않고 알린다.
            logger.warning(
                "Step 14b: disjoint 그룹 %s 가 unsat 을 유발하지만 멤버 전부 config "
                "근거가 있다 — 자동 수정하지 않는다. 영향 클래스: %s. "
                "rules/domain/domain_config.json 의 disjoint 그룹 또는 계층을 SME 가 "
                "판단해야 한다.",
                sorted(_local(m) for m in member_set),
                sorted(_local(c) for c in victims),
            )
            continue

        # 하나씩 덜어내며 이 그룹이 더 이상 unsat 을 만들지 않을 때까지.
        kept = set(member_set)
        removed_here: list[URIRef] = []
        for cand in removable:
            still = any(
                len(ancestry[c] & kept) >= 2 for c in victims
            )
            if not still:
                break
            kept.discard(cand)
            removed_here.append(cand)

        if not removed_here:
            continue

        # OWL 2 DL: DisjointClasses 는 멤버 2개 이상. 미달이면 노드째 제거.
        if len(kept) < 2:
            if members_node is not None:
                try:
                    rdflib.collection.Collection(g, members_node).clear()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Step 14b: collection clear 실패: %s", exc)
            for t in list(g.triples((bnode, None, None))):
                g.remove(t)
            groups_dropped += 1
            members_removed += len(removed_here)
            samples.append(
                f"{sorted(_local(m) for m in member_set)} → 노드 제거 "
                f"(멤버 2개 미달)"
            )
        else:
            try:
                coll = rdflib.collection.Collection(g, members_node)
                coll.clear()
                for m in sorted(kept, key=_local):
                    coll.append(m)
            except Exception as exc:  # noqa: BLE001 — 실패 시 원본 유지
                logger.warning(
                    "Step 14b: 그룹 재작성 실패 (원본 유지): %s", exc,
                )
                continue
            groups_split += 1
            members_removed += len(removed_here)
            samples.append(
                f"{sorted(_local(m) for m in removed_here)} 제거 ← "
                f"{sorted(_local(m) for m in member_set)}"
            )
        unsat_classes_freed |= {_local(c) for c in victims}

    if members_removed:
        logger.info(
            "Step 14b: 직교 축 혼재 disjoint 정리 — 그룹 %d개 축 분리, %d개 제거, "
            "멤버 %d개 덜어냄. unsat 해소 대상: %s",
            groups_split, groups_dropped, members_removed,
            sorted(unsat_classes_freed),
        )

    return StepResult(
        name="step_14b_disjoint_axis_split",
        stats={
            "disjoint_axis_groups_split": groups_split,
            "disjoint_axis_groups_dropped": groups_dropped,
            "disjoint_axis_members_removed": members_removed,
            "disjoint_axis_unsat_freed": sorted(unsat_classes_freed),
            "disjoint_axis_samples": samples[:10],
        },
        triples_delta=len(g) - before,
        step_number="14b",
        step_label="disjoint_axis_split",
    )
