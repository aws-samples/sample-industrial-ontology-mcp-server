"""FK 컬럼 이름의 **방향** 이 OP 선택에 반영되는지 고정.

## 배경 (2026-08-17 실측)

``_build_fk_skeleton`` 의 후보 순위는 ``(domain, range)`` 특이도로만 정해졌다 —
**FK 컬럼 이름을 보지 않았다**. 그래서 같은 ``(domain, range)`` 를 갖는 OP 가 여럿이면
**모든 FK 컬럼이 같은 승자를 고른다**.

배포 T-Box 는 ``Transportation → WarehouseMaster`` OP 를 3개 선언한다:

    hasDestinationWarehouse
    transportationHasDestinationWarehouse
    transportationHasOriginWarehouse

``Transportation.csv`` 의 FK 컬럼은 ``Origin_Warehouse`` / ``Destination_Warehouse``
둘인데, 동점 2차 키(이름 오름차순) 때문에 **둘 다** ``hasDestinationWarehouse`` 를
골랐다.

## 왜 이것이 "차선 선택" 이 아니라 데이터 소실인가

두 컬럼이 같은 술어를 쓰면 출발 창고 정보가 목적지와 뒤섞여 **사라진다**. CSV 300행의
출발 창고가 KG 에 존재하지 않고, ``transportationHasOriginWarehouse`` 는 인스턴스 0건
유령 OP 로 남는다. 이는 S12 CQ 실패를 만드는 전형적인 phantom OP 문제다.

게이트가 못 잡은 이유: 두 트리플 다 domain/range 를 만족하므로 conformance 통과,
개수도 정상이다. "틀린 창고를 가리킨다" 는 **의미** 결함이라
구조 게이트만으로 잡을 수 없다.
"""
from __future__ import annotations

import random

import pytest

from tests.helpers_tbox_names import op_for_column as _op_for_column
from tools.abox_generation import (
    _build_fk_skeleton,
    _column_direction_affinity,
    _direction_tokens,
)

# 배포 T-Box 의 실제 구성을 재현한 후보 3개 — 셋 다 같은 (domain, range).
WAREHOUSE_OPS = [
    {"name": "hasDestinationWarehouse",
     "domain": "Transportation", "range": "WarehouseMaster"},
    {"name": "transportationHasDestinationWarehouse",
     "domain": "Transportation", "range": "WarehouseMaster",
     "inverse": "isDestinationWarehouseOf"},
    {"name": "transportationHasOriginWarehouse",
     "domain": "Transportation", "range": "WarehouseMaster",
     "inverse": "isOriginWarehouseOf"},
]


@pytest.fixture
def _tbox_stub(monkeypatch):
    """T-Box 조회를 스텁 — 이 테스트의 대상은 순위 로직뿐이다."""
    import tools.abox_generation as ab

    monkeypatch.setattr(ab, "_class_exists_in_tbox", lambda c: True)
    monkeypatch.setattr(ab, "_get_superclasses", lambda c: {c})
    monkeypatch.setattr(ab, "_get_source_class_restriction_props", lambda c: set())
    monkeypatch.setattr(ab, "_fk_column_to_class", lambda c: "WarehouseMaster")
    ab._reset_fk_skeleton_cache()
    yield
    ab._reset_fk_skeleton_cache()


def _chosen(fk_column: str) -> str:
    """해당 컬럼에 대해 선택된 forward OP 이름."""
    result = _build_fk_skeleton(
        "Transportation", fk_column.lower().replace("_", ""),
        WAREHOUSE_OPS, None, fk_column,
    )
    assert result is not None
    _target, skeleton, _unverified = result
    fwd = [i[1] for i in skeleton if i[0] == "fwd"]
    assert len(fwd) == 1, f"forward 후보가 {len(fwd)}개: {skeleton}"
    return fwd[0]


def _chosen_via_public_api(fk_column: str) -> str:
    """``_detect_fk_property`` **공개 진입점** 을 통해 선택된 forward OP.

    ``_build_fk_skeleton`` 을 직접 부르는 위 헬퍼와 달리, 프로덕션 호출부가 쓰는
    경로를 그대로 탄다. 이 구분이 중요한 이유는 아래
    ``TestProductionCallSitePassesRawColumn`` 참조.
    """
    from tools.abox_generation import _detect_fk_property, _reset_fk_skeleton_cache

    _reset_fk_skeleton_cache()
    results = _detect_fk_property(
        "Transportation", fk_column, "WH001", WAREHOUSE_OPS,
    )
    _reset_fk_skeleton_cache()
    fwd = [r[0] for r in results if len(r) == 2]
    assert len(fwd) == 1, f"forward 후보가 {len(fwd)}개: {results}"
    return fwd[0]


