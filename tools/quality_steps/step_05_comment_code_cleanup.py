"""Step 5 — Comment 내 코드 패턴 정리.

본문 ontology_quality.py 의 Step 5 블록을 그대로 모듈로 옮김.

LLM 이 한국어 ``rdfs:comment@ko`` 안에 ``steel:foo, steel:bar`` 같은 코드
스니펫을 흘리는 것을 정리. ``한글IOF`` → ``한글. IOF`` 띄어쓰기 보정 +
연속 공백 단일화.
"""
from __future__ import annotations

import logging
import re

from rdflib import RDFS, Graph, Literal

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    code_pattern = re.compile(
        r"\s*,?\s*steel:\w+(?:\s*,\s*steel:\w+)*\s*\.?\s*"
    )
    cleaned = 0
    for s, p, o in list(g.triples((None, RDFS.comment, None))):
        if not isinstance(o, Literal) or o.language != "ko":
            continue
        text = str(o)
        new_text = code_pattern.sub(" ", text)
        new_text = re.sub(r"([가-힣])IOF", r"\1. IOF", new_text)
        new_text = re.sub(r"\s{2,}", " ", new_text).strip()
        if new_text != text:
            g.remove((s, p, o))
            g.add((s, p, Literal(new_text, lang="ko")))
            cleaned += 1
    return StepResult(
        name="step_05_comment_code_cleanup",
        stats={"comments_cleaned": cleaned},
        triples_delta=len(g) - before,
        step_number=5,
        step_label="comment_code_cleanup",
    )
