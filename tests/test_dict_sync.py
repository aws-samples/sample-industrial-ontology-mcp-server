"""Tests for _enrich_metadata() — 시맨틱 딕셔너리 메타데이터 동기화."""

import json
import os
import time
from unittest.mock import patch

from tools.semantic_dictionary import _enrich_metadata

# ── 헬퍼 ─────────────────────────────────────────


def _base_metadata():
    """enrichment 전 기본 메타데이터."""
    return {
        "version": "2.0",
        "domain": "steel",
        "tbox_triples": 100,
        "abox_triples": 200,
    }


# ── 추론 그래프 통계 ─────────────────────────────


class TestInferenceStats:
    """추론 그래프 파일 크기 + loss manifest 보존률."""

    def test_inferred_file_size(self, tmp_path):
        """INFERRED_PATH가 존재하면 파일 크기를 메타데이터에 추가."""
        inf = tmp_path / "all_inferred.ttl"
        inf.write_text("dummy content 1234567890")

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(inf)), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "no2")):
            meta = _enrich_metadata(_base_metadata())

        assert meta["inferred_file_size_bytes"] == inf.stat().st_size
        # 기존 키 보존
        assert meta["version"] == "2.0"
        assert meta["tbox_triples"] == 100

    def test_preservation_rate_from_manifest(self, tmp_path):
        """inference_loss_manifest.json이 있으면 보존률을 읽어온다."""
        inf = tmp_path / "all_inferred.ttl"
        inf.write_text("data")
        manifest = tmp_path / "inference_loss_manifest.json"
        manifest.write_text(json.dumps({
            "preservation_score": {"meaningful_triples_ratio": 0.95},
        }))

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(inf)), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "no2")):
            meta = _enrich_metadata(_base_metadata())

        assert meta["inference_preservation_rate"] == 0.95

    def test_no_inferred_file(self, tmp_path):
        """추론 파일이 없으면 관련 키를 추가하지 않는다."""
        with patch("tools.semantic_dictionary.INFERRED_PATH", str(tmp_path / "missing.ttl")), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "no2")):
            meta = _enrich_metadata(_base_metadata())

        assert "inferred_file_size_bytes" not in meta
        assert "inference_preservation_rate" not in meta

    def test_manifest_missing_keys(self, tmp_path):
        """manifest에 preservation_score 키가 없으면 None이 된다."""
        inf = tmp_path / "all_inferred.ttl"
        inf.write_text("data")
        manifest = tmp_path / "inference_loss_manifest.json"
        manifest.write_text(json.dumps({"some_other": 1}))

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(inf)), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "no2")):
            meta = _enrich_metadata(_base_metadata())

        assert meta["inference_preservation_rate"] is None


# ── LPG 변환 상태 ────────────────────────────────


class TestLpgState:
    """LPG semantic dictionary 존재 여부 + 통계."""

    def test_lpg_dict_available(self, tmp_path):
        """LPG 딕셔너리가 존재하면 노드/관계 수를 읽어온다."""
        lpg = tmp_path / "semantic_dictionary.json"
        lpg.write_text(json.dumps({
            "metadata": {"total_nodes": 500, "total_relationships": 1200},
        }))

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(lpg)), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "no2")):
            meta = _enrich_metadata(_base_metadata())

        assert meta["lpg_dictionary_available"] is True
        assert meta["lpg_nodes"] == 500
        assert meta["lpg_relationships"] == 1200

    def test_lpg_dict_missing(self, tmp_path):
        """LPG 딕셔너리가 없으면 available=False만 설정."""
        with patch("tools.semantic_dictionary.INFERRED_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no_lpg")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "no2")):
            meta = _enrich_metadata(_base_metadata())

        assert meta["lpg_dictionary_available"] is False
        assert "lpg_nodes" not in meta

    def test_lpg_dict_malformed_json(self, tmp_path):
        """LPG 파일이 깨진 JSON이면 available=True, 통계 키는 없다."""
        lpg = tmp_path / "semantic_dictionary.json"
        lpg.write_text("not valid json {{{")

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(lpg)), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "no2")):
            meta = _enrich_metadata(_base_metadata())

        assert meta["lpg_dictionary_available"] is True
        assert "lpg_nodes" not in meta


# ── Staleness 감지 ───────────────────────────────


