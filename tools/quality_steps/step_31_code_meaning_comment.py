"""Step 31: 코드값 DatatypeProperty 의 한국어 rdfs:comment 에 코드값 의미를 덧붙인다.

코드값 컬럼은 값마다 업무 의미가 있다 (예: "10=typeA, 20=typeA2, 30=typeB").
T-Box 는 이 의미를 담지 않고, 자동 생성된 rdfs:comment 는 컬럼 이름만 적는다.
의미가 없으면 NL→SPARQL 엔진과 그 엔진이 쓰는 T-Box 임베딩이 "업무 용어" 같은
업무 표현을 원시 코드 필터로 옮기지 못한다.

입력 파일은 ``rules_path("code_meanings.json")``, 곧
``rules/domain/code_meanings.json`` 이다. 이 리포는 이 파일을 싣지 않으며
(``.gitignore`` 대상) 생성 도구도 제공하지 않는다. 배포가 원천 스키마의 코드
정의 자료에서 직접 만든다. 같은 파일을 ``tools/semantic_dictionary.py`` 도
읽는다. 이 step 이 기대하는 형태는 다음과 같다::

    {
      "columns": {
        "CODE_COL_1": {"10": "typeA", "20": "typeA2", "30": "typeB"}
      }
    }

- 최상위 키는 ``columns`` 하나만 읽고 나머지 최상위 키는 무시한다.
- ``columns`` 의 키는 원천 스키마 컬럼 코드다. 후보 컬럼 코드를 대문자로 바꿔
  대조하므로 키도 대문자로 적어야 맞는다.
- 값은 ``{코드값: 의미}`` dict 다. 의미가 빈 코드값은 문구에서 빠지고, 남은
  코드값은 문자열 정렬 순서로 ``"코드값: 10=typeA, 20=typeA2, 30=typeB"`` 처럼
  나열된다.
- 파일이 없거나 ``columns`` 가 비어 있으면 그래프를 바꾸지 않는 no-op 이고, stats 는
  ``{"code_comment_annotated": 0, "skipped": "no_code_meanings"}`` 다. 파일 읽기나
  JSON 파싱이 실패해도 경고 로그를 남기고 같은 no-op 으로 끝난다.

동작 성질:

  - 결정적이다 (LLM 을 쓰지 않는다).
  - 멱등이다. 한국어 comment 에 이미 "코드값:" 문구가 있는 DP 는 건너뛴다.
  - 덮어쓰기에 안전하다. 기존 ko comment 를 지우고 문구를 덧붙인 literal 하나로
    바꾸므로 ko comment 가 둘로 늘지 않는다. 다른 언어 comment 는 그대로 둔다.
    ko comment 가 없으면 문구만으로 새 ko comment 를 만든다.
  - 대상은 ``ctx.domain_ns`` 에 속한 DatatypeProperty 뿐이다.

원천 컬럼 코드는 권위가 높은 순서로 복원한다.

  1. ``dcterms:source``: T-Box 생성기가 기록한 값이며 추측하지 않는다.
  2. DP label/comment 에 든 "(COLUMN_CODE)" 토큰.
  3. DP 이름에서 가장 긴 클래스 이름 접두 (대소문자 무시) 를 떼고 남은 camelCase
     꼬리를 UPPER_SNAKE 로 바꾼 값.

위치: ``_POST_STEPS`` 에서 step_29 뒤, step_31b 앞에 등록된다. 자동 주입 DP 와
생성 DP 가 모두 존재하고 comment 정리 step 이 끝난 뒤에 돈다.
"""
from __future__ import annotations

import json
import logging
import os
import re

from rdflib import RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL

from domain.rules_paths import rules_path
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_CODE_MEANINGS_PATH = rules_path("code_meanings.json")

# label/comment 에 괄호로 든 원천 스키마 컬럼 코드를 뽑는다.
# 예: "표면 결함 코드(CODE_COL_1)" → "CODE_COL_1".
_COLUMN_CODE_RE = re.compile(r"\(([A-Z][A-Z0-9_]{2,})\)")
# Authoritative DP → source column link, written by the T-Box generator.
_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")
# Marker guarding idempotency — once appended, the clause starts with this.
_CODE_CLAUSE_PREFIX = "코드값:"


def _load_code_columns() -> dict:
    """Load {COLUMN_CODE: {value: meaning}} from code_meanings.json. Empty if absent."""
    if not os.path.exists(_CODE_MEANINGS_PATH):
        return {}
    try:
        with open(_CODE_MEANINGS_PATH, encoding="utf-8") as f:
            return json.load(f).get("columns", {})
    except (OSError, ValueError) as e:
        logger.warning("code_meanings.json 로드 실패: %s", e)
        return {}


