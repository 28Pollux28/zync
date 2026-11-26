#!/usr/bin/env python3
"""End-to-end test of the challenge manager against a running stack
(CTFd + Zync + Galvanize), with a real challenge repository.

    E2E_CTFD=https://ctf.example E2E_ADMIN=admin E2E_PASSWORD=... \
    E2E_PLAYER=player E2E_PLAYER_PASSWORD=... python3 e2e_polypwn.py

It clones the repository through Zync, builds every image on Galvanize's
hosts, rebuilds and ingests ("all": failed builds are not ingested),
syncs, deploys one instance as a player, checks the guards, then deletes
everything it created (and the clone).
Requires: requests. TLS verification is off (self-signed test stacks).
"""
import os
import re
import sys
import time

import requests
import urllib3

urllib3.disable_warnings()

CTFD = os.environ["E2E_CTFD"].rstrip("/")
ADMIN = os.environ.get("E2E_ADMIN", "admin")
PASSWORD = os.environ["E2E_PASSWORD"]
PLAYER = os.environ.get("E2E_PLAYER")
PLAYER_PASSWORD = os.environ.get("E2E_PLAYER_PASSWORD")
REPO_URL = os.environ.get("E2E_REPO_URL", "https://github.com/polycyber/PolyPwnCTF-2026-Challenges.git")
REPO = os.environ.get("E2E_REPO_NAME", "PolyPwnCTF-2026-Challenges")
SKIP_BUILD = os.environ.get("E2E_SKIP_BUILD") == "1"
API = CTFD + "/admin/zync_challenges/api"

failures = []


def check(cond, message):
    print(("  ok   " if cond else "  FAIL ") + message)
    if not cond:
        failures.append(message)
    return cond


def step(title):
    print(f"\n== {title}")


class Session:
    def __init__(self, name, password):
        self.s = requests.Session()
        self.s.verify = False
        page = self.s.get(CTFD + "/login").text
        nonce = re.search(r'csrfNonce\': "([^"]+)"', page).group(1)
        r = self.s.post(CTFD + "/login", data={"name": name, "password": password, "nonce": nonce}, allow_redirects=False)
        assert r.status_code in (302, 303), f"login of {name} failed"
        self.nonce = re.search(r'csrfNonce\': "([^"]+)"', self.s.get(CTFD + "/challenges").text).group(1)

    def get(self, url, **kw):
        return self.s.get(url, **kw)

    def post(self, url, json=None, **kw):
        return self.s.post(url, json=json, headers={"CSRF-Token": self.nonce}, **kw)

    def delete(self, url, **kw):
        return self.s.delete(url, headers={"CSRF-Token": self.nonce}, **kw)


def data(resp):
    body = resp.json()
    assert body.get("success") is not False, body
    return body.get("data")


def wait_op(admin, op, timeout=3600):
    """Follow a Galvanize operation through Zync until it finishes."""
    deadline, offset, last_print = time.time() + timeout, 0, 0
    while time.time() < deadline:
        cur = data(admin.get(f"{API}/galvanize/operations/{op['id']}?offset={offset}"))
        for line in cur["lines"]:
            if line["level"] in ("error", "warning", "success") or time.time() - last_print > 30:
                print(f"     [{line['level']}] {line['message'][:160]}")
                last_print = time.time()
        offset = cur["line_count"]
        if cur["status"] != "running":
            return cur
        time.sleep(3)
    raise TimeoutError("operation did not finish")