class TestStaleness:
    """T-Box/Inferred 대비 딕셔너리 staleness 감지."""

    def test_stale_when_inferred_newer(self, tmp_path):
        """추론 파일이 딕셔너리보다 새로우면 stale=True."""
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        # 시간 차이를 확보
        old_time = time.time() - 10
        os.utime(str(dict_file), (old_time, old_time))

        inf = tmp_path / "all_inferred.ttl"
        inf.write_text("newer")

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(inf)), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(dict_file)):
            meta = _enrich_metadata(_base_metadata())

        assert meta["stale"] is True

    def test_not_stale_when_dict_newer(self, tmp_path):
        """딕셔너리가 추론 파일보다 새로우면 stale=False."""
        inf = tmp_path / "all_inferred.ttl"
        inf.write_text("older")
        old_time = time.time() - 10
        os.utime(str(inf), (old_time, old_time))

        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(inf)), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(dict_file)):
            meta = _enrich_metadata(_base_metadata())

        assert meta["stale"] is False

    def test_tbox_newer_than_dict(self, tmp_path):
        """T-Box가 딕셔너리보다 새로우면 tbox_newer_than_dict=True."""
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        old_time = time.time() - 10
        os.utime(str(dict_file), (old_time, old_time))

        tbox = tmp_path / "t_box.ttl"
        tbox.write_text("newer tbox")

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no2")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.semantic_dictionary.TBOX_PATH", str(tbox)):
            meta = _enrich_metadata(_base_metadata())

        assert meta["tbox_newer_than_dict"] is True

    def test_no_dict_file_no_staleness(self, tmp_path):
        """딕셔너리 파일 자체가 없으면 staleness 키를 추가하지 않는다."""
        with patch("tools.semantic_dictionary.INFERRED_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no2")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "missing.json")):
            meta = _enrich_metadata(_base_metadata())

        assert "stale" not in meta
        assert "tbox_newer_than_dict" not in meta


# ── 생성 문맥: 자기 이전 세대를 재지 않는가 ───────────────────────────────
#
# 2026-08-30 실측. ``_enrich_metadata`` 의 **유일한 배포 호출자는 생성 경로**
# (``generate_semantic_dictionary``) 이고, 그 시점의 디스크 파일은 **직전 세대**다
# (``_save_dictionary`` 는 그 뒤에 돈다). 그래서 갓 만든 딕셔너리에 낡음이 찍혔다:
#
#     S6.5 v1 저장    10:47:41
#     all_inferred    10:57:44
#     S10 v2 저장     11:06:40  → 파일에 stale=true 가 각인됐다
#
# v2 는 10:57:44 추론 그래프를 **읽어서** 만들었으므로 낡지 않았다. 비교 대상이
# 자기 이전 세대(v1 파일 mtime)였을 뿐이다. 기준선 실행에서는 우연히 반대로
# 찍혀, 두 실행의 값이 서로 반대 방향으로 틀렸다.
#
# 이 리포에 같은 형태가 반복 기록돼 있다 — 게이트가 삭제 스텝 앞에서 재서 배포되지
# 않는 중간 상태를 측정했고(step_22f), guard 가 S2 초안과 S3 완료본을 비교해 48분을
# 폐기했다. 측정은 자기가 판정하려는 **대상**을 재야 한다.