def _camel_tail_to_column(dp_local: str, class_names_lower: set[str]) -> str | None:
    """Recover a column code from a DP name by stripping the class-camel prefix.

    e.g. dp ``materialADetailCodeCol1`` with class ``MaterialADetail`` →
    tail ``CodeCol1`` → ``CODE_COL_1``. Best-effort; returns None when the
    prefix cannot be identified.
    """
    # Find the longest class-camel-lower prefix that dp_local starts with.
    best = ""
    for cn in class_names_lower:
        if dp_local.lower().startswith(cn) and len(cn) > len(best):
            best = cn
    if not best:
        return None
    tail = dp_local[len(best):]
    if not tail:
        return None
    # Split CamelCase / acronym runs into UPPER_SNAKE:
    #   "CodeCol1" → CODE_COL_1 ; "TypeCol2" → TYPE_COL_2.
    tokens = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]*|[0-9]+", tail)
    if not tokens:
        return None
    return "_".join(t.upper() for t in tokens)


def _extract_column_codes(
    g: Graph, dp_uri: URIRef, dp_local: str, class_names_lower: set[str]
) -> list[str]:
    """Recover a DP's source column codes, most authoritative first.

    ``dcterms:source`` is written by the T-Box generator and never guessed, so
    it wins. The label/comment token and the de-camel-cased DP name are
    best-effort fallbacks for DPs that predate the annotation.
    """
    declared = [
        str(o).strip().upper()
        for o in g.objects(dp_uri, _DCTERMS_NS.source)
        if str(o).strip()
    ]
    if declared:
        return declared
    for pred in (RDFS.label, RDFS.comment):
        for o in g.objects(dp_uri, pred):
            if isinstance(o, Literal):
                m = _COLUMN_CODE_RE.search(str(o))
                if m:
                    return [m.group(1).upper()]
    guessed = _camel_tail_to_column(dp_local, class_names_lower)
    return [guessed] if guessed else []


def _format_clause(meanings: dict) -> str:
    """{"10": "typeA", ...} → '코드값: 10=typeA, 20=typeA2, ...' (deterministic order)."""
    parts = [f"{v}={meanings[v]}" for v in sorted(meanings) if meanings[v]]
    return f"{_CODE_CLAUSE_PREFIX} " + ", ".join(parts)


def _ko_comment(g: Graph, dp_uri: URIRef) -> Literal | None:
    """Return the Korean rdfs:comment literal for a DP, if any."""
    for o in g.objects(dp_uri, RDFS.comment):
        if isinstance(o, Literal) and o.language == "ko":
            return o
    return None


def apply(g: Graph, ctx: StepContext) -> StepResult:
    columns = _load_code_columns()
    if not columns:
        return StepResult(
            name="step_31_code_meaning_comment",
            stats={"code_comment_annotated": 0, "skipped": "no_code_meanings"},
            step_number="31",
            step_label="code_meaning_comment",
        )

    steel_str = ctx.domain_ns
    class_names_lower = {
        str(c).replace(steel_str, "").lower()
        for c in g.subjects(RDF.type, OWL.Class)
        if str(c).startswith(steel_str)
    }

    before = len(g)
    annotated = 0
    samples: list[str] = []

    for dp_uri in set(g.subjects(RDF.type, OWL.DatatypeProperty)):
        if not str(dp_uri).startswith(steel_str):
            continue
        dp_local = str(dp_uri).replace(steel_str, "")
        col, meanings = None, None
        for cand in _extract_column_codes(g, dp_uri, dp_local, class_names_lower):
            if columns.get(cand):
                col, meanings = cand, columns[cand]
                break
        if not meanings:
            continue
        existing = _ko_comment(g, dp_uri)
        existing_text = str(existing) if existing is not None else ""
        # Idempotent: already annotated in a prior run.
        if _CODE_CLAUSE_PREFIX in existing_text:
            continue
        clause = _format_clause(meanings)
        new_text = f"{existing_text} {clause}".strip() if existing_text else clause
        # Override-safe: replace the ko comment (remove old ko, keep other langs).
        if existing is not None:
            g.remove((dp_uri, RDFS.comment, existing))
        g.add((dp_uri, RDFS.comment, Literal(new_text, lang="ko")))
        annotated += 1
        if len(samples) < 10:
            samples.append(f"{dp_local}({col})")

    if annotated:
        logger.info(
            "Step 31: annotated %d code-valued DP comments with code meanings (%s)",
            annotated, samples[:5],
        )

    return StepResult(
        name="step_31_code_meaning_comment",
        stats={"code_comment_annotated": annotated, "samples": samples},
        triples_delta=len(g) - before,
        step_number="31",
        step_label="code_meaning_comment",
    )
