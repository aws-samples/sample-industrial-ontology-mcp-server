"""End-to-end loss budget aggregation — A-Box + Inference + LPG manifest 통합."""
from __future__ import annotations

import json
import logging
import os

from config import GENERATED_ABOX_DIR, INFERRED_PATH

logger = logging.getLogger(__name__)


def _sum_losses(losses: dict) -> int:
    return sum(
        cat.get("count", 0) for cat in losses.values() if isinstance(cat, dict)
    )


def load_loss_budget(
    *,
    generated_abox_dir: str | None = None,
    inferred_path: str | None = None,
) -> dict | None:
    """A-Box + LPG + Inference loss manifest를 로드하여 end-to-end 손실 예산 반환.

    Args:
        generated_abox_dir: A-Box 산출물 디렉토리 (테스트 주입용).
        inferred_path: inferred TTL 경로 (테스트 주입용).
    """
    abox_dir = generated_abox_dir or GENERATED_ABOX_DIR
    inf_path = inferred_path or INFERRED_PATH
    try:
        abox_manifest_path = os.path.join(abox_dir, "abox_loss_manifest.json")
        lpg_manifest_path = os.path.join(
            os.path.dirname(inf_path), "neo4j", "lpg_loss_manifest.json"
        )
        inf_manifest_path = os.path.join(
            os.path.dirname(inf_path), "inference_loss_manifest.json"
        )

        budget: dict = {"stages": {}, "total_loss_items": 0}

        if os.path.exists(abox_manifest_path):
            with open(abox_manifest_path, encoding="utf-8") as f:
                abox_lm = json.load(f)
            losses = abox_lm.get("losses", {})
            fidelity = abox_lm.get("fidelity_score", {})
            n = _sum_losses(losses)
            budget["stages"]["abox_generation"] = {
                "unmapped_columns": losses.get("unmapped_columns", {}).get("count", 0),
                "fk_failures": losses.get("fk_referential_failures", {}).get("count", 0),
                "coercion_failures": losses.get("type_coercion_failures", {}).get("count", 0),
                "duplicate_pks": losses.get("duplicate_pk_rows", {}).get("count", 0),
                "total_loss_items": n,
                "fidelity": fidelity,
            }
            budget["total_loss_items"] += n

        if os.path.exists(lpg_manifest_path):
            with open(lpg_manifest_path, encoding="utf-8") as f:
                lpg_lm = json.load(f)
            lpg_losses = lpg_lm.get("losses", {})
            n = _sum_losses(lpg_losses)
            budget["stages"]["lpg_conversion"] = {
                "owl_axiom_exclusions": lpg_losses.get("owl_axiom_exclusions", {}).get("count", 0),
                "bnode_losses": lpg_losses.get("bnode_losses", {}).get("count", 0),
                "external_drops": lpg_losses.get("external_reference_drops", {}).get("count", 0),
                "total_loss_items": n,
                "preservation": lpg_lm.get("preservation_score", {}),
            }
            budget["total_loss_items"] += n

        if os.path.exists(inf_manifest_path):
            with open(inf_manifest_path, encoding="utf-8") as f:
                inf_lm = json.load(f)
            inf_losses = inf_lm.get("losses", {})
            n = _sum_losses(inf_losses)
            budget["stages"]["inference"] = {
                "noise_pruned": n,
                "type_pollution": inf_losses.get("type_pollution_removed", {}).get("count", 0),
                "preservation": inf_lm.get("preservation_score", {}),
            }
            budget["total_loss_items"] += n

        # 누적 보존률
        if budget["stages"]:
            preservation_scores: list[float] = []
            for stage_data in budget["stages"].values():
                pres = stage_data.get("preservation", stage_data.get("fidelity", {}))
                ratio: float | None = None
                if isinstance(pres, dict):
                    ratio = pres.get("meaningful_triples_ratio", pres.get("overall", None))
                elif isinstance(pres, int | float):
                    ratio = float(pres)
                if ratio is not None:
                    preservation_scores.append(float(ratio))
            if preservation_scores:
                cumulative = 1.0
                for p in preservation_scores:
                    cumulative *= p
                budget["cumulative_preservation_rate"] = round(cumulative, 4)

        return budget if budget["stages"] else None
    except Exception as e:
        logger.debug("Loss budget 로드 실패: %s", e)
        return None