class TestDirectionTokenExtraction:
    def test_word_boundary_not_substring(self):
        """``to`` 가 ``transportation`` 안에 있다고 방향성으로 읽으면 안 된다.

        부분 문자열 매칭이면 운송 테이블의 **모든** 컬럼이 방향성으로 오판되고,
        엉뚱한 컬럼에 -20 이 붙어 정상 선택이 깨진다.
        """
        assert _direction_tokens("Transportation_ID") == frozenset()
        assert _direction_tokens("Transport_Mode") == frozenset()

    def test_snake_and_camel_both_split(self):
        assert "origin" in _direction_tokens("Origin_Warehouse")
        assert "origin" in _direction_tokens("transportationHasOriginWarehouse")
        assert "destination" in _direction_tokens("hasDestinationWarehouse")

    def test_korean_substring_match_retained(self):
        """한글은 조사·복합어로 붙으므로 부분 문자열 유지 (출발 → 출발지)."""
        assert "출발" in _direction_tokens("출발지창고")


class TestAffinityScoring:
    def test_opposing_direction_is_penalized(self):
        for op in ("hasDestinationWarehouse",
                   "transportationHasDestinationWarehouse"):
            assert _column_direction_affinity("Origin_Warehouse", op) == -20

    def test_same_direction_is_rewarded(self):
        assert _column_direction_affinity(
            "Origin_Warehouse", "transportationHasOriginWarehouse") == 5
        assert _column_direction_affinity(
            "Destination_Warehouse", "hasDestinationWarehouse") == 5

    def test_no_tokens_means_no_opinion(self):
        """**정당한 입력 보존**: 방향 근거가 없으면 개입하지 않는다.

        판정 근거 없이 점수를 흔들면 무관한 컬럼의 선택이 바뀌어
        ``_candidate_sort_key`` 가 막으려던 이름 드리프트를 되살린다.
        """
        assert _column_direction_affinity(
            "Transportation_ID", "hasDestinationWarehouse") == 0
        assert _column_direction_affinity(
            "Origin_Warehouse", "hasWarehouse") == 0, (
            "OP 에 방향 토큰이 없는데 점수를 줬다"
        )

    def test_same_axis_side_synonyms_are_not_opposing(self):
        """같은 축 **같은 쪽** 의 동의어는 대립이 아니다 (origin vs 출발)."""
        assert _column_direction_affinity("출발_창고", "hasOriginWarehouse") == 5


