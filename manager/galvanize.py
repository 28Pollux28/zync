"""Server-side client of the Galvanize admin API.

Galvanize owns the challenge repositories: it clones and updates them, reads
the challenge definitions and builds the images on its Ansible hosts. Zync
only reads definitions and files from it to manage the challenges in CTFd.
"""
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone

import jwt
import requests

DOWNLOAD_CHUNK = 1 << 20


class GalvanizeError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# Scope of the tokens Zync's server uses for Galvanize's repository, build,
# image and file endpoints. Tokens handed to browsers (the dashboard's) never
# carry it: they manage instances, nothing more.
MANAGE_SCOPE = "manage"


def admin_token(secret: str, minutes: int = 10, challenge_name: str = "", category: str = "", scope: str = None) -> str:
    """Admin JWT for Galvanize (shared with the dashboard and config check)."""
    now = datetime.now(tz=timezone.utc)
    claims = {
        "exp": now + timedelta(minutes=minutes),
        "iat": now,
        "user_id": "0",
        "team_id": "0",
        "role": "admin",
        "challenge_name": challenge_name,
        "category": category,
    }
    if scope:
        claims["scope"] = scope
    return jwt.encode(claims, secret, algorithm="HS256")


class GalvanizeClient:
    def __init__(self, url: str, secret: str, timeout: float = 30):
        self.url = url.rstrip("/")
        self.secret = secret
        self.timeout = timeout

    @classmethod
    def from_config(cls):
        from ..views.admin_config import ZyncConfig

        config = ZyncConfig.query.filter_by(id=1).first()
        if not config or not config.deployer_url or not config.jwt_secret:
            raise GalvanizeError("Galvanize is not configured (Zync Config)")
        return cls(config.deployer_url, config.jwt_secret)

    # ── Plumbing ────────────────────────────────────────────────────────────

    def _headers(self):
        # Short-lived and server-side only: the only tokens with the manage scope
        return {"Authorization": f"Bearer {admin_token(self.secret, minutes=5, scope=MANAGE_SCOPE)}"}

    def _request(self, method, path, *, params=None, json=None, stream=False, timeout=None):
        try:
            resp = requests.request(method, self.url + path, params=params, json=json, headers=self._headers(),
                                    timeout=timeout or self.timeout, stream=stream)
        except requests.RequestException as e:
            raise GalvanizeError(f"Cannot reach Galvanize at {self.url}: {e}")
        if resp.status_code >= 400:
            message = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    message = body.get("message")
            except ValueError:
                pass
            resp.close()
            if resp.status_code == 404 and not message:
                # Not Galvanize's answer: the proxy in front of a stopped
                # Galvanize answers 404, which would read as a missing
                # repository, file or operation
                raise GalvanizeError(f"Cannot reach Galvanize at {self.url}: HTTP 404 without Galvanize's answer")
            raise GalvanizeError(f"Galvanize: {message or f'HTTP {resp.status_code}'}", resp.status_code)
        return resp

    def _json(self, method, path, **kwargs):
        resp = self._request(method, path, **kwargs)
        if resp.status_code in (202, 204) and not resp.content:
            return None
        return resp.json()

    # ── Repositories and definitions ────────────────────────────────────────

    def repositories(self) -> dict:
        return self._json("GET", "/admin/repos")

    def challenges(self, repo=None) -> dict:
        return self._json("GET", "/admin/challenges", params={"repo": repo} if repo is not None else None)

    def file_checksum(self, path: str):
        """{"sha1", "size"} of a challenge file, or None when it does not exist."""
        try:
            return self._json("GET", "/admin/challenges/file", params={"path": path, "checksum": "true"})
        except GalvanizeError as e:
            if e.status == 404:
                return None
            raise

    def download(self, path: str):
        """Stream a challenge file to a temporary file. Returns (file, sha1),
        the file positioned at its start, or None when it does not exist."""
        try:
            resp = self._request("GET", "/admin/challenges/file", params={"path": path}, stream=True, timeout=300)
        except GalvanizeError as e:
            if e.status == 404:
                return None
            raise
        tmp = tempfile.TemporaryFile()
        digest = hashlib.sha1()
        try:
            for chunk in resp.iter_content(DOWNLOAD_CHUNK):
                tmp.write(chunk)
                digest.update(chunk)
        except Exception:
            tmp.close()
            raise
        finally:
            resp.close()
        tmp.seek(0)
        return tmp, digest.hexdigest()

    # ── Operations (clone, pull, remove, build, images) ─────────────────────

    def clone(self, url, name=None, ref=None, force=False):
        body = {"url": url, "force": bool(force)}
        if name:
            body["name"] = name
        if ref:
            body["ref"] = ref
        return self._json("POST", "/admin/repos", json=body)

    def pull(self, repo):
        return self._json("POST", f"/admin/repos/{requests.utils.quote(repo, safe='')}/pull")

    def remove(self, repo):
        return self._json("DELETE", f"/admin/repos/{requests.utils.quote(repo, safe='')}")

    def build(self, repo=None, challenges=None, no_cache=False):
        body = {"no_cache": bool(no_cache)}
        if repo:
            body["repo"] = repo
        if challenges is not None:
            body["challenges"] = challenges
        return self._json("POST", "/admin/build", json=body)

    def images(self, action, repo=None, challenges=None):
        body = {"action": action}
        if repo:
            body["repo"] = repo
        if challenges is not None:
            body["challenges"] = challenges
        return self._json("POST", "/admin/images", json=body)

    def operations(self):
        return self._json("GET", "/admin/operations")

    def operation(self, op_id, offset=0):
        return self._json("GET", f"/admin/operations/{requests.utils.quote(op_id, safe='')}", params={"offset": offset})

    def cancel(self, op_id):
        return self._json("POST", f"/admin/operations/{requests.utils.quote(op_id, safe='')}/cancel")
