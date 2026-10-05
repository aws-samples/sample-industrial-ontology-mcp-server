"""Step 12 — 도메인별 중간 추상 클래스 생성 (DIT/NOC 향상).

본문 ontology_quality.py 의 Step 12 블록을 그대로 모듈로 옮김.

도메인 그룹별로 추상 부모 클래스를 생성하여 평면적 계층을 개선한다.
OntoQA 메트릭: DIT (Depth of Inheritance Tree), NOC (Number of Children) 향상.

post: 새 중간 parent 가 추가되면 child→grandparent 직접 링크가 redundant 가
되므로 ``_remove_redundant_subclass`` 를 한 번 더 호출.
"""
from __future__ import annotations

import logging
import os

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.graph_utils import would_violate_disjoint
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def _reconcile_iof_parent(
    g: Graph, cls_uri: URIRef, cfg: dict, steel_str: str,
) -> tuple[int, list[str]]:
    """추상 클래스의 **외부(IOF) 부모** 를 ``design_patterns.json`` 에 맞춘다.

    ## 왜 필요한가

    예전엔 클래스를 **새로 만들 때만** ``cfg["parent"]`` 를 붙였다. 그런데 S2 가 같은
    이름의 추상 클래스를 먼저 만들면(프롬프트에 골격이 주입되므로 흔하다) 이 스텝은
    "이미 존재" 로 스킵하고, S2 가 상상한 IOF 부모가 그대로 배포된다. config 는
    읽히지만 적용되지 않는 **죽은 설정** 이 된다.

    실측 (2026-08-17, 배포 T-Box): ``design_patterns.json`` 의 ``iof_parent`` 와 실제
    ``rdfs:subClassOf`` 가 **9건 불일치** 했다 —

      SupplyChainManagement   config PlannedProcess       실제 MaterialArtifact
      ProductionManagement    config PlannedProcess       실제 MaterialArtifact
      MonitoringManagement    config MeasurementICE       실제 MaterialArtifact
      RealTimeDataManagement  config MeasurementICE       실제 MaterialArtifact
      MaintenanceManagement   config MaintenanceProcess   실제 MaintenanceActivity
      EnergyManagement / EnvironmentalMonitoring / ManufacturingProcessStep 은
      config 부모 **에 더해** MaterialArtifact 가 여분으로 붙어 있었다.
      QualityMeasurement 은 IOF 부모가 아예 없었다.

    즉 추상 '관리/모니터링' 카테고리가 물리 객체(``MaterialArtifact``)로 선언됐다.

    ## 이것이 실제로 만드는 논리 오류

    IOF 를 **실제로 로드** 하면 ``SupplierMaster ⊑ Organization`` 과
    ``SupplierMaster ⊑ SupplyChainMaster ⊑ SupplyChainManagement ⊑ MaterialArtifact``
    가 충돌해 ``SupplierMaster`` 가 **unsatisfiable** 이 된다 (HermiT 실측 —
    ``Organization`` ⊥ ``MaterialArtifact``).

    배포 상태에서는 ``owl:imports`` 가 네임스페이스 드리프트로 한 트리플도 로드하지
    않아(``Core.rdf`` 는 ``/ontology/construct/``, T-Box 는 ``/ontology/core/Core/``)
    이 모순이 조용히 숨어 있었다. 즉 "HermiT consistent" 는 공리가 없어서 얻은 침묵이다.

    ## 범위

    **외부 부모만** 건드린다. 도메인 부모(``steel:``)와 ``owl:Thing`` 은 보존한다 —
    이 함수의 권한은 IOF 정렬뿐이다. config 에 ``iof_parent`` 가 없거나 도메인 IRI 면
    아무것도 하지 않는다.

    Returns:
        ``(교정 건수, 샘플 목록)``.
    """
    want = URIRef(cfg["parent"]) if cfg.get("parent") else None
    if want is None:
        return 0, []
    if not str(want).startswith(("http://", "https://")):
        return 0, []
    if str(want).startswith(steel_str):
        return 0, []                       # 도메인 부모는 이 함수의 소관이 아니다

    fixed = 0
    samples: list[str] = []
    stale_parents = [
        p for p in g.objects(cls_uri, RDFS.subClassOf)
        if isinstance(p, URIRef)
        and str(p).startswith(("http://", "https://"))
        and not str(p).startswith(steel_str)
        and p != OWL.Thing
        and p != want
    ]
    for stale in stale_parents:
        g.remove((cls_uri, RDFS.subClassOf, stale))
        fixed += 1
        samples.append(f"{cfg['class']}: -{str(stale).rsplit('/', 1)[-1]}")
        logger.warning(
            "IOF 부모 교정: %s 에서 config 미승인 부모 %s 제거 (config=%s). 추상 "
            "카테고리를 물리 객체로 선언하면 IOF 로드 시 unsatisfiable 이 된다",
            cfg["class"], str(stale).rsplit("/", 1)[-1],
            str(want).rsplit("/", 1)[-1],
        )
    if (cls_uri, RDFS.subClassOf, want) not in g:
        g.add((cls_uri, RDFS.subClassOf, want))
        fixed += 1
        samples.append(f"{cfg['class']}: +{str(want).rsplit('/', 1)[-1]}")
        logger.info(
            "IOF 부모 보강: %s ⊑ %s (config)",
            cfg["class"], str(want).rsplit("/", 1)[-1],
        )
    return fixed, samples


