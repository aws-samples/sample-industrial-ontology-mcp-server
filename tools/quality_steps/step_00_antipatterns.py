"""Step 0 — 안티패턴 자동 수정 (5 sub-step 통합).

본문 ontology_quality.py 의 Step 0 블록을 그대로 모듈로 옮김. 5 sub-step:

- 0a Lonely Disjoint count (Step 1 이 재구축하므로 카운트만 — 비파괴)
- 0b Asymmetric inverseOf 수정 (graph 에 이미 있는 inverseOf 를 대칭화)
- 0c Redundant subClassOf 제거 (``_remove_redundant_subclass`` wrapper)
- 0d TransitiveProperty + inverseOf 충돌 해소 (OWL RL 추론 폭발 방지)
- 0e 유령 URI 교정 (``<...#pfx:foo>`` → 그 prefix 의 진짜 네임스페이스)
"""
from __future__ import annotations

import logging
import re

import rdflib
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def _count_lonely_disjoint(g: Graph) -> int:
    """0a — AllDisjointClasses 의 멤버 수가 1 이하인 그룹 카운트."""
    count = 0
    for bnode in list(g.subjects(RDF.type, OWL.AllDisjointClasses)):
        members_node = list(g.objects(bnode, OWL.members))
        if not members_node:
            continue
        try:
            members = list(rdflib.collection.Collection(g, members_node[0]))
        except Exception as e:
            logger.debug(
                "AllDisjointClasses members collection 파싱 실패: %s", e
            )
            continue
        if len(members) <= 1:
            count += 1
            logger.info(
                "Lonely AllDisjointClasses 감지 (%d멤버) — Step 1에서 재구축됨",
                len(members),
            )
    return count


#: ``is<X>Of`` / ``<x>Has<Y>`` 처럼 **이름이 방향을 선언하는** OP 를 판별하는 정규식.
#: 도메인 중립: 클래스 이름을 박지 않고 접두/접미 형태만 본다.
_NAME_SAYS_INVERSE = re.compile(r"^is[A-Z].*Of([A-Z]\w*)?$")
_NAME_SAYS_FORWARD = re.compile(r"^(has[A-Z]\w*|\w+Has[A-Z]\w*)$")


