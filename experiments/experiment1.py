from __future__ import annotations

import pathlib
import re
from typing import Any, Dict, Optional

from autograde.common.grading import entry
from autograde.common.utils import (
    PrivacyMasker,
    collect_commits,
    collect_docs,
    find_pom,
    safe_name,
    truncate,
)


EXPERIMENT_ID = "experiment1"
RUBRIC_ID = "experiment1-env-init-v3-fixed-objective-subjective"
RUBRIC = [
    {
        "id": "env_setup",
        "max": 15,
        "objective_max": 3,
        "subjective_max": 12,
        "name": "开发环境配置与验证",
    },
    {
        "id": "skeleton_project",
        "max": 15,
        "objective_max": 3,
        "subjective_max": 12,
        "name": "骨架项目创建与导入",
    },
    {
        "id": "mirror_build",
        "max": 15,
        "objective_max": 6,
        "subjective_max": 9,
        "name": "国内镜像源配置与构建成功",
        "build_score": 6,
    },
    {
        "id": "run_page_mark",
        "max": 15,
        "objective_max": 0,
        "subjective_max": 15,
        "name": "基础运行与页面标识修改",
    },
    {
        "id": "process_doc",
        "max": 15,
        "objective_max": 3,
        "subjective_max": 12,
        "name": "过程记录文档",
    },
    {
        "id": "ai_log",
        "max": 10,
        "objective_max": 1,
        "subjective_max": 9,
        "name": "AI 使用记录",
    },
    {
        "id": "incremental_commits",
        "max": 5,
        "objective_max": 4,
        "subjective_max": 1,
        "name": "多次提交与渐进完成",
    },
    {
        "id": "member_completeness",
        "max": 10,
        "objective_max": 6,
        "subjective_max": 4,
        "name": "组内成员提交完整性",
    },
]
RUBRIC_BY_ID = {r["id"]: r for r in RUBRIC}
EXPECTED_TOTAL = sum(r["max"] for r in RUBRIC)
OBJECTIVE_TOTAL_MAX = sum(r["objective_max"] for r in RUBRIC)
SUBJECTIVE_TOTAL_MAX = sum(r["subjective_max"] for r in RUBRIC)
if OBJECTIVE_TOTAL_MAX + SUBJECTIVE_TOTAL_MAX != EXPECTED_TOTAL:
    raise ValueError("experiment1 rubric objective/subjective split must sum to 100")

PROMPTS = {
    "env_setup": (
        "评估『开发环境配置与验证』主观分（本项主观上限12；满分15，强客观上限3，仅覆盖 step 文档存在）。"
        "判断 JDK/Git/Maven 版本是否为真实命令输出，IDE 和数据库是否有可用性验证，"
        "以及记录是否完整可信。材料：step_doc_full、version_text_hits、doc_figures。"
    ),
    "skeleton_project": (
        "评估『骨架项目创建与导入』主观分（本项主观上限12；满分15，强客观上限3，仅覆盖 pom.xml 存在）。"
        "判断是否使用 Spring Initializr，Group/Artifact/Package 是否清楚，"
        "pom 坐标是否体现本组信息，项目结构与 IDE 导入是否可复验。"
        "材料：pom_snippet、code_signals、step_doc_full、doc_figures。"
    ),
    "mirror_build": (
        "评估『国内镜像源配置与构建成功』主观分（本项主观上限9；满分15，强客观上限6，仅覆盖 Maven 构建成功）。"
        "判断镜像源配置位置、配置原因、构建命令/输出或截图是否可信。"
        "材料：step_doc_full、mirror_evidence_candidates、build、doc_figures。"
    ),
    "run_page_mark": (
        "评估『基础运行与页面标识修改』主观分（本项主观上限15；满分15，本项无强客观分）。"
        "判断是否记录 localhost 访问、页面结果、本组标识/组号/图标修改，以及运行闭环。"
        "材料：step_doc_full、code_signals、doc_figures。"
    ),
    "process_doc": (
        "评估『过程记录文档』主观分（本项主观上限12；满分15，强客观上限3，仅覆盖 step 文档存在）。"
        "判断是否覆盖环境验证、镜像配置、项目初始化、构建运行、页面修改、提交记录、问题处理，"
        "并判断是否真实而非模板。材料：step_doc_full、step_total_chars、doc_figures。"
    ),
    "ai_log": (
        "评估『AI 使用记录』主观分（本项主观上限9；满分10，强客观上限1，仅覆盖 AI 日志文件存在）。"
        "判断 Prompt、AI 输出、采纳/未采纳、人工核查、责任说明和提交 hash 关联质量。"
        "材料：ai_log_full、git_log_oneline、verified_commit_hashes、unverified_commit_hashes、doc_figures。"
    ),
    "incremental_commits": (
        "评估『多次提交与渐进完成』主观分（本项主观上限1；满分5，强客观上限4，覆盖提交次数）。"
        "判断提交是否跨阶段、跨日期、语义化、粒度合理，而不是一次性堆砌。"
    ),
    "member_completeness": (
        "评估『组内成员提交完整性』主观分（本项主观上限4；满分10，强客观上限6，覆盖作者数）。"
        "判断每位作者提交是否有实质内容，是否存在单人代全组或空提交。"
    ),
}


