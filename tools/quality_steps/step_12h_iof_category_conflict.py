"""Step 12h — IOF 부모의 **BFO 범주 충돌** 제거 (unsat 방지).

## 왜 필요한가

IOF namespace 교정으로 참조가 실제로 해소되자(3/24 → 19/26) 숨어 있던 결함이
드러났다 — HermiT **unsat 8건** (2026-08-18 실측):

    ChemicalAnalysis, SurfaceQuality, NDTResults, MechanicalProperties,
    ProductionResult, DimensionalData, Transportation, SupplierMaster

원인은 한 클래스가 **양립 불가한 IOF 부모 두 개**를 갖는 것이다:

| 클래스 | 충돌 |
|--------|------|
| ``SupplierMaster`` | ``InformationContentEntity`` + ``Organization`` |
| ``QualityManagement`` (→ 자손 5개) | ``…InformationContentEntity`` + ``MaterialArtifact`` |
| ``Transportation`` | ``InformationContentEntity`` + ``MaterialLocationChangeProcess`` |
| ``ProductionResult`` | ``InformationContentEntity`` + ``ProductProductionProcess`` |

BFO 는 continuant(정보/물질) 와 occurrent(프로세스) 를 배타적으로 두므로, 두
분기를 동시에 상속하면 인스턴스를 가질 수 없다. 앞의 셋은 S2 LLM 이 만든 직접
매핑이고 정보 계부모는 ``design_patterns.json`` 의 추상 계층에서 온다.

## 판정 기준 — 데이터가 무엇인지 본다

CSV 를 보면 네 클래스 모두 **기록** 이다:
``Production_Result.csv`` = ``Result_ID`` + 수량/수율, ``Transportation.csv`` =
``Transport_ID`` + 출발시각, ``Supplier_Master.csv`` = 공급업체 마스터 레코드.
프로세스·조직·물질 **자체** 가 아니라 그것에 **대한 기록** 이다. 그래서 정보
엔티티 계보를 유지하고 **반대 범주 매핑을 제거** 한다.

제거는 IOF 부모에 한정한다 — 도메인 계층(``steel:``)은 건드리지 않는다.

## 선언자에게 적용한다 (2026-08-18 정정)

처음에는 "그 클래스가 **직접** 선언한 IOF 부모만 제거" 로 썼다. 그러면 충돌 부모를
조상이 선언한 경우 **아무도 처리하지 않는다** — 그 조상 자신은 자기 조상에 정보
계보가 없어 판정에서 빠지기 때문이다. 실측으로 드러난 사례:

    EquipmentManagement ⊑ iof:MaterialArtifact          ← 물질 (여기가 선언자)
     ├ EquipmentAsset ─┬ TagMaster       ⊑ MasterData (정보) + iof:Identifier
     │                 └ EquipmentMaster ⊑ MasterData (정보)
     └ EquipmentEvent ─┬ EquipmentStatus ⊑ TransactionRecord (정보)
                       └ AlarmEvents     ⊑ iof:Event (occurrent)

``TagMaster`` 등 4개가 unsat 인데 이 스텝은 **0건 제거**를 보고했다. 카운터가 0 인
것과 결함이 없는 것은 다르다. 이제 충돌 부모를 실제로 **선언한** 도메인 클래스
(자신 또는 조상) 에서 끊는다.

## 왜 게이트가 아니라 정리인가

이 매핑은 S2 LLM 이 매 재생성마다 다시 만들 수 있다. 경고만 남기면 배포 T-Box 가
unsat 상태로 나가고, unsat 이면 추론기가 그 클래스에 대해 **아무것도** 말하지
않는다 (조용한 전손). 결정적으로 제거하고 stats·로그에 남긴다.
"""
from __future__ import annotations

import logging

from rdflib import RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: BFO 범주 축 — **서로 배타적** 인 두 묶음. IOF 클래스의 조상에 어느 쪽이
#: 나타나는지로 판정한다.
#:
#: ⚠️ 동봉된 ``bfo.owl`` 은 563 트리플 **스텁** 이고 상위 계층이 없다 (실측:
#: ``BFO_0000015`` / ``BFO_0000031`` / ``BFO_0000027`` 모두 ``rdfs:subClassOf`` 가
#: 비어 있다). 그래서 ``BFO_0000002``(continuant) / ``BFO_0000003``(occurrent)
#: 최상위까지 올라가는 판정은 **영구히 발화하지 않는다** — 처음 그렇게 썼다가
#: 제거 0건을 실측했다. IOF 체인이 실제로 닿는 리프 IRI 를 직접 열거한다.
_BFO = "http://purl.obolibrary.org/obo/"
#: occurrent 계열 (process / process boundary / temporal region 등)
_BFO_OCCURRENT_LEAVES = frozenset({
    _BFO + "BFO_0000003",   # occurrent
    _BFO + "BFO_0000015",   # process
    _BFO + "BFO_0000035",   # process boundary
})
#: 정보가 **아닌** continuant 계열 (물질·독립 연속체·집합체)
_BFO_MATERIAL_LEAVES = frozenset({
    _BFO + "BFO_0000004",   # independent continuant
    _BFO + "BFO_0000040",   # material entity
    _BFO + "BFO_0000027",   # object aggregate
    _BFO + "BFO_0000030",   # object
})
#: 정보 계열 (generically dependent continuant)
_BFO_INFO_LEAVES = frozenset({
    _BFO + "BFO_0000031",   # generically dependent continuant
})

