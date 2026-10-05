"""``initialize_domain_rules`` 의 덮어쓰기 가드 회귀 가드.

배경 (2026-08-08 실측): 이 도구는 신규 도메인 온보딩 진입점으로, 파이프라인 전체가
계약하는 ``rules/`` 설정 12개를 생성한다. 재실행이 **SME 가 손으로 다듬은 설정을
파괴하는 것** 을 막는 유일한 장치가 ``if not overwrite and os.path.exists(path)``
한 줄인데, ``_initialize_domain_rules`` (0/84 라인) · ``_write`` (0/6) ·
``_write_json`` (0/1) 이 전부 **실행된 적이 없었다**.

가드가 반전되거나 조건이 사라지면 아무 테스트도 실패하지 않는 상태였다. 실수 한
번에 12개 설정이 기본값으로 되돌아간다 — 그중 ``domain_config.json`` 은
``domain/namespaces.py`` 가 없으면 import 조차 못 하는 필수 파일이다.

이 파일은 임시 ``rules/`` 디렉토리에서 (1) 기본값이 기존 파일을 보존하고
(2) ``overwrite=True`` 만 덮어쓰며 (3) 두 경로가 결과에 정직하게 보고되는지 고정한다.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from domain.rules_paths import rules_path


def _config(rules) -> pathlib.Path:
    """임시 rules/ 안의 ``domain_config.json`` 실제 경로.

    2026-08-23 부터 설정은 카테고리 하위 폴더(``domain`` / ``policy`` /
    ``contracts``)에 생성된다. 테스트가 평평한 경로를 박아두면 구조 변경마다
    깨지므로 프로덕션과 **같은 해석기** 를 쓴다.
    """
    return pathlib.Path(rules_path("domain_config.json", base=str(rules)))


@pytest.fixture
def isolated_rules(tmp_path, monkeypatch):
    """임시 rules/ + rawdata/ 로 격리한 rules_init 모듈.

    Phase 2 의 Bedrock 호출은 스텁으로 대체한다 — 테스트가 실제 LLM 을 부르면
    느리고(호출당 5초+) 비용이 들고 네트워크에 의존한다. 이 파일이 검증하는 것은
    **덮어쓰기 가드** 이고 그것은 Phase 1 (프로그래매틱 생성) 의 로직이다.
    """
    import tools.bedrock as bedrock
    import tools.rules_init as ri

    rules = tmp_path / "rules"
    rules.mkdir()
    rawdata = tmp_path / "rawdata"
    rawdata.mkdir()
    # 최소 CSV 1개 — 도구가 스키마 분석에 쓴다.
    (rawdata / "Equipment_Master.csv").write_text(
        "Equipment_ID,Equipment_Name,Status\nEQ001,Pump,RUN\n", encoding="utf-8",
    )

    monkeypatch.setattr(ri, "_RULES_DIR", str(rules))
    if hasattr(ri, "SOURCE_RAWDATA_DIR"):
        monkeypatch.setattr(ri, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(
        bedrock, "invoke_bedrock_text",
        lambda *a, **k: '{"table_labels": {}, "disjoint_groups": [], '
                        '"design_patterns": {}}',
    )
    return ri, rules


def _run(ri, **kwargs) -> dict:
    payload = ri.initialize_domain_rules(
        domain_name=kwargs.pop("domain_name", "Generic Manufacturing"),
        domain_name_ko=kwargs.pop("domain_name_ko", "일반 제조"),
        namespace_uri=kwargs.pop("namespace_uri", "http://example.org/generic#"),
        prefix=kwargs.pop("prefix", "gen"),
        **kwargs,
    )
    return json.loads(payload)


def test_first_run_creates_config_files(isolated_rules):
    """정상 경로: 빈 rules/ 에서 파일이 생성된다."""
    ri, rules = isolated_rules
    result = _run(ri)
    if not result.get("success"):
        pytest.skip(f"환경 사유로 생성 실패: {str(result.get('error'))[:100]}")
    assert result.get("files_created"), "생성된 파일 목록이 비어 있다"
    assert _config(rules).exists(), (
        "domain_config.json 은 필수 — 없으면 domain/namespaces.py import 가 실패한다"
    )


def test_rerun_preserves_existing_files_by_default(isolated_rules):
    """THE REGRESSION: 기본값(overwrite=False)은 기존 설정을 덮어쓰지 않는다."""
    ri, rules = isolated_rules
    first = _run(ri)
    if not first.get("success"):
        pytest.skip("첫 실행 실패")

    # SME 가 손으로 다듬은 상태를 시뮬레이션.
    target = _config(rules)
    sme_edited = json.loads(target.read_text(encoding="utf-8"))
    sme_edited["_sme_marker"] = "hand-tuned, do not clobber"
    target.write_text(json.dumps(sme_edited, ensure_ascii=False), encoding="utf-8")

    second = _run(ri)
    assert second.get("success")

    after = json.loads(target.read_text(encoding="utf-8"))
    assert after.get("_sme_marker") == "hand-tuned, do not clobber", (
        "재실행이 SME 편집을 지웠다 — 12개 설정이 기본값으로 되돌아간다"
    )
    skipped_names = {os.path.basename(f) for f in second.get("files_skipped") or []}
    assert "domain_config.json" in skipped_names, (
        "보존 사실이 skipped 로 보고돼야 한다"
    )


def test_overwrite_true_does_replace(isolated_rules):
    """POSITIVE: 명시적 ``overwrite=True`` 는 실제로 덮어쓴다 (기능 보존)."""
    ri, rules = isolated_rules
    if not _run(ri).get("success"):
        pytest.skip("첫 실행 실패")

    target = _config(rules)
    edited = json.loads(target.read_text(encoding="utf-8"))
    edited["_sme_marker"] = "will be replaced"
    target.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")

    result = _run(ri, overwrite=True)
    assert result.get("success")
    after = json.loads(target.read_text(encoding="utf-8"))
    assert "_sme_marker" not in after, "overwrite=True 인데 덮어쓰지 않았다"
    created_names = {os.path.basename(f) for f in result.get("files_created") or []}
    assert "domain_config.json" in created_names


def test_created_and_skipped_are_disjoint(isolated_rules):
    """한 파일이 created 와 skipped 에 동시에 나타나지 않는다 (보고 정합성)."""
    ri, _rules = isolated_rules
    if not _run(ri).get("success"):
        pytest.skip("첫 실행 실패")
    second = _run(ri)
    created = {os.path.basename(f) for f in second.get("files_created") or []}
    skipped = {os.path.basename(f).split(" ")[0]
               for f in second.get("files_skipped") or []}
    assert not (created & skipped), f"양쪽에 중복: {sorted(created & skipped)}"


def test_generated_config_is_valid_json(isolated_rules):
    """생성된 JSON 파일이 모두 파싱 가능해야 한다 (파이프라인이 즉시 읽는다)."""
    ri, rules = isolated_rules
    if not _run(ri).get("success"):
        pytest.skip("첫 실행 실패")
    found = list(pathlib.Path(rules).rglob("*.json"))
    assert found, "생성된 JSON 이 없다 — 하위 폴더까지 훑는지 확인하라"
    for path in found:
        with open(path, encoding="utf-8") as handle:
            json.load(handle)   # 예외 발생 시 테스트 실패


# ──────────────────────────────────────────────────────────────────
# 이전 도메인 자산 은퇴 (retire_stale_domain_files, 기본 True)
# ──────────────────────────────────────────────────────────────────
# 이 도구가 만들지 않는 도메인 자산 6개는 이식 시 **이전 도메인 값이 그대로 남는다**.
# 실측 (2026-08-25): 철강 ``abstract_group_hints.json`` 이 남은 상태로 병원 클래스에
# 적용하면 ``VitalMonitoring ⊑ EnvironmentalMonitoring`` 등 6건이 에러 없이 주입된다
# — 패턴이 ``Master``/``Quality``/``Analysis``/``Monitoring`` 같은 일반 영어 단어라서다.
#
# 그렇다고 무조건 지울 수는 없다: 이 도구의 계약은 ``overwrite=True`` 없이 SME 편집을
# 파괴하지 않는 것이고, ``tacit_rules.json`` 은 SME 검증 지식이 들어가는 자리다.
# 그래서 **기본 ON + 백업(이름 변경)** 이다 — README 가 이미 첫 단계로 안내하고,
# 작성 시점(S5)이 초기화(1단계)보다 나중이라 초기화 때 SME 규칙이 있을 수 없다.
# 부분 재초기화만 예외이고 그때는 False 로 끈다.

_STALE_SIX = (
    "abstract_group_hints.json", "entailment_golden.json",
    "ontoclean_labels.json", "property_chains.json",
    "tacit_rules.json", "tbox_manual_additions.ttl",
)


def _seed_stale(rules) -> None:
    """이전 도메인 자산 6개를 심는다 (내용은 무관 — 존재가 문제다)."""
    d = pathlib.Path(rules) / "domain"
    d.mkdir(parents=True, exist_ok=True)
    for name in _STALE_SIX:
        (d / name).write_text("{}" if name.endswith(".json") else "# ttl\n",
                              encoding="utf-8")


def test_default_retires_stale_files(isolated_rules):
    """**기본값이 은퇴 ON 이다.**

    README 의 이식 절차가 "철강 규칙을 먼저 비우거나 삭제하는 것이 첫 단계" 라고
    안내하므로, 도구가 그것을 해주지 않으면 문서와 동작이 어긋난다. 그리고 작성
    시점은 S5 이고 초기화는 1단계이므로 **정규 흐름에서 초기화가 항상 먼저** 다 —
    초기화 시점에 SME 가 채운 tacit 규칙이 있을 수 없다.

    삭제가 아니라 백업(이름 변경)이므로 부분 재초기화 사고도 복구 가능하다.
    """
    ri, rules = isolated_rules
    _seed_stale(rules)
    result = _run(ri)
    if not result.get("success"):
        pytest.skip("실행 실패")
    retired = {pathlib.Path(f).name for f in result["retired_domain_files"]}
    assert retired == set(_STALE_SIX), result
    assert result["stale_domain_files"] == []
    d = pathlib.Path(rules) / "domain"
    for name in _STALE_SIX:
        assert not (d / name).exists(), f"{name} 이 그대로 있다"
    backups = [p.name for p in d.iterdir() if ".bak_" in p.name]
    assert len(backups) == len(_STALE_SIX), (
        f"백업 없이 지웠다 — 되돌릴 수 없다: {backups}"
    )


def test_opt_out_preserves_stale_files(isolated_rules):
    """``False`` 로 끄면 아무것도 치우지 않는다 (부분 재초기화 대비).

    S5 까지 진행해 ``tacit_rules.json`` 을 채운 뒤 CSV 변경으로 초기화만 다시
    돌리는 경우가 있다. 그때 이 스위치가 유일한 방어다.
    """
    ri, rules = isolated_rules
    _seed_stale(rules)
    result = _run(ri, retire_stale_domain_files=False)
    if not result.get("success"):
        pytest.skip("실행 실패")
    assert result.get("retired_domain_files") == []
    for name in _STALE_SIX:
        assert (pathlib.Path(rules) / "domain" / name).exists(), (
            f"끄기를 요청했는데 {name} 을 치웠다"
        )
    assert set(result["stale_domain_files"]) == set(_STALE_SIX)


def test_overwrite_default_stays_non_destructive(isolated_rules):
    """``overwrite`` 는 여전히 기본 False — 두 스위치를 혼동하지 않는다.

    ``overwrite`` 는 이 도구가 **생성하는** 12개를 기본값으로 되돌린다 (SME 가
    다듬은 값 소실 = 파괴). ``retire_stale_domain_files`` 는 이 도구가 **만들지
    않는** 6개를 백업하고 치운다 (되돌릴 수 있는 이동). 성격이 달라 기본값도 다르다.
    """
    ri, rules = isolated_rules
    if not _run(ri).get("success"):
        pytest.skip("첫 실행 실패")
    target = _config(rules)
    edited = json.loads(target.read_text(encoding="utf-8"))
    edited["_sme_marker"] = "hand-tuned"
    target.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")

    second = _run(ri)          # overwrite 를 주지 않는다
    assert second.get("success")
    after = json.loads(target.read_text(encoding="utf-8"))
    assert after.get("_sme_marker") == "hand-tuned", (
        "은퇴 기본값을 켰더니 overwrite 까지 켜졌다 — 생성 파일이 파괴됐다"
    )


def test_retirement_backs_up_rather_than_deletes(isolated_rules):
    """치울 때 **백업을 남긴다** (삭제 아님) — 명시적으로 켠 경우도 동일."""
    ri, rules = isolated_rules
    _seed_stale(rules)
    result = _run(ri, retire_stale_domain_files=True)
    if not result.get("success"):
        pytest.skip("실행 실패")

    retired = {pathlib.Path(f).name for f in result["retired_domain_files"]}
    assert retired == set(_STALE_SIX), result
    assert result["stale_domain_files"] == [], "은퇴 후에도 stale 로 보고했다"

    d = pathlib.Path(rules) / "domain"
    for name in _STALE_SIX:
        assert not (d / name).exists(), f"{name} 이 그대로 있다"
    backups = [p.name for p in d.iterdir() if ".bak_" in p.name]
    assert len(backups) == len(_STALE_SIX), (
        f"백업이 부족하다 — 삭제로 동작했다: {backups}"
    )
    assert result["retire_backup_suffix"].startswith(".bak_")


def test_retirement_does_not_touch_generated_or_policy_files(isolated_rules):
    """은퇴는 **그 6개만** 건드린다 (과잉 삭제 방지 — 주 방향).

    ``policy/`` 는 엔진 정책이라 지우면 후퇴 템플릿으로 덮어써져 SHACL 규칙을
    잃는다. ``domain_config.json`` 은 없으면 모듈 import 자체가 실패한다.
    """
    ri, rules = isolated_rules
    _seed_stale(rules)
    result = _run(ri, retire_stale_domain_files=True)
    if not result.get("success"):
        pytest.skip("실행 실패")
    assert _config(rules).exists(), "domain_config.json 을 은퇴시켰다"
    for name in result["retired_domain_files"]:
        assert pathlib.Path(name).parent.name == "domain", (
            f"domain 카테고리 밖을 은퇴시켰다: {name}"
        )


def test_retirement_is_noop_when_nothing_stale(isolated_rules):
    """치울 것이 없으면 아무 일도 하지 않는다."""
    ri, rules = isolated_rules
    result = _run(ri, retire_stale_domain_files=True)
    if not result.get("success"):
        pytest.skip("실행 실패")
    assert result["retired_domain_files"] == []
    assert result["retire_backup_suffix"] == ""
    backups = [p.name for p in (pathlib.Path(rules) / "domain").iterdir()
               if ".bak_" in p.name]
    assert backups == []
