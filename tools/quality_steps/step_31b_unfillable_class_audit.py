"""Step 31b — **채워질 수 없는 클래스** 진단 (read-only).

추론까지 끝나도 인스턴스가 0인 클래스가 남는다. 실측 (2026-08-30 배포 T-Box):
92개 중 **14개**가 그 상태다. 그런데 원인이 두 갈래이고 **필요한 조치가 다르다**:

======================================= ===== =========================================
그룹                                     개수   원인
======================================= ===== =========================================
A. 값 불일치 (``value_mismatch``)          7   정의 공리의 값이 CSV 실값과 다르다
B. 정의 공리 없음 (``no_definition``)       7   판별 근거가 아예 없다
======================================= ===== =========================================

## A — 값 불일치: LLM 이 CSV 를 보지 않고 값을 발명했다

============================== ============ ==============================
클래스                           공리 값       CSV 실값
============================== ============ ==============================
``AnalogTag`` / ``DigitalTag``  ANALOG/DIGITAL ``FLOAT`` (50행)
``GoodQualityRealTimeData``     GOOD         ``0`` / ``1`` / ``2``
``HighSeverityAlarmEvent``      HIGH         ``High`` (대소문자만 다름!)
``InboundInventoryTransaction`` IN           ``Inbound``
``RecycledWasteManagement``     Recycle      ``Recycling``
============================== ============ ==============================

일부는 **대소문자·어미만 다르다** (``HIGH``/``High``, ``Recycle``/``Recycling``).
그래서 "고치기 쉬워 보인다" — 그러나 **자동 교정하지 않는다**:

* 값 어휘는 SME 소유다. ``Recycle`` 을 ``Recycling`` 으로 바꾸는 것은 도메인 판단
  이고, ``IN`` → ``Inbound`` 는 그 CSV 세대에만 맞을 수 있다.
* 공리를 데이터에 맞추는 것은 **지표 매수**다. 이 리포에 같은 판단이 기록돼 있다
  (파티션 정리 시 "공리 값을 CSV 에 맞춰 고치지 않는다").
* 대소문자만 다른 경우조차 위험하다 — 추론기는 리터럴을 정확히 비교하므로
  ``High`` 로 바꾸면 맞지만, 다음 CSV 가 ``HIGH`` 로 오면 다시 0이 된다. 근본
  해결은 값 정규화 정책이고 그것도 SME 결정이다.

## B — 정의 공리 없음: 이름만 있는 클래스

``BadQualityRealTimeData`` / ``EquipmentAsset`` / ``HighStrengthProduct`` 등 7개는
``owl:equivalentClass`` 도 ``owl:Restriction`` 도 없다. 추론기가 어떤 개체를 이
클래스로 분류할 **근거가 없으므로** 영구히 빈다.

이것도 지우지 않는다 — ``step_21b`` 가 이미 6개 신호로 "죽은 스텁" 을 판정하고,
이 클래스들은 계층에 자리가 있어(부모가 있고 disjoint 그룹 멤버) 보존 대상이다.
지우면 계층과 파티션이 함께 무너진다.

## 그래서 무엇을 하는가 — 침묵을 없앤다

이 상태의 진짜 문제는 **아무 게이트도 이것을 보고하지 않는다**는 것이다.
``class_instance_count`` check 는 "인스턴스 0" 을 세지만 **원인을 구분하지 않아**
"추상 클래스라 정상" 과 "값이 어긋나 영구히 빔" 이 같은 줄에 나온다. 그래서
14건이 상수 노이즈로 깔려 아무도 읽지 않았다.

이 스텝은 각 클래스를 원인별로 분류하고, 값 불일치에는 **CSV 실값과 근접 후보**
를 함께 낸다 (SME 가 판단할 재료). read-only 이고 T-Box 를 수정하지 않는다.

환경변수:
  - ``TBOX_UNFILLABLE_GATE``: ``warn`` (default) | ``fail``
  - ``TBOX_UNFILLABLE_MAX``: 허용 개수 (default 14 — 현재 실측값. 낮추려면
    원인을 고쳐야 하고, **올리는 것은 게이트를 끄는 것**이다)
"""
from __future__ import annotations

import contextlib
import csv
import difflib
import logging
import os

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")

#: 현재 실측 상한. 올리면 게이트가 꺼지는 방향이므로 **원인을 고쳐서** 내려야 한다.
_DEFAULT_MAX = 14