#: 정보 엔티티 계보의 뿌리 — 이 계보를 **유지** 하고 반대 범주를 제거한다.
#: 도메인-중립: IOF(외래 표준) 어휘이고 자사 클래스명을 박지 않는다.
_INFO_ROOT_LOCAL = "InformationContentEntity"


def _iof_ns() -> str:
    from domain.namespaces import IOF_CORE

    return IOF_CORE


def _load_reference_graph() -> Graph | None:
    """동봉된 IOF/BFO 파일을 읽어 범주 판정용 그래프를 만든다.

    파일이 없으면 ``None`` — 판정 불가이므로 아무것도 제거하지 않는다
    (0건과 판정 불가를 혼동하면 정당한 매핑을 지운다).
    """
    import os

    import config

    base = getattr(config, "SOURCE_REFERENCE_DIR", None) or os.path.join(
        "data", "source", "reference",
    )
    if not os.path.isdir(base):
        return None
    g = Graph()
    loaded = 0
    for fname in sorted(os.listdir(base)):
        if not fname.lower().endswith((".rdf", ".owl", ".ttl")):
            continue
        try:
            g.parse(os.path.join(base, fname))
            loaded += 1
        except Exception as exc:  # noqa: BLE001 — 일부 실패는 무시
            logger.debug("step_12h: reference 파싱 실패 (%s): %s", fname, exc)
    return g if loaded else None


