"""2홉 경로 자동 생성 테스트."""


def _make_classes_dict():
    """Test fixture: 3 클래스 간 단순 2홉 구조."""
    return {
        "EquipmentMaster": {
            "object_properties_outgoing": [
                {"property": "hasTag", "target": ["TagMaster"]},
            ],
        },
        "TagMaster": {
            "object_properties_outgoing": [
                {"property": "triggersAlarm", "target": ["AlarmEvents"]},
            ],
        },
        "AlarmEvents": {
            "object_properties_outgoing": [],
        },
    }


def test_two_hop_basic_path():
    from tools.semantic_dictionary import _compute_two_hop_paths
    result = _compute_two_hop_paths(_make_classes_dict())
    assert "EquipmentMaster" in result
    paths = result["EquipmentMaster"]
    assert len(paths) == 1
    p = paths[0]
    assert p["path"] == ["hasTag", "triggersAlarm"]
    assert p["via"] == "TagMaster"
    assert p["target"] == "AlarmEvents"
    assert "sparql_hint" in p


def test_two_hop_excludes_self_reference():
    cd = {
        "A": {"object_properties_outgoing": [{"property": "p1", "target": ["B"]}]},
        "B": {"object_properties_outgoing": [{"property": "p2", "target": ["A"]}]},
    }
    from tools.semantic_dictionary import _compute_two_hop_paths
    result = _compute_two_hop_paths(cd)
    # A → B → A 경로는 제외됨
    assert "A" not in result or all(p["target"] != "A" for p in result.get("A", []))


def test_two_hop_scoring_prefers_master():
    cd = {
        "Src": {
            "object_properties_outgoing": [
                {"property": "hasTx", "target": ["TxMid"]},
                {"property": "hasMaster", "target": ["MasterMid"]},
            ],
        },
        "TxMid": {
            "object_properties_outgoing": [
                {"property": "p2", "target": ["TxEnd"]},
            ],
        },
        "MasterMid": {
            "object_properties_outgoing": [
                {"property": "p2", "target": ["MasterEnd"]},
            ],
        },
        "TxEnd": {"object_properties_outgoing": []},
        "MasterEnd": {"object_properties_outgoing": []},
    }
    abox_stats = {
        "per_class": {
            "TxEnd": {"instance_count": 10000},  # transaction
            "MasterEnd": {"instance_count": 50},  # master
        }
    }
    from tools.semantic_dictionary import _compute_two_hop_paths
    result = _compute_two_hop_paths(cd, abox_stats=abox_stats)
    paths = result["Src"]
    # Master target 이 상위에 와야 함
    assert paths[0]["target"] == "MasterEnd"
    assert paths[0]["score"] > paths[1]["score"]


def test_two_hop_max_per_class_limit():
    """클래스당 최대 N개만 반환."""
    cd = {
        "A": {"object_properties_outgoing": [
            {"property": f"op{i}", "target": [f"B{i}"]} for i in range(20)
        ]},
    }
    for i in range(20):
        cd[f"B{i}"] = {"object_properties_outgoing": [
            {"property": f"op2_{i}", "target": [f"C{i}"]}
        ]}
        cd[f"C{i}"] = {"object_properties_outgoing": []}
    from tools.semantic_dictionary import _compute_two_hop_paths
    result = _compute_two_hop_paths(cd, max_per_class=5)
    assert len(result["A"]) == 5


def test_two_hop_sparql_hint_format():
    from domain.namespaces import NS_PREFIX
    from tools.semantic_dictionary import _compute_two_hop_paths
    result = _compute_two_hop_paths(_make_classes_dict())
    hint = result["EquipmentMaster"][0]["sparql_hint"]
    assert f"{NS_PREFIX}:EquipmentMaster" in hint
    assert f"{NS_PREFIX}:hasTag" in hint
    assert f"{NS_PREFIX}:triggersAlarm" in hint