def wait_job(admin, job, timeout=3600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = data(admin.get(f"{API}/jobs/{job['id']}"))
        if cur["status"] not in ("queued", "running"):
            return cur
        time.sleep(2)
    raise TimeoutError("job did not finish")


def run_job(admin, action, keys=None, **opts):
    resp = admin.post(f"{API}/jobs", json=dict({"action": action, "repo": REPO, "keys": keys}, **opts))
    assert resp.status_code == 200, resp.text
    job = wait_job(admin, data(resp))
    errors = [l for l in job["log"].splitlines() if l.startswith("[ERROR]")]
    return job, errors


def challenge_rows(admin):
    return data(admin.get(f"{API}/challenges", params={"repo": REPO}))


def by_dir(rows, suffix):
    """A challenge by its folder (<category folder>/<challenge folder>)."""
    return next(c for c in rows if c["dir"].endswith("/" + suffix))


def snapshot(admin, chal_id):
    """IDs of a challenge's flags, hints and files (unchanged by a no-op sync)."""
    out = {}
    for kind in ("flags", "hints", "files"):
        items = admin.get(f"{CTFD}/api/v1/challenges/{chal_id}/{kind}").json()["data"]
        out[kind] = sorted(i["id"] for i in items)
    return out


def main():
    admin = Session(ADMIN, PASSWORD)

    step("Overview")
    ov = data(admin.get(f"{API}/overview"))
    check(ov["galvanize"].get("configured") and not ov["galvanize"].get("error"), f"Galvanize reachable ({ov['galvanize']})")
    check(any(REPO_URL.startswith(s) for s in ov["clone_sources"]), f"{REPO_URL} is an allowed source")
    if any(r["name"] == REPO for r in ov["repos"]):
        print("  (repository already cloned: replacing it)")

    step("Guards of the clone")
    r = admin.post(f"{API}/galvanize/clone", json={"url": "https://evil.example.com/x.git"})
    check(r.status_code == 403, f"URL outside the allowed sources refused ({r.status_code})")
    r = admin.post(f"{API}/galvanize/clone", json={"url": "https://user:tok@github.com/polycyber/x.git"})
    check(r.status_code == 403, f"credentials in the URL refused ({r.status_code})")

    step("Clone")
    t0 = time.time()
    r = admin.post(f"{API}/galvanize/clone", json={"url": REPO_URL, "name": REPO, "force": True})
    check(r.status_code == 202, f"clone started ({r.status_code} {r.text[:120]})")
    op = wait_op(admin, data(r))
    check(op["status"] in ("success", "warning"), f"clone finished: {op['status']} in {time.time() - t0:.0f}s")
    repo = next((x for x in data(admin.get(f"{API}/overview"))["repos"] if x["name"] == REPO), None)
    if not check(bool(repo and repo["is_git"] and repo.get("commit")), f"repository listed: {repo and {k: repo.get(k) for k in ('branch', 'commit', 'challenges')}}"):
        print("\nCannot go on without the repository")
        return 1

    step("Definitions")
    listing = challenge_rows(admin)
    rows, skipped = listing["challenges"], listing["skipped"]
    check(len(rows) + len(skipped) == repo["challenges"] == 56, f"56 definition files: {len(rows)} read, {len(skipped)} skipped")
    for sk in skipped:
        print(f"     skipped {sk['path']}: {sk['reason']}")
    deployable = [c for c in rows if c["deployable"]]
    buildable = [c for c in deployable if c["build"]["buildable"]]
    print(f"     {len(deployable)} deployable, {len(buildable)} with something to build")
    refused = [f"{c['path']}: {c['build'].get('reason')}" for c in rows if c["build"].get("refused")]
    check(not refused, f"no build refused by the image policy {refused}")
    declared = [f for c in rows for f in c.get("files") or []]
    file_errors = [f"{c['path']}: {f['declared']}: {f['error']}" for c in rows for f in c.get("files") or [] if f.get("error")]
    # The listing relays each definition's files and commit (a listing
    # without them passed this check before)
    check(all("files" in c for c in rows) and declared and all(f.get("path") or f.get("error") for f in declared),
          f"declared files resolved by Galvanize ({len(declared)} files, {len(file_errors)} errors)")
    check(all(c.get("commit") == repo["commit"] for c in rows), f"definitions at the repository's commit {repo['commit']}")
    for e in file_errors:
        print(f"     file error {e}")
    check(by_dir(rows, "Forensic/Nexus Labs 1")["build"]["kind"] == "image", "http challenge with a sibling compose file builds its Dockerfile")
    check(by_dir(rows, "Web/Vault13 Research Terminal")["build"]["kind"] == "compose", "inline compose definition builds from the compose file")
    check(by_dir(rows, "Web/CatLab 1")["build"]["kind"] == "none", "compose with registry images only: nothing to build")

    step("Ingest, dry run")
    job, errors = run_job(admin, "ingest", dry_run=True)
    summary = job["summary"]
    print(f"     would install {len(summary['installed'])}, invalid {len(summary['failed'])}: {summary['failed']}")
    for e in errors:
        print("     " + e[:200])
    check(all(c["ctfd"] is None for c in challenge_rows(admin)["challenges"]), "dry run changed nothing")

    if not SKIP_BUILD:
        step(f"Build every image on Galvanize's hosts ({len(buildable)} challenges)")
        t0 = time.time()
        r = admin.post(f"{API}/galvanize/build", json={"repo": REPO})
        check(r.status_code == 202, "build started")
        op = wait_op(admin, data(r), timeout=7200)
        res = op.get("result") or {}
        print(f"     built {len(res.get('built', []))}, failed {res.get('failed')}, nothing to build {len(res.get('skipped', []))}, {time.time() - t0:.0f}s")
        check(len(res.get("built", [])) > 0, "images built")

        step("Images on the hosts")
        op = wait_op(admin, data(admin.post(f"{API}/galvanize/images", json={"action": "status", "repo": REPO})))
        images = (op.get("result") or {}).get("images") or {}
        present = sorted(i for i, hosts in images.items() if all(s == "present" for s in hosts.values()))
        print(f"     present on every host: {len(present)}/{len(images)}")
        built_images = [img for c in buildable if c["path"] in set(res.get("built", [])) for img in c["build"]["images"]]
        check(all(i in present for i in built_images), "every built image is on the host")
        failed_labels = sorted(f"{c['category']}/{c['name']}" for c in rows if c["path"] in set(res.get("failed", [])))

    step("Rebuild + ingest (all)" if not SKIP_BUILD else "Ingest")
    t0 = time.time()
    job, errors = run_job(admin, "ingest" if SKIP_BUILD else "all")
    summary = job["summary"]
    print(f"     installed {len(summary['installed'])}, existing {len(summary['existing'])}, failed {summary['failed']}, "
          f"build failed {summary.get('build_failed')} ({time.time() - t0:.0f}s)")
    for e in errors:
        print("     " + e[:200])
    rows = challenge_rows(admin)["challenges"]
    in_ctfd = [c for c in rows if c["ctfd"]]
    check(len(in_ctfd) == len(summary["installed"]) + len(summary["existing"]), f"{len(in_ctfd)} challenges linked in CTFd")
    if not SKIP_BUILD:
        check(sorted(summary["build_failed"]) == failed_labels, f"the builds that failed are the ones \"all\" did not ingest {failed_labels}")
        ingested = {f"{c['category']}/{c['name']}" for c in in_ctfd}
        check(not ingested & set(summary["build_failed"]), "no challenge whose build failed is in CTFd")

    step("Content in CTFd")
    ids_by_name = {c["name"]: c["ctfd"]["id"] for c in rows if c["ctfd"]}
    d3 = by_dir(rows, "Cryptography/Dexter 3")
    if check(d3["ctfd"] is not None, f"Dexter 3 ingested ({d3['name']})"):
        req = admin.get(f"{CTFD}/api/v1/challenges/{d3['ctfd']['id']}/requirements").json()["data"] or {}
        want = sorted(ids_by_name[n] for n in d3["requirements"])
        check(sorted(req.get("prerequisites", [])) == want, f"Dexter 3 requires {d3['requirements']} = {want} ({req})")
    heap = by_dir(rows, "Forensic/Ghost in the Heap")
    if check(heap["ctfd"] is not None, "Ghost in the Heap ingested"):
        files = admin.get(f"{CTFD}/api/v1/challenges/{heap['ctfd']['id']}/files").json()["data"]
        if check(len(files) == 1 and files[0]["location"].endswith("memdump.raw"), f"memdump.raw attached ({files})"):
            with admin.get(f"{CTFD}/files/{files[0]['location']}", stream=True) as r:
                size = int(r.headers.get("Content-Length", 0))
            check(size == 536870912, f"the real 512 MB file, not a Git LFS pointer ({size} bytes)")
    f4 = by_dir(rows, "OSINT/La Fondatrice 4")
    if check(f4["ctfd"] is not None, "La Fondatrice 4 ingested"):
        hints = admin.get(f"{CTFD}/api/v1/challenges/{f4['ctfd']['id']}/hints").json()["data"]
        check(len(hints) >= 2, f"hints attached ({len(hints)})")
    visible = sum(1 for c in rows if c["ctfd"] and c["ctfd"]["state"] == "visible")
    check(visible == len(in_ctfd), f"challenges visible once complete ({visible}/{len(in_ctfd)})")

    step("Sync without changes")
    sample = [c["ctfd"]["id"] for c in rows if c["ctfd"] and c["dir"].split("/", 1)[1] in
              ("Forensic/Ghost in the Heap", "OSINT/La Fondatrice 4", "Cryptography/Dexter 3", "Cryptography/NSSHA 2")]
    before = {n: snapshot(admin, n) for n in sample}
    t0 = time.time()
    job, errors = run_job(admin, "sync")
    print(f"     synced {len(job['summary']['synced'])}, failed {job['summary']['failed']} ({time.time() - t0:.0f}s)")
    check(not errors, f"no sync error {errors[:3]}")
    after = {n: snapshot(admin, n) for n in sample}
    check(before == after, "flags, hints and files keep their IDs")
    check("Uploaded" not in job["log"], "no file uploaded again")

    step("Player deploys an instance (existing Zync/Galvanize flow)")
    candidates = [c for c in rows if c["deployable"] and c["ctfd"] and c["dir"].endswith("Web/Vault13 Research Terminal")]
    if PLAYER and candidates and not SKIP_BUILD:
        target = candidates[0]
        player = Session(PLAYER, PLAYER_PASSWORD)
        url = player.get(f"{CTFD}/api/v1/deploy/url").json()["deployer_url"]
        token = player.post(f"{CTFD}/api/v1/deploy/token", json={"challenge_id": target["ctfd"]["id"]}).json()["token"]
        g = requests.Session()
        g.verify = False
        g.headers["Authorization"] = f"Bearer {token}"
        body = {"category": target["category"], "challenge_name": target["name"]}
        r = g.post(f"{url}/deploy", json=body)
        check(r.status_code in (200, 202), f"deploy requested for {target['name']} ({r.status_code} {r.text[:120]})")
        status = {}
        for _ in range(120):
            status = g.get(f"{url}/status").json()
            if status.get("status") in ("running", "error"):
                break
            time.sleep(3)
        check(status.get("status") == "running", f"instance running from the images built by Galvanize ({status.get('status')}, {status.get('error') or status.get('connection_info')})")

        step("Guards while the instance runs")
        r = admin.post(f"{API}/galvanize/remove", json={"repo": REPO})
        check(r.status_code == 409, f"repository removal refused ({r.status_code}: {r.text[:160]})")
        r = admin.post(f"{API}/galvanize/images", json={"action": "remove", "repo": REPO})
        check(r.status_code == 409, f"image removal refused ({r.status_code})")

        r = g.post(f"{url}/terminate", json=body)
        check(r.status_code in (200, 202), f"terminate requested ({r.status_code})")
        for _ in range(60):
            s = g.get(f"{url}/status")
            if s.status_code == 404 or s.json().get("status") in (None, "stopped"):
                break
            time.sleep(3)
    else:
        print("  (skipped: set E2E_PLAYER / E2E_PLAYER_PASSWORD, and build)")

    step("Delete everything from CTFd")
    job, errors = run_job(admin, "delete")
    print(f"     deleted {len(job['summary']['deleted'])}, failed {job['summary']['failed']}")
    check(not job["summary"]["failed"], "every challenge deleted (dependencies handled)")
    rows = challenge_rows(admin)["challenges"]
    check(all(c["ctfd"] is None for c in rows), "nothing left in CTFd")

    if not SKIP_BUILD:
        step("Remove the built images")
        op = wait_op(admin, data(admin.post(f"{API}/galvanize/images", json={"action": "remove", "repo": REPO})))
        check(op["status"] in ("success", "warning"), f"images removed ({op['status']})")

    step("Remove the repository")
    time.sleep(5)
    r = admin.post(f"{API}/galvanize/remove", json={"repo": REPO})
    check(r.status_code == 202, f"removal started ({r.status_code} {r.text[:160]})")
    if r.status_code == 202:
        op = wait_op(admin, data(r))
        check(op["status"] == "success", "repository removed")
        check(all(x["name"] != REPO for x in data(admin.get(f"{API}/overview"))["repos"]), "no longer listed")

    print(f"\n{len(failures)} failure(s)")
    for f in failures:
        print("  - " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