def _csv_values_for(dp_uri: URIRef, parent_local: str, g: Graph) -> dict[str, int]:
    """DP 의 ``dcterms:source`` 컬럼에서 CSV 실값 분포를 읽는다."""
    from config import SOURCE_RAWDATA_DIR

    columns = [
        str(o).strip() for o in g.objects(dp_uri, _DCTERMS_SOURCE) if str(o).strip()
    ]
    if not columns:
        return {}
    try:
        from domain.table_mapping import load_table_class_mapping
        table_class = load_table_class_mapping()
    except Exception:  # noqa: BLE001
        return {}
    table = next(
        (t for t, cls in (table_class or {}).items() if cls == parent_local), None,
    )
    if not table:
        return {}
    path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    header = {k.lower(): k for k in rows[0] if k}
    column = header.get(columns[0].lower())
    if not column:
        return {}
    out: dict[str, int] = {}
    for row in rows:
        value = str(row.get(column, "")).strip()
        if value:
            out[value] = out.get(value, 0) + 1
    return out


def _instance_counts(domain_ns: str) -> dict[str, int] | None:
    """추론 그래프에서 클래스별 typed 인스턴스 수. 실패 시 ``None``.

    ``None`` 은 **판정 불가** 다 — 0 과 혼동하면 정상 클래스를 전부 미충족으로
    보고한다 (이 리포의 fail-closed 규약).
    """
    import re

    from config import INFERRED_PATH

    if not os.path.exists(INFERRED_PATH):
        return None
    inst_ns = domain_ns.rstrip("#") + "/instances#"
    subj_re = re.compile(r"^<" + re.escape(inst_ns) + r"[^>]+>")
    cls_re = re.compile(r"<" + re.escape(domain_ns) + r"([A-Za-z0-9_]+)>")
    counts: dict[str, int] = {}
    buffer: list[str] = []
    try:
        with open(INFERRED_PATH, encoding="utf-8") as handle:
            for line in handle:
                buffer.append(line)
                if not line.rstrip().endswith("."):
                    continue
                statement = "".join(buffer)
                buffer = []
                if not subj_re.match(statement):
                    continue
                match = re.search(r"(?:^|;|\s)a\s(.*?)(?:;|\.\s*$)", statement, re.S)
                if not match:
                    continue
                for name in cls_re.findall(match.group(1)):
                    counts[name] = counts.get(name, 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("step_31b: 추론 그래프 읽기 실패: %s", exc)
        return None
    return counts


def _classify(g: Graph, domain_ns: str, cls: URIRef) -> dict:
    """빈 클래스 하나의 원인을 분류한다."""
    local = str(cls)[len(domain_ns):]
    parents = [
        str(p)[len(domain_ns):] for p in g.objects(cls, RDFS.subClassOf)
        if isinstance(p, URIRef) and str(p).startswith(domain_ns)
    ]
    # 정의 공리 찾기 — equivalentClass → Restriction(onProperty, hasValue).
    for eq in g.objects(cls, OWL.equivalentClass):
        if (eq, RDF.type, OWL.Restriction) not in g:
            continue
        prop = next(iter(g.objects(eq, OWL.onProperty)), None)
        value = next(iter(g.objects(eq, OWL.hasValue)), None)
        if prop is None or value is None:
            continue
        parent = parents[0] if parents else ""
        actual = _csv_values_for(prop, parent, g) if parent else {}
        if not actual:
            return {
                "class": local, "cause": "evidence_unavailable",
                "detail": "CSV 대조 불가 (출처 컬럼/테이블 미확인) — 판정 보류",
            }
        claimed = str(value)
        # 근접 후보를 제시한다. **자동 교정은 하지 않는다** — 값 어휘는 SME 소유다.
        #
        # 대소문자를 무시하고 비교한다: 실측 불일치의 상당수가 표기 차이다
        # (``HIGH``/``High``, ``Recycle``/``Recycling``, ``IN``/``Inbound``).
        # 대소문자를 그대로 비교하면 ``difflib`` 이 그것들을 놓쳐 근접 후보가
        # 비고, 그러면 SME 가 "완전히 다른 값" 으로 오해한다.
        lower_map: dict[str, str] = {}
        for original in actual:
            lower_map.setdefault(original.lower(), original)
        near_lower = difflib.get_close_matches(
            claimed.lower(), list(lower_map), n=3, cutoff=0.5,
        )
        near = [lower_map[k] for k in near_lower]
        # 접두 관계도 근접으로 본다 (``IN`` → ``Inbound``). difflib 은 길이 차가
        # 크면 유사도를 낮게 주므로 이것만으로는 놓친다.
        for original in actual:
            low = original.lower()
            claim_low = claimed.lower()
            if original in near:
                continue
            if low.startswith(claim_low) or claim_low.startswith(low):
                near.append(original)
        near = near[:3]
        return {
            "class": local,
            "cause": "value_mismatch",
            "on_property": str(prop)[len(domain_ns):],
            "claimed_value": claimed,
            "csv_values": dict(sorted(actual.items(), key=lambda kv: -kv[1])[:6]),
            "near_matches": near,
            "detail": (
                f"정의 공리가 '{claimed}' 를 요구하지만 CSV 에 그 값이 없다"
                + (f" — 근접 후보: {near}" if near else "")
                + ". 값 어휘는 SME 가 확정해야 한다 (공리를 데이터에 맞춰 "
                  "자동 수정하면 다음 CSV 세대에서 다시 어긋난다)."
            ),
        }
    # 정의 공리가 없다 — 추론기가 분류할 근거가 없다.
    return {
        "class": local,
        "cause": "no_definition",
        "parents": parents,
        "detail": (
            "owl:equivalentClass Restriction 도 판별 DP 도 없어 추론기가 어떤 "
            "개체를 이 클래스로 분류할 근거가 없다. 계층에 자리가 있어 "
            "step_21b 가 보존하므로 영구히 빈 상태로 남는다 — 판별 조건을 "
            "추가하거나 클래스를 재검토하라."
        ),
    }


def _audit(g: Graph, domain_ns: str) -> dict:
    counts = _instance_counts(domain_ns)
    if counts is None:
        return {
            "skipped": "inferred_graph_unavailable",
            "unfillable": [],
            "unfillable_count": 0,
        }
    declared = sorted(
        (c for c in g.subjects(RDF.type, OWL.Class)
         if isinstance(c, URIRef) and str(c).startswith(domain_ns)),
        key=str,
    )
    findings: list[dict] = []
    for cls in declared:
        local = str(cls)[len(domain_ns):]
        # 익명 표현식 skolem 노드는 클래스 열거 대상이 아니다 (공용 규약).
        if local.startswith("Union_") or "_someValuesFrom" in local:
            continue
        if counts.get(local, 0) > 0:
            continue
        findings.append(_classify(g, domain_ns, cls))

    by_cause: dict[str, int] = {}
    for item in findings:
        by_cause[item["cause"]] = by_cause.get(item["cause"], 0) + 1
    return {
        "unfillable_count": len(findings),
        "by_cause": by_cause,
        "unfillable": findings,
        "classes_declared": len(declared),
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """추론 후에도 빈 클래스를 원인별로 분류한다 (read-only)."""
    try:
        stats = _audit(g, ctx.domain_ns)
    except Exception as exc:  # noqa: BLE001 — 진단이 파이프라인을 막지 않는다
        logger.warning("step_31b: 감사 실패 (건너뜀): %s", exc)
        return StepResult(
            name="step_31b_unfillable_class_audit",
            stats={"unfillable_class_audit": {"error": str(exc)[:200]}},
            step_number="31b",
            step_label="unfillable_class_audit",
        )

    count = stats.get("unfillable_count", 0)
    if count:
        mismatch = [f for f in stats["unfillable"] if f["cause"] == "value_mismatch"]
        logger.warning(
            "Step 31b: 추론 후에도 인스턴스 0인 클래스 %d개 (원인별 %s). "
            "class_instance_count check 는 원인을 구분하지 않아 '추상 클래스라 "
            "정상' 과 '값이 어긋나 영구히 빔' 을 같은 줄에 낸다 — 그래서 이 %d건이 "
            "상수 노이즈로 깔려 있었다. 값 불일치 %d건은 근접 후보와 함께 "
            "보고한다 (자동 교정하지 않는다 — 값 어휘는 SME 소유).",
            count, stats.get("by_cause"), count, len(mismatch),
        )
        for item in mismatch[:5]:
            logger.warning(
                "  [값 불일치] %s: 공리='%s' CSV=%s 근접=%s",
                item["class"], item.get("claimed_value"),
                list(item.get("csv_values", {}))[:4], item.get("near_matches"),
            )

    max_allowed = _DEFAULT_MAX
    with contextlib.suppress(ValueError):
        max_allowed = int(os.getenv("TBOX_UNFILLABLE_MAX", str(max_allowed)))
    mode = os.getenv("TBOX_UNFILLABLE_GATE", "warn").strip().lower()
    if mode == "fail" and count > max_allowed:
        raise RuntimeError(
            f"채워질 수 없는 클래스 {count}개 > 허용 {max_allowed}. "
            f"원인별: {stats.get('by_cause')}. "
            "TBOX_UNFILLABLE_MAX 를 올리는 것은 게이트를 끄는 것이다 — "
            "값 어휘(SME)나 판별 조건을 고쳐라."
        )

    return StepResult(
        name="step_31b_unfillable_class_audit",
        stats={"unfillable_class_audit": stats},
        step_number="31b",
        step_label="unfillable_class_audit",
    )