class TestWriteContextDoesNotMeasureItsOwnPastGeneration:
    """생성 문맥(``is_being_written=True``)의 계약."""

    def _patched(self, tmp_path, *, dict_age_s: float, inferred_age_s: float,
                 tbox_age_s: float):
        """딕셔너리/추론/T-Box 파일을 주어진 '나이'로 만들어 patch 컨텍스트 반환."""
        now = time.time()
        paths = {}
        for name, age in (("semantic_dictionary.json", dict_age_s),
                          ("all_inferred.ttl", inferred_age_s),
                          ("t_box.ttl", tbox_age_s)):
            f = tmp_path / name
            f.write_text("x")
            os.utime(str(f), (now - age, now - age))
            paths[name] = str(f)
        return paths

    def test_fresh_dictionary_is_never_stale(self, tmp_path):
        """THE REGRESSION: 갓 생성한 딕셔너리에 ``stale: true`` 가 찍히지 않는다.

        실측 배치를 그대로 재현한다 — 디스크의 딕셔너리 파일(직전 세대)이
        추론 그래프보다 **오래됐다**. 예전 구현은 이 조건에서 stale=true 를 찍었다.
        """
        p = self._patched(tmp_path, dict_age_s=1200, inferred_age_s=600,
                          tbox_age_s=1800)
        with patch("tools.semantic_dictionary.INFERRED_PATH", p["all_inferred.ttl"]), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", p["semantic_dictionary.json"]), \
             patch("tools.semantic_dictionary.TBOX_PATH", p["t_box.ttl"]):
            meta = _enrich_metadata(_base_metadata(), is_being_written=True)

        assert meta["stale"] is False, (
            "갓 생성한 딕셔너리가 낡았다고 각인됐다 — 비교 대상이 자기 이전 세대다"
        )
        assert meta["tbox_newer_than_dict"] is False

    def test_write_context_records_the_input_generation(self, tmp_path):
        """생성 시점에 **읽은 입력의 세대**를 각인한다 (이후 판정의 근거)."""
        p = self._patched(tmp_path, dict_age_s=1200, inferred_age_s=600,
                          tbox_age_s=1800)
        with patch("tools.semantic_dictionary.INFERRED_PATH", p["all_inferred.ttl"]), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", p["semantic_dictionary.json"]), \
             patch("tools.semantic_dictionary.TBOX_PATH", p["t_box.ttl"]):
            meta = _enrich_metadata(_base_metadata(), is_being_written=True)

        assert "source_mtimes" in meta, "입력 세대가 각인되지 않았다"
        assert meta["source_mtimes"]["inferred"] == os.path.getmtime(
            p["all_inferred.ttl"])
        assert meta["source_mtimes"]["tbox"] == os.path.getmtime(p["t_box.ttl"])

    def test_staleness_fields_present_not_omitted(self, tmp_path):
        """생성 문맥에서도 두 필드를 **명시**한다 — 키 부재는 오독된다.

        이 리포의 함정: 소비자가 키 부재를 "판정 실패" 가 아니라 "정상" 으로
        읽는다 (필드 부재 ≠ 값 0). ``step_22f`` 도 같은 이유로 조기 반환 경로에
        자기점검 필드를 넣는다.
        """
        p = self._patched(tmp_path, dict_age_s=10, inferred_age_s=10,
                          tbox_age_s=10)
        with patch("tools.semantic_dictionary.INFERRED_PATH", p["all_inferred.ttl"]), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", p["semantic_dictionary.json"]), \
             patch("tools.semantic_dictionary.TBOX_PATH", p["t_box.ttl"]):
            meta = _enrich_metadata(_base_metadata(), is_being_written=True)

        assert "stale" in meta and "tbox_newer_than_dict" in meta

    def test_read_context_uses_recorded_generation(self, tmp_path):
        """조회 문맥은 각인된 입력 세대와 비교한다 (파일 mtime 은 간접 신호).

        추론 그래프가 **각인된 세대보다 새로우면** 낡음이다 — 딕셔너리 파일
        자신의 mtime 과 무관하게.
        """
        p = self._patched(tmp_path, dict_age_s=5, inferred_age_s=600,
                          tbox_age_s=1800)
        recorded = dict(_base_metadata())
        # 딕셔너리는 '한참 전' 추론 그래프를 읽어 만들어졌다고 각인
        recorded["source_mtimes"] = {
            "inferred": os.path.getmtime(p["all_inferred.ttl"]) - 100,
            "tbox": os.path.getmtime(p["t_box.ttl"]) - 100,
        }
        with patch("tools.semantic_dictionary.INFERRED_PATH", p["all_inferred.ttl"]), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", p["semantic_dictionary.json"]), \
             patch("tools.semantic_dictionary.TBOX_PATH", p["t_box.ttl"]):
            meta = _enrich_metadata(recorded)

        assert meta["stale"] is True, (
            "각인된 입력 세대보다 새 추론 그래프가 있는데 낡음으로 보지 않았다"
        )
        assert meta["tbox_newer_than_dict"] is True

    def test_read_context_without_record_falls_back_to_file_mtime(self, tmp_path):
        """각인이 없는 옛 딕셔너리는 종전대로 파일 mtime 으로 판정한다.

        하위호환 — 각인 필드가 없다고 판정을 포기하면 기존 파일에서 신선도 신호가
        사라진다.
        """
        p = self._patched(tmp_path, dict_age_s=1200, inferred_age_s=600,
                          tbox_age_s=1800)
        with patch("tools.semantic_dictionary.INFERRED_PATH", p["all_inferred.ttl"]), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", p["semantic_dictionary.json"]), \
             patch("tools.semantic_dictionary.TBOX_PATH", p["t_box.ttl"]):
            meta = _enrich_metadata(_base_metadata())   # 각인 없음

        assert meta["stale"] is True          # inferred(600s) > dict(1200s)
        assert meta["tbox_newer_than_dict"] is False   # tbox(1800s) < dict(1200s)

    def test_generation_path_passes_the_flag(self, monkeypatch):
        """배선 계약: 생성 경로가 실제로 ``is_being_written=True`` 를 넘기는가.

        함수 경계 아래 배선은 단위 테스트로 안 잡힌다 — 이 리포에 "단위 테스트
        14건이 초록인데 산출물이 두 번 안 바뀌었다" 가 기록돼 있다.

        **소스 문자열 검사로는 안 된다.** 처음엔 ``"is_being_written=True" in src``
        로 썼는데, 그 문자열이 바로 위 **설명 주석**에도 있어서 호출부에서 플래그를
        빼는 mutation 이 그대로 통과했다 (같은 함정이 이 리포에 기록돼 있다:
        "소스 문자열 검사만으론 mutant 생존"). 그래서 AST 로 **실제 호출의 키워드
        인자**를 본다.
        """
        import ast
        import inspect
        import textwrap

        from tools import semantic_dictionary as sd

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(sd.generate_semantic_dictionary)))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "_enrich_metadata"
        ]
        assert calls, "생성 경로가 _enrich_metadata 를 부르지 않는다"
        for call in calls:
            flags = {
                kw.arg: getattr(kw.value, "value", None)
                for kw in call.keywords
            }
            assert flags.get("is_being_written") is True, (
                "생성 경로가 is_being_written=True 를 넘기지 않는다 — 갓 만든 "
                f"딕셔너리에 stale 이 찍힌다 (실제 키워드: {flags})"
            )


