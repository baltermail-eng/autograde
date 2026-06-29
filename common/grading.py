from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .utils import PrivacyMasker, mask_deep


def entry(
    rubric_by_id: Dict[str, Dict[str, Any]],
    prompts: Dict[str, str],
    rid: str,
    score: int,
    evidence: List[str],
    materials: Dict[str, Any],
    masker: PrivacyMasker,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    rubric = rubric_by_id[rid]
    total_max = int(rubric["max"])
    objective_max = int(rubric.get("objective_max", total_max))
    subjective_max = int(rubric.get("subjective_max", max(0, total_max - objective_max)))
    actual = min(score, objective_max)
    prompt = prompts[rid]
    if materials.get("doc_figures"):
        prompt += (
            " 若 materials.doc_figures 非空，请读取 exported_path 指向的图片，"
            "把截图中的命令输出、运行页面、IDE导入或环境验证证据纳入主观评分。"
        )
    return (
        {
            "score": actual,
            "max": objective_max,
            "rubric_total_max": total_max,
            "subjective_max": subjective_max,
            "objective_missing": max(0, objective_max - actual),
            "evidence": evidence,
        },
        {
            "max": subjective_max,
            "rubric_id": rid,
            "rubric_name": rubric["name"],
            "rubric_total_max": total_max,
            "objective_max": objective_max,
            "objective_score": actual,
            "prompt": prompt,
            "materials": mask_deep(materials, masker),
        },
    )