def _fix_name_direction_contradiction(g: Graph, steel_str: str) -> tuple[int, list[str]]:
    """0f — **이름이 뜻하는 방향과 domain/range 가 반대인 OP** 를 교정.

    ## 왜 필요한가

    ``isProductOfRolling`` 은 이름이 "이 제품은 압연 공정의 산출물이다" 를 뜻하므로
    ``domain=ProductMaster, range=ProcessRolling`` 이어야 한다. 그런데 S2 가 그 반대로
    (``ProcessRolling → ProductMaster``) 선언한 채 배포됐다.

    그 자체로도 틀리지만 **전파** 가 더 나쁘다 (2026-08-15 실측 인과):

      1. S2 가 ``isProductOfRolling`` 을 Process→Product 로 잘못 선언.
      2. Jury 가 짝인 ``rollingHasProduct`` 의 domain/range 를 **삭제** (커밋 383f8bd
         의 jury_fixes 가드가 이제 막는 경로).
      3. ``step_09b`` 가 "range 누락 + inverseOf 있음 → inverse.domain 을 range 로"
         규칙으로 채운다 — 그 inverse 의 domain 이 이미 뒤집혀 있으므로 오류가 복제된다.
      4. 결과: ``rollingHasProduct = ProductMaster → ProcessRolling`` 이고, 이 OP 는
         ``rdfs:subPropertyOf iof-core:hasOutput`` 이다 ⟹ **"제품이 공정을 산출한다"**
         를 함의한다. owlrl 실측: ``P034 iof-core:hasOutput PR_1`` = True,
         올바른 방향 = False.

    어떤 게이트도 잡지 못했다 — domain/range 가 **존재하므로** conformance 통과,
    두 클래스가 disjoint 도 아니라 HermiT consistent, ``check_quality_rules`` 는
    이름과 방향의 관계를 보지 않는다 (107 이슈 중 critical 0 / high 0).

    ## 판정 기준 (보수적 — 이름이 스스로 대상을 지목할 때만)

    ``is<X>Of<Target>`` 형태는 이름 안에 **range 가 무엇이어야 하는지** 가 적혀 있다.
    ``isProductOfRolling`` 의 ``Rolling`` 은 range 쪽 클래스를 가리킨다. 그래서
    ``domain`` 이 그 ``<Target>`` 과 일치하면 (즉 이름이 range 라고 말한 것을 domain
    으로 선언했으면) **방향이 뒤집힌 것이 확실하다**.

    ``<Target>`` → 클래스 매칭은 접미 일치로 한다: 선언된 도메인 클래스 중 이름이
    ``<Target>`` 로 끝나는 것 (``Rolling`` → ``ProcessRolling``). 후보가 여러 개면
    모호하므로 **건드리지 않는다**.

    ``Of`` 뒤가 비어 있거나(``isProductOf``) 매칭 클래스가 없거나 domain/range 가
    단일하지 않으면 건드리지 않는다 — 틀린 교정은 없는 교정보다 나쁘다 (같은
    파이프라인의 step_15c domain/range 처리와 같은 원칙).

    짝 OP 의 상태는 **보지 않는다**: 실측에서 짝(``rollingHasProduct``)은 Jury 가
    domain/range 를 지운 상태(0건)였다. 짝이 온전할 것을 요구하면 정작 이 사고를
    못 잡는다.
    """
    fixed = 0
    samples: list[str] = []

    def _ln(u) -> str:
        return str(u).rsplit("/", 1)[-1].rsplit("#", 1)[-1]

    domain_classes = {
        _ln(c) for c in g.subjects(RDF.type, OWL.Class)
        if isinstance(c, URIRef) and str(c).startswith(steel_str)
    }

    for prop in sorted(g.subjects(RDF.type, OWL.ObjectProperty), key=str):
        if not str(prop).startswith(steel_str):
            continue
        name = _ln(prop)
        m = _NAME_SAYS_INVERSE.match(name)
        if not m:
            continue
        target = m.group(1)                    # ``Of`` 뒤의 토큰 (없으면 None)
        if not target:
            continue
        doms = [x for x in g.objects(prop, RDFS.domain) if isinstance(x, URIRef)]
        rngs = [x for x in g.objects(prop, RDFS.range) if isinstance(x, URIRef)]
        if len(doms) != 1 or len(rngs) != 1:
            continue                           # 모호 — 판정 보류
        dom, rng = doms[0], rngs[0]
        # 이름이 지목한 <Target> 에 해당하는 선언 클래스 찾기.
        # 접미 일치를 먼저 보고, 없으면 부분 문자열로 넓힌다 — 클래스명이 접미가
        # 아닌 위치에 토큰을 담는 경우가 있다 (실측: ``OfSteelmaking`` →
        # ``ProcessSteelmakingFurnace``, 접미로는 0건).
        # 어느 단계든 **후보가 유일할 때만** 채택한다. 넓힌 매칭은 모호해질 수 있는데
        # (실측: ``Product`` → 5개, ``Equipment`` → 4개) 그때는 판정을 보류한다.
        matches = [c for c in domain_classes if c.endswith(target)]
        if len(matches) != 1:
            matches = [c for c in domain_classes if target in c]
        if len(matches) != 1:
            continue                           # 후보 0 또는 다수 — 판정 보류
        target_cls = matches[0]
        if _ln(dom) != target_cls or _ln(rng) == target_cls:
            continue                           # domain 이 <Target> 이 아니면 정상
        # 뒤집는다.
        g.remove((prop, RDFS.domain, dom))
        g.remove((prop, RDFS.range, rng))
        g.add((prop, RDFS.domain, rng))
        g.add((prop, RDFS.range, dom))
        fixed += 1
        samples.append(
            f"{name}: {_ln(dom)}→{_ln(rng)} ⇒ {_ln(rng)}→{_ln(dom)} "
            f"(이름의 'Of{target}' 가 range 를 지목하는데 domain 이었다)"
        )
        logger.warning(
            "안티패턴 0f: %s 의 domain 이 이름이 지목한 range 클래스(%s)였다 — "
            "뒤집었다 (%s→%s ⇒ %s→%s). 원인은 S2 오선언이며, step_09b 가 이것을 "
            "짝 OP 로 복제하면 iof-core:hasOutput 하위에서 '제품이 공정을 산출' 을 "
            "함의한다",
            name, target_cls, _ln(dom), _ln(rng), _ln(rng), _ln(dom),
        )
    return fixed, samples