# ── 기존 키 보존 ─────────────────────────────────


class TestPreservation:
    """_enrich_metadata는 기존 메타데이터 키를 절대 제거하지 않는다."""

    def test_all_original_keys_preserved(self, tmp_path):
        """어떤 파일 조합이든 원본 키가 모두 남아있어야 한다."""
        original = _base_metadata()
        original["custom_field"] = "should survive"
        original_keys = set(original.keys())

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(tmp_path / "no")), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(tmp_path / "no2")), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(tmp_path / "no3")):
            meta = _enrich_metadata(original)

        assert original_keys.issubset(set(meta.keys()))
        assert meta["custom_field"] == "should survive"


# ── 전체 조합 (모든 파일 존재) ────────────────────


class TestFullCombination:
    """모든 파일이 존재하는 시나리오."""

    def test_all_files_present(self, tmp_path):
        """추론/LPG/딕셔너리 모두 존재할 때 전체 키 세트 확인."""
        # 추론 파일 + manifest
        inf = tmp_path / "all_inferred.ttl"
        inf.write_text("inferred data")
        manifest = tmp_path / "inference_loss_manifest.json"
        manifest.write_text(json.dumps({
            "preservation_score": {"meaningful_triples_ratio": 0.88},
        }))

        # LPG 딕셔너리
        lpg_dir = tmp_path / "neo4j"
        lpg_dir.mkdir()
        lpg = lpg_dir / "semantic_dictionary.json"
        lpg.write_text(json.dumps({
            "metadata": {"total_nodes": 300, "total_relationships": 800},
        }))

        # 시맨틱 딕셔너리 (구 버전 — stale 상태)
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        old_time = time.time() - 100
        os.utime(str(dict_file), (old_time, old_time))

        # T-Box
        tbox = tmp_path / "t_box.ttl"
        tbox.write_text("tbox data")

        with patch("tools.semantic_dictionary.INFERRED_PATH", str(inf)), \
             patch("tools.semantic_dictionary.LPG_SEMANTIC_DICT_PATH", str(lpg)), \
             patch("tools.semantic_dictionary.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.semantic_dictionary.TBOX_PATH", str(tbox)):
            meta = _enrich_metadata(_base_metadata())

        # 추론 통계
        assert meta["inferred_file_size_bytes"] > 0
        assert meta["inference_preservation_rate"] == 0.88
        # LPG
        assert meta["lpg_dictionary_available"] is True
        assert meta["lpg_nodes"] == 300
        assert meta["lpg_relationships"] == 800
        # Staleness
        assert meta["stale"] is True
        assert meta["tbox_newer_than_dict"] is True
        # 원본 키 보존
        assert meta["version"] == "2.0"
        assert meta["domain"] == "steel"