def detect_code_signals(work_dir: pathlib.Path) -> Dict[str, Any]:
    signals: Dict[str, Any] = {}
    pom = find_pom(work_dir)
    signals["pom_exists"] = pom is not None
    signals["pom_path"] = str(pom.relative_to(work_dir)) if pom else None
    pom_text = pom.read_text(encoding="utf-8", errors="replace") if pom else ""
    project_root = pom.parent if pom else work_dir
    pom_no_parent = re.sub(r"<parent>.*?</parent>", "", pom_text, flags=re.DOTALL | re.IGNORECASE)
    group_id_match = re.search(r"<groupId>\s*([^<\s]+)\s*</groupId>", pom_no_parent)
    gid = group_id_match.group(1).strip() if group_id_match else ""
    signals.update({
        "project_root": str(project_root.relative_to(work_dir)) if project_root != work_dir else ".",
        "spring_boot_parent": bool(re.search(r"spring-boot-starter-parent", pom_text)),
        "spring_boot_web": bool(re.search(r"spring-boot-starter-web", pom_text)),
        "thymeleaf": bool(re.search(r"thymeleaf", pom_text, re.IGNORECASE)),
        "mirror_in_pom": bool(re.search(
            r"aliyun|maven\.aliyun\.com|huawei\.com/repository|mirrors\.163\.com",
            pom_text,
            re.IGNORECASE,
        )),
        "group_id": gid,
        "group_id_customized": bool(gid)
        and "com.example" not in gid
        and "demo" not in gid.lower()
        and "org.springframework" not in gid,
        "src_main_java": (project_root / "src" / "main" / "java").is_dir(),
        "spring_app_class": bool(list(project_root.rglob("*Application.java"))[:1]) if pom else False,
    })

    template_dir = project_root / "src" / "main" / "resources" / "templates"
    static_dir = project_root / "src" / "main" / "resources" / "static"
    html_files = []
    for base in (template_dir, static_dir):
        if base.is_dir():
            html_files.extend(base.rglob("*.html"))
    page_hits = []
    for html in html_files[:30]:
        try:
            content = html.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"小组|组号|group|Group|组名|组标志|logo", content, re.IGNORECASE):
            page_hits.append(str(html.relative_to(work_dir)))
    settings_files = [
        p for p in work_dir.rglob("settings.xml")
        if not any(part in {".git", "target"} for part in p.parts)
    ]
    signals.update({
        "templates_dir": template_dir.is_dir(),
        "static_dir": static_dir.is_dir(),
        "html_files": len(html_files),
        "page_mark_hits": page_hits,
        "settings_xml_in_project": bool(settings_files),
        "settings_xml_paths": [str(p.relative_to(work_dir)) for p in settings_files[:5]],
        "pom_snippet": truncate(pom_text, 3000) if pom_text else "",
    })
    return signals