def _remove_category_conflicts(g: Graph, steel_str: str) -> dict:
    ref = _load_reference_graph()
    if ref is None:
        logger.info(
            "step_12h: IOF/BFO reference 파일 없음 — 범주 충돌 판정을 건너뛴다",
        )
        return {"iof_category_conflicts_removed": 0, "iof_category_conflict_samples": []}

    iof_ns = _iof_ns()

    def _ancestors(node: URIRef, graph: Graph, seen: set | None = None) -> set:
        seen = seen or set()
        if node in seen:
            return set()
        seen.add(node)
        out = {node}
        for p in graph.objects(node, RDFS.subClassOf):
            if isinstance(p, URIRef):
                out |= _ancestors(p, graph, seen)
        return out

    def _axis(iof_cls: URIRef) -> str | None:
        """IOF 클래스의 BFO 축: ``occurrent`` / ``material`` / ``info`` / None."""
        anc = {str(a) for a in _ancestors(iof_cls, ref)}
        if anc & _BFO_OCCURRENT_LEAVES:
            return "occurrent"
        if anc & _BFO_MATERIAL_LEAVES:
            return "material"
        if anc & _BFO_INFO_LEAVES:
            return "info"
        return None

    def _is_info(iof_cls: URIRef) -> bool:
        """정보 엔티티 계보인가 — IOF 루트 또는 BFO gdc 로 판정."""
        anc = _ancestors(iof_cls, ref)
        if any(str(a) == iof_ns + _INFO_ROOT_LOCAL for a in anc):
            return True
        return bool({str(a) for a in anc} & _BFO_INFO_LEAVES)

    removed = 0
    samples: list[str] = []
    domain_classes = sorted(
        {s for s in g.subjects(RDFS.subClassOf, None)
         if isinstance(s, URIRef) and str(s).startswith(steel_str)},
        key=str,
    )
    # 충돌 부모는 그 클래스가 **직접** 선언했을 수도, 조상이 선언했을 수도 있다.
    # 어느 쪽이든 제거는 **선언자** 에게 적용해야 한다 — 아래 (선언자, 부모) 쌍으로
    # 모아 중복 제거한다.
    #
    # 처음에는 "직접 선언만 제거, 상속분은 그 상위 클래스를 처리할 때" 로 썼는데,
    # 그 상위 클래스는 자신의 조상에 정보 계보가 없어 ``has_info`` 판정에서 빠지고
    # 결과적으로 **아무도 처리하지 않는다** — 카운터는 0건인데 unsat 은 남는 침묵이
    # 된다 (실측 2026-08-18: ``EquipmentManagement ⊑ iof:MaterialArtifact`` 가
    # 2~3홉 아래 ``TagMaster`` / ``EquipmentMaster`` / ``EquipmentStatus`` /
    # ``AlarmEvents`` 를 unsat 으로 만들었고 이 스텝은 0건을 보고했다).
    to_remove: set[tuple[URIRef, URIRef]] = set()
    for cls in domain_classes:
        # 이 클래스의 **모든** 조상(도메인 계층 포함) 에서 IOF 부모를 모은다 —
        # 충돌은 직접 매핑과 상위 추상 계층 사이에서 생긴다.
        anc = _ancestors(cls, g)
        iof_parents = {
            a for a in anc if isinstance(a, URIRef) and str(a).startswith(iof_ns)
        }
        axes = {p: _axis(p) for p in iof_parents}
        has_info = any(_is_info(p) for p in iof_parents)
        if not has_info:
            continue
        # 정보 계보가 있는데 occurrent(프로세스) 나 material(물질·조직) 도
        # 있으면 그 반대 범주 매핑을 끊는다. 데이터가 "기록" 이라는 판정에 따라
        # 정보 계보를 유지한다 (모듈 docstring 의 CSV 근거 참조).
        for p, axis in axes.items():
            if axis not in ("occurrent", "material"):
                continue
            if _is_info(p):
                continue
            # 이 부모를 **선언한** 도메인 클래스를 찾는다 (cls 자신이거나 조상).
            for declarer in g.subjects(RDFS.subClassOf, p):
                if not isinstance(declarer, URIRef):
                    continue
                if not str(declarer).startswith(steel_str):
                    continue
                if declarer not in anc:
                    continue
                to_remove.add((declarer, p))

    for declarer, p in sorted(to_remove, key=lambda t: (str(t[0]), str(t[1]))):
        if (declarer, RDFS.subClassOf, p) not in g:
            continue
        g.remove((declarer, RDFS.subClassOf, p))
        removed += 1
        if len(samples) < 10:
            samples.append(
                f"{str(declarer)[len(steel_str):]} -⊑ iof:{str(p)[len(iof_ns):]} "
                f"({_axis(p)})",
            )

    if removed:
        logger.warning(
            "Step 12h: IOF 범주 충돌 %d건 제거 — 한 클래스가 정보 엔티티와 "
            "프로세스/물질 계보를 동시에 상속하면 BFO 배타성 때문에 "
            "**unsatisfiable** 이 되고, 추론기가 그 클래스에 대해 아무것도 "
            "말하지 않는다. 예: %s",
            removed, samples[:5],
        )
    return {
        "iof_category_conflicts_removed": removed,
        "iof_category_conflict_samples": samples,
    }