class TestSkeletonPicksMatchingDirection:
    def test_origin_and_destination_pick_different_ops(self, _tbox_stub):
        """THE REGRESSION: 두 컬럼이 서로 다른 술어를 골라야 한다."""
        origin = _chosen("Origin_Warehouse")
        dest = _chosen("Destination_Warehouse")
        assert origin != dest, (
            f"두 FK 컬럼이 같은 OP({origin})를 골랐다 — 출발 창고 300건이 "
            "목적지와 뒤섞여 소실된다"
        )
        assert "Origin" in origin, f"출발 컬럼이 {origin} 을 골랐다"
        assert "Destination" in dest, f"목적지 컬럼이 {dest} 을 골랐다"

    def test_choice_invariant_to_candidate_order(self, _tbox_stub):
        """입력 순서가 흔들려도 선택이 같아야 한다 (기존 결정성 정책 유지)."""
        import tools.abox_generation as ab

        rng = random.Random(20260817)
        expected = None
        for _ in range(20):
            shuffled = WAREHOUSE_OPS[:]
            rng.shuffle(shuffled)
            ab._reset_fk_skeleton_cache()
            _t, skel, _u = _build_fk_skeleton(
                "Transportation", "originwarehouse", shuffled, None,
                "Origin_Warehouse",
            )
            got = [i[1] for i in skel if i[0] == "fwd"][0]
            expected = expected or got
            assert got == expected, "후보 순서에 따라 선택이 바뀐다"

    def test_restriction_bonus_cannot_override_wrong_direction(self, _tbox_stub):
        """Restriction 가산점(+10)이 반대 방향(-20)을 이기지 못해야 한다.

        Restriction 을 만족시키려고 **틀린 창고를 가리키는** 트리플을 쓰는 것은
        카디널리티 통과를 위해 사실을 왜곡하는 것이다.
        """
        import tools.abox_generation as ab

        monkey_props = {"hasDestinationWarehouse"}
        ab._reset_fk_skeleton_cache()
        original = ab._get_source_class_restriction_props
        ab._get_source_class_restriction_props = (
            lambda c: monkey_props if c == "Transportation" else set()
        )
        try:
            got = _chosen("Origin_Warehouse")
        finally:
            ab._get_source_class_restriction_props = original
            ab._reset_fk_skeleton_cache()
        assert got == "transportationHasOriginWarehouse", (
            f"Restriction 가산점이 방향 오류를 이겼다 — {got} 선택됨"
        )

    def test_column_without_direction_keeps_legacy_choice(self, _tbox_stub):
        """방향 토큰 없는 컬럼은 기존(이름 오름차순) 선택을 유지한다.

        **정당한 입력 보존** — 이 변경의 범위는 방향 대립뿐이다.
        """
        import tools.abox_generation as ab

        ab._reset_fk_skeleton_cache()
        _t, skel, _u = _build_fk_skeleton(
            "Transportation", "warehouseid", WAREHOUSE_OPS, None, "Warehouse_ID",
        )
        got = [i[1] for i in skel if i[0] == "fwd"][0]
        assert got == "hasDestinationWarehouse", (
            f"방향 근거 없는 컬럼의 선택이 바뀌었다: {got}"
        )

    def test_raw_column_omitted_is_backward_compatible(self, _tbox_stub):
        """``fk_column_raw`` 미전달 시 기존 동작 (호출자 호환)."""
        import tools.abox_generation as ab

        ab._reset_fk_skeleton_cache()
        _t, skel, _u = _build_fk_skeleton(
            "Transportation", "originwarehouse", WAREHOUSE_OPS, None,
        )
        got = [i[1] for i in skel if i[0] == "fwd"][0]
        assert got == "hasDestinationWarehouse"


