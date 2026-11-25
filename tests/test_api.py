"""Admin routes, job lock and the existing Zync features (regressions)."""
import time
from datetime import datetime, timedelta

import jwt
import pytest

from CTFd.models import Challenges, db
from CTFd.plugins.zync.manager import jobs
from CTFd.plugins.zync.manager.galvanize import GalvanizeError, admin_token
from tests.helpers import login_as_user, register_user


def admin_client(app):
    return login_as_user(app, name="admin", password="password")


def wait_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/admin/zync_challenges/api/jobs/{job_id}").get_json()["data"]
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_routes_are_admin_only(app, galvanize):
    register_user(app)
    client = login_as_user(app)
    for path in ("/admin/zync_challenges", "/admin/zync_challenges/api/overview",
                 "/admin/zync_challenges/api/challenges?repo=Repo", "/admin/zync_challenges/api/jobs"):
        assert client.get(path).status_code in (302, 403), path
    resp = client.post("/admin/zync_challenges/api/jobs", json={"action": "ingest", "repo": "Repo"})
    assert resp.status_code in (302, 403)


def test_page_and_overview(app, galvanize):
    galvanize.repositories = lambda: {"repositories": [{"name": "Repo", "is_git": True, "challenges": 1}],
                                      "clone_sources": ["https://github.com/org/"]}
    client = admin_client(app)
    assert b"Zync Challenges" in client.get("/admin/zync_challenges").data
    data = client.get("/admin/zync_challenges/api/overview").get_json()["data"]
    assert data["repos"][0]["name"] == "Repo"
    assert data["clone_sources"] == ["https://github.com/org/"]
    assert data["orphan_repos"] == []


def test_overview_reports_galvanize_errors(app, galvanize):
    def fail():
        raise GalvanizeError("Cannot reach Galvanize")

    galvanize.repositories = fail
    data = admin_client(app).get("/admin/zync_challenges/api/overview").get_json()["data"]
    assert data["galvanize"]["error"] == "Cannot reach Galvanize"


def test_challenges_listing_merges_ctfd_state(app, galvanize):
    galvanize.add("Repo", "misc", "a", {"name": "A", "category": "Misc", "flags": ["x"]})
    client = admin_client(app)
    rows = client.get("/admin/zync_challenges/api/challenges?repo=Repo").get_json()["data"]["challenges"]
    assert rows[0]["ctfd"] is None and rows[0]["key"] == "Repo/misc/a/challenge.yml"

    resp = client.post("/admin/zync_challenges/api/jobs", json={"action": "ingest", "repo": "Repo"})
    assert resp.status_code == 200, resp.get_json()
    job = wait_job(client, resp.get_json()["data"]["id"])
    assert job["status"] == "success", job["log"]
    rows = client.get("/admin/zync_challenges/api/challenges?repo=Repo").get_json()["data"]["challenges"]
    assert rows[0]["ctfd"]["state"] == "visible"


def test_challenges_listing_relays_files_and_commit(app, galvanize):
    # Regression: the declared files (as Galvanize resolved them) and the
    # commit were not relayed
    galvanize.commits["Repo"] = "abc1234"
    galvanize.add("Repo", "misc", "a", {"name": "A", "category": "Misc", "flags": ["x"], "files": ["a.txt", "b.txt"]},
                  files={"a.txt": "a"})
    rows = admin_client(app).get("/admin/zync_challenges/api/challenges?repo=Repo").get_json()["data"]["challenges"]
    assert rows[0]["commit"] == "abc1234"
    assert rows[0]["files"][0] == {"declared": "a.txt", "path": "Repo/misc/a/a.txt", "size": 1}
    assert rows[0]["files"][1]["declared"] == "b.txt" and "not found" in rows[0]["files"][1]["error"]


def test_unknown_repository(app, galvanize):
    """Regression: Galvanize listed nothing for an unknown repository, so a
    job on it succeeded without doing anything. Galvanize now answers 404."""
    client = admin_client(app)
    resp = client.get("/admin/zync_challenges/api/challenges?repo=Missing")
    assert resp.status_code == 404 and "Repository not found" in resp.get_json()["error"]
    for action in ("ingest", "sync", "all", "delete"):
        resp = client.post("/admin/zync_challenges/api/jobs", json={"action": action, "repo": "Missing"})
        job = wait_job(client, resp.get_json()["data"]["id"])
        assert job["status"] == "failed", (action, job["log"])
        assert "Repository 'Missing' not found in Galvanize" in job["log"]
        assert "Unexpected error" not in job["log"]


