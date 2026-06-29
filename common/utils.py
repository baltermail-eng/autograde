from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote


BASELINE_BRANCHES = {"main", "master", "develop", "dev", "trunk"}
SKIP_DIRS = {"target", "build", ".idea", "node_modules", ".mvn", ".git", "__pycache__"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"}
IMAGE_REF_RE = re.compile(r"!\[[^\]]*]\(([^)]+)\)")
HTML_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
DATA_IMAGE_RE = re.compile(
    r"^data:image/(?P<ext>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
    re.DOTALL,
)
SEMANTIC_PREFIX_RE = re.compile(
    r"^(feat|fix|test|docs|refactor|exp\d+|ch\d+|ci|chore|style|perf|build|init)"
    r"(\([^)]*\))?[!:]",
    re.IGNORECASE,
)
EXCLUDED_AUTHOR_RE = [
    re.compile(r"^liuben$", re.IGNORECASE),
    re.compile(r"^teacher$", re.IGNORECASE),
    re.compile(r"^course[-_]?admin$", re.IGNORECASE),
]
EXCLUDED_EMAIL_RE = [re.compile(r"^liuben(@|$)", re.IGNORECASE)]


def run_cmd(cmd: List[str], cwd: Optional[pathlib.Path] = None, timeout: int = 60) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"{type(exc).__name__}: {exc}"


def git(repo: pathlib.Path, *args: str, timeout: int = 60) -> Tuple[int, str, str]:
    return run_cmd(["git", *args], cwd=repo, timeout=timeout)


def truncate(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    return text[: max_chars - 80] + f"\n\n...[truncated, original {len(text)} chars]..."


def tail(text: str, max_chars: int = 4000) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    return text[-max_chars:]


def read_safe(path: pathlib.Path, max_chars: int = 20_000) -> Optional[str]:
    if not path.is_file():
        return None
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return truncate(path.read_text(encoding=enc, errors="replace"), max_chars)
        except Exception:
            continue
    return None


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "unknown")
    return cleaned.strip("._") or "unknown"


def mask_deep(obj: Any, masker: "PrivacyMasker") -> Any:
    if isinstance(obj, dict):
        return {k: mask_deep(v, masker) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_deep(v, masker) for v in obj]
    if isinstance(obj, str):
        return masker.mask(obj)
    return obj


class PrivacyMasker:
    student_id_re = re.compile(r"(?<!\d)\d{8,12}(?!\d)")
    email_re = re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b")

    def __init__(self) -> None:
        self.maps: Dict[str, Dict[str, str]] = {
            "name": {},
            "id": {},
            "email": {},
            "gitee": {},
        }
        self.counters: Dict[str, int] = {k: 0 for k in self.maps}

    def register_roster(self, roster: List[Tuple[str, str]]) -> None:
        for idx, (sid, name) in enumerate(roster, start=1):
            sid = unicodedata.normalize("NFC", (sid or "").strip())
            name = unicodedata.normalize("NFC", (name or "").strip())
            if sid:
                self.maps["id"][sid] = f"ID_{idx:03d}"
            if name:
                self.maps["name"][name] = f"STUDENT_{idx:03d}"
        for key in ("id", "name"):
            self.counters[key] = max(self.counters[key], len(roster))

    def register_email(self, email: str) -> str:
        value = (email or "").strip().lower()
        return self.lookup("email", value) if value else ""

    def register_gitee(self, username: str) -> Optional[str]:
        value = (username or "").strip()
        deny = {"root", "admin", "user", "test", "ci", "bot", "github-actions", "noreply"}
        if len(value) < 3 or value.lower() in deny:
            return None
        return self.lookup("gitee", value)

    def lookup(self, kind: str, value: str) -> str:
        if value not in self.maps[kind]:
            self.counters[kind] += 1
            prefix = {"name": "STUDENT", "id": "ID", "email": "EMAIL", "gitee": "GITEE"}[kind]
            self.maps[kind][value] = f"{prefix}_{self.counters[kind]:03d}"
        return self.maps[kind][value]

    def mask(self, text: str) -> str:
        if not text:
            return text
        text = unicodedata.normalize("NFC", text)
        text = self.email_re.sub(lambda m: self.lookup("email", m.group(0).lower()), text)
        text = re.sub(
            r"(gitee\.com/)([A-Za-z0-9_.\-]+)",
            lambda m: m.group(1) + self.lookup("gitee", m.group(2)),
            text,
        )
        for user in sorted(self.maps["gitee"], key=len, reverse=True):
            if len(user) >= 3:
                text = re.sub(re.escape(user), self.maps["gitee"][user], text)
        for name in sorted(self.maps["name"], key=len, reverse=True):
            if len(name) >= 2:
                text = text.replace(name, self.maps["name"][name])
        text = self.student_id_re.sub(
            lambda m: self.maps["id"].get(m.group(0), self.lookup("id", m.group(0))),
            text,
        )
        return text

    def export(self) -> Dict[str, Dict[str, str]]:
        return {k: dict(v) for k, v in self.maps.items()}


def load_roster(path: pathlib.Path) -> List[Tuple[str, str]]:
    result: List[Tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = unicodedata.normalize("NFC", raw.strip())
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        result.append((parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))
    return result


def list_group_branches(repo: pathlib.Path, pattern: str = "*") -> List[str]:
    import fnmatch

    rc, out, _ = git(repo, "branch", "-r", "--format=%(refname:short)", timeout=30)
    if rc != 0:
        return []
    branches: List[str] = []
    for line in out.splitlines():
        ref = line.strip()
        if not ref:
            continue
        name = ref.removeprefix("origin/")
        if name in BASELINE_BRANCHES or name.startswith("HEAD"):
            continue
        if fnmatch.fnmatch(name, pattern):
            branches.append(name)
    return sorted(branches)


def get_branch_tip_sha(repo: pathlib.Path, branch: str) -> Optional[str]:
    rc, out, _ = git(repo, "rev-parse", f"refs/remotes/origin/{branch}", timeout=30)
    if rc != 0:
        rc, out, _ = git(repo, "rev-parse", f"origin/{branch}", timeout=30)
    sha = out.strip()
    return sha if rc == 0 and sha else None


def extract_branch(repo: pathlib.Path, branch: str, dest: pathlib.Path) -> Tuple[bool, str]:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        archive = subprocess.Popen(
            ["git", "archive", f"refs/remotes/origin/{branch}"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tar = subprocess.Popen(
            ["tar", "-x", "-C", str(dest)],
            stdin=archive.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert archive.stdout is not None
        archive.stdout.close()
        _, tar_err = tar.communicate(timeout=60)
        archive.wait(timeout=10)
        if tar.returncode != 0:
            return False, tar_err.decode("utf-8", errors="replace")
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


ALIYUN_SETTINGS = """\
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror>
      <id>aliyun</id>
      <url>https://maven.aliyun.com/repository/public</url>
      <mirrorOf>central</mirrorOf>
    </mirror>
  </mirrors>
</settings>
"""


def find_pom(root: pathlib.Path) -> Optional[pathlib.Path]:
    candidates = [
        p for p in root.rglob("pom.xml")
        if not any(part in {".git", "target"} for part in p.parts)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
    return candidates[0]


def run_mvn_build(work_dir: pathlib.Path, timeout: int = 15 * 60) -> Dict[str, Any]:
    pom = find_pom(work_dir)
    if not pom:
        return {"status": "no_pom_found", "build_ok": None}
    mvn = shutil.which("mvn")
    if not mvn:
        return {"status": "skipped", "reason": "mvn not in PATH", "build_ok": None}
    settings = work_dir / "_maven_mirror.xml"
    settings.write_text(ALIYUN_SETTINGS, encoding="utf-8")
    cmd = [mvn, "-B", "-ntp", "-q", "-s", str(settings), "-f", str(pom), "test"]
    t0 = time.perf_counter()
    rc, out, err = run_cmd(cmd, cwd=work_dir, timeout=timeout)
    return {
        "status": "success" if rc == 0 else "failed",
        "build_ok": rc == 0,
        "exit_code": rc,
        "duration_seconds": round(time.perf_counter() - t0, 2),
        "pom_path": str(pom.relative_to(work_dir)),
        "stdout_tail": tail(out),
        "stderr_tail": tail(err),
    }


def strip_md_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return raw
    if raw.startswith("<") and ">" in raw:
        return raw[1: raw.index(">")]
    return raw.split(" ", 1)[0] if " " in raw else raw


def is_relative_to(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_image_ref(ref: str, doc_path: pathlib.Path, work_dir: pathlib.Path) -> Optional[pathlib.Path]:
    target = unquote(strip_md_url(ref)).split("#", 1)[0].split("?", 1)[0]
    if not target or target.startswith(("http://", "https://", "data:")):
        return None
    candidate = pathlib.Path(target)
    if candidate.is_absolute():
        return candidate
    first = (doc_path.parent / candidate).resolve()
    if first.exists():
        return first
    second = (work_dir / candidate).resolve()
    if second.exists():
        return second
    return first


def export_doc_figures(
    work_dir: pathlib.Path,
    docs_dir: pathlib.Path,
    markdown_files: List[pathlib.Path],
    figures_dir: Optional[pathlib.Path],
) -> List[Dict[str, Any]]:
    if not figures_dir or not docs_dir.is_dir():
        return []
    exports: List[Dict[str, Any]] = []
    seen: set[str] = set()
    figures_dir.mkdir(parents=True, exist_ok=True)

    def add_local(src: pathlib.Path, doc_path: Optional[pathlib.Path], original_ref: str, kind: str) -> None:
        if not src.is_file() or src.suffix.lower() not in IMAGE_EXTS:
            return
        resolved = str(src.resolve())
        if resolved in seen:
            return
        seen.add(resolved)
        digest = hashlib.sha1(resolved.encode("utf-8", errors="ignore")).hexdigest()[:10]
        out_path = figures_dir / f"{digest}_{safe_name(src.name)}"
        shutil.copy2(src, out_path)
        exports.append({
            "original_ref": original_ref,
            "source_kind": kind,
            "source_path": src.relative_to(work_dir).as_posix() if is_relative_to(src, work_dir) else str(src),
            "source_doc": doc_path.relative_to(work_dir).as_posix()
            if doc_path and is_relative_to(doc_path, work_dir) else (str(doc_path) if doc_path else ""),
            "exported_path": str(out_path),
        })

    def add_data_uri(ref: str, doc_path: pathlib.Path, index: int) -> None:
        m = DATA_IMAGE_RE.match(ref.strip())
        if not m:
            return
        ext = m.group("ext").lower().replace("svg+xml", "svg")
        ext = "jpg" if ext == "jpeg" else ext
        suffix = f".{ext}"
        if suffix not in IMAGE_EXTS:
            return
        try:
            raw = base64.b64decode(m.group("data"), validate=False)
        except Exception:
            return
        digest = hashlib.sha1(raw).hexdigest()[:10]
        if digest in seen:
            return
        seen.add(digest)
        out_path = figures_dir / f"{safe_name(doc_path.stem)}_{index}_{digest}{suffix}"
        out_path.write_bytes(raw)
        exports.append({
            "original_ref": "data:image/...;base64",
            "source_kind": "embedded_data_uri",
            "source_path": "",
            "source_doc": doc_path.relative_to(work_dir).as_posix() if is_relative_to(doc_path, work_dir) else doc_path.name,
            "exported_path": str(out_path),
        })

    for md_path in markdown_files:
        text = read_safe(md_path, max_chars=200_000) or ""
        refs = IMAGE_REF_RE.findall(text) + HTML_IMG_RE.findall(text)
        for idx, raw_ref in enumerate(refs, start=1):
            ref = strip_md_url(raw_ref)
            if ref.startswith("data:image/"):
                add_data_uri(ref, md_path, idx)
                continue
            src = resolve_image_ref(ref, md_path, work_dir)
            if src:
                add_local(src, md_path, ref, "markdown_reference")
    for img in sorted(docs_dir.rglob("*")):
        if img.is_file() and img.suffix.lower() in IMAGE_EXTS:
            add_local(img, None, img.name, "docs_image_file")
    return exports


def collect_docs(
    work_dir: pathlib.Path,
    masker: PrivacyMasker,
    figures_dir: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    docs_dir = work_dir / "docs"
    result: Dict[str, Any] = {"step_docs": [], "ai_logs": [], "figures": []}
    if not docs_dir.is_dir():
        return result
    markdown_files = sorted(docs_dir.rglob("*.md"))
    result["figures"] = export_doc_figures(work_dir, docs_dir, markdown_files, figures_dir)
    for path in markdown_files:
        text = read_safe(path, max_chars=20_000)
        if text is None:
            continue
        entry = {
            "filename": path.name,
            "char_count": len(text),
            "full_text": masker.mask(text),
        }
        name = path.name.lower()
        if "step" in name or "git-setup" in name or "setup" in name:
            result["step_docs"].append(entry)
        elif "ai-log" in name or "ai_log" in name:
            result["ai_logs"].append(entry)
    return result


def _is_excluded_author(name: str, email: str) -> bool:
    loc = email.split("@", 1)[0] if "@" in email else email
    return (
        any(p.match(name or "") for p in EXCLUDED_AUTHOR_RE)
        or any(p.match(loc or "") for p in EXCLUDED_AUTHOR_RE)
        or any(p.search(email or "") for p in EXCLUDED_EMAIL_RE)
    )


def _week_key(date_str: str) -> str:
    try:
        d = _dt.date.fromisoformat(date_str[:10])
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except (TypeError, ValueError):
        return "unknown"


def collect_commits(repo: pathlib.Path, branch: str, masker: PrivacyMasker) -> Dict[str, Any]:
    rc, out, _ = git(
        repo,
        "log",
        f"refs/remotes/origin/{branch}",
        "--format=%H%x09%aN%x09%aE%x09%ad%x09%s",
        "--date=short",
        timeout=30,
    )
    rc2, out2, _ = git(
        repo,
        "log",
        f"main..refs/remotes/origin/{branch}",
        "--format=%H",
        "--no-merges",
        timeout=30,
    )
    if rc2 != 0:
        rc2, out2, _ = git(
            repo,
            "log",
            f"master..refs/remotes/origin/{branch}",
            "--format=%H",
            "--no-merges",
            timeout=30,
        )
    diverged_hashes = {h.strip() for h in out2.splitlines() if h.strip()} if rc2 == 0 else set()

    rows: List[Dict[str, Any]] = []
    authors: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    dates: List[str] = []
    excluded_count = 0
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t", 4)
            if len(parts) < 5:
                continue
            sha, author_name, author_email, author_date, subject = parts
            if _is_excluded_author(author_name, author_email):
                excluded_count += 1
                continue
            row = {
                "hash": sha[:8],
                "date": author_date,
                "author": masker.mask(author_name),
                "email": masker.register_email(author_email),
                "subject": masker.mask(subject),
                "in_diverged": sha in diverged_hashes,
                "semantic": bool(SEMANTIC_PREFIX_RE.match(subject)),
                "insertions": None,
                "deletions": None,
                "files_changed": None,
            }
            rows.append(row)
            dates.append(author_date)
            authors.setdefault((author_name.strip(), author_email.strip().lower()), []).append(row)
            loc = author_email.split("@", 1)[0] if "@" in author_email else ""
            if re.fullmatch(r"[A-Za-z0-9_.\-]{3,30}", loc):
                masker.register_gitee(loc)
            if author_name and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-\s@]{1,40}", author_name):
                masker.register_gitee(author_name.strip())

    rc3, out3, _ = git(
        repo,
        "log",
        f"main..refs/remotes/origin/{branch}",
        "--no-merges",
        "--format=%H",
        "--numstat",
        timeout=60,
    )
    if rc3 != 0:
        rc3, out3, _ = git(
            repo,
            "log",
            f"master..refs/remotes/origin/{branch}",
            "--no-merges",
            "--format=%H",
            "--numstat",
            timeout=60,
        )
    if rc3 == 0:
        cur, ins, dels, files = "", 0, 0, 0
        diff_map: Dict[str, Dict[str, int]] = {}
        for raw in out3.splitlines():
            line = raw.strip()
            if re.fullmatch(r"[0-9a-f]{40}", line):
                if cur:
                    diff_map[cur[:8]] = {"insertions": ins, "deletions": dels, "files_changed": files}
                cur, ins, dels, files = line, 0, 0, 0
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    ins += int(parts[0]) if parts[0] != "-" else 0
                    dels += int(parts[1]) if parts[1] != "-" else 0
                    files += 1
                except ValueError:
                    pass
        if cur:
            diff_map[cur[:8]] = {"insertions": ins, "deletions": dels, "files_changed": files}
        for row in rows:
            if row["hash"] in diff_map:
                row.update(diff_map[row["hash"]])

    per_author_stats: Dict[str, Any] = {}
    for (name, _email), commit_rows in sorted(authors.items(), key=lambda x: -len(x[1])):
        per_author_stats[masker.mask(name)] = {
            "commit_count": len(commit_rows),
            "commits": mask_deep(commit_rows[:30], masker),
        }
    diverged_rows = [r for r in rows if r.get("in_diverged")]
    semantic_count = sum(1 for r in diverged_rows if r.get("semantic"))
    semantic_ratio = semantic_count / len(diverged_rows) if diverged_rows else 0.0
    weekly: Dict[str, int] = {}
    for date in dates:
        key = _week_key(date)
        weekly[key] = weekly.get(key, 0) + 1
    return {
        "commit_count_total": len(rows),
        "commit_count_diverged_from_main": len(diverged_rows),
        "excluded_author_count": excluded_count,
        "distinct_authors": len(authors),
        "unique_commit_days": len(set(d for d in dates if d)),
        "first_commit_date": dates[-1] if dates else "",
        "last_commit_date": dates[0] if dates else "",
        "semantic_ratio": round(semantic_ratio, 3),
        "semantic_count": semantic_count,
        "weekly_histogram": weekly,
        "authors_masked": [
            {"name": masker.mask(name), "email": masker.register_email(email), "commits": len(commits)}
            for (name, email), commits in sorted(authors.items(), key=lambda x: -len(x[1]))
        ][:20],
        "per_author_stats": per_author_stats,
        "recent_commits": mask_deep(rows[:30], masker),
    }


def leak_check(payload_text: str, roster: List[Tuple[str, str]]) -> List[str]:
    leaks: List[str] = []
    for sid, name in roster:
        for value in (sid, name):
            if value and len(value) >= 2 and value in payload_text:
                leaks.append(repr(value))
    return leaks[:20]

