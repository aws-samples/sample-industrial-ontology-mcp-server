"""``table_class_mapping.json`` 캐시의 mtime 기반 무효화 회귀 가드.

배경 (2026-07-25 실측): ``_load_table_pk_columns`` / ``_load_table_class_mapping``
이 파일을 한 번만 읽고 프로세스 수명 내내 캐시했다. MCP 서버는 장수명이므로
운영 중 매핑을 고쳐도 서버 재시작 전까지 반영되지 않는다.

증상: 신규 테이블 ``Facility_Operation_Master`` 의 복합 PK
``[FACILITY_CD, OPER_KIND_FLAG]`` 를 등록했는데 구 캐시가 비어 있어 휴리스틱 PK
(``SITE_CODE`` — 154행 중 2개만 유니크) 가 쓰였고, 154행이 인스턴스 2개로
뭉쳤다. 클래스명도 매핑을 못 찾아 파일명 기반 ``FacilityOperationMaster`` 로
생성됐다.

T-Box 캐시군(``_invalidate_tbox_caches_if_stale``) 은 이미 mtime 검사를 하므로
같은 패턴을 적용했다.
"""
from __future__ import annotations

import time


def test_pk_cache_reloads_when_file_mtime_changes(tmp_path, monkeypatch):
    """mtime 이 바뀌면 PK 캐시를 버리고 다시 읽는다."""
    import json

    import tools.abox_generation as ab

    mapping = tmp_path / "table_class_mapping.json"
    mapping.write_text(json.dumps({
        "table_class_mapping": {"T_ONE": "steel:ClassOne"},
        "table_pk_columns": {"T_ONE": ["KEY_A"]},
    }), encoding="utf-8")
    monkeypatch.setattr(ab, "_RULES_DIR", str(tmp_path))
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_MTIME", 0.0)

    assert ab._load_table_pk_columns()["T_ONE"] == ["KEY_A"]

    # 매핑을 갱신 — mtime 이 확실히 달라지도록 대기 후 기록.
    time.sleep(1.1)
    mapping.write_text(json.dumps({
        "table_class_mapping": {"T_ONE": "steel:ClassOne"},
        "table_pk_columns": {"T_ONE": ["KEY_A", "KEY_B"]},
    }), encoding="utf-8")

    assert ab._load_table_pk_columns()["T_ONE"] == ["KEY_A", "KEY_B"], (
        "mtime 변경 후에도 구 캐시를 반환하면 운영 중 매핑 수정이 무시된다"
    )


def test_class_map_cache_reloads_when_file_mtime_changes(tmp_path, monkeypatch):
    """mtime 이 바뀌면 클래스 매핑 캐시도 다시 읽는다."""
    import json

    import tools.abox_generation as ab

    mapping = tmp_path / "table_class_mapping.json"
    mapping.write_text(json.dumps({
        "table_class_mapping": {"T_ONE": "steel:ClassOne"},
        "table_pk_columns": {},
    }), encoding="utf-8")
    monkeypatch.setattr(ab, "_RULES_DIR", str(tmp_path))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)

    assert ab._load_table_class_mapping() == {"T_ONE": "ClassOne"}

    time.sleep(1.1)
    mapping.write_text(json.dumps({
        "table_class_mapping": {
            "T_ONE": "steel:ClassOne",
            "T_TWO": "steel:ClassTwo",
        },
        "table_pk_columns": {},
    }), encoding="utf-8")

    result = ab._load_table_class_mapping()
    assert result == {"T_ONE": "ClassOne", "T_TWO": "ClassTwo"}


def test_cache_is_reused_when_file_unchanged(tmp_path, monkeypatch):
    """파일이 안 바뀌면 캐시를 재사용한다 (매 호출 디스크 재파싱 방지)."""
    import json

    import tools.abox_generation as ab

    mapping = tmp_path / "table_class_mapping.json"
    mapping.write_text(json.dumps({
        "table_class_mapping": {"T_ONE": "steel:ClassOne"},
        "table_pk_columns": {"T_ONE": ["KEY_A"]},
    }), encoding="utf-8")
    monkeypatch.setattr(ab, "_RULES_DIR", str(tmp_path))
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_MTIME", 0.0)

    first = ab._load_table_pk_columns()
    second = ab._load_table_pk_columns()
    assert first is second, "mtime 동일 시 같은 dict 객체를 반환해야 한다"