class TestProductionCallSitePassesRawColumn:
    """호출부가 **원본** 컬럼명을 넘기는지 — 정규화된 이름은 방향 판정을 무력화한다.

    ## 왜 별도 테스트가 필요한가 (2026-08-17 실측)

    방향 점수를 넣고 단위 테스트 14건이 통과했는데도 **A-Box 산출물은 그대로였다**:
    출발 창고 여전히 0건, ``hasDestinationWarehouse`` 300건.

    원인은 배선이었다. 호출부(``_process_row``)가 ``col_normalized``
    (= ``originwarehouse``) 를 ``fk_column`` 으로 넘겼다. 방향 판정은 **단어 경계** 로
    토큰을 찾으므로 (``to`` 가 ``transportationid`` 에 걸리는 오탐을 막기 위해)
    밑줄이 사라진 문자열에서는 ``origin`` 을 찾지 못한다 →  점수 0 → 판정 무력화.

    내 단위 테스트는 ``_build_fk_skeleton`` 을 직접 호출하며 raw 이름을 줬기 때문에
    이 결함에 **도달하지 못했다** — 픽스처 미도달형 생존이다.

    이 클래스는 **공개 진입점** 으로만 검증해 그 갭을 막는다.
    """

    def test_public_entry_point_distinguishes_direction(self, _tbox_stub):
        """THE WIRING REGRESSION: ``_detect_fk_property`` 경유로도 갈라져야 한다."""
        origin = _chosen_via_public_api("Origin_Warehouse")
        dest = _chosen_via_public_api("Destination_Warehouse")
        assert origin != dest, (
            f"공개 진입점에서 두 컬럼이 같은 OP({origin})를 골랐다 — 단위 테스트는 "
            "통과하지만 프로덕션 경로는 무력화된 상태다"
        )
        assert "Origin" in origin and "Destination" in dest

    def test_normalized_column_name_loses_word_boundary(self):
        """정규화된 이름으로는 방향 토큰을 찾을 수 없다 — 이 사실 자체를 고정한다.

        누군가 호출부를 다시 ``col_normalized`` 로 되돌리면 위 테스트가 죽는데,
        **왜** 죽는지 이 테스트가 설명한다.
        """
        assert _direction_tokens("Origin_Warehouse") == frozenset({"origin"})
        assert _direction_tokens("originwarehouse") == frozenset(), (
            "정규화된 이름에서 토큰이 잡히면 부분 문자열 매칭으로 회귀한 것이다 "
            "(그러면 to/transportationid 오탐이 되살아난다)"
        )

    def test_call_site_source_passes_raw_column_name(self):
        """``_process_row_columns`` 가 ``col_name`` 을 넘기는지 **소스로** 확인.

        ## 왜 소스 검사인가

        이 배선은 함수 경계 아래에 있어 단위 테스트로 도달할 수 없다.
        ``_process_row_columns`` 는 인자 13개(writer / ctx / tbox_info / contract …)를
        요구해서 호출하려면 A-Box 생성 절반을 재구성해야 하고, 그렇게 만든 픽스처는
        실제 파이프라인과 어긋나기 쉽다.

        ``_detect_fk_property`` 를 직접 부르는 테스트로는 **부족하다** — 실제로 부족했다:
        내가 그 경로만 덮었을 때 호출부를 ``col_normalized`` 로 되돌리는 mutation 이
        17건 전부를 통과했다.

        그래서 두 겹으로 막는다: (1) 이 소스 검사 (2) 산출물 검사
        (``TestDeployedAboxHasBothDirections``). 소스 검사는 회귀를 **즉시** 잡고,
        산출물 검사는 그것이 실제로 트리플을 만들었는지 확인한다.
        """
        import inspect
        import re

        from tools import abox_generation as ab

        src = inspect.getsource(ab._process_row_columns)
        m = re.search(
            r"_detect_fk_property\(\s*\n\s*class_name,\s*(\w+),", src,
        )
        assert m, "호출 형태가 바뀌었다 — 이 테스트를 갱신하라"
        passed = m.group(1)
        assert passed == "col_name", (
            f"호출부가 {passed!r} 를 넘긴다. 방향 판정은 단어 경계를 보므로 정규화된 "
            "이름(originwarehouse)에서는 무력화된다 — 원본 col_name 을 넘겨야 한다"
        )

    def test_normalization_still_happens_internally(self, _tbox_stub):
        """raw 를 넘겨도 target class 해석은 정규화된 이름으로 동작한다.

        **정당한 입력 보존**: 호출부 인자를 바꿔 FK 해석 자체가 깨지면 안 된다.
        """
        from tools.abox_generation import (
            _detect_fk_property,
            _reset_fk_skeleton_cache,
        )
        _reset_fk_skeleton_cache()
        raw = _detect_fk_property(
            "Transportation", "Origin_Warehouse", "WH001", WAREHOUSE_OPS,
        )
        _reset_fk_skeleton_cache()
        norm = _detect_fk_property(
            "Transportation", "originwarehouse", "WH001", WAREHOUSE_OPS,
        )
        _reset_fk_skeleton_cache()
        # target URI (튜플 2번째 원소) 는 두 경로에서 동일해야 한다.
        assert raw and norm
        assert str(raw[0][1]) == str(norm[0][1]), (
            "정규화 여부가 target URI 를 바꿨다 — FK 해석이 깨진다"
        )


class TestNoCollateralDamageOnRealCsvColumns:
    """실제 CSV 컬럼 전수에서 방향 판정이 과잉 발동하지 않는지.

    범위는 **FK 컬럼** 으로 한정한다 — ``_build_fk_skeleton`` 은
    ``_fk_column_to_class`` 가 대상 클래스를 못 찾으면 순위 계산 전에 반환하므로,
    FK 가 아닌 컬럼은 방향 점수의 영향을 받을 수 없다.

    이 구분은 1차 작성에서 놓쳤다: ``Energy_Efficiency.csv`` 의
    ``Energy_Input``/``Energy_Output`` 은 input↔output 대립 짝을 이루지만 둘 다
    **수치 측정값** 이라 FK 가 아니다 (``_fk_column_to_class`` → None).
    "토큰이 있는 컬럼" 으로 세면 영향 범위를 과대평가한다.
    """

    def _fk_direction_tokens_by_table(self) -> dict[str, set[str]]:
        import csv
        import os

        import config
        from tools.abox_generation import _fk_column_to_class

        d = config.SOURCE_RAWDATA_DIR
        if not os.path.isdir(d):
            pytest.skip("CSV 디렉토리 없음")
        out: dict[str, set[str]] = {}
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".csv"):
                continue
            with open(os.path.join(d, fn), encoding="utf-8-sig") as fh:
                cols = next(csv.reader(fh))
            toks: set[str] = set()
            for c in cols:
                if not _fk_column_to_class(c.lower().replace("_", "")):
                    continue           # FK 아님 — 순위 계산에 도달하지 않는다
                toks |= set(_direction_tokens(c))
            if toks:
                out[fn] = toks
        return out

    def test_only_transportation_has_an_opposing_fk_pair(self):
        from tools.ontology_quality import _DIRECTIONAL_AXES

        per_table = self._fk_direction_tokens_by_table()
        opposing = {
            fn for fn, toks in per_table.items()
            if any(toks & a and toks & b for a, b in _DIRECTIONAL_AXES)
        }
        assert opposing == {"Transportation.csv"}, (
            f"대립 짝을 가진 FK 컬럼 테이블이 예상과 다르다: {opposing}. 새 테이블이 "
            "추가됐다면 방향 판정 영향 범위를 재확인하라"
        )

    def test_non_fk_numeric_columns_are_out_of_scope(self):
        """input/output 대립을 이루지만 FK 가 아닌 컬럼은 영향권 밖이다."""
        from tools.abox_generation import _fk_column_to_class

        for col in ("Energy_Input", "Energy_Output", "Iron_Output_ton",
                    "Target_Tensile_MPa"):
            assert _direction_tokens(col), f"{col} 에 방향 토큰이 없다 (전제 확인)"
            assert _fk_column_to_class(col.lower().replace("_", "")) is None, (
                f"{col} 이 FK 로 해석된다 — 방향 점수가 수치 컬럼에 적용될 수 있다"
            )


