"""Regression: 직교 축이 섞인 disjoint 그룹만 분리하고, 정당한 disjoint 는 보존한다.

2026-08-19 실측: S2 가 서로 직교하는 두 분류 축을 한 ``owl:AllDisjointClasses`` 에
섞어 넣어 HermiT unsat 6건이 났다 (``EquipmentMaster`` / ``TagMaster`` /
``EquipmentStatus`` / ``RealTimeData`` / ``FailureCause`` / ``ItemSupplierMap``).

    ['MasterData', 'TransactionRecord', 'EnvironmentalMonitoring',
     'EnergyManagement', 'QualityManagement', 'EquipmentManagement']

축 A(데이터 성격) = MasterData / TransactionRecord
축 B(업무 도메인) = EquipmentManagement / QualityManagement / ...

A-Box 는 unsat 클래스의 인스턴스를 만들 수 없으므로 방치하면 6개 CSV 테이블이 KG
에서 통째로 사라진다.

**이 파일이 주장하는 것**: 카운터가 1 이상인가가 아니라 —
 (1) 정당한 disjoint (config 근거 있음) 를 **보존** 하는가,
 (2) 잘못 섞인 그룹만 골라 축을 **분리** 하는가,
 (3) OWL 2 DL 제약 (DisjointClasses 는 멤버 ≥ 2) 을 지키는가.

파괴적 스텝이므로 "무엇을 안 건드리는가" 가 더 중요하다.
"""

import contextlib
from unittest.mock import patch

from rdflib import OWL, RDF, RDFS, BNode, Graph, Namespace

from tools.quality_steps import step_14b_disjoint_axis_split as step
from tools.quality_steps._base import StepContext

NS = Namespace("http://example.com/steel-ontology#")


def _mk_graph(classes, subclass_of=(), disjoint_groups=()):
    """테스트용 최소 T-Box 구성."""
    g = Graph()
    for c in classes:
        g.add((NS[c], RDF.type, OWL.Class))
    for child, parent in subclass_of:
        g.add((NS[child], RDFS.subClassOf, NS[parent]))
    for members in disjoint_groups:
        node = BNode()
        g.add((node, RDF.type, OWL.AllDisjointClasses))
        import rdflib.collection
        lst = BNode()
        coll = rdflib.collection.Collection(g, lst)
        for m in members:
            coll.append(NS[m])
        g.add((node, OWL.members, lst))
    return g


def _groups(g: Graph) -> list[set[str]]:
    """현재 그래프의 AllDisjointClasses 그룹 목록 (local name 집합)."""
    import rdflib.collection
    out = []
    for node in g.subjects(RDF.type, OWL.AllDisjointClasses):
        lists = list(g.objects(node, OWL.members))
        if not lists:
            continue
        with contextlib.suppress(Exception):
            out.append({
                str(m).split("#")[-1]
                for m in rdflib.collection.Collection(g, lists[0])
            })
    return out


def _run(g: Graph, config_groups):
    """config DISJOINT_GROUPS 를 주입해 스텝 실행."""
    with patch("tools.ontology_quality.DISJOINT_GROUPS", config_groups):
        return step.apply(g, StepContext(domain_ns=str(NS)))


# ── 보존: 정당한 구조를 건드리지 않는가 (NEGATIVE 방향) ──────────────────

def test_preserves_disjoint_when_no_class_violates_it():
    """unsat 을 유발하지 않는 disjoint 는 그대로 둔다."""
    g = _mk_graph(
        classes=["A", "B", "X"],
        subclass_of=[("X", "A")],            # X 는 A 만 상속 — 충돌 없음
        disjoint_groups=[["A", "B"]],
    )
    res = _run(g, [["A", "B"]])
    assert _groups(g) == [{"A", "B"}], "충돌 없는 그룹이 변경됐다"
    assert res.stats["disjoint_axis_members_removed"] == 0
    assert res.triples_delta == 0


def test_preserves_config_backed_disjoint_even_when_it_causes_unsat():
    """config 근거가 있는 멤버만으로 된 충돌은 **자동 수정하지 않는다**.

    SME 가 승인한 disjoint 를 "unsat 이 사라진다" 는 이유로 지우면 진짜 모델링
    오류가 조용히 묻힌다. 경고만 내고 사람 판단에 맡기는 것이 정책이다.
    """
    g = _mk_graph(
        classes=["A", "B", "X"],
        subclass_of=[("X", "A"), ("X", "B")],   # X 가 두 축 동시 상속 → unsat
        disjoint_groups=[["A", "B"]],
    )
    res = _run(g, [["A", "B"]])                 # 둘 다 config 근거 있음
    assert _groups(g) == [{"A", "B"}], "config 근거 있는 disjoint 가 삭제됐다"
    assert res.stats["disjoint_axis_members_removed"] == 0
    assert res.stats["disjoint_axis_groups_split"] == 0