def test_job_validation(app, galvanize):
    client = admin_client(app)
    assert client.post("/admin/zync_challenges/api/jobs", json={"action": "explode", "repo": "Repo"}).status_code == 400
    assert client.post("/admin/zync_challenges/api/jobs", json={"action": "ingest"}).status_code == 400
    assert client.post("/admin/zync_challenges/api/jobs",
                       json={"action": "ingest", "repo": "Repo", "keys": "all"}).status_code == 400


def test_job_lock_is_exclusive_and_taken_over_when_stale(app):
    with app.app_context():
        job_a = jobs.ZyncJob(action="ingest", repo="R", params="{}", status="running", log="")
        job_b = jobs.ZyncJob(action="sync", repo="R", params="{}", status="queued", log="")
        db.session.add_all([job_a, job_b])
        db.session.commit()
        assert jobs._acquire(job_a.id) == (True, None)
        acquired, holder = jobs._acquire(job_b.id)
        assert not acquired and holder == job_a.id
        assert jobs.active_job().id == job_a.id

        # job_a's worker died: no heartbeat for longer than STALE_AFTER
        with db.engine.begin() as conn:
            conn.execute(jobs._lock.update().values(heartbeat_at=datetime.utcnow() - timedelta(minutes=5)))
        assert jobs.active_job() is None
        assert jobs._acquire(job_b.id) == (True, job_a.id)
        jobs._release(job_b.id)
        assert jobs.active_job() is None


def test_second_job_is_refused_while_one_runs(app, galvanize, monkeypatch):
    import threading

    from CTFd.plugins.zync.manager import actions

    release = threading.Event()

    def slow(log, repo, keys=None, force=False, dry_run=False):
        release.wait(10)
        return {}

    monkeypatch.setitem(actions.FUNCTIONS, "sync", slow)
    client = admin_client(app)
    first = client.post("/admin/zync_challenges/api/jobs", json={"action": "sync", "repo": "Repo"})
    assert first.status_code == 200
    second = client.post("/admin/zync_challenges/api/jobs", json={"action": "sync", "repo": "Repo"})
    assert second.status_code == 409
    release.set()
    assert wait_job(client, first.get_json()["data"]["id"])["status"] == "success"
    jobs_list = client.get("/admin/zync_challenges/api/jobs").get_json()["data"]
    assert "log" not in jobs_list[0], "the job list does not carry logs"


def test_galvanize_relay(app, galvanize):
    calls = []
    galvanize.pull = lambda repo: calls.append(("pull", repo)) or {"id": "op-9", "status": "running"}

    def busy(*a, **k):
        raise GalvanizeError("Galvanize: Operation 1 is still running", 409)

    galvanize.clone = busy
    client = admin_client(app)
    resp = client.post("/admin/zync_challenges/api/galvanize/pull", json={"repo": "Repo"})
    assert resp.status_code == 202 and calls == [("pull", "Repo")]
    resp = client.post("/admin/zync_challenges/api/galvanize/clone", json={"url": "https://github.com/org/x.git"})
    assert resp.status_code == 409
    assert client.post("/admin/zync_challenges/api/galvanize/clone", json={}).status_code == 400
    assert client.post("/admin/zync_challenges/api/galvanize/images", json={"action": "x"}).status_code == 400
    assert client.post("/admin/zync_challenges/api/galvanize/build",
                       json={"challenges": ["bad"]}).status_code == 400
    assert client.post("/admin/zync_challenges/api/galvanize/explode", json={}).status_code == 404


# ── Existing features (regressions) ─────────────────────────────────────────

def test_admin_token():
    token = admin_token("secret", minutes=5, challenge_name="c", category="k")
    claims = jwt.decode(token, "secret", algorithms=["HS256"])
    assert (claims["role"], claims["challenge_name"], claims["category"]) == ("admin", "c", "k")
    assert "scope" not in claims, "no manage scope unless asked"