class TestFkCandidacyHonorsExplicitPatterns:
    """``fk_patterns.json`` 의 **명시 매핑** 이 접미사 규칙에 막히지 않는지.

    ## 배경 (2026-08-17 실측) — 방향 판정보다 앞단의 결함

    방향 판정과 배선을 다 고친 뒤에도 A-Box 의 출발 창고는 **0건이었다**. 원인은 더
    앞단이었다: ``_is_fk_candidate`` 가 ``id``/``code`` 접미사 + 화이트리스트 2개만
    인정했고, 명시 매핑은 ``no``/``ref``/``num`` 접미사에 **곁들여서만** 봤다.

    그래서 ``fk_patterns.json`` 이 ``originwarehouse`` / ``destinationwarehouse`` 를
    ``WarehouseMaster`` 로 선언했는데도 접미사가 안 맞아 **FK 경로에 진입조차
    못 했다** — 또 하나의 죽은 설정이다 (19개 엔트리 중 2개, 둘 다 실제 CSV 컬럼).

    ## 이것이 만든 착시

    KG 에 보이던 ``hasDestinationWarehouse`` 300건은 CSV 가 아니라
    ``tacit_cq_shortcuts.ttl`` 이 채운 것이었다. 즉 **수동 보정이 CSV 사실의 부재를
    덮고 있었다** — "300건 있으니 목적지는 정상" 이라는 내 초기 판단이 틀린 근거다.
    출발 창고는 tacit 에도 없었으므로 그냥 사라져 있었다.
    """

    def test_explicit_pattern_beats_suffix_rule(self):
        """명시 선언은 접미사와 무관하게 FK 후보다."""
        from tools.abox_generation import _FK_PATTERNS, _is_fk_candidate

        for col in ("originwarehouse", "destinationwarehouse"):
            assert col in _FK_PATTERNS, f"{col} 전제 확인 실패"
            assert _is_fk_candidate(col), (
                f"{col} 이 fk_patterns.json 에 선언됐는데 FK 후보가 아니다 — "
                "설정을 읽지만 적용하지 않는 죽은 설정"
            )

    def test_no_declared_pattern_is_ignored(self):
        """**모든** 선언 엔트리가 후보로 인정되는지 (죽은 설정 0)."""
        from tools.abox_generation import _FK_PATTERNS, _is_fk_candidate

        dead = [k for k in _FK_PATTERNS if not _is_fk_candidate(k)]
        assert not dead, f"선언됐지만 무시되는 엔트리: {dead}"

    def test_suffix_heuristic_still_works_for_undeclared(self):
        """**정당한 입력 보존**: 선언되지 않은 ``*_id``/``*_code`` 는 계속 인식."""
        from tools.abox_generation import _FK_PATTERNS, _is_fk_candidate

        for col in ("someunknownid", "someunknowncode"):
            assert col not in _FK_PATTERNS, "이 테스트는 미선언 컬럼을 가정한다"
            assert _is_fk_candidate(col), f"{col} 접미사 휴리스틱이 깨졌다"

    def test_unrelated_column_still_rejected(self):
        """방향 무관 일반 컬럼은 여전히 FK 가 아니다 (과잉 확대 방지)."""
        from tools.abox_generation import _is_fk_candidate

        for col in ("temperaturec", "efficiencypercent", "rootcause"):
            assert not _is_fk_candidate(col), f"{col} 이 FK 로 오인됐다"