def _drop_reversed_subproperty(g: Graph, steel_str: str) -> tuple[int, list[str]]:
    """0g — ``rdfs:subPropertyOf`` 가 **방향이 반대인** 부모를 가리키면 제거.

    ``P ⊑ Q`` 는 "P 로 이어진 모든 쌍이 Q 로도 이어진다" 를 뜻한다. 그래서 P 가
    ``A→B`` 이고 Q 가 ``B→A`` 면 그 선언은 **역방향 트리플을 파생시킨다** —
    A-Box 사실 ``x P y`` 에서 ``x Q y`` 가 나오는데, Q 의 domain/range 상 그것은
    ``y Q x`` 여야 할 값이다.

    실측 (2026-08-16, 이 파이프라인):
      ``hasContinuousCastingProduct`` (Process→Product) 가
      ``subPropertyOf isProductOfContinuousCasting`` (Product→Process) 였다.
      tacit 4,320 트리플(정방향)에서 **제품이 공정을 산출** 하는 4,320 트리플이
      파생됐고, 그 부모가 ``iof-core:hasOutput`` 계열이라 IOF 표준 위반까지 갔다.
      T-Box+tacit 만으로 owlrl 재현: 거짓 hasOutput 4,320건.

    발생 경로: ``step_15c`` 가 미선언 OP 를 선언할 때 같은 ``(domain, range)`` 서명의
    OP 를 부모로 붙였는데, 그 시점의 부모는 **뒤집힌 상태** 였다 (0f 가 나중에
    교정). 즉 0f 가 부모를 바로잡으면 자식의 subPropertyOf 가 모순으로 남는다 —
    이 sub-step 은 그 잔여를 청소한다. **0f 뒤에 실행해야 한다.**

    ``inverseOf`` 로 대체하지 않는다: 그 관계가 진짜 inverse 인지 이 단계에서
    확정할 수 없고, 자식/부모 모두 이미 각자의 inverse 를 가질 수 있다. 틀린 공리를
    다른 틀린 공리로 바꾸는 것보다 **제거** 가 안전하다 (없는 공리 > 틀린 공리).
    """
    removed = 0
    samples: list[str] = []

    def _ln(u) -> str:
        return str(u).rsplit("/", 1)[-1].rsplit("#", 1)[-1]

    def _dr(p) -> tuple:
        d = [x for x in g.objects(p, RDFS.domain) if isinstance(x, URIRef)]
        r = [x for x in g.objects(p, RDFS.range) if isinstance(x, URIRef)]
        return (d[0] if len(d) == 1 else None, r[0] if len(r) == 1 else None)

    for child, parent in list(g.subject_objects(RDFS.subPropertyOf)):
        if not (isinstance(child, URIRef) and isinstance(parent, URIRef)):
            continue
        if not (str(child).startswith(steel_str) and str(parent).startswith(steel_str)):
            continue
        if (child, RDF.type, OWL.ObjectProperty) not in g:
            continue
        cd, cr = _dr(child)
        pd, pr = _dr(parent)
        if not (cd and cr and pd and pr):
            continue                       # 판정 불가 — 건드리지 않는다
        if cd == cr:
            continue                       # self-referential 은 반대 판정 불가
        if (cd, cr) != (pr, pd):
            continue                       # 방향이 반대가 아니면 정상
        g.remove((child, RDFS.subPropertyOf, parent))
        removed += 1
        samples.append(
            f"{_ln(child)}({_ln(cd)}→{_ln(cr)}) ⊑ "
            f"{_ln(parent)}({_ln(pd)}→{_ln(pr)})"
        )
        logger.warning(
            "안티패턴 0g: %s (%s→%s) 가 방향이 반대인 %s (%s→%s) 의 "
            "subPropertyOf 였다 — 제거했다. 남겨두면 정방향 사실에서 역방향 "
            "트리플이 파생된다 (실측: tacit 4,320 트리플 → 거짓 hasOutput 4,320건)",
            _ln(child), _ln(cd), _ln(cr), _ln(parent), _ln(pd), _ln(pr),
        )
    return removed, samples