def apply(g: Graph, ctx: StepContext) -> StepResult:
    import config  # 테스트 monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", ...) 반영
    from tools.ontology_quality import (
        DOMAIN_NS_OBJ,
        _load_hierarchy_config,
        _remove_redundant_subclass,
    )
    SOURCE_RAWDATA_DIR = config.SOURCE_RAWDATA_DIR

    before = len(g)
    steel_str = ctx.domain_ns
    intermediate_classes_added = 0
    intermediate_subclass_added = 0
    disjoint_conflicts_skipped = 0
    ap_redundant_removed_step12 = 0
    iof_parent_corrected = 0
    iof_parent_samples: list[str] = []
    try:
        _HIERARCHY_CONFIG = _load_hierarchy_config()

        existing_classes = {
            str(c) for c in g.subjects(RDF.type, OWL.Class)
            if str(c).startswith(steel_str)
        }

        _hier_csv_names: set[str] = set()
        try:
            if os.path.isdir(SOURCE_RAWDATA_DIR):
                _hier_csv_names = {
                    fn.rsplit(".", 1)[0].replace("_", "").lower()
                    for fn in os.listdir(SOURCE_RAWDATA_DIR)
                    if fn.lower().endswith(".csv")
                }
        except Exception as e:
            logger.debug(
                "CSV 디렉토리 스캔 실패 (hierarchy config에는 영향 없음): %s", e
            )

        for domain, cfg in _HIERARCHY_CONFIG.items():
            if _hier_csv_names:
                _has_csv_child = any(
                    tbl.replace("_", "").lower() in _hier_csv_names
                    or any(tbl.replace("_", "").lower() in cn
                           for cn in _hier_csv_names)
                    for tbl in cfg["children_tables"]
                )
                if not _has_csv_child:
                    # CSV 자식이 없으면 **새로 만들지는** 않는다. 그러나 클래스가 이미
                    # 존재하면 그 IOF 부모는 여전히 config 에 맞춰야 한다 — 이 가드는
                    # "생성" 권한에 대한 것이고, 이미 배포된 잘못된 정렬을 방치할 이유가
                    # 아니다. 실측: MonitoringManagement / RealTimeDataManagement /
                    # ManufacturingProcessStep 3개가 이 continue 때문에 교정을 못 받아
                    # MaterialArtifact 오정렬로 남았다.
                    _existing = DOMAIN_NS_OBJ[cfg["class"]]
                    if str(_existing) in existing_classes:
                        n, s = _reconcile_iof_parent(
                            g, _existing, cfg, steel_str,
                        )
                        iof_parent_corrected += n
                        iof_parent_samples.extend(s)
                    logger.info(
                        "중간 클래스 스킵 (CSV 자식 없음): %s (%s)",
                        cfg["class"], domain,
                    )
                    continue

            parent_cls_uri = DOMAIN_NS_OBJ[cfg["class"]]

            if str(parent_cls_uri) in existing_classes:
                logger.info("중간 클래스 이미 존재, 스킵: %s", cfg["class"])
            else:
                g.add((parent_cls_uri, RDF.type, OWL.Class))
                g.add((parent_cls_uri, RDFS.label, Literal(cfg["label_en"], lang="en")))
                g.add((parent_cls_uri, RDFS.label, Literal(cfg["label_ko"], lang="ko")))
                g.add((parent_cls_uri, RDFS.comment, Literal(cfg["comment_ko"], lang="ko")))
                g.add((parent_cls_uri, RDFS.subClassOf, URIRef(cfg["parent"])))
                intermediate_classes_added += 1
                logger.info("중간 추상 클래스 생성: %s (%s)", cfg["class"], domain)

            # 클래스가 이미 있어도 IOF 부모는 config 에 맞춘다 (헬퍼 참조).
            _n, _s = _reconcile_iof_parent(g, parent_cls_uri, cfg, steel_str)
            iof_parent_corrected += _n
            iof_parent_samples.extend(_s)

            for table_name in cfg["children_tables"]:
                child_cls_name = table_name.replace("_", "")
                child_cls_uri = DOMAIN_NS_OBJ[child_cls_name]
                if str(child_cls_uri) not in existing_classes:
                    continue

                existing_parents = {
                    str(p) for p in g.objects(child_cls_uri, RDFS.subClassOf)
                    if isinstance(p, URIRef) and str(p).startswith(steel_str)
                }
                if str(parent_cls_uri) in existing_parents:
                    continue

                # 이미 다른 disjoint 그룹에 속한 클래스에 두 번째 그룹을 붙이면
                # 그 클래스는 **unsatisfiable** 이 되고 (HermiT), A-Box 가 인스턴스를
                # 만들 수 없어 해당 CSV 테이블이 KG 에서 사라진다. 실측 (2026-08-11):
                # FailureCause / MaintenanceHistory / TagMaster 3개가 이 경로로
                # unsatisfiable 이 됐다 — 8개 도메인 그룹은 S2 부터 disjoint 인데
                # 이 스텝이 그것을 보지 않았다.
                conflict = would_violate_disjoint(g, child_cls_uri, parent_cls_uri)
                if conflict:
                    logger.warning(
                        "중간 클래스 부모 추가 건너뜀: %s ⊑ %s — disjoint 충돌 "
                        "(%s ⊥ %s). 추가하면 unsatisfiable 이 되어 A-Box 인스턴스가 "
                        "생성되지 않는다",
                        child_cls_name, cfg["class"], conflict[0], conflict[1],
                    )
                    disjoint_conflicts_skipped += 1
                    continue

                g.add((child_cls_uri, RDFS.subClassOf, parent_cls_uri))
                intermediate_subclass_added += 1

    except Exception as e:
        logger.warning("중간 클래스 생성 실패: %s", e)

    ap_redundant_removed_step12 = _remove_redundant_subclass(g, steel_str)

    return StepResult(
        name="step_12_intermediate_abstract",
        stats={
            "intermediate_classes_added": intermediate_classes_added,
            "intermediate_disjoint_conflicts_skipped": disjoint_conflicts_skipped,
            "intermediate_subclass_added": intermediate_subclass_added,
            "intermediate_redundant_reduced": ap_redundant_removed_step12,
            "iof_parent_corrected": iof_parent_corrected,
            "iof_parent_samples": iof_parent_samples[:20],
            # 본문 stats accumulator: ``antipattern_redundant_removed`` 는
            # Step 0/12/14 가 누적. 코디네이터에서 += 처리하도록 별도 키로 반환.
            "_step12_redundant_delta": ap_redundant_removed_step12,
        },
        triples_delta=len(g) - before,
        step_number=12,
        step_label="intermediate_abstract_classes",
    )
