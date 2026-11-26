"""Test fixtures: a real CTFd app (CTFd's own test helpers) with Zync loaded,
and a fake Galvanize serving challenge definitions and files.

Run inside the CTFd image, from /opt/CTFd (see tests/run.sh).
"""
import hashlib
import io
import posixpath
from copy import deepcopy

import pytest

from tests.helpers import create_ctfd, destroy_ctfd

# CTFd's modules can only be imported inside an application: create one, as
# CTFd does at startup, so that the plugin (and the test modules importing
# it) are loaded.
destroy_ctfd(create_ctfd(enable_plugins=True))


class Log:
    """Job logger stand-in, keeping every line."""

    def __init__(self):
        self.lines = []

    def _add(self, level, msg):
        self.lines.append((level, str(msg)))

    def debug(self, m): self._add("DEBUG", m)
    def info(self, m): self._add("INFO", m)
    def success(self, m): self._add("SUCCESS", m)
    def warning(self, m): self._add("WARNING", m)
    def error(self, m): self._add("ERROR", m)

    def text(self, level=None):
        return "\n".join(m for lv, m in self.lines if level is None or lv == level)


LFS_POINTER = b"version https://git-lfs.github.com/spec/"


class FakeGalvanize:
    """In-memory Galvanize admin API: definitions, files and operations.

    It resolves declared files and classifies builds as Galvanize does."""

    url = "http://galvanize.test"

    def __init__(self):
        self.definitions = []  # API items of /admin/challenges
        self.files = {}  # path relative to challenge_dir -> bytes
        self.skipped = []
        self.downloads = []
        self.checksums = []
        self.builds = []
        self.build_result = None  # {"built": [...], "failed": [...], "skipped": [...]}: paths
        self.commits = {}  # repository -> commit; a list is consumed one commit per listing
        self.repos = set()  # repositories without definitions (those with some are known)
        self.cancelled = []
        self.unreachable = False  # Galvanize does not answer

    def add(self, repo, category_dir, folder, definition, files=None, deployable=None, build=None):
        d = f"{repo}/{category_dir}/{folder}"
        deployable = definition.get("type") == "zync" if deployable is None else deployable
        build = dict(build or {"kind": "none", "images": []})
        build.setdefault("buildable", deployable and build["kind"] != "none")
        item = {
            "path": f"{d}/challenge.yml", "dir": d, "repository": repo,
            "category": definition["category"], "name": definition["name"], "type": definition.get("type", "standard"),
            "deployable": deployable, "build": build, "definition": deepcopy(definition),
        }
        self.definitions.append(item)
        for name, content in (files or {}).items():
            self.files[f"{d}/{name}"] = content if isinstance(content, bytes) else content.encode()
        return item

    def definition(self, name):
        return next(i for i in self.definitions if i["name"] == name)["definition"]

    def _resolve(self, d, declared):
        declared = str(declared).replace("\\", "/")
        norm = posixpath.normpath(declared)
        if declared.startswith("/"):
            return {"declared": declared, "error": "absolute file paths are not allowed"}
        if norm == ".." or norm.startswith("../"):
            return {"declared": declared, "error": "the path leaves the challenge folder"}
        if ".git" in norm.split("/"):
            return {"declared": declared, "error": "files inside .git are not allowed"}
        for path in (f"{d}/{norm}", f"{d}/files/{norm}"):
            if path in self.files:
                f = {"declared": declared, "path": path, "size": len(self.files[path])}
                if self.files[path].startswith(LFS_POINTER):
                    f["error"] = "a Git LFS pointer, not the file: Galvanize needs git-lfs to fetch it"
                return f
        return {"declared": declared, "error": "not found (looked in the challenge folder and its files/ subfolder)"}

    def _commit(self, repo):
        commit = self.commits.get(repo)
        if isinstance(commit, list):
            commit = commit.pop(0) if len(commit) > 1 else commit[0]
        return commit

    # API used by Zync
    def repositories(self):
        return {"repositories": [{"name": name} for name in self.commits], "clone_sources": []}

    def operations(self):
        return []

    def cancel(self, op_id):
        self.cancelled.append(op_id)

    def challenges(self, repo=None):
        if self.unreachable:
            from CTFd.plugins.zync.manager.galvanize import GalvanizeError

            raise GalvanizeError("Cannot reach Galvanize at http://galvanize.test: Connection refused")
        # Galvanize answers 404 for a repository it does not have
        if repo and repo not in self.repos | {i["repository"] for i in self.definitions}:
            from CTFd.plugins.zync.manager.galvanize import GalvanizeError

            raise GalvanizeError(f"Galvanize: Repository not found: {repo}", 404)
        items = deepcopy([i for i in self.definitions if repo is None or i["repository"] == repo])
        commits = {}
        for item in items:
            declared = item["definition"].get("files") or []
            item["files"] = [self._resolve(item["dir"], f) for f in declared if f] if isinstance(declared, list) else []
            if item["repository"] not in commits:
                commits[item["repository"]] = self._commit(item["repository"])
            if commits[item["repository"]]:
                item["commit"] = commits[item["repository"]]
        return {"challenges": items, "skipped": list(self.skipped)}

    def file_checksum(self, path):
        self.checksums.append(path)
        if path not in self.files:
            return None
        return {"sha1": hashlib.sha1(self.files[path]).hexdigest(), "size": len(self.files[path])}

    def download(self, path):
        if path not in self.files:
            return None
        self.downloads.append(path)
        return io.BytesIO(self.files[path]), hashlib.sha1(self.files[path]).hexdigest()

    def build(self, repo=None, challenges=None, no_cache=False):
        self.builds.append({"repo": repo, "challenges": challenges, "no_cache": no_cache})
        return {"id": "op-1", "status": "running"}

    def _build_result(self):
        if self.build_result is not None:
            return self.build_result
        result = {"built": [], "failed": [], "skipped": []}
        by_path = {i["path"]: i for i in self.definitions}
        for ref in (self.builds[-1]["challenges"] if self.builds else []):
            b = by_path[ref["path"]]["build"]
            result["failed" if b.get("refused") else "built" if b["kind"] != "none" else "skipped"].append(ref["path"])
        return result

    def operation(self, op_id, offset=0):
        result = self._build_result()
        return {"id": op_id, "status": "warning" if result.get("failed") else "success", "result": result,
                "lines": [{"level": "info", "message": "building"}][offset:], "line_count": 1}


@pytest.fixture
def app():
    app = create_ctfd(enable_plugins=True)
    yield app
    destroy_ctfd(app)


@pytest.fixture
def galvanize(monkeypatch):
    from CTFd.plugins.zync.manager import galvanize as module

    fake = FakeGalvanize()
    monkeypatch.setattr(module.GalvanizeClient, "from_config", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def log():
    return Log()
