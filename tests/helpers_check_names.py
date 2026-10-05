"""KG check 의 **display name 정본**을 소스에서 유도한다 (하드코딩 금지).

## 왜 (2026-09-05 실측)

두 테스트가 유효 display name 집합을 각각 하드코딩해 두었고, 그 목록에
``스키마 참조 무결성`` 이 빠져 있었다 (2026-08-31 에 추가된 22번째 check). 그래서
``_MUTATION_CATEGORY_EXPECTED_CATCHERS`` 에 **실재하는** 이름을 넣자 "fake 이름" 이라며
실패했다 — 검사기가 자기 목록의 낡음을 결함으로 보고한 것이다.

레지스트리(``CheckRegistry``) 는 짧은 슬러그(``fk_ref``)를 키로 쓰므로 display name 을
주지 않고, 그것을 얻으려면 check 를 실제로 돌려야 한다(수십 초). 그래서 소스에서
``"name": "..."`` 를 읽는다 — 이 리포의 원칙대로 **판정기를 가리키는** 방식이다.
"""
from __future__ import annotations

import glob
import re

#: T-Box S4.5 validator 짧은 별칭 (``tools/mutation_runner.py::_validator_registry``).
TBOX_VALIDATOR_ALIASES = frozenset({"syntax", "quality", "hermit", "classify", "shacl"})

_CHECKS_GLOB = "tools/validation_support/checks/*.py"
_NAME_RE = re.compile(r'"name":\s*"([^"]+)"')


def kg_check_display_names() -> frozenset[str]:
    """``validate_kg`` check 가 응답에 싣는 ``name`` 값 전수.

    ``mutation_runner`` 가 ``caught_by`` 의 키로 쓰는 것이 바로 이 값이다.
    """
    names: set[str] = set()
    paths = sorted(glob.glob(_CHECKS_GLOB))
    assert paths, f"check 소스를 찾지 못했다: {_CHECKS_GLOB}"
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            names.update(_NAME_RE.findall(handle.read()))
    assert names, "check 소스에서 display name 을 하나도 읽지 못했다"
    return frozenset(names)


def valid_catcher_identifiers() -> frozenset[str]:
    """``caught_by`` 에 나타날 수 있는 모든 식별자 (T-Box 별칭 ∪ KG display name)."""
    return TBOX_VALIDATOR_ALIASES | kg_check_display_names()