def test_server_client_uses_the_manage_scope_and_browsers_never_get_it(app, configured, monkeypatch):
    from CTFd.plugins.zync.manager.galvanize import GalvanizeClient

    headers = GalvanizeClient("http://galvanize.test", "s3cret")._headers()
    claims = jwt.decode(headers["Authorization"].split()[1], "s3cret", algorithms=["HS256"])
    assert claims["scope"] == "manage" and claims["role"] == "admin"
    client = admin_client(app)
    data = client.get("/admin/zync_token").get_json()
    assert "scope" not in jwt.decode(data["token"], "s3cret", algorithms=["HS256"])


def test_a_404_that_is_not_galvanizes_answer_is_unreachable(monkeypatch):
    # Behind Traefik, a stopped Galvanize answers 404 "page not found": it
    # must not read as a repository, file or operation Galvanize does not have
    import requests

    from CTFd.plugins.zync.manager import galvanize as module

    class Response:
        def __init__(self, status, body):
            self.status_code, self.body = status, body

        def json(self):
            if self.body is None:
                raise ValueError("not JSON")
            return self.body

        def close(self):
            pass

    answers = {}
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: answers["next"])
    client = module.GalvanizeClient("http://galvanize.test", "s3cret")

    answers["next"] = Response(404, None)
    with pytest.raises(module.GalvanizeError) as e:
        client.challenges("Repo")
    assert e.value.status is None and "Cannot reach Galvanize" in str(e.value)
    with pytest.raises(module.GalvanizeError):
        client.file_checksum("Web/x/file.txt")

    answers["next"] = Response(404, {"message": "Repository not found: Repo"})
    with pytest.raises(module.GalvanizeError) as e:
        client.challenges("Repo")
    assert e.value.status == 404 and "Repository not found" in str(e.value)
    assert client.file_checksum("Web/x/file.txt") is None

    answers["next"] = Response(500, None)
    with pytest.raises(module.GalvanizeError) as e:
        client.challenges("Repo")
    assert e.value.status == 500 and "HTTP 500" in str(e.value)


def test_job_cancel_route(app):
    from CTFd.plugins.zync.manager.jobs import ZyncJob

    with app.app_context():
        db.session.add(ZyncJob(id=41, action="ingest", repo="Repo", params="{}", status="running"))
        db.session.add(ZyncJob(id=42, action="ingest", repo="Repo", params="{}", status="success"))
        db.session.commit()
    client = admin_client(app)
    assert client.post("/admin/zync_challenges/api/jobs/41/cancel", json={}).status_code == 202
    assert client.post("/admin/zync_challenges/api/jobs/42/cancel", json={}).status_code == 409
    assert client.post("/admin/zync_challenges/api/jobs/43/cancel", json={}).status_code == 404
    with app.app_context():
        assert ZyncJob.query.filter_by(id=41).one().status == "cancelling"


@pytest.fixture
def configured(app):
    from CTFd.plugins.zync.views.admin_config import ZyncConfig

    with app.app_context():
        db.session.add(ZyncConfig(id=1, deployer_url="http://galvanize.test", jwt_secret="s3cret"))
        db.session.add(Challenges(name="web1", category="Web", value=10, type="zync", state="visible"))
        db.session.commit()


def test_dashboard_tokens_still_work(app, configured):
    client = admin_client(app)
    data = client.get("/admin/zync_token").get_json()
    assert data["api_url"] == "http://galvanize.test"
    assert jwt.decode(data["token"], "s3cret", algorithms=["HS256"])["role"] == "admin"
    data = client.post("/admin/zync_status_token", json={"category": "Web", "challenge_name": "web1"}).get_json()
    assert jwt.decode(data["token"], "s3cret", algorithms=["HS256"])["challenge_name"] == "web1"


def test_player_deploy_token_still_works(app, configured):
    register_user(app)
    client = login_as_user(app)
    with app.app_context():
        chal_id = Challenges.query.filter_by(name="web1").one().id
    resp = client.post("/api/v1/deploy/token", json={"challenge_id": chal_id})
    claims = jwt.decode(resp.get_json()["token"], "s3cret", algorithms=["HS256"])
    assert (claims["role"], claims["challenge_name"], claims["category"]) == ("user", "web1", "Web")
    assert client.get("/api/v1/deploy/url").get_json()["deployer_url"] == "http://galvanize.test"


def test_zync_challenge_type_still_registered(app):
    from CTFd.plugins.challenges import CHALLENGE_CLASSES

    assert "zync" in CHALLENGE_CLASSES
