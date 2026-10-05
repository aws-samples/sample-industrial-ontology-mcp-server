"""Referential check 그룹.

FK 참조 무결성, FK-OP 매핑, 댕글링 참조, Closed-World 관점의
master/FK 검증. master_data.ttl과의 상호작용이 핵심.

공유 자원: SharedCheckContext.typed_subjects, op_uris.
설정 주입: master_data_path / tbox_path / rules_dir / source_rawdata_dir
  (테스트에서 test 간 격리를 위해).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from collections import defaultdict

from rdflib import OWL, RDF, RDFS, BNode, Graph, URIRef

from config import (
    MASTER_DATA_PATH,
    SOURCE_RAWDATA_DIR,
)
from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.rules_paths import rules_path
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.validation_support.common import (
    SharedCheckContext,
    local,
    validate_prop_name,
)
from tools.validation_support.thresholds import get_tier_thresholds

logger = logging.getLogger(__name__)


def check_fk_referential_integrity(
    g: Graph, tbox: Graph = None, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """5. FK 참조 무결성 — ObjectProperty 대상이 실제로 존재하는지.

    T-Box에서 모든 ObjectProperty + range를 동적 추출하여 댕글링 참조를 체크.
    rdflib 직접 순회 + 공유 typed_subjects 캐시로 SPARQL 호출 N×2 제거.
    """
    prop_checks: list[tuple[str, URIRef, str]] = []
    if tbox is not None:
        for prop in tbox.subjects(RDF.type, OWL.ObjectProperty):
            if not isinstance(prop, URIRef) or not str(prop).startswith(DOMAIN_NS):
                continue
            prop_name = local(str(prop))
            ranges = [local(str(r)) for r in tbox.objects(prop, RDFS.range)
                      if isinstance(r, URIRef) and str(r).startswith(DOMAIN_NS)]
            range_type = ranges[0] if ranges else "owl:Thing"
            prop_checks.append((prop_name, prop, range_type))

    if not prop_checks:
        fallback = [
            ("hasTag", "TagMaster"),
            ("hasFailureCause", "FailureCause"),
            ("hasMaintenanceHistory", "MaintenanceHistory"),
            ("hasChemicalAnalysis", "ChemicalAnalysis"),
            ("hasEquipmentStatus", "EquipmentStatus"),
        ]
        prop_checks = [(name, URIRef(f"{DOMAIN_NS}{name}"), t) for name, t in fallback]

    # SPARQL 단일 쿼리로 per-OP total + dangling count 집계.
    # dangling 판정: object 가 IRI 이지만 rdf:type 트리플이 없음 (typed_subjects
    # 에 미포함) → FILTER NOT EXISTS { ?o a ?_t }.
    values_block = " ".join(f"<{u}>" for _, u, _ in prop_checks)
    q = (
        "SELECT ?p (COUNT(?o) AS ?total) "
        "(SUM(IF(isIRI(?o) && NOT EXISTS { ?o a ?_t }, 1, 0)) AS ?dangling) "
        "WHERE { "
        f"VALUES ?p {{ {values_block} }} "
        "?s ?p ?o "
        "} GROUP BY ?p"
    )
    prop_stats: dict[str, tuple[int, int]] = {}
    for row in g.query(q):
        try:
            total = int(row[1]) if row[1] is not None else 0
            dang = int(row[2]) if row[2] is not None else 0
        except (TypeError, ValueError):
            continue
        prop_stats[str(row[0])] = (total, dang)

    dangling = []
    checked_count = 0
    for prop_name, prop_uri, expected_type in prop_checks:
        validate_prop_name(prop_name)
        total, dangling_count = prop_stats.get(str(prop_uri), (0, 0))
        if total == 0:
            continue
        checked_count += 1
        if dangling_count > 0:
            dangling.append({
                "property": prop_name,
                "expected_range": expected_type,
                "total": total,
                "dangling": dangling_count,
                "rate": round(dangling_count / max(total, 1) * 100, 1),
            })

    return {
        "name": "FK 참조 무결성",
        "passed": len(dangling) == 0,
        "checked_properties": checked_count,
        "total_op_in_tbox": len(prop_checks),
        "dangling": dangling,
    }


def check_fk_op_coverage(
    tbox: Graph,
    *,
    rules_dir: str | None = None,
    source_rawdata_dir: str | None = None,
) -> dict[str, object]:
    """11. FK-OP Gap 분석 — CSV FK 컬럼에 대응하는 ObjectProperty 존재 여부."""
    # rules 디렉토리
    fk_path = (
        rules_path("fk_patterns.json") if rules_dir is None
        else rules_path("fk_patterns.json", base=rules_dir)
    )
    if not os.path.exists(fk_path):
        return {
            "name": "FK-OP Gap 분석",
            "passed": True,
            "message": "fk_patterns.json 규칙 파일 없음 (건너뜀)",
            "checked": 0,
            "missing_ops": [],
        }

    with open(fk_path, encoding="utf-8") as f:
        fk_data = json.load(f)
    fk_patterns: dict[str, str] = fk_data.get("patterns", {})
    suffix_rules = fk_data.get("suffix_rules", [])

    tbox_ops: set[str] = set()
    op_details: dict[str, dict] = {}
    for prop in tbox.subjects(RDF.type, OWL.ObjectProperty):
        if isinstance(prop, URIRef) and str(prop).startswith(DOMAIN_NS):
            name = _local_name(str(prop))
            tbox_ops.add(name.lower())
            domains = [_local_name(str(d)) for d in tbox.objects(prop, RDFS.domain)
                       if isinstance(d, URIRef) and str(d).startswith(DOMAIN_NS)]
            ranges = [_local_name(str(r)) for r in tbox.objects(prop, RDFS.range)
                      if isinstance(r, URIRef) and str(r).startswith(DOMAIN_NS)]
            op_details[name.lower()] = {
                "domain": domains[0] if domains else None,
                "range": ranges[0] if ranges else None,
            }

    fk_columns: list[dict[str, str]] = []
    csv_dir = source_rawdata_dir if source_rawdata_dir is not None else SOURCE_RAWDATA_DIR
    if os.path.isdir(csv_dir):
        for fname in os.listdir(csv_dir):
            if not fname.lower().endswith(".csv"):
                continue
            csv_path = os.path.join(csv_dir, fname)
            table_name = fname.rsplit(".", 1)[0]
            try:
                with open(csv_path, encoding="utf-8-sig") as cf:
                    reader = csv.reader(cf)
                    headers = next(reader, [])
                for header in headers:
                    normalized = re.sub(r"[_\s]", "", header).lower()
                    if normalized in fk_patterns:
                        fk_columns.append({
                            "table": table_name,
                            "column": header,
                            "normalized": normalized,
                            "target_class": fk_patterns[normalized],
                        })
                    else:
                        for rule in suffix_rules:
                            sfx = rule["suffix"]
                            if normalized.endswith(sfx) and len(normalized) > len(sfx):
                                base = normalized[:-len(sfx)]
                                target = rule["template"].format(base=base.capitalize())
                                fk_columns.append({
                                    "table": table_name,
                                    "column": header,
                                    "normalized": normalized,
                                    "target_class": target,
                                })
            except Exception as e:
                logger.debug("CSV FK 스캔 실패 (%s): %s", fname, e)

    # 실제 CSV 테이블 집합 (SampleMaster 같이 target 이지만 CSV 에 실물이 없는 경우
    # FK-OP 가 존재할 수 없으므로 missing_ops 에서 제외).
    csv_table_names_lower: set[str] = set()
    if os.path.isdir(csv_dir):
        for fname in os.listdir(csv_dir):
            if fname.lower().endswith(".csv"):
                base = fname.rsplit(".", 1)[0]
                # CamelCase 정규화: "Item_Master" → "itemmaster"
                csv_table_names_lower.add(base.replace("_", "").replace("-", "").lower())

    missing_ops = []
    covered = 0
    skipped_phantom = 0
    for fk in fk_columns:
        target_class = fk["target_class"]
        table = fk["table"]
        source_class = table.replace("_", "").replace("-", "").lower()

        # **FK 는 (source, target) 쌍이다 — range 만 보면 안 된다.**
        #
        # 예전 조건은 `any(d["range"] == target_class for d in op_details.values())`
        # 로 **domain 을 무시** 했다. 그래서 어떤 클래스에서든 그 타겟을 가리키는
        # OP 가 하나만 있으면 그 타겟을 참조하는 **모든 테이블의 FK** 가 커버로
        # 셌다. 실측 (2026-08-12 배포 T-Box): ``ItemMaster`` 를 range 로 갖는 OP
        # 를 9개에서 1개로 줄여도 ``coverage=100.0% / passed=True`` 였다 —
        # ``Item_Code`` FK 를 쓰는 테이블이 5개인데 OP 1개로 전부 커버가 됐다.
        # 게이트가 깨진 축에 건강을 보고하면 게이트가 없는 것보다 나쁘다.
        #
        # ``owl:Thing`` domain 은 **커버로 세지 않는다.** universal 이라 "어떤
        # source 도 허용" 이지만, 이 게이트가 묻는 것은 "이 테이블의 이 컬럼을
        # 표현할 OP 가 있는가" 다. 실측 (2026-08-12): A-Box 생성기는 owl:Thing
        # domain OP 를 후보로 쓰지 못하고(``load_object_properties`` 가 domain 을
        # 도메인 NS 로 필터해 ``None``), 배포본의 그런 OP 62개는 값이 0건이다.
        # 그것을 커버로 세면 뮤테이션(range OP 9→1)이 통과해 게이트가 무력해진다.
        # 자기 테이블의 PK 는 FK 가 아니다 — ``Tag_Master.Tag_ID`` 는 자신을
        # 가리키므로 OP 로 표현할 대상이 없다 (self-loop OP 는 step_15b 가 오히려
        # 제거한다). 이름 규칙만으로 FK 패턴에 걸린 PK 를 커버 미달로 세면 게이트가
        # 영구히 도달 불가능한 목표를 요구한다.
        if source_class == target_class.lower():
            skipped_phantom += 1
            continue

        # 이름 매칭도 **쌍으로** 판정한다. ``expected_op in tbox_ops`` 는 이름만
        # 보므로 ``hasItemMaster`` 가 어딘가 선언돼 있으면 그 타겟을 참조하는 모든
        # 테이블의 FK 가 커버로 셌다 (뮤테이션으로 확인: range OP 를 9→1 로 줄여도
        # 통과).
        def _covers(
            meta: dict, *, src: str = source_class, tgt: str = target_class,
        ) -> bool:
            """이 OP 가 ``(src → tgt)`` FK 를 표현하는가.

            루프 변수를 **기본값으로 바인딩** 한다 — 클로저가 늦게 바인딩되면 모든
            FK 가 마지막 쌍으로 판정되는 조용한 오작동이 된다 (ruff B023).
            """
            return (
                (meta.get("range") or "").lower() == tgt.lower()
                and (meta.get("domain") or "").lower() == src
            )

        has_op = any(_covers(d) for d in op_details.values())
        if has_op:
            covered += 1
        else:
            # CSV 에 target_class 에 대응하는 파일이 없으면 FK 타겟이 존재하지 않음.
            # 예: Sample_ID 컬럼 → SampleMaster 기대하나 CSV 에 SampleMaster 없음.
            # 이런 phantom 타겟은 검증 대상에서 제외.
            if target_class.lower() not in csv_table_names_lower:
                skipped_phantom += 1
                continue
            missing_ops.append({
                "table": table,
                "fk_column": fk["column"],
                "target_class": target_class,
                "suggested_op": f"has{target_class}",
                "suggested_domain": table,
                "suggested_range": target_class,
            })

    total_fk = len(fk_columns)
    # phantom 타겟은 분모에서 제외 (CSV 에 실제 타겟 테이블 없으면 FK-OP 자체
    # 가 성립 불가 — 검증 대상에서 벗어남).
    effective_total = max(total_fk - skipped_phantom, 1)
    coverage = round(covered / effective_total * 100, 1) if effective_total > 0 else 100
    # 커버리지 85% 이상이면 PASS. 완전 100% 는 T-Box 가 모든 CSV FK 의 타겟
    # 클래스를 선언했을 때만 가능한데, 일부 FK 는 의도적으로 ObjectProperty 로
    # 모델링하지 않을 수 있음. 환경변수 FK_OP_COVERAGE_THRESHOLD 로 조정.
    import os as _os
    cov_threshold = float(_os.getenv("FK_OP_COVERAGE_THRESHOLD", "85.0"))

    return {
        "name": "FK-OP Gap 분석",
        "passed": coverage >= cov_threshold,
        "total_fk_columns": total_fk,
        "effective_fk_columns": effective_total,
        "phantom_target_skipped": skipped_phantom,
        "covered": covered,
        "coverage_pct": coverage,
        "threshold_pct": cov_threshold,
        "missing_ops": missing_ops[:20],
    }


def check_dangling_references(
    g: Graph, *, class_tiers: dict[str, str] | None = None,
    shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """15. 댕글링 참조 탐지 — ObjectProperty 대상에 rdf:type 없는 노드."""
    if shared is not None:
        typed_subjects = shared.typed_subjects
    else:
        typed_subjects = {
            s for s, _, _ in g.triples((None, RDF.type, None))
            if isinstance(s, URIRef)
        }

    total_op_triples = 0
    dangling_count = 0
    prop_counts: dict[str, int] = {}
    samples: list[dict[str, str]] = []
    query = (
        "SELECT ?p ?o WHERE { ?s ?p ?o . "
        "FILTER(isIRI(?o)) "
        f"FILTER(STRSTARTS(STR(?p), \"{DOMAIN_NS}\")) "
        "}"
    )
    for row in g.query(query):
        p, o = row[0], row[1]
        total_op_triples += 1
        if o not in typed_subjects:
            dangling_count += 1
            prop_name = local(str(p))
            prop_counts[prop_name] = prop_counts.get(prop_name, 0) + 1
            if len(samples) < 10:
                samples.append({
                    "property": prop_name,
                    "target": local(str(o)),
                })

    dangling_rate = round(dangling_count / max(total_op_triples, 1) * 100, 1)

    max_rate_pct: float = 5.0  # 기본값 (%)
    if class_tiers is not None:
        tier_thresholds = get_tier_thresholds()
        active_tiers = set(class_tiers.values())
        tier_rates = [
            tier_thresholds[t]["dangling_rate"] * 100
            for t in active_tiers if t in tier_thresholds
        ]
        if tier_rates:
            max_rate_pct = min(tier_rates)

    return {
        "name": "댕글링 참조 탐지",
        "passed": dangling_rate < max_rate_pct,
        "total_op_triples": total_op_triples,
        "dangling_count": dangling_count,
        "dangling_rate_pct": dangling_rate,
        "by_property": dict(sorted(prop_counts.items(), key=lambda x: -x[1])[:10]),
        "samples": samples,
    }


def check_undeclared_op(
    g: Graph, tbox: Graph = None, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """A-Box에서 사용된 OP가 T-Box에 선언돼 있는지 (orphan OP 탐지).

    기존 OP check들은 모두 "T-Box 선언 → A-Box 준수" 단방향만 본다.
    그 거울상인 "A-Box에는 쓰였지만 T-Box에 선언이 없는 OP"(orphan/undeclared)
    는 사각지대였다 — undeclared OP 는 domain/range 제약이 없어 어떤 데이터든
    위반 없이 통과하기 때문. 이 check 가 데이터 측에서 출발해 그 갭을 메운다.

    판정(gate): 그래프에서 술어로 쓰였고 object 가 IRI 인 DOMAIN_NS predicate 중,
    T-Box 에 **어떤 property 로도 선언되지 않은** 것(orphan OP)을 위반으로 본다.
    표준 어휘(rdf/rdfs/owl/prov/dcterms 등 비-DOMAIN_NS)는 대상 외.

    별도 경고(non-gating): T-Box 에 DatatypeProperty 로 선언됐는데 IRI 값에
    쓰인 경우는 추론 단계 type-pollution 이 만드는 별개 결함이라 gate 에서
    제외하고 ``dp_with_iri_value`` 로만 보고한다 (OP 정합성과 무관).
    """
    declared_ops: set[str] = set()
    declared_dps: set[str] = set()
    if tbox is not None:
        for p in tbox.subjects(RDF.type, OWL.ObjectProperty):
            if isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS):
                declared_ops.add(str(p))
        for p in tbox.subjects(RDF.type, OWL.DatatypeProperty):
            if isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS):
                declared_dps.add(str(p))

    # 그래프에서 실제 사용된 DOMAIN_NS predicate 중 object 가 IRI 인 것만
    # (object property 사용으로 간주). Literal object 는 DP 이므로 제외.
    used_ops: dict[str, int] = defaultdict(int)
    q = (
        "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o . "
        "FILTER(isIRI(?o)) "
        f"FILTER(STRSTARTS(STR(?p), \"{DOMAIN_NS}\")) "
        "} GROUP BY ?p"
    )
    for row in g.query(q):
        used_ops[str(row[0])] = int(row[1]) if row[1] is not None else 0

    undeclared: list[dict] = []        # gate: T-Box 에 전혀 없는 OP
    dp_with_iri: list[dict] = []        # non-gating: DP 인데 IRI 값 (추론 오염)
    for op_uri, count in used_ops.items():
        if op_uri in declared_ops:
            continue
        entry = {
            "property": _local_name(op_uri),
            "uri": op_uri,
            "usage_count": count,
        }
        if op_uri in declared_dps:
            dp_with_iri.append(entry)
        else:
            undeclared.append(entry)

    undeclared.sort(key=lambda x: -x["usage_count"])
    dp_with_iri.sort(key=lambda x: -x["usage_count"])
    return {
        "name": "미선언 ObjectProperty 탐지",
        # gate 는 orphan OP(완전 미선언)만. DP-with-IRI 는 별개 추론 오염이라 제외.
        "passed": len(undeclared) == 0,
        "used_op_count": len(used_ops),
        "declared_op_count": len(declared_ops),
        "undeclared_count": len(undeclared),
        "undeclared": undeclared[:20],
        "dp_with_iri_value_count": len(dp_with_iri),
        "dp_with_iri_value": dp_with_iri[:20],
    }


def check_undeclared_dp(
    g: Graph, tbox: Graph = None, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """A-Box 에서 리터럴 값으로 쓰인 DP 가 T-Box 에 선언돼 있는지.

    ``check_undeclared_op`` 의 **DP 거울상**이다. 그쪽은 IRI 값 술어(OP)만 보고, DP 축은
    "선언된 DP 가 IRI 값에 쓰였나"(``dp_with_iri_value``) 라는 다른 질문만 다뤘다. 그래서
    **A-Box 가 쓰는데 T-Box 에 없는 DP** 는 23 check 전체에서 사각지대였다.

    ## 실측 (2026-08-27)

    배포 산출물에서 미선언 DP **17건**이 발견됐고 어느 게이트도 잡지 못했다. 성격이 둘로
    갈린다:

        유형 A (10건) 단위 약어 대소문자 skew
                      A-Box `rollingForceKn`  vs T-Box `rollingForceKN`
                      A-Box `blastFurnaceCo2Percent` vs `blastFurnaceCO2Percent`
        유형 B (7건)  클래스 접두가 다르거나 짝이 없음
                      A-Box `airEmissionPollutantType` (1,560건)
                      T-Box `airEmissionMonitoringPollutantType`

    **원인은 코드 결함이 아니라 산출물 세대 불일치였다** — A-Box 는 8/26 01:03, T-Box 는
    8/27 06:33 이고 그 사이 S2 를 3회 돌렸다. `_col_to_prop` 을 현재 T-Box 로 직접
    호출하면 `Rolling_Force_kN → rollingForceKN` 을 정확히 반환한다(실측).

    그래서 이 게이트의 값은 **"A-Box 를 다시 만들어야 한다"** 를 알려주는 것이다. 그
    신호가 없으면 낡은 A-Box 로 추론·검증을 계속 돌리면서 정의 클래스가 왜 비었는지
    (`airEmissionMonitoringPollutantType` 이 A-Box 0건) 를 엉뚱한 곳에서 찾게 된다 —
    실제로 그렇게 됐다.

    ## 판정

    술어로 쓰였고 **object 가 리터럴**인 DOMAIN_NS predicate 중 T-Box 에 어떤 property
    로도 선언되지 않은 것을 위반으로 본다. IRI 값 술어는 OP 축(``check_undeclared_op``)
    소관이므로 제외한다 — 같은 술어를 두 게이트가 이중 계산하면 어느 쪽을 고쳐야 하는지
    흐려진다.
    """
    declared: set[str] = set()
    if tbox is not None:
        for prop_type in (
            OWL.DatatypeProperty, OWL.ObjectProperty, OWL.AnnotationProperty,
            RDF.Property,
        ):
            for p in tbox.subjects(RDF.type, prop_type):
                if isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS):
                    declared.add(str(p))

    used: dict[str, int] = {}
    for _s, p, o in g:
        if not (isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS)):
            continue
        if isinstance(o, URIRef | BNode):
            continue          # IRI 값 = OP 축 (check_undeclared_op 소관)
        used[str(p)] = used.get(str(p), 0) + 1

    undeclared = [
        {
            "property": uri.split("#")[-1],
            "uri": uri,
            "usage_count": count,
            # 이름이 비슷한 T-Box 선언이 있으면 skew 후보로 함께 준다 — 사람이
            # "재생성이 필요한가 / 이름을 고쳐야 하는가" 를 즉시 판단할 수 있다.
            "tbox_candidate": _closest_declared(uri, declared),
        }
        for uri, count in used.items()
        if uri not in declared
    ]
    undeclared.sort(key=lambda x: -x["usage_count"])
    return {
        "name": "미선언 DatatypeProperty 탐지",
        "passed": len(undeclared) == 0,
        "used_dp_count": len(used),
        "declared_property_count": len(declared),
        "undeclared_count": len(undeclared),
        "undeclared": undeclared[:20],
    }


def _closest_declared(uri: str, declared: set[str]) -> str | None:
    """미선언 DP 의 이름이 T-Box 선언과 대소문자/접두만 다른 경우 그 짝을 찾는다.

    skew(세대 불일치)와 진짜 누락을 구분하는 신호다. 짝이 있으면 "A-Box 재생성"
    이 조치이고, 없으면 T-Box 선언이나 CSV 매핑을 봐야 한다.
    """
    local = uri.split("#")[-1]
    norm = local.lower()
    for cand in declared:
        cand_local = cand.split("#")[-1]
        if cand_local == local:
            continue
        cn = cand_local.lower()
        # 대소문자만 다른 경우 / 한쪽이 다른 쪽 접미인 경우 (클래스 접두 차이)
        if cn == norm or cn.endswith(norm) or norm.endswith(cn):
            return cand_local
    return None


def load_master_instance_uris(
    master_data_path: str | None = None,
) -> set[URIRef]:
    """master_data.ttl에서 인스턴스 URI만 추출 (rdf:type 서브젝트)."""
    path = master_data_path if master_data_path is not None else MASTER_DATA_PATH
    if not os.path.exists(path):
        return set()
    mg = _new_graph()
    try:
        mg.parse(path, format="turtle")
    except Exception as e:
        logger.warning("master_data 파싱 실패: %s", e)
        return set()
    master_uris: set[URIRef] = set()
    for s in mg.subjects(RDF.type, None):
        if isinstance(s, URIRef) and str(s).startswith(str(DOMAIN_INST_NS)):
            master_uris.add(s)
    return master_uris


def check_closed_world_master_orphan(
    g: Graph, tbox: Graph = None, *, shared: SharedCheckContext | None = None,
    master_data_path: str | None = None,
    class_tiers: dict[str, str] | None = None,
) -> dict[str, object]:
    """21. Closed-World: master 고립 — master 인스턴스가 transaction에서 미참조.

    과거 단일 임계 30% 적용 시, ProductionPlan/MaintenanceHistory 같은 트랜잭션성
    레코드가 master_data.ttl 에 섞여 있을 때 자연 희소 참조를 위반으로 오판.
    tier 분류 (abox_stats.json → master / transaction / inferred) 를 쓰면
    per-class threshold 를 적용해 transaction tier 는 60% 까지 허용한다.
    """
    master_uris = load_master_instance_uris(master_data_path)
    if not master_uris:
        return {
            "name": "Closed-World master 고립",
            "passed": True,
            "message": "master_data.ttl 없음 — skip",
            "master_total": 0,
            "orphan_count": 0,
        }

    # Master URI → class_name (master_data.ttl 내 rdf:type 기준)
    uri_to_class: dict[URIRef, str] = {}
    if master_data_path and os.path.exists(master_data_path):
        mg = _new_graph()
        try:
            mg.parse(master_data_path, format="turtle")
            for s, _, o in mg.triples((None, RDF.type, None)):
                # 첫 steel: class 타입만 사용 (여러 개면 제일 먼저 찍힌 것)
                if (
                    isinstance(s, URIRef) and isinstance(o, URIRef)
                    and s in master_uris and str(o).startswith(DOMAIN_NS)
                    and s not in uri_to_class
                ):
                    uri_to_class[s] = local(str(o))
        except Exception as e:
            logger.debug("master_data 타입 추출 실패: %s", e)

    referenced: set[URIRef] = set()
    values_block = " ".join(f"<{u}>" for u in master_uris)
    query = (
        "SELECT DISTINCT ?o WHERE { "
        f"  VALUES ?o {{ {values_block} }} "
        "  ?s ?p ?o . "
        "}"
    )
    try:
        for row in g.query(query):
            referenced.add(row[0])
    except Exception as e:
        logger.warning("cw_master_orphan SPARQL 실패, fallback: %s", e)
        for _, _, o in g.triples((None, None, None)):
            if isinstance(o, URIRef) and o in master_uris:
                referenced.add(o)
                if len(referenced) == len(master_uris):
                    break

    orphans = master_uris - referenced
    import os as _os
    env_threshold = _os.getenv("CW_MASTER_ORPHAN_THRESHOLD")
    overall_orphan_rate = len(orphans) / max(len(master_uris), 1)

    # 환경변수 강제 지정이 있으면 tier 무시하고 단일 threshold 적용 (이전 동작 보존)
    if env_threshold is not None:
        threshold = float(env_threshold)
        passed = overall_orphan_rate <= threshold
        return {
            "name": "Closed-World master 고립",
            "passed": passed,
            "master_total": len(master_uris),
            "orphan_count": len(orphans),
            "orphan_rate": round(overall_orphan_rate * 100, 2),
            "threshold_pct": threshold * 100,
            "sample_orphans": [local(str(u)) for u in list(orphans)[:10]],
        }

    # tier 기반 per-class 평가: class 별 orphan 비율이 해당 tier threshold 를
    # 넘는 경우만 FAIL. tier 정보가 없으면 전체 30% 단일 fallback.
    tier_thresholds = get_tier_thresholds()
    # 각 tier 기본 orphan_rate — thresholds.py 에서 master=30%, transaction=60%.
    per_class_counts: dict[str, dict[str, int]] = {}
    for uri in master_uris:
        cls = uri_to_class.get(uri, "_unknown")
        d = per_class_counts.setdefault(cls, {"total": 0, "orphan": 0})
        d["total"] += 1
        if uri in orphans:
            d["orphan"] += 1

    # pure-source 클래스 (T-Box 에서 아무 OP 의 range 로도 쓰이지 않는 클래스) 는
    # 고립 검증에서 제외. ItemSupplierMap, ProductionPlan, MaintenanceHistory
    # 같이 FK 의 target 이 아닌 source 로만 쓰이는 테이블은 "아무도 이 class 를
    # 가리키지 않음" 이 자연스럽다.
    pure_source_classes: set[str] = set()
    if tbox is not None:
        referenced_ranges: set[str] = set()
        for _, _, rng in tbox.triples((None, RDFS.range, None)):
            if isinstance(rng, URIRef) and str(rng).startswith(DOMAIN_NS):
                referenced_ranges.add(local(str(rng)))
        for cls in per_class_counts:
            if cls and cls != "_unknown" and cls not in referenced_ranges:
                pure_source_classes.add(cls)

    per_class_report: list[dict] = []
    per_class_violations: list[str] = []
    for cls, counts in per_class_counts.items():
        tier = (class_tiers or {}).get(cls, "master")
        threshold_pct = tier_thresholds.get(tier, {}).get(
            "master_orphan_rate", 0.30)
        rate = counts["orphan"] / max(counts["total"], 1)
        is_pure_source = cls in pure_source_classes
        entry = {
            "class": cls,
            "tier": tier,
            "total": counts["total"],
            "orphan": counts["orphan"],
            "orphan_rate_pct": round(rate * 100, 2),
            "threshold_pct": round(threshold_pct * 100, 2),
            "pure_source": is_pure_source,
            # pure-source 는 항상 통과 (target 이 아니므로 참조 안 되는 게 정상)
            "passed": is_pure_source or rate <= threshold_pct,
        }
        per_class_report.append(entry)
        if not entry["passed"]:
            per_class_violations.append(
                f"{cls} ({tier}): {counts['orphan']}/{counts['total']} "
                f"= {entry['orphan_rate_pct']}% > {entry['threshold_pct']}%"
            )

    passed = len(per_class_violations) == 0
    # 보고 시 상위 3개 tier threshold 노출 (이전 스키마 호환 위해 overall 도 유지)
    overall_threshold = tier_thresholds.get("master", {}).get(
        "master_orphan_rate", 0.30)
    return {
        "name": "Closed-World master 고립",
        "passed": passed,
        "master_total": len(master_uris),
        "orphan_count": len(orphans),
        "orphan_rate": round(overall_orphan_rate * 100, 2),
        "threshold_pct": overall_threshold * 100,
        "per_class": sorted(per_class_report,
                            key=lambda d: -d["orphan_rate_pct"])[:20],
        "per_class_violations": per_class_violations,
        "sample_orphans": [local(str(u)) for u in list(orphans)[:10]],
    }


def check_closed_world_fk_unresolved(
    g: Graph, tbox: Graph = None, *, shared: SharedCheckContext | None = None,
    master_data_path: str | None = None,
) -> dict[str, object]:
    """22. Closed-World: FK 미해결 — OP 객체가 master에도 transaction에도 없는 케이스."""
    master_uris = load_master_instance_uris(master_data_path)
    if not master_uris:
        return {
            "name": "Closed-World FK 미해결",
            "passed": True,
            "message": "master_data.ttl 없음 — skip",
            "total_fk_triples": 0,
            "unresolved_count": 0,
        }

    if shared is None and tbox is not None:
        shared = SharedCheckContext(g, tbox)
    transaction_typed = shared.typed_subjects if shared is not None else {
        s for s, _, _ in g.triples((None, RDF.type, None)) if isinstance(s, URIRef)
    }

    op_uris: set[URIRef] = set()
    if tbox is not None:
        for p in tbox.subjects(RDF.type, OWL.ObjectProperty):
            if isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS):
                op_uris.add(p)

    if not op_uris:
        return {
            "name": "Closed-World FK 미해결",
            "passed": True,
            "total_fk_triples": 0,
            "unresolved_count": 0,
            "message": "T-Box에 ObjectProperty 없음 — skip",
        }

    unresolved_by_prop: dict[str, int] = defaultdict(int)
    total_fk = 0
    unresolved_total = 0
    values_block = " ".join(f"<{u}>" for u in op_uris)
    query = (
        "SELECT ?p ?o WHERE { "
        f"  VALUES ?p {{ {values_block} }} "
        "  ?s ?p ?o . "
        "  FILTER(isIRI(?o)) "
        "}"
    )
    for row in g.query(query):
        p, o = row[0], row[1]
        total_fk += 1
        if o not in master_uris and o not in transaction_typed:
            unresolved_by_prop[local(str(p))] += 1
            unresolved_total += 1

    rate = unresolved_total / max(total_fk, 1)
    threshold = 0.05
    top = sorted(unresolved_by_prop.items(), key=lambda kv: -kv[1])[:10]
    return {
        "name": "Closed-World FK 미해결",
        "passed": rate <= threshold,
        "total_fk_triples": total_fk,
        "unresolved_count": unresolved_total,
        "unresolved_rate": round(rate * 100, 2),
        "threshold_pct": threshold * 100,
        "top_properties": [{"property": n, "unresolved": c} for n, c in top],
    }
