from __future__ import annotations

import base64
import datetime as _dt
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


DEFAULT_COUCHDB_URL = "http://127.0.0.1:5984/_utils/#database/javaee-2026/"


class CouchDBConfigError(RuntimeError):
    pass


class CouchDBRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class CouchDBTarget:
    base_url: str
    database: str


def parse_couchdb_url(url: str = DEFAULT_COUCHDB_URL) -> CouchDBTarget:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise CouchDBConfigError(f"无效 CouchDB URL：{url}")
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    database = ""
    if "/_utils/" in parsed.path and parsed.fragment.startswith("database/"):
        database = parsed.fragment.split("/", 1)[1].strip("/")
    elif parsed.path and parsed.path != "/":
        database = parsed.path.strip("/").split("/", 1)[0]
    if not database:
        raise CouchDBConfigError(f"无法从 URL 解析数据库名：{url}")
    return CouchDBTarget(base_url=base_url.rstrip("/"), database=database)


def credentials_from_env() -> Tuple[str, str]:
    user = os.environ.get("COUCHDBUSER")
    password = os.environ.get("COUCHDBPASSWORD")
    missing = [name for name, value in (("COUCHDBUSER", user), ("COUCHDBPASSWORD", password)) if not value]
    if missing:
        raise CouchDBConfigError(
            "未检测到 CouchDB 账号密码环境变量："
            + ", ".join(missing)
            + "。请先设置 COUCHDBUSER 和 COUCHDBPASSWORD。"
        )
    return user or "", password or ""


class CouchDBClient:
    def __init__(self, target: CouchDBTarget, username: str, password: str, timeout: int = 20) -> None:
        self.target = target
        self.username = username
        self.password = password
        self.timeout = timeout

    @classmethod
    def from_env(cls, url: str = DEFAULT_COUCHDB_URL, timeout: int = 20) -> "CouchDBClient":
        target = parse_couchdb_url(url)
        username, password = credentials_from_env()
        return cls(target, username, password, timeout=timeout)

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        auth = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        req = Request(
            self.target.base_url + path,
            data=raw,
            method=method,
            headers={
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return json.loads(text) if text else None
        except HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                return {"error": "not_found", "status": 404}
            raise CouchDBRequestError(f"CouchDB {method} {path} 失败：HTTP {exc.code} {text}") from exc
        except URLError as exc:
            raise CouchDBRequestError(f"无法连接 CouchDB：{exc}") from exc

    def db_path(self, suffix: str = "") -> str:
        db = quote(self.target.database, safe="")
        return f"/{db}{suffix}"

    def ensure_database(self) -> None:
        head = self._request("GET", self.db_path())
        if isinstance(head, dict) and head.get("error") == "not_found":
            self._request("PUT", self.db_path())

    def get_doc(self, doc_id: str) -> Optional[Dict[str, Any]]:
        result = self._request("GET", self.db_path("/" + quote(doc_id, safe="")))
        if isinstance(result, dict) and result.get("error") == "not_found":
            return None
        return result

    def put_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        doc_id = str(doc["_id"])
        existing = self.get_doc(doc_id)
        if existing and "_rev" in existing:
            doc = dict(doc)
            doc["_rev"] = existing["_rev"]
        return self._request("PUT", self.db_path("/" + quote(doc_id, safe="")), doc)

    def branch_doc_id(self, experiment_id: str, branch: str) -> str:
        return f"autograde:{experiment_id}:group:{quote(branch, safe='')}"

    def latest_doc_id(self, experiment_id: str) -> str:
        return f"autograde:{experiment_id}:latest"

    def run_doc_id(self, experiment_id: str, generated_at: str) -> str:
        safe = generated_at.replace(":", "").replace("+", "Z")
        return f"autograde:{experiment_id}:run:{safe}"

    def get_branch_state(self, experiment_id: str, branch: str) -> Optional[Dict[str, Any]]:
        doc = self.get_doc(self.branch_doc_id(experiment_id, branch))
        return doc if doc else None

    def save_payload(self, experiment_id: str, payload: Dict[str, Any]) -> None:
        self.ensure_database()
        now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        index: Dict[str, Any] = {}
        for item in payload.get("items", []):
            branch = item.get("branch")
            if not branch:
                continue
            build_success = item.get("build", {}).get("build_ok") is True
            last_hash = item.get("last_graded_commit", "")
            doc_id = self.branch_doc_id(experiment_id, branch)
            doc = {
                "_id": doc_id,
                "type": "autograde_group_result",
                "experiment_id": experiment_id,
                "branch": branch,
                "last_commit_hash": last_hash,
                "build_success": build_success,
                "updated_at": now,
                "item": item,
            }
            self.put_doc(doc)
            index[branch] = {
                "doc_id": doc_id,
                "last_commit_hash": last_hash,
                "build_success": build_success,
                "updated_at": now,
            }
        latest = {
            "_id": self.latest_doc_id(experiment_id),
            "type": "autograde_latest",
            "experiment_id": experiment_id,
            "updated_at": now,
            "generated_at": payload.get("generated_at"),
            "last_updated_at": payload.get("last_updated_at"),
            "rubric": payload.get("rubric"),
            "repo": payload.get("repo"),
            "branches_pattern": payload.get("branches_pattern"),
            "branch_index": index,
        }
        self.put_doc(latest)
        run_doc = dict(payload)
        run_doc.update({
            "_id": self.run_doc_id(experiment_id, payload.get("last_updated_at") or now),
            "type": "autograde_run",
            "experiment_id": experiment_id,
            "stored_at": now,
        })
        self.put_doc(run_doc)

