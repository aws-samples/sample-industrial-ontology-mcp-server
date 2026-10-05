"""rules/*.json 파일 스키마 + cross-reference 검증.

독립 실행 (python -m tools.validate_rules) 또는 프로그램 호출 (validate_rules_files).

검증 대상:
- common_dp.json: common_datatype_properties 배열 스키마, required_props_by_class
  에서 참조하는 DP 이름이 common 또는 aliases 에 존재하는지.
- fk_patterns.json: patterns(dict) + suffix_rules(list) 스키마.
- value_heuristics.json: 주요 필드 타입.
- value_normalizations.json: enum_synonyms 각 entry 의 canonical/aliases.
- table_class_mapping.json: class_prop_mapping 엔트리의 class/col 포맷.
"""
from __future__ import annotations

import json
import os
import sys

from domain.rules_paths import RULES_ROOT, rules_path

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RULES_DIR = RULES_ROOT


def _load(filename: str) -> dict:
    path = rules_path(filename, base=_RULES_DIR)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _check_common_dp(common: dict, issues: list) -> None:
    dps = common.get("common_datatype_properties", [])
    if not isinstance(dps, list):
        issues.append("common_dp.json: common_datatype_properties must be a list")
        return
    names: set[str] = set()
    aliases_all: set[str] = set()
    for i, entry in enumerate(dps):
        if not isinstance(entry, dict):
            issues.append(f"common_dp[{i}]: not a dict")
            continue
        name = entry.get("name")
        if not name:
            issues.append(f"common_dp[{i}]: missing 'name'")
            continue
        names.add(name)
        if not entry.get("range"):
            issues.append(f"common_dp[{name}]: missing 'range'")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list):
            issues.append(f"common_dp[{name}]: aliases must be a list")
            continue
        for alias in aliases:
            aliases_all.add(str(alias))

    # required_props_by_class 에서 참조한 DP 이름이 common 목록 또는
    # aliases 에 있는지 확인
    req = common.get("required_props_by_class", {})
    if not isinstance(req, dict):
        issues.append("required_props_by_class must be a dict")
        return
    known = names | aliases_all
    for cls, dps_list in req.items():
        if cls.startswith("_"):
            continue
        if not isinstance(dps_list, list):
            issues.append(f"required_props_by_class[{cls}]: must be list")
            continue
        for entry in dps_list:
            if isinstance(entry, str):
                dp_name = entry
                cond = None
            elif isinstance(entry, dict):
                dp_name = entry.get("dp")
                cond = entry.get("if")
            else:
                issues.append(f"required_props_by_class[{cls}]: invalid entry {entry!r}")
                continue
            if not dp_name or dp_name not in known:
                issues.append(
                    f"required_props_by_class[{cls}] references unknown DP '{dp_name}' "
                    f"(not in common_datatype_properties[name|aliases])",
                )
            if cond is not None:
                if not isinstance(cond, dict):
                    issues.append(f"required_props_by_class[{cls}][{dp_name}]: if must be dict")
                    continue
                for _k in ("dp", "op", "value"):
                    if _k not in cond:
                        issues.append(
                            f"required_props_by_class[{cls}][{dp_name}]: if missing '{_k}'",
                        )
                if cond.get("op") not in ("==", "!="):
                    issues.append(
                        f"required_props_by_class[{cls}][{dp_name}]: "
                        f"unsupported op '{cond.get('op')}' (only == / != supported)",
                    )


def _check_fk_patterns(fk: dict, issues: list) -> None:
    pats = fk.get("patterns")
    if pats is not None and not isinstance(pats, dict):
        issues.append("fk_patterns.patterns must be a dict")
    rules = fk.get("suffix_rules", [])
    if not isinstance(rules, list):
        issues.append("fk_patterns.suffix_rules must be a list")
        return
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            issues.append(f"fk_patterns.suffix_rules[{i}]: not a dict")
            continue
        if not rule.get("suffix"):
            issues.append(f"fk_patterns.suffix_rules[{i}]: missing 'suffix'")
        if not rule.get("template"):
            issues.append(f"fk_patterns.suffix_rules[{i}]: missing 'template'")
    transforms = fk.get("fk_value_transforms")
    if transforms is not None and not isinstance(transforms, dict):
        issues.append("fk_patterns.fk_value_transforms must be a dict")
    elif isinstance(transforms, dict):
        for col, spec in transforms.items():
            if col.startswith("_"):
                continue
            if not isinstance(spec, dict):
                issues.append(f"fk_patterns.fk_value_transforms[{col!r}]: not a dict")
                continue
            n = spec.get("strip_suffix_chars")
            if n is not None and (not isinstance(n, int) or isinstance(n, bool) or n <= 0):
                issues.append(
                    f"fk_patterns.fk_value_transforms[{col!r}].strip_suffix_chars "
                    "must be a positive integer"
                )


def _check_value_heuristics(vh: dict, issues: list) -> None:
    list_fields = [
        "numeric_keywords", "timestamp_columns", "pk_class_suffixes",
        "transaction_filename_suffixes", "master_filename_suffixes",
        "null_sentinels",
    ]
    for f in list_fields:
        val = vh.get(f)
        if val is None:
            continue
        if not isinstance(val, list):
            issues.append(f"value_heuristics.{f} must be a list (got {type(val).__name__})")


def _check_value_normalizations(vn: dict, issues: list) -> None:
    enums = vn.get("enum_synonyms", {})
    if not isinstance(enums, dict):
        issues.append("value_normalizations.enum_synonyms must be a dict")
        return
    for col, mapping in enums.items():
        if col.startswith("_"):
            continue
        if not isinstance(mapping, dict):
            issues.append(f"enum_synonyms[{col}] must be a dict (canonical → [aliases])")
            continue
        for canonical, aliases in mapping.items():
            if not isinstance(aliases, list):
                issues.append(
                    f"enum_synonyms[{col}][{canonical}] aliases must be a list",
                )


def validate_rules_files() -> list[str]:
    """Validate every known rules/*.json. Returns list of issue strings."""
    issues: list[str] = []
    _check_common_dp(_load("common_dp.json"), issues)
    _check_fk_patterns(_load("fk_patterns.json"), issues)
    _check_value_heuristics(_load("value_heuristics.json"), issues)
    _check_value_normalizations(_load("value_normalizations.json"), issues)
    return issues


def validate_rules() -> str:
    """rules/*.json 스키마 + cross-reference 검증.

    rules/ 파일 (fk_patterns / common_dp / value_heuristics /
    value_normalizations 등) 을 수동 편집한 뒤 또는 initialize_domain_rules
    실행 직후 호출해 스키마 위반을 조기 발견한다. T-Box 생성 (S2) 전에
    실행 권장.

    Returns:
        JSON 문자열. {"success": bool, "issue_count": int, "issues": [...]}
    """
    issues = validate_rules_files()
    return json.dumps(
        {"success": not issues, "issue_count": len(issues), "issues": issues},
        ensure_ascii=False,
        indent=2,
    )


def main() -> int:
    issues = validate_rules_files()
    if not issues:
        print("rules/*.json validation: OK")
        return 0
    print(f"rules validation found {len(issues)} issue(s):")
    for i in issues:
        print(f"  - {i}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