def test_preserves_hierarchy_never_removes_subclassof():
    """다중상속은 정당하다 — 계층은 절대 끊지 않는다.

    고치는 대상은 잘못 섞인 disjoint 선언이고, 계층은 RR/DIT 개선의 근거다.
    """
    g = _mk_graph(
        classes=["MasterData", "TransactionRecord", "DomainX", "Cls"],
        subclass_of=[("Cls", "MasterData"), ("Cls", "DomainX")],
        disjoint_groups=[["MasterData", "TransactionRecord", "DomainX"]],
    )
    before = set(g.triples((None, RDFS.subClassOf, None)))
    _run(g, [["MasterData", "TransactionRecord"]])
    after = set(g.triples((None, RDFS.subClassOf, None)))
    assert before == after, "subClassOf 가 변경됐다 — 계층을 건드리면 안 된다"


def test_untouched_when_no_disjoint_groups_exist():
    """disjoint 가 없으면 no-op (도메인-중립 — 다른 도메인에서 오작동 금지)."""
    g = _mk_graph(classes=["A", "B"], subclass_of=[("A", "B")])
    res = _run(g, [])
    assert res.triples_delta == 0
    assert res.stats["disjoint_axis_members_removed"] == 0


# ── 수정: 잘못 섞인 축만 분리하는가 ──────────────────────────────────────

def test_splits_mixed_axis_group_by_removing_unbacked_member():
    """config 근거 없는 멤버를 덜어내 축을 분리한다 (실측 형태 재현)."""
    g = _mk_graph(
        classes=["MasterData", "TransactionRecord", "EquipmentManagement",
                 "EquipmentAsset", "EquipmentMaster"],
        subclass_of=[("EquipmentAsset", "EquipmentManagement"),
                     ("EquipmentMaster", "EquipmentAsset"),
                     ("EquipmentMaster", "MasterData")],
        # 두 축이 섞인 그룹
        disjoint_groups=[["MasterData", "TransactionRecord",
                          "EquipmentManagement"]],
    )
    # config 에는 MasterData/TransactionRecord 만 근거가 있다
    res = _run(g, [["MasterData", "TransactionRecord"]])

    groups = _groups(g)
    assert len(groups) == 1
    assert "EquipmentManagement" not in groups[0], (
        "근거 없는 멤버가 제거되지 않았다 — unsat 이 남는다"
    )
    assert {"MasterData", "TransactionRecord"} <= groups[0], (
        "config 근거 있는 멤버가 함께 사라졌다"
    )
    assert "EquipmentMaster" in res.stats["disjoint_axis_unsat_freed"]


def test_drops_node_when_fewer_than_two_members_remain():
    """멤버가 2개 미달로 줄면 노드째 제거한다 (OWL 2 DL 제약).

    HermiT 는 '멤버 0~1개 DisjointClasses' 를 만나면 **온톨로지 로드를 거부** 한다
    — 검증기가 아예 열지 못하면 그 침묵이 다른 결함을 전부 덮는다.
    """
    g = _mk_graph(
        classes=["AxisA", "AxisB", "Cls"],
        subclass_of=[("Cls", "AxisA"), ("Cls", "AxisB")],
        disjoint_groups=[["AxisA", "AxisB"]],
    )
    res = _run(g, [])                 # 둘 다 config 근거 없음
    assert _groups(g) == [], "멤버 2개 미달 그룹이 남았다 (HermiT 로드 거부 위험)"
    assert res.stats["disjoint_axis_groups_dropped"] == 1
    # 노드 자체가 사라졌는지 (rest-only 잔여물 없이)
    assert not list(g.subjects(RDF.type, OWL.AllDisjointClasses))


def test_no_group_left_with_single_member():
    """수정 후 어떤 그룹도 멤버 1개로 남지 않는다 (전역 불변식)."""
    g = _mk_graph(
        classes=["P", "Q", "R", "Cls1", "Cls2"],
        subclass_of=[("Cls1", "P"), ("Cls1", "Q"),
                     ("Cls2", "Q"), ("Cls2", "R")],
        disjoint_groups=[["P", "Q"], ["Q", "R"]],
    )
    _run(g, [])
    for grp in _groups(g):
        assert len(grp) >= 2, f"멤버 {len(grp)}개 그룹이 남았다: {grp}"


# ── 배선: 파이프라인에 등록됐고 순서가 맞는가 ────────────────────────────

