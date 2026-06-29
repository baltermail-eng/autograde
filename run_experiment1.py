from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import re
import shutil
import sys
from typing import Any, Dict, List, Tuple

from autograde.common.couchdb import (
    DEFAULT_COUCHDB_URL,
    CouchDBClient,
    CouchDBConfigError,
    CouchDBRequestError,
)
from autograde.common.utils import (
    PrivacyMasker,
    extract_branch,
    get_branch_tip_sha,
    git,
    leak_check,
    list_group_branches,
    load_roster,
    run_cmd,
    run_mvn_build,
    safe_name,
)
from autograde.experiments import experiment1


DEFAULT_REPO_URL = "https://gitee.com/java-ee-technology-course/experiment-1.git"


def _ensure_repo(repo: pathlib.Path, repo_url: str) -> int:
    if (repo / ".git").is_dir():
        return 0
    if not repo_url:
        print(f"[autograde-exp1] 错误：{repo} 不是 git 仓库且未提供 --repo-url", file=sys.stderr)
        return 2
    print(f"[autograde-exp1] 克隆 {repo_url} -> {repo}", file=sys.stderr)
    repo.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = run_cmd(["git", "clone", repo_url, str(repo)], cwd=repo.parent, timeout=300)
    if rc != 0:
        print(f"[autograde-exp1] 克隆失败：{err.strip()}", file=sys.stderr)
        return 2
    return 0


def _connect_couchdb(url: str) -> CouchDBClient:
    try:
        client = CouchDBClient.from_env(url)
        client.ensure_database()
        return client
    except (CouchDBConfigError, CouchDBRequestError) as exc:
        print(f"[autograde-exp1] CouchDB 报警：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _should_skip_from_couch(
    client: CouchDBClient,
    branch: str,
    current_hash: str,
) -> Tuple[bool, Dict[str, Any] | None]:
    previous = client.get_branch_state(experiment1.EXPERIMENT_ID, branch)
    if not previous:
        return False, None
    same_hash = previous.get("last_commit_hash") == current_hash
    build_success = previous.get("build_success") is True
    if same_hash and build_success and isinstance(previous.get("item"), dict):
        item = dict(previous["item"])
        item["incremental_status"] = "skipped_unchanged_couchdb"
        return True, item
    return False, previous


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实验1模块化评分与 CouchDB 存储入口")
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument("--repo-dir", default="./project-1/grading-work/experiment-1")
    parser.add_argument("--worktree-dir", default="./project-1/grading-work/worktrees-exp1")
    parser.add_argument("--figures-dir", default="./grading-1/figures")
    parser.add_argument("--output", default="./project-1/grading-experiment1.json")
    parser.add_argument("--branches-pattern", default="*")
    parser.add_argument("--only-branch", default=None)
    parser.add_argument("--roster", required=True, help="花名册路径；必须提供，用于脱敏自检")
    parser.add_argument("--no-build", dest="do_build", action="store_false")
    parser.add_argument("--no-fetch", dest="do_fetch", action="store_false")
    parser.add_argument("--build-timeout", type=int, default=15 * 60)
    parser.add_argument("--couchdb-url", default=DEFAULT_COUCHDB_URL)
    parser.set_defaults(do_build=True, do_fetch=True)
    args = parser.parse_args(argv)

    roster_path = pathlib.Path(args.roster).resolve()
    if not roster_path.is_file():
        print(f"[autograde-exp1] 错误：必须提供有效 --roster，未找到：{roster_path}", file=sys.stderr)
        return 2
    roster = load_roster(roster_path)
    if not roster:
        print(f"[autograde-exp1] 错误：--roster 文件为空或无法解析：{roster_path}", file=sys.stderr)
        return 2
    print(f"[autograde-exp1] 花名册：{len(roster)} 人", file=sys.stderr)

    client = _connect_couchdb(args.couchdb_url)
    repo = pathlib.Path(args.repo_dir).resolve()
    wt_root = pathlib.Path(args.worktree_dir).resolve()
    figures_root = pathlib.Path(args.figures_dir).resolve()
    out_path = pathlib.Path(args.output).resolve()
    wt_root.mkdir(parents=True, exist_ok=True)
    figures_root.mkdir(parents=True, exist_ok=True)

    rc = _ensure_repo(repo, args.repo_url)
    if rc:
        return rc
    if args.do_fetch:
        rc, _, err = git(repo, "fetch", "--all", "--prune", timeout=120)
        if rc != 0:
            print(f"[autograde-exp1] 警告：fetch 失败：{err.strip()}", file=sys.stderr)

    branches = list_group_branches(repo, args.branches_pattern)
    if args.only_branch:
        branches = [b for b in branches if b == args.only_branch]
    print(f"[autograde-exp1] 共 {len(branches)} 条分支", file=sys.stderr)

    items: List[Dict[str, Any]] = []
    last_commit_index: Dict[str, str] = {}
    for branch in branches:
        tip = get_branch_tip_sha(repo, branch) or ""
        if tip:
            last_commit_index[branch] = tip
        skip, previous_item = _should_skip_from_couch(client, branch, tip)
        if skip and previous_item:
            print(f"[autograde-exp1] 跳过 {branch}：hash 未变且上次构建成功", file=sys.stderr)
            items.append(previous_item)
            continue

        print(f"[autograde-exp1] ▶ {branch}", file=sys.stderr)
        masker = PrivacyMasker()
        if roster:
            masker.register_roster(roster)
        wt_path = wt_root / safe_name(branch)
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
        ok, note = extract_branch(repo, branch, wt_path)
        if not ok:
            items.append({"branch": branch, "extract_status": "failed", "extract_note": note})
            continue
        try:
            build = run_mvn_build(wt_path, timeout=args.build_timeout) if args.do_build else {
                "status": "skipped",
                "build_ok": None,
            }
            scored = experiment1.score_branch(branch, repo, wt_path, masker, build, figures_root)
            scored["extract_status"] = "ok"
            if tip:
                scored["last_graded_commit"] = tip
            priv = wt_root / f"_privacy_{safe_name(branch)}.json"
            priv.write_text(json.dumps(masker.export(), ensure_ascii=False, indent=2), encoding="utf-8")
            scored["privacy_map_local"] = str(priv)
            items.append(scored)
            summary = scored["summary"]
            print(
                f"[autograde-exp1]   ✓ 强客观 {summary['auto_total']}/{summary['objective_total_max']}，"
                f"主观待评上限 {summary['ai_pending_total_max']}，"
                f"当前最高可得 {summary['max_possible_after_objective']}/100",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            items.append({"branch": branch, "extract_status": "scoring_failed", "error": f"{type(exc).__name__}: {exc}"})
            print(f"[autograde-exp1]   ✗ 评分异常：{exc}", file=sys.stderr)
        finally:
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    payload = {
        "generated_at": now,
        "last_updated_at": now,
        "rubric": experiment1.RUBRIC_ID,
        "rubric_definition": experiment1.RUBRIC,
        "repo": str(repo),
        "branches_pattern": args.branches_pattern,
        "last_commit_index": last_commit_index,
        "items": items,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if roster:
        leaks = leak_check(text, roster)
        if leaks:
            print(f"[autograde-exp1] 脱敏报警：仍有花名册原值 {leaks[:5]}", file=sys.stderr)
        else:
            print("[autograde-exp1] 脱敏自检通过", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    client.save_payload(experiment1.EXPERIMENT_ID, payload)
    print(f"[autograde-exp1] 已写入 JSON：{out_path}", file=sys.stderr)
    print(f"[autograde-exp1] 已存入 CouchDB：{client.target.base_url}/{client.target.database}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