def _fix_asymmetric_inverse(g: Graph, steel_str: str) -> tuple[int, int]:
    """0b — graph 에 있는 inverseOf 를 양방향 대칭화.

    **참조하는 프로퍼티는 선언한다.** 대칭 leg 만 넣고 ``owl:ObjectProperty``
    선언을 빼면, 이 스텝 자신이 T-Box 안에 미선언 술어를 만든다 —
    ``validate_kg`` 의 undeclared_op check 가 정확히 그것을 잡는다. step_02 가
    같은 이유로 :83 에서 양쪽을 선언하는데, 0b 는 그 방어를 갖지 않았다.

    실측 (2026-08-22 파이프라인 실행): S2 초안이 ``steamEnergyHasEquipment
    owl:inverseOf equipmentHasSteamEnergy`` 만 (한 방향) 내보냈고, 0b 가 역방향
    leg 을 채우면서 ``equipmentHasSteamEnergy`` 를 선언하지 않았다.
    ``antipattern_asymmetric_fixed: 2`` 가 S9 ``undeclared_op`` 위반 2건과 정확히
    같은 수인 것은 우연이 아니다 — equipmentHasSteamEnergy(usage 720) /
    equipmentHasTag(usage 50). 추론이 ``prp-inv`` 로 그 이름에 770 트리플을
    파생시키므로 domain/range 제약 없는 술어가 실데이터를 나른다. 같은 2쌍이 S9
    bidirectional 의 ``incompatible_pairs`` 로도 잡혔다 — 뿌리가 하나다
    (``_inverseof_compatibility`` 가 "한 쪽만 비어 있으면 선언 불완전" 으로 보수
    판정하므로, domain/range 가 0건인 미선언 leg 은 언제나 incompatible 이 된다).

    ``domain``/``range`` 는 **넣지 않는다** — 짝의 것을 뒤집어 채우면 방향 오류를
    복제한다 (step_02 :79 와 동일 정책, step_09b 가 그 방식으로 오류를 전파한
    전례). 근거를 갖고 채우는 것은 step_09/09a 의 책임이고, 그 스텝들은
    ``owl:ObjectProperty`` 로 **선언된** 것만 순회한다 — 즉 선언이 없으면 그들이
    이 OP 를 보지 못해 domain/range 가 영구히 빈다. 선언만 주입하면 나머지는
    기존 스텝이 근거를 갖고 채운다 (실측 2026-08-22 S3 재실행: domain=
    EquipmentMaster / range=SteamEnergy 가 채워져 incompatible 도 함께 해소,
    undeclared 2건 → 0건).

    Returns:
        ``(대칭화한 leg 수, 새로 선언한 OP 수)``. 선언 수를 별도로 돌려주는 이유:
        ``antipattern_asymmetric_fixed`` 하나만 보면 "대칭화했다" 는 사실이
        "미선언을 남겼다" 는 사실을 가린다. 이번 사고에서 그 숫자(2)는 정상처럼
        보였고 결함은 S9 까지 가서야 드러났다.
    """
    fixed = 0
    declared = 0
    # **순회 축은 ``inverseOf`` 트리플이다 — "선언된 OP 주어" 가 아니다.**
    # 후자로 돌면 짝이 미선언인 leg 을 놓친다: dangling leg 의 이름은 실행마다
    # 달라서(실측 2026-08-22: 아카이브 3개 스냅샷이 각각 hasStackEquipment /
    # tagHasEquipment / equipmentHasSteamEnergy) 어느 쪽이 선언돼 있는지 미리 알 수
    # 없다. 트리플 축으로 돌면 방향과 무관하게 양쪽을 다 본다.
    for prop, inv in list(g.subject_objects(OWL.inverseOf)):
        if not (isinstance(prop, URIRef) and isinstance(inv, URIRef)):
            continue
        if not (str(prop).startswith(steel_str) or str(inv).startswith(steel_str)):
            continue                       # 외래 ↔ 외래 쌍은 우리 소관이 아니다
        if (inv, OWL.inverseOf, prop) not in g:
            g.add((inv, OWL.inverseOf, prop))
            fixed += 1
        for side in (prop, inv):
            # 도메인 소속 이름만 선언한다 — 외래 온톨로지(iof-core 등) 프로퍼티를
            # 우리 T-Box 가 선언하면 그 온톨로지의 공리를 사칭한다.
            #
            # **이미 다른 property 타입으로 선언된 이름은 건드리지 않는다.** OP+DP
            # 동시 선언은 OWL 2 DL 위반이라 HermiT 이 온톨로지를 열지 못하고, 그러면
            # S4/S9.1 게이트가 통째로 사라진다 (검증기 실패 = 검증 없음). step_15c
            # 가 ``declared_dps`` 로 같은 충돌을 피하는 것과 동일한 계약.
            if not str(side).startswith(steel_str):
                continue
            existing_types = set(g.objects(side, RDF.type))
            if existing_types & {OWL.DatatypeProperty, OWL.AnnotationProperty}:
                continue
            if OWL.ObjectProperty not in existing_types:
                g.add((side, RDF.type, OWL.ObjectProperty))
                declared += 1
    if declared:
        logger.info(
            "안티패턴 0b: inverseOf 로 참조되지만 미선언이던 OP %d개를 선언했다 — "
            "선언을 빼면 domain/range 제약 없는 술어가 되어 어떤 데이터든 통과한다",
            declared,
        )
    return fixed, declared


