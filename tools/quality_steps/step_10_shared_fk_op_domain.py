"""Step 10 — 공유 FK ObjectProperty **측정** (읽기 전용).

본문 ontology_quality.py 의 Step 10 블록을 모듈로 옮김.

CSV FK 컬럼을 스캔해 여러 테이블(3개 이상)이 같은 FK 를 쓰는 OP 를 찾는다.
예전에는 그런 OP 의 ``rdfs:domain`` 을 ``owl:Thing`` 으로 **확장했으나**,
2026-08-11 실측으로 그 확장이 이득 없이 손해만 낸다는 것이 확인돼 측정만 한다.

## 왜 확장을 멈췄는가

**이득이 0이다.** step_10 을 read-only 로 만들고 A-Box 를 재생성해도 인스턴스
70,701 / OP 트리플 89,494 / 총 727,827 트리플이 **완전 동일**하다. S4 5종도 동일
(critical 0 / HermiT consistent / unsatisfiable 0 / SHACL conforms). 근본 원인은
A-Box 생성기가 ``owl:Thing`` domain 을 아예 못 읽는다는 것이다 —
``domain/tbox_utils.load_object_properties`` 가 domain 을 도메인 NS 로 필터해
``owl:Thing`` → ``None`` 이 되고, ``_build_fk_skeleton`` 은 그런 OP 를 Restriction
에 걸려 있을 때만 후보로 쓴다.

**손해는 셋이다.**

1. **검증 사각지대.** ``check_domain_range_conformance`` 는 domain 이 universal 인
   OP 를 등록조차 못 했다 (옛 ``if domains and ranges``). 그래서 domain 뿐 아니라
   **range 검사까지 함께** 잃었다 — 실측 62개 OP / A-Box 트리플 12,846건이
   무검사였고 커버리지가 167/229(72.9%) 였다. mutation 으로 확인: domain·range
   위반을 주입해도 놓쳤다. (검사 쪽은 ``checks/semantic.py`` 에서 별도 수정)
2. **concrete domain 파괴.** 이 스텝이 ``owl:Thing`` domain 의 **유일한 출처** 다
   (스텝별 추적: 0 → 49). 그중 ``surfaceQualityHasProduct`` /
   ``wasteGeneratedByEquipment`` / ``ghgEmissionOfEquipment`` 는 ``owl:inverseOf``
   가 없어 ``step_09a`` 의 복원 경로가 **존재하지 않는다** — 영구 손실이었다.
   read-only 화 후 각각 ``SurfaceQuality`` / ``WasteManagement`` / ``GHGEmission``
   로 보존된다.
3. **중복 게이트 시야 상실.** ``step_22d`` 도 ``owl:Thing`` domain OP 를 집계에서
   빼므로 배포본 중복이 43 으로 보였으나 실질은 61 이었다.

``step_09a`` 를 이 스텝 뒤로 옮기는 안은 **폐기됐다**: 위 3개 OP 는 09a 가 복원할
수 없어 손실이 남는다. 확장을 애초에 하지 않는 것이 정답이다.

배경: 원본 본문에서는 Step 15 의 함수 스코프 ``from config import
SOURCE_RAWDATA_DIR`` 때문에 Python 함수 전체에서 ``SOURCE_RAWDATA_DIR`` 가
로컬로 취급되어 Step 10 이 ``UnboundLocalError`` 로 silent fail 됐다.
Phase 4/5 의 step 모듈 추출로 함수 내부 재 import 가 모두 사라져 본 step 이
정상 활성화되었다.

monkeypatch 호환: ``import config`` + ``config.SOURCE_RAWDATA_DIR`` 동적
참조 패턴으로 ``monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", ...)`` 가
runtime 에 반영되도록 한다.
"""
from __future__ import annotations

import csv
import glob
import logging
import os

from rdflib import OWL, RDF, RDFS, Graph

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    import config
    from domain.uri_conventions import local_name as _local_name
    from tools import ontology_quality as _oq

    # 테스트 호환 우선순위:
    # 1) ``monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", ...)`` — 기존 테스트 다수
    # 2) ``monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", ...)`` — 신규 테스트
    # 3) module-level config import
    SOURCE_RAWDATA_DIR = (
        getattr(_oq, "SOURCE_RAWDATA_DIR", None)
        or config.SOURCE_RAWDATA_DIR
    )
    from tools.ontology_quality import _FK_PATTERNS
    before = len(g)
    steel_str = ctx.domain_ns
    domain_broadened = 0
    shared_fk_ops: list[str] = []

    try:
        fk_to_sources: dict[str, set[str]] = {}
        for csv_path in sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))):
            table_name = os.path.basename(csv_path).replace(".csv", "")
            class_name = table_name.replace("_", "")
            with open(csv_path, encoding="utf-8-sig") as f:
                header = next(csv.reader(f), [])
            for col in header:
                col_lower = col.lower().replace("_", "")
                target = _FK_PATTERNS.get(col_lower)
                if target and target != class_name:
                    fk_to_sources.setdefault(target, set()).add(class_name)

        for target_class, source_classes in fk_to_sources.items():
            if len(source_classes) < 3:
                continue

            for prop in set(g.subjects(RDF.type, OWL.ObjectProperty)):
                if not str(prop).startswith(steel_str):
                    continue
                ranges = [_local_name(str(r)) for r in g.objects(prop, RDFS.range)]
                if target_class not in ranges:
                    continue
                domains = [_local_name(str(d)) for d in g.objects(prop, RDFS.domain)]
                if not domains:
                    continue
                current_domain = domains[0]

                if current_domain in ("Thing", ""):
                    continue
                if current_domain in source_classes and len(source_classes) >= 3:
                    # **읽기 전용**: 공유 FK 사실만 기록하고 domain 을 바꾸지 않는다.
                    # 아래 docstring 의 근거 참조.
                    shared_fk_ops.append(
                        f"{_local_name(str(prop))}({current_domain}→{target_class})",
                    )
                    domain_broadened += 1
    except Exception as e:
        logger.warning("공유 FK 측정 실패: %s", e)

    if shared_fk_ops:
        logger.info(
            "Step 10: 3개 이상 테이블이 공유하는 FK 를 잇는 OP %d개 (측정만, "
            "domain 변경 없음): %s",
            len(shared_fk_ops), sorted(shared_fk_ops)[:5],
        )

    return StepResult(
        name="step_10_shared_fk_op_domain",
        stats={
            # 이름은 하위 호환을 위해 유지하되 의미는 "확장 후보 수" 다.
            "domain_broadened": 0,
            "shared_fk_ops_measured": domain_broadened,
            "shared_fk_op_samples": sorted(shared_fk_ops)[:10],
        },
        triples_delta=len(g) - before,        # 항상 0 — read-only
        step_number=10,
        step_label="shared_FK_domain_measurement",
    )