def test_step_registered_in_pipeline_after_step_14():
    """스텝이 파이프라인에 있고, 계층을 세우는 12/14 **뒤** 에 온다.

    앞에 두면 확정되지 않은 계층을 보고 판정한다 — 함수만 만들고 배선을 빠뜨리면
    단위 테스트는 초록인데 산출물은 안 바뀐다 (이 리포에서 반복된 실패 유형).
    """
    import tools.quality_steps as qs

    names = [getattr(f, "__module__", "") for f in qs._MAIN_POST_STEP9]
    joined = [n.split(".")[-1] for n in names]
    assert "step_14b_disjoint_axis_split" in joined, (
        "step_14b 가 파이프라인에 등록되지 않았다"
    )
    idx14b = joined.index("step_14b_disjoint_axis_split")
    for earlier in ("step_12_intermediate_abstract", "step_14_subgroup_creation"):
        assert earlier in joined, f"{earlier} 가 목록에 없다 (테스트 전제 붕괴)"
        assert joined.index(earlier) < idx14b, (
            f"{earlier} 가 step_14b 뒤에 있다 — 계층 확정 전에 판정하게 된다"
        )


def test_step_result_contract():
    """StepResult 계약 (코디네이터가 stats 를 병합하고 change_log 를 쓴다)."""
    g = _mk_graph(classes=["A"], disjoint_groups=[])
    res = _run(g, [])
    assert res.name == "step_14b_disjoint_axis_split"
    assert res.step_number == "14b"
    assert res.step_label == "disjoint_axis_split"
    for key in ("disjoint_axis_groups_split", "disjoint_axis_groups_dropped",
                "disjoint_axis_members_removed", "disjoint_axis_unsat_freed",
                "disjoint_axis_samples"):
        assert key in res.stats, f"stats 에 {key} 가 없다"


# ── 실측 시나리오: 배포 T-Box 의 6건이 해소되는가 ────────────────────────

def test_real_world_six_unsat_scenario():
    """2026-08-19 실측 구조를 재현해 6개 클래스가 전부 해소되는지 확인."""
    g = _mk_graph(
        classes=["MasterData", "TransactionRecord", "EquipmentManagement",
                 "QualityManagement", "EnergyManagement",
                 "EquipmentAsset", "EquipmentEvent", "MaintenanceManagement",
                 "EquipmentMaster", "TagMaster", "EquipmentStatus",
                 "RealTimeData", "FailureCause", "ItemSupplierMap"],
        subclass_of=[
            ("EquipmentAsset", "EquipmentManagement"),
            ("EquipmentEvent", "EquipmentManagement"),
            ("MaintenanceManagement", "EquipmentManagement"),
            ("EquipmentMaster", "EquipmentAsset"), ("EquipmentMaster", "MasterData"),
            ("TagMaster", "EquipmentAsset"), ("TagMaster", "MasterData"),
            ("EquipmentStatus", "EquipmentEvent"),
            ("EquipmentStatus", "TransactionRecord"),
            ("RealTimeData", "EquipmentEvent"),
            ("RealTimeData", "TransactionRecord"),
            ("FailureCause", "MaintenanceManagement"),
            ("FailureCause", "TransactionRecord"),
            ("ItemSupplierMap", "MasterData"),
            ("ItemSupplierMap", "TransactionRecord"),
        ],
        disjoint_groups=[
            ["MasterData", "TransactionRecord"],
            ["MasterData", "TransactionRecord", "EquipmentManagement",
             "QualityManagement", "EnergyManagement"],
        ],
    )
    res = _run(g, [])   # config 근거 없음 — S2 자유 생성분

    freed = set(res.stats["disjoint_axis_unsat_freed"])
    expected = {"EquipmentMaster", "TagMaster", "EquipmentStatus",
                "RealTimeData", "FailureCause", "ItemSupplierMap"}
    assert expected <= freed, f"해소되지 않은 클래스: {expected - freed}"

    # 수정 후 어떤 클래스도 한 그룹의 멤버 2개를 조상으로 갖지 않아야 한다.
    groups = [set(x) for x in _groups(g)]

    def ancestors(name, seen=frozenset()):
        out = set()
        for p in g.objects(NS[name], RDFS.subClassOf):
            pn = str(p).split("#")[-1]
            if pn in seen:
                continue
            out.add(pn)
            out |= ancestors(pn, seen | {pn})
        return out

    for cls in expected:
        anc = ancestors(cls) | {cls}
        for grp in groups:
            hit = anc & grp
            assert len(hit) < 2, f"{cls} 가 여전히 unsat: {sorted(hit)}"