def _strip_transitive_with_inverse(g: Graph, steel_str: str) -> int:
    """0d — TransitiveProperty + inverseOf 동시 선언 시 TransitiveProperty 제거.

    OWL RL prp-trp + prp-inv 결합으로 추론 폭발이 발생하므로 정책상
    TransitiveProperty 를 제거 (inverseOf 유지). 전이 관계는 sub-property
    체인 등 다른 경로로 대체 가능.
    """
    removed = 0
    for prop in list(g.subjects(RDF.type, OWL.TransitiveProperty)):
        if not (isinstance(prop, URIRef) and str(prop).startswith(steel_str)):
            continue
        has_inverse = any(True for _ in g.objects(prop, OWL.inverseOf))
        if not has_inverse:
            has_inverse = any(True for _ in g.subjects(OWL.inverseOf, prop))
        if has_inverse:
            g.remove((prop, RDF.type, OWL.TransitiveProperty))
            removed += 1
            logger.info(
                "안티패턴: TransitiveProperty+inverseOf 충돌 해소 — %s 에서 "
                "TransitiveProperty 제거",
                str(prop),
            )
    return removed


def _fix_double_prefix_uri(g: Graph, steel_str: str) -> int:  # noqa: ARG001
    """0e — ``<...#pfx:foo>`` 유령 URI 를 **그 prefix 가 가리키는** IRI 로 교정.

    ``steel_str`` 은 형제 sub-step 과의 시그니처 일관성 때문에 유지한다 —
    도메인 NS 판정은 이제 공용 헬퍼가 설정에서 직접 읽는다.

    이전 구현은 콜론 앞을 판별 없이 버려 (``local.split(":", 1)[1]``) 외래
    온톨로지 참조를 도메인 IRI 로 뭉갰다. 실측 피해 (2026-08-09 S3 실행):
    ``<...#iof-core:MaterialArtifact>`` 13건이 ``steel:MaterialArtifact`` 가 됐고
    그 클래스는 T-Box 어디에도 선언돼 있지 않다 — IOF 매핑이 사라진 자리에
    미선언 참조만 남았다. 총 42 트리플이 같은 방식으로 손실됐다.

    이제 :func:`domain.graph_utils.split_embedded_prefix` 가 판정한다 — 콜론 앞이
    **알려진 prefix 일 때만** 유령으로 보고, 그 prefix 의 진짜 네임스페이스로
    복구한다. 미등록 prefix 와 정당한 콜론(인스턴스 IRI 의 timestamp 등)은
    건드리지 않는다.
    """
    from domain.graph_utils import split_embedded_prefix

    def _repaired(node):
        """복구 대상이면 정상 IRI, 아니면 ``None`` (변경 없음)."""
        if not isinstance(node, URIRef):
            return None
        split = split_embedded_prefix(str(node), g)
        if split is None:
            return None
        namespace, local = split
        return URIRef(namespace + local)

    fixed = 0
    for s, p, o in list(g):
        repaired = [_repaired(s), _repaired(p), _repaired(o)]
        if not any(r is not None for r in repaired):
            continue
        new_s, new_p, new_o = (
            r if r is not None else orig
            for r, orig in zip(repaired, (s, p, o), strict=True)
        )
        g.remove((s, p, o))
        g.add((new_s, new_p, new_o))
        fixed += 1
    return fixed


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import _remove_redundant_subclass

    before = len(g)
    steel_str = ctx.domain_ns

    ap_lonely_removed = _count_lonely_disjoint(g)
    ap_asymmetric_fixed, ap_inverse_declared = _fix_asymmetric_inverse(g, steel_str)
    ap_redundant_removed = _remove_redundant_subclass(g, steel_str)
    ap_transitive_with_inverse_removed = _strip_transitive_with_inverse(
        g, steel_str
    )
    ap_double_prefix_fixed = _fix_double_prefix_uri(g, steel_str)
    # 0f 는 **step_09b 보다 먼저** 돌아야 한다 — 09b 가 뒤집힌 inverse.domain 을
    # 짝 OP 로 복제하기 전에 원본을 고쳐야 오류가 전파되지 않는다.
    ap_name_direction_fixed, ap_name_direction_samples = (
        _fix_name_direction_contradiction(g, steel_str)
    )
    # 0g 는 **0f 뒤에** 온다 — 0f 가 부모 방향을 바로잡으면 자식의 subPropertyOf 가
    # 모순으로 남는다. 그 잔여를 여기서 청소한다.
    ap_reversed_subprop_removed, ap_reversed_subprop_samples = (
        _drop_reversed_subproperty(g, steel_str)
    )

    return StepResult(
        name="step_00_antipatterns",
        stats={
            "antipattern_lonely_removed": ap_lonely_removed,
            "antipattern_asymmetric_fixed": ap_asymmetric_fixed,
            # 0b 가 대칭화하면서 **선언까지** 해준 OP 수. 0 이 정상 (S2 초안이
            # 양쪽을 선언했다는 뜻). >0 이면 S2 프롬프트가 한 방향만 내보내고
            # 있다는 신호다 — 이 수치가 없으면 미선언 결함이 S9 까지 안 보인다.
            "antipattern_inverse_declared": ap_inverse_declared,
            "antipattern_redundant_removed": ap_redundant_removed,
            "antipattern_transitive_with_inverse_removed":
                ap_transitive_with_inverse_removed,
            "antipattern_double_prefix_fixed": ap_double_prefix_fixed,
            "antipattern_name_direction_fixed": ap_name_direction_fixed,
            "antipattern_name_direction_samples": ap_name_direction_samples[:10],
            "antipattern_reversed_subproperty_removed": ap_reversed_subprop_removed,
            "antipattern_reversed_subproperty_samples":
                ap_reversed_subprop_samples[:10],
        },
        triples_delta=len(g) - before,
        step_number=0,
        step_label="antipattern_auto_fix",
    )