def _remove_property_axis_conflicts(g: Graph, steel_str: str) -> dict:
    """OP 의 ``rdfs:subPropertyOf iof:*`` 가 **domain 범주와 어긋나면** 끊는다.

    ## 왜 필요한가 (2026-08-30 실측)

    클래스 축(위 :func:`_remove_category_conflicts`)만 보면 **프로퍼티를 통한**
    범주 오염을 놓친다. IOF 상위 프로퍼티는 자기 ``rdfs:domain`` 을 갖고 있고,
    하위 프로퍼티를 선언하면 그 domain 이 **하위의 domain 클래스에 전파**된다:

        steel:hasFuelEnergySource
            rdfs:domain steel:FuelConsumption ;
            rdfs:subPropertyOf iof:hasInput .

        iof:hasInput  rdfs:domain obo:BFO_0000015 (process)

    ⇒ ``FuelConsumption`` 이 process 가 되어야 하는데, 그 클래스는
    ``iof:MeasurementInformationContentEntity`` (정보 개체) 다. BFO 는
    occurrent(process) 와 continuant(정보) 를 배타적으로 두므로 **unsatisfiable**.

    실측: 배포 T-Box 의 IOF subPropertyOf 선언 12건 중 이것 **하나**가 unsat 을
    만들었고, 나머지 11건(``hasParticipantAtSomeTime`` 8 / ``isLocatedIn`` 3)은
    상위 domain 이 범주 충돌을 일으키지 않아 안전하다. 즉 "IOF 매핑을 쓰지 말라"
    가 아니라 **범주가 맞는지 보라** 가 정답이다.

    ## 왜 명명 Restriction 이 이것을 숨겼나

    이 unsat 은 T-Box 를 **익명 Restriction 으로 정규화한 뒤에만** 드러난다.
    배포 상태(명명 IRI)에서 HermiT 은 ``consistent: true`` / ``unsat 0`` 을
    반환한다 — ``check_reasoner_blindness`` 가 실증하는 눈멂이다. 그래서 이 정리는
    익명화 수정과 **같이 착륙해야** 한다: 익명화만 하면 S4 가 선재 결함으로
    hard-fail 하고, 이 정리만 하면 검증기가 여전히 눈멀어 있다.

    제거는 **IOF 상위 프로퍼티 링크에 한정** 한다 — OP 자체와 domain/range 는
    건드리지 않는다 (관계는 유효하고 표준 정렬만 틀렸다).
    """
    ref = _load_reference_graph()
    if ref is None:
        return {"iof_property_axis_conflicts_removed": 0,
                "iof_property_axis_conflict_samples": []}

    iof_ns = _iof_ns()

    def _ancestors(node: URIRef, graph: Graph, seen: set | None = None) -> set:
        seen = seen or set()
        if node in seen:
            return set()
        seen.add(node)
        out = {node}
        for p in graph.objects(node, RDFS.subClassOf):
            if isinstance(p, URIRef):
                out |= _ancestors(p, graph, seen)
        return out

    def _bfo_axis_of(cls: URIRef, graph: Graph) -> str | None:
        """클래스의 BFO 축 — 도메인 그래프와 reference 를 함께 거슬러 올라간다."""
        # 도메인 계층으로 IOF 부모까지 올라간 뒤, reference 로 BFO 축을 본다.
        anc = _ancestors(cls, graph)
        for a in anc:
            if not isinstance(a, URIRef):
                continue
            ref_anc = {str(x) for x in _ancestors(a, ref)}
            if ref_anc & _BFO_OCCURRENT_LEAVES:
                return "occurrent"
            if ref_anc & _BFO_INFO_LEAVES or any(
                str(x) == iof_ns + _INFO_ROOT_LOCAL for x in _ancestors(a, ref)
            ):
                return "info"
            if ref_anc & _BFO_MATERIAL_LEAVES:
                return "material"
        return None

    removed = 0
    samples: list[str] = []
    for prop, super_prop in sorted(
        g.subject_objects(RDFS.subPropertyOf), key=lambda t: (str(t[0]), str(t[1])),
    ):
        if not (isinstance(prop, URIRef) and str(prop).startswith(steel_str)):
            continue
        if not (isinstance(super_prop, URIRef) and str(super_prop).startswith(iof_ns)):
            continue
        # 상위 프로퍼티가 요구하는 domain 축.
        super_domains = [
            d for d in ref.objects(super_prop, RDFS.domain) if isinstance(d, URIRef)
        ]
        if not super_domains:
            continue
        required = None
        for d in super_domains:
            ref_anc = {str(x) for x in _ancestors(d, ref)} | {str(d)}
            if ref_anc & _BFO_OCCURRENT_LEAVES:
                required = "occurrent"
                break
            if ref_anc & _BFO_INFO_LEAVES:
                required = "info"
                break
            if ref_anc & _BFO_MATERIAL_LEAVES:
                required = "material"
                break
        if required is None:
            continue
        # 이 OP 의 domain 클래스가 실제로 어느 축인가.
        own_domains = [
            d for d in g.objects(prop, RDFS.domain)
            if isinstance(d, URIRef) and str(d).startswith(steel_str)
        ]
        if len(own_domains) != 1:
            # domain 이 없거나 여럿이면 판정 근거가 약하다 — 건드리지 않는다.
            continue
        actual = _bfo_axis_of(own_domains[0], g)
        if actual is None or actual == required:
            continue
        g.remove((prop, RDFS.subPropertyOf, super_prop))
        removed += 1
        if len(samples) < 10:
            samples.append(
                f"{str(prop)[len(steel_str):]} -⊑ "
                f"iof:{str(super_prop)[len(iof_ns):]} "
                f"(상위 domain={required} vs "
                f"{str(own_domains[0])[len(steel_str):]}={actual})",
            )

    if removed:
        logger.warning(
            "Step 12h: IOF 상위 프로퍼티 범주 충돌 %d건 제거 — 상위 프로퍼티의 "
            "rdfs:domain 이 하위 OP 의 domain 클래스에 전파되어 BFO 배타 범주를 "
            "강요하면 그 클래스가 **unsatisfiable** 이 된다 (명명 Restriction "
            "상태에서는 HermiT 이 이것을 보지 못한다). 예: %s",
            removed, samples[:5],
        )
    return {
        "iof_property_axis_conflicts_removed": removed,
        "iof_property_axis_conflict_samples": samples,
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    stats = _remove_category_conflicts(g, ctx.domain_ns)
    stats.update(_remove_property_axis_conflicts(g, ctx.domain_ns))
    return StepResult(
        name="step_12h_iof_category_conflict",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="12h",
        step_label="iof_category_conflict",
    )