def _entry(rid: str, score: int, evidence: list[str], materials: Dict[str, Any], masker: PrivacyMasker):
    return entry(RUBRIC_BY_ID, PROMPTS, rid, score, evidence, materials, masker)


def score_branch(
    branch: str,
    repo: pathlib.Path,
    work_dir: pathlib.Path,
    masker: PrivacyMasker,
    build: Dict[str, Any],
    figures_root: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    branch_figures_dir = figures_root / safe_name(branch) if figures_root else None
    code = detect_code_signals(work_dir)
    docs = collect_docs(work_dir, masker, branch_figures_dir)
    commits = collect_commits(repo, branch, masker)

    step_docs = docs.get("step_docs", [])
    ai_logs = docs.get("ai_logs", [])
    doc_figures = docs.get("figures", [])
    step_full = "\n\n---\n\n".join(d["full_text"] for d in step_docs if d.get("full_text"))
    ai_full = "\n\n---\n\n".join(d["full_text"] for d in ai_logs if d.get("full_text"))
    git_log_oneline = "\n".join(
        f"{r['hash']} {r['date']} {r['subject']}" for r in commits["recent_commits"][:30]
    )

    auto_scores: Dict[str, Any] = {}
    ai_pending: Dict[str, Any] = {}

    s, ev = 0, []
    if step_docs:
        s += 3
        ev.append(f"docs/step*.md 存在（{len(step_docs)} 个文件）")
    ver_hits = re.findall(
        r"(openjdk|java version|git version|apache maven)\s+[\d.]+",
        step_full,
        re.IGNORECASE,
    )
    auto_scores["env_setup"], ai_pending["env_setup"] = _entry(
        "env_setup", s, ev, {
            "step_doc_full": truncate(step_full, 8000),
            "version_text_hits": ver_hits,
            "doc_figures": doc_figures,
        }, masker)

    s, ev = 0, []
    if code["pom_exists"]:
        s += 3
        ev.append(f"pom.xml 存在：{code['pom_path']}")
    auto_scores["skeleton_project"], ai_pending["skeleton_project"] = _entry(
        "skeleton_project", s, ev, {
            "pom_snippet": code["pom_snippet"],
            "code_signals": {k: v for k, v in code.items() if k != "pom_snippet"},
            "step_doc_full": truncate(step_full, 4000),
            "doc_figures": doc_figures,
        }, masker)

    s, ev = 0, []
    if build.get("build_ok") is True:
        s += 6
        ev.append("Maven 构建成功（BUILD SUCCESS）")
    elif build.get("build_ok") is False:
        ev.append(f"Maven 构建失败 rc={build.get('exit_code')}")
    elif build.get("status") == "no_pom_found":
        ev.append("未找到 pom.xml，构建跳过")
    mirror_candidates = []
    if code["mirror_in_pom"]:
        mirror_candidates.append("pom.xml 内含镜像源配置")
    if code["settings_xml_in_project"]:
        mirror_candidates.append(f"项目内含 settings.xml：{code['settings_xml_paths']}")
    if re.search(r"aliyun|镜像|mirror|settings\.xml", step_full, re.IGNORECASE):
        mirror_candidates.append("step 文档中提及镜像源配置")
    auto_scores["mirror_build"], ai_pending["mirror_build"] = _entry(
        "mirror_build", s, ev, {
            "step_doc_full": truncate(step_full, 4000),
            "mirror_evidence_candidates": mirror_candidates,
            "build": build,
            "doc_figures": doc_figures,
        }, masker)

    auto_scores["run_page_mark"], ai_pending["run_page_mark"] = _entry(
        "run_page_mark", 0, [], {
            "step_doc_full": truncate(step_full, 6000),
            "code_signals": {k: v for k, v in code.items() if k != "pom_snippet"},
            "doc_figures": doc_figures,
        }, masker)

    s, ev = 0, []
    total_step_chars = sum(d["char_count"] for d in step_docs)
    if step_docs:
        s += 3
        ev.append(f"step 文档存在（{len(step_docs)} 个，共 {total_step_chars} 字）")
    auto_scores["process_doc"], ai_pending["process_doc"] = _entry(
        "process_doc", s, ev, {
            "step_doc_full": step_full,
            "step_total_chars": total_step_chars,
            "doc_figures": doc_figures,
        }, masker)

    s, ev = 0, []
    if ai_logs:
        s += 1
        ev.append(f"AI 日志文件存在（{len(ai_logs)} 个）")
    known_hashes = {r["hash"] for r in commits["recent_commits"]}
    found_hashes = re.findall(r"\b[0-9a-f]{7,8}\b", ai_full)
    verified = [h for h in found_hashes if h[:8] in known_hashes]
    auto_scores["ai_log"], ai_pending["ai_log"] = _entry(
        "ai_log", s, ev, {
            "ai_log_full": ai_full,
            "git_log_oneline": git_log_oneline,
            "verified_commit_hashes": verified[:20],
            "unverified_commit_hashes": [h for h in found_hashes if h[:8] not in known_hashes][:20],
            "doc_figures": doc_figures,
        }, masker)

    diverged = commits["commit_count_diverged_from_main"]
    days = commits["unique_commit_days"]
    s, ev = 0, []
    if diverged >= 5:
        s = 4
    elif diverged >= 3:
        s = 3
    elif diverged >= 2:
        s = 2
    elif diverged >= 1:
        s = 1
    if diverged:
        ev.append(f"{diverged} 次分支提交")
    auto_scores["incremental_commits"], ai_pending["incremental_commits"] = _entry(
        "incremental_commits", s, ev, {
            "commit_count_diverged": diverged,
            "unique_days": days,
            "semantic_ratio": commits["semantic_ratio"],
            "weekly_histogram": commits["weekly_histogram"],
            "recent_commits": commits["recent_commits"][:20],
        }, masker)

    distinct = commits["distinct_authors"]
    s, ev = 0, []
    if distinct >= 4:
        s = 6
    elif distinct == 3:
        s = 5
    elif distinct == 2:
        s = 4
    elif distinct == 1:
        s = 2
    ev.append(f"distinct_authors = {distinct}")
    auto_scores["member_completeness"], ai_pending["member_completeness"] = _entry(
        "member_completeness", s, ev, {"per_author_stats": commits["per_author_stats"]}, masker)

    objective_total = sum(v["score"] for v in auto_scores.values())
    objective_total_max = sum(v["max"] for v in auto_scores.values())
    subjective_total_max = sum(v["max"] for v in ai_pending.values())

    return {
        "branch": branch,
        "build": build,
        "commits": {k: v for k, v in commits.items() if k != "recent_commits"},
        "code_signals": {k: v for k, v in code.items() if k != "pom_snippet"},
        "doc_summary": {
            "step_doc_count": len(step_docs),
            "ai_log_count": len(ai_logs),
            "figure_count": len(doc_figures),
            "figure_export_dir": str(branch_figures_dir) if branch_figures_dir else "",
            "step_total_chars": total_step_chars,
            "ai_total_chars": sum(d["char_count"] for d in ai_logs),
        },
        "auto_scores": auto_scores,
        "ai_pending": ai_pending,
        "summary": {
            "auto_total": objective_total,
            "objective_total": objective_total,
            "objective_total_max": objective_total_max,
            "ai_pending_total_max": subjective_total_max,
            "subjective_total_max": subjective_total_max,
            "max_possible_after_objective": objective_total + subjective_total_max,
            "objective_missing": max(0, objective_total_max - objective_total),
            "expected_total_max": EXPECTED_TOTAL,
        },
    }
