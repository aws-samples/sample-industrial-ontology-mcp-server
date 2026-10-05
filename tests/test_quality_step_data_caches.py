"""T-Box 후처리에서 소스 데이터 재읽기를 막는 캐시 회귀 가드.

**왜 필요한가** (2026-07-25 실측): T-Box 후처리 step 여러 개가 A-Box / rawdata CSV
를 각자 읽는데, 캐시가 없어 한 번의 ``improve_tbox`` 호출에서 같은 파일을 반복
스캔했다. A-Box 가 533MB 로 커지자 이게 드러났다:

    step_22_dup_op  8.2s   ← _load_abox_op_usage 가 rdflib 로 A-Box 전체 파싱
    step_12d        4.2s   ← _collect_expected_columns_per_class (CSV 전 행)
    step_13b        3.8s   ← _column_fill_ratios (CSV 전 행)
    step_13 / 27    3.4s   ← _build_csv_pk_map (CSV 전 행)
    step_12f        2.8s   ← A-Box 정규식 스캔
    → 파이프라인 1회 26초. 12 트리플짜리 단위 테스트 45개가 타임아웃.

특히 ``_load_abox_op_usage`` 는 rdflib 파싱이라 수 GB RSS 까지 썼다. 필요한 건
predicate 별 등장 횟수뿐이므로 텍스트 스캔으로 바꾸고, 모든 로더에 mtime+size
서명 기반 캐시를 붙였다 (파이프라인 재실행 26s → 4.4s).

이 테스트는 ① 두 번째 호출이 파일을 다시 읽지 않는지 ② CSV/A-Box 가 바뀌면
캐시가 무효화되는지를 고정한다. 캐시가 무효화되지 않으면 CSV 를 고쳐도 T-Box
후처리가 옛 데이터로 판단하는 더 나쁜 버그가 된다.
"""
from __future__ import annotations

import json


def _reset_module_caches(monkeypatch):
    """캐시 전역을 초기화 — 테스트 간 오염 방지."""
    import tools.ontology_quality as oq
    from tools.quality_steps import step_12f_orphan_dp_prune as s12f
    from tools.quality_steps import step_13b_cardinality_data_reality as s13b

    monkeypatch.setattr(oq, "_ABOX_OP_USAGE_CACHE", {})
    monkeypatch.setattr(oq, "_EXPECTED_COLUMNS_CACHE", None)
    monkeypatch.setattr(oq, "_CSV_PK_MAP_CACHE", None)
    monkeypatch.setattr(s13b, "_FILL_RATIO_CACHE", None)
    monkeypatch.setattr(s12f, "_ABOX_USED_CACHE", {})


def _count_opens(monkeypatch, target_suffix: str) -> list[str]:
    """``open()`` 호출 중 ``target_suffix`` 로 끝나는 경로를 기록한다."""
    import builtins

    opened: list[str] = []
    real_open = builtins.open

    def _tracking_open(path, *args, **kwargs):
        if isinstance(path, str | bytes) and str(path).endswith(target_suffix):
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _tracking_open)
    return opened


# ── A-Box OP usage ────────────────────────────────────────────────────