class TestDeployedAboxHasBothWarehouseDirections:
    """**산출물 기반**: 배포 A-Box 가 CSV 의 출발·목적 창고를 모두 담는지.

    앞의 단위/소스 테스트가 모두 통과하는 동안에도 산출물은 두 번 비어 있었다
    (배선 결함 → FK 후보 판정 결함). 최종 판단은 **산출물** 로만 한다.

    OP 이름은 ``_op_for_column`` 으로 해상한다 — 이름을 박으면 S2 개명 때 "데이터가
    정상인데 실패" 한다 (2026-09-05 실측).
    """

    def test_all_csv_rows_have_correct_both_directions(self):
        import csv
        import os

        from rdflib import URIRef

        import config as _cfg
        from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS

        csv_path = os.path.join(_cfg.SOURCE_RAWDATA_DIR, "Transportation.csv")
        if not (os.path.exists(_cfg.ABOX_PATH) and os.path.exists(csv_path)):
            pytest.skip("A-Box 또는 Transportation.csv 없음")

        from domain.tbox_utils import _new_graph, fast_parse_turtle
        g = _new_graph()
        fast_parse_turtle(g, _cfg.ABOX_PATH)

        def _tail(u) -> str:
            return str(u).rsplit("#", 1)[-1].rsplit("/", 1)[-1]

        with open(csv_path, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        assert rows, "CSV 가 비었다"

        origin_op = URIRef(DOMAIN_NS + _op_for_column(
            "Origin_Warehouse", "Transportation", "WarehouseMaster"))
        dest_op = URIRef(DOMAIN_NS + _op_for_column(
            "Destination_Warehouse", "Transportation", "WarehouseMaster"))
        assert origin_op != dest_op, (
            f"두 컬럼이 같은 OP 로 해상됐다 ({_tail(origin_op)}) — 방향이 뭉개진다"
        )

        mismatch = []
        for r in rows:
            subj = URIRef(DOMAIN_INST_NS + f"Transportation_{r['Transport_ID']}")
            origin = [
                _tail(x).replace("WarehouseMaster_", "")
                for x in g.objects(subj, origin_op)
            ]
            dest = [
                _tail(x).replace("WarehouseMaster_", "")
                for x in g.objects(subj, dest_op)
            ]
            if origin != [r["Origin_Warehouse"]] or dest != [r["Destination_Warehouse"]]:
                mismatch.append(
                    (r["Transport_ID"], r["Origin_Warehouse"], origin,
                     r["Destination_Warehouse"], dest)
                )

        assert not mismatch, (
            f"{len(mismatch)}/{len(rows)} 행의 창고 관계가 CSV 와 다르다. "
            f"샘플: {mismatch[:3]}"
        )

    def test_origin_is_not_conflated_with_destination(self):
        """두 술어가 **서로 다른** 값 집합을 갖는지 — 뭉개짐 재발 감지.

        같은 술어로 뭉개지면 두 컬럼의 값이 구별 불가능해진다. 개수만 세면
        (300 + 300 = 600) 통과하므로 **값 수준** 으로 확인한다.
        """
        import os

        from rdflib import URIRef

        import config as _cfg
        from domain.namespaces import DOMAIN_NS

        if not os.path.exists(_cfg.ABOX_PATH):
            pytest.skip("A-Box 없음")
        from domain.tbox_utils import _new_graph, fast_parse_turtle
        g = _new_graph()
        fast_parse_turtle(g, _cfg.ABOX_PATH)

        pairs_origin = set(g.subject_objects(URIRef(DOMAIN_NS + _op_for_column(
            "Origin_Warehouse", "Transportation", "WarehouseMaster"))))
        pairs_dest = set(g.subject_objects(URIRef(DOMAIN_NS + _op_for_column(
            "Destination_Warehouse", "Transportation", "WarehouseMaster"))))
        assert pairs_origin, "출발 창고 트리플이 0건이다"
        assert pairs_dest, "목적 창고 트리플이 0건이다"
        overlap = pairs_origin & pairs_dest
        assert not overlap, (
            f"같은 (운송, 창고) 쌍이 두 술어에 모두 있다 ({len(overlap)}건) — "
            "방향이 뭉개졌다"
        )