def test_abox_op_usage_is_text_scanned_not_parsed(tmp_path, monkeypatch):
    """rdflib 파싱이 아니라 텍스트 스캔이어야 한다 (500MB+ 대응).

    파싱 방식이면 문법이 깨진 A-Box 에서 예외/빈 dict 가 되지만, 텍스트 스캔은
    predicate 이름을 세어 낸다. 이 차이로 구현 방식을 고정한다.
    """
    import config
    import tools.ontology_quality as oq

    _reset_module_caches(monkeypatch)
    abox = tmp_path / "a_box.ttl"
    # 접두어 선언이 없어 rdflib 로는 파싱 불가한 조각
    abox.write_text(
        "steel-inst:x steel:performedAtFacility steel-inst:F1 ;\n"
        "\tsteel:performedAtFacility steel-inst:F2 ;\n"
        "\tsteel:idCol1 \"O1\" .\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    counts = oq._load_abox_op_usage()
    assert counts["performedAtFacility"] == 2
    assert counts["idCol1"] == 1


def test_abox_op_usage_reads_file_once(tmp_path, monkeypatch):
    """두 번째 호출은 파일을 다시 열지 않는다."""
    import config
    import tools.ontology_quality as oq

    _reset_module_caches(monkeypatch)
    abox = tmp_path / "a_box.ttl"
    abox.write_text("steel-inst:x steel:idCol1 \"O1\" .\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    opened = _count_opens(monkeypatch, "a_box.ttl")
    first = oq._load_abox_op_usage()
    second = oq._load_abox_op_usage()

    assert first == second
    assert len(opened) == 1, f"파일을 {len(opened)}번 열었다 — 캐시 미작동"


def test_abox_op_usage_cache_invalidates_on_change(tmp_path, monkeypatch):
    """A-Box 가 바뀌면 캐시를 버리고 다시 읽는다."""
    import config
    import tools.ontology_quality as oq

    _reset_module_caches(monkeypatch)
    abox = tmp_path / "a_box.ttl"
    abox.write_text("steel-inst:x steel:idCol1 \"O1\" .\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    assert oq._load_abox_op_usage()["idCol1"] == 1
    # 내용 + 크기 변경 → 서명이 달라진다
    abox.write_text(
        "steel-inst:x steel:idCol1 \"O1\" .\n"
        "steel-inst:y steel:idCol1 \"O2\" .\n",
        encoding="utf-8",
    )
    assert oq._load_abox_op_usage()["idCol1"] == 2, "CSV/A-Box 변경이 반영돼야 한다"


# ── CSV 로더 ───────────────────────────────────────────────────────────


def _stub_rawdata(tmp_path, monkeypatch, *, rows: str = "K1,v\n"):
    """rawdata CSV + table_class_mapping 스텁."""
    import config
    import tools.abox_generation as ab

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir(exist_ok=True)
    (rawdata / "T_FACT.csv").write_text("KEY,VAL\n" + rows, encoding="utf-8")

    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "table_class_mapping.json").write_text(json.dumps({
        "table_class_mapping": {"T_FACT": "steel:Fact"},
        "table_pk_columns": {"T_FACT": ["KEY"]},
    }), encoding="utf-8")

    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(ab, "_RULES_DIR", str(rules))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_MTIME", 0.0)
    return rawdata / "T_FACT.csv"


def test_expected_columns_reads_csv_once(tmp_path, monkeypatch):
    """_collect_expected_columns_per_class 는 CSV 를 한 번만 읽는다."""
    import tools.ontology_quality as oq

    _reset_module_caches(monkeypatch)
    _stub_rawdata(tmp_path, monkeypatch)
    # config 모듈 상수를 함수가 모듈 전역으로 참조하므로 함께 덮는다
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata"))

    opened = _count_opens(monkeypatch, "T_FACT.csv")
    first = oq._collect_expected_columns_per_class()
    second = oq._collect_expected_columns_per_class()

    assert first == second
    assert len(opened) == 1, f"CSV 를 {len(opened)}번 읽었다 — 캐시 미작동"


def test_expected_columns_cache_invalidates_on_csv_change(tmp_path, monkeypatch):
    """CSV 가 바뀌면 다시 읽는다 — 옛 스키마로 판단하면 더 나쁜 버그."""
    import tools.ontology_quality as oq

    _reset_module_caches(monkeypatch)
    csv_path = _stub_rawdata(tmp_path, monkeypatch)
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata"))

    before = oq._collect_expected_columns_per_class()
    assert "Fact" in before
    assert "val" in before["Fact"]["expected_columns"]

    csv_path.write_text("KEY,VAL,EXTRA\nK1,v,e\n", encoding="utf-8")
    after = oq._collect_expected_columns_per_class()
    assert "extra" in after["Fact"]["expected_columns"], (
        "CSV 컬럼 추가가 반영돼야 한다"
    )


def test_csv_pk_map_reads_csv_once(tmp_path, monkeypatch):
    """_build_csv_pk_map 은 CSV 를 한 번만 읽는다 (step 13·27 공유)."""
    import tools.ontology_quality as oq

    _reset_module_caches(monkeypatch)
    _stub_rawdata(tmp_path, monkeypatch, rows="K1,a\nK2,b\n")
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata"))

    opened = _count_opens(monkeypatch, "T_FACT.csv")
    first = oq._build_csv_pk_map()
    second = oq._build_csv_pk_map()

    assert first == second
    assert len(opened) == 1, f"CSV 를 {len(opened)}번 읽었다 — 캐시 미작동"


def test_fill_ratios_reads_csv_once(tmp_path, monkeypatch):
    """step_13b 의 충진율 측정도 한 번만 읽는다."""
    import tools.ontology_quality as oq
    from tools.quality_steps import step_13b_cardinality_data_reality as s13b

    _reset_module_caches(monkeypatch)
    _stub_rawdata(tmp_path, monkeypatch, rows="K1,a\nK2,\n")
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata"))

    opened = _count_opens(monkeypatch, "T_FACT.csv")
    ratios1, pk1 = s13b._column_fill_ratios()
    ratios2, pk2 = s13b._column_fill_ratios()

    assert ratios1 == ratios2 and pk1 == pk2
    assert ratios1["Fact"]["VAL"] == 0.5
    assert len(opened) == 1, f"CSV 를 {len(opened)}번 읽었다 — 캐시 미작동"


def test_fill_ratios_cache_invalidates_on_csv_change(tmp_path, monkeypatch):
    """CSV 충진율이 바뀌면 새 값을 쓴다 — 낡은 비율로 제약을 지우면 위험."""
    import tools.ontology_quality as oq
    from tools.quality_steps import step_13b_cardinality_data_reality as s13b

    _reset_module_caches(monkeypatch)
    csv_path = _stub_rawdata(tmp_path, monkeypatch, rows="K1,a\nK2,\n")
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata"))

    assert s13b._column_fill_ratios()[0]["Fact"]["VAL"] == 0.5
    csv_path.write_text("KEY,VAL\nK1,a\nK2,b\n", encoding="utf-8")
    assert s13b._column_fill_ratios()[0]["Fact"]["VAL"] == 1.0


def test_abox_used_predicates_reads_once(tmp_path, monkeypatch):
    """step_12f 의 A-Box 사용 스캔도 같은 후보 집합이면 한 번만 읽는다."""
    import config
    from tools.quality_steps import step_12f_orphan_dp_prune as s12f

    _reset_module_caches(monkeypatch)
    abox = tmp_path / "a_box.ttl"
    abox.write_text("steel-inst:x steel:usedDp \"v\" .\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    opened = _count_opens(monkeypatch, "a_box.ttl")
    candidates = {"usedDp", "unusedDp"}
    first = s12f._abox_used_predicates(candidates)
    second = s12f._abox_used_predicates(candidates)

    assert first == second == {"usedDp"}
    assert len(opened) == 1, f"A-Box 를 {len(opened)}번 읽었다 — 캐시 미작동"
