"""Admin page and JSON API of the challenge manager (/admin/zync_challenges).

Repositories, builds and images are Galvanize operations, relayed here so the
page has one API; ingest, sync and delete are Zync jobs working on CTFd.
"""
from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy.orm import defer

from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_current_user

from ..manager import actions, catalog
from ..manager.galvanize import GalvanizeClient, GalvanizeError
from ..manager.jobs import JobBusy, ZyncJob, active_job, request_cancel, start_job

JOB_ACTIONS = {"ingest", "sync", "all", "delete"}


def _error(message, code=400):
    return jsonify({"success": False, "error": message}), code


def _ok(data, code=200):
    return jsonify({"success": True, "data": data}), code


def _galvanize_error(e: GalvanizeError):
    code = e.status if e.status in (400, 403, 404, 409) else 502
    return _error(str(e), code)


def _keys(value):
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("'keys' must be a list of definition paths")
    return value


def _refs(value):
    """Challenges of a build or image request: definition paths, as {path}."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, dict) and isinstance(v.get("path"), str) for v in value):
        raise ValueError("'challenges' must be a list of {path}")
    return [{"path": v["path"]} for v in value]


def define_zync_admin_challenges(app):
    bp = Blueprint("admin_zync_challenges", __name__, template_folder="templates", static_folder="assets",
                   static_url_path="/admin/zync_challenges/static")

    @bp.route("/admin/zync_challenges", methods=["GET"])
    @admins_only
    def page():
        return render_template("zync_challenges.html")

    @bp.route("/admin/zync_challenges/api/overview", methods=["GET"])
    @admins_only
    def overview():
        data = {"galvanize": {"configured": False}, "repos": [], "clone_sources": []}
        try:
            client = GalvanizeClient.from_config()
            data["galvanize"] = {"configured": True, "url": client.url}
            listing = client.repositories()
            data["repos"] = listing.get("repositories") or []
            data["clone_sources"] = listing.get("clone_sources") or []
        except GalvanizeError as e:
            data["galvanize"]["error"] = str(e)
        known = {r["name"] for r in data["repos"]}
        # Repositories gone from Galvanize whose challenges are still in CTFd
        data["orphan_repos"] = [r for r in catalog.linked_repos() if r not in known]
        job = active_job()
        data["active_job"] = job.to_dict(offset=len(job.log or "")) if job else None
        return _ok(data)

    @bp.route("/admin/zync_challenges/api/challenges", methods=["GET"])
    @admins_only
    def challenges():
        repo = request.args.get("repo", "")
        try:
            entries, skipped = catalog.load(GalvanizeClient.from_config(), repo)
        except GalvanizeError as e:
            # A repository gone from Galvanize (404): the challenges ingested
            # from it are still listed, to be deleted. When Galvanize cannot
            # answer, the error is shown: all would look gone otherwise.
            if e.status == 404 and repo in catalog.linked_repos():
                entries, skipped = [], []
            else:
                return _galvanize_error(e)
        rows = []
        for entry in entries + catalog.orphan_entries(repo, {e.path for e in entries}):
            chal = catalog.linked_challenge(entry)
            rows.append({
                "key": entry.key,
                "path": entry.path,
                "dir": entry.dir,
                "name": entry.name,
                "category": entry.category,
                "type": entry.type,
                "deployable": entry.deployable,
                "orphan": not entry.data,
                "build": entry.build,
                "files": entry.files,
                "commit": entry.commit,
                "requirements": entry.requirement_names(),
                "ctfd": {"id": chal.id, "state": chal.state, "type": chal.type, "value": chal.value,
                         "name": chal.name} if chal else None,
            })
        return _ok({"repo": repo, "challenges": rows, "skipped": skipped})

    # ── Galvanize operations ─────────────────────────────────────────────────

    @bp.route("/admin/zync_challenges/api/galvanize/<action>", methods=["POST"])
    @admins_only
    def galvanize_action(action):
        body = request.get_json(silent=True) or {}
        try:
            client = GalvanizeClient.from_config()
            if action == "clone":
                url = str(body.get("url") or "").strip()
                if not url:
                    return _error("A git URL is required")
                op = client.clone(url, (body.get("name") or "").strip() or None, (body.get("ref") or "").strip() or None,
                                  bool(body.get("force")))
            elif action == "pull":
                op = client.pull(str(body.get("repo") or ""))
            elif action == "remove":
                op = client.remove(str(body.get("repo") or ""))
            elif action == "build":
                op = client.build(body.get("repo") or None, _refs(body.get("challenges")), bool(body.get("no_cache")))
            elif action == "images":
                if body.get("action") not in ("status", "remove"):
                    return _error("action must be status or remove")
                op = client.images(body["action"], body.get("repo") or None, _refs(body.get("challenges")))
            else:
                return _error(f"Unknown action: {action}", 404)
        except ValueError as e:
            return _error(str(e))
        except GalvanizeError as e:
            return _galvanize_error(e)
        return _ok(op, 202)

    @bp.route("/admin/zync_challenges/api/galvanize/operations", methods=["GET"])
    @admins_only
    def galvanize_operations():
        try:
            return _ok(GalvanizeClient.from_config().operations())
        except GalvanizeError as e:
            return _galvanize_error(e)

    @bp.route("/admin/zync_challenges/api/galvanize/operations/<op_id>", methods=["GET"])
    @admins_only
    def galvanize_operation(op_id):
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0
        try:
            return _ok(GalvanizeClient.from_config().operation(op_id, offset))
        except GalvanizeError as e:
            return _galvanize_error(e)

    @bp.route("/admin/zync_challenges/api/galvanize/operations/<op_id>/cancel", methods=["POST"])
    @admins_only
    def galvanize_cancel(op_id):
        try:
            GalvanizeClient.from_config().cancel(op_id)
        except GalvanizeError as e:
            return _galvanize_error(e)
        return _ok(None, 202)

    # ── Zync jobs (CTFd side) ────────────────────────────────────────────────

    @bp.route("/admin/zync_challenges/api/jobs", methods=["GET"])
    @admins_only
    def list_jobs():
        jobs = ZyncJob.query.options(defer(ZyncJob.log)).order_by(ZyncJob.id.desc()).limit(25).all()
        data = []
        for job in jobs:
            item = job.to_dict(offset=0, with_log=False)
            data.append(item)
        return _ok(data)

    @bp.route("/admin/zync_challenges/api/jobs", methods=["POST"])
    @admins_only
    def create_job():
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in JOB_ACTIONS:
            return _error(f"Unknown action: {action!r}")
        repo = body.get("repo")
        if not isinstance(repo, str):
            return _error("A repository is required")
        try:
            params = {"repo": repo, "keys": _keys(body.get("keys")), "force": bool(body.get("force")),
                      "dry_run": bool(body.get("dry_run"))}
        except ValueError as e:
            return _error(str(e))
        user = get_current_user()
        try:
            job = start_job(current_app._get_current_object(), action, repo, params,
                            user.name if user else None, actions.FUNCTIONS[action])
        except JobBusy as e:
            return _error(str(e), 409)
        return _ok(job.to_dict())

    @bp.route("/admin/zync_challenges/api/jobs/<int:job_id>/cancel", methods=["POST"])
    @admins_only
    def cancel_job(job_id):
        if not ZyncJob.query.filter_by(id=job_id).first():
            return _error("Job not found", 404)
        if not request_cancel(job_id):
            return _error("The job is not running", 409)
        return _ok(None, 202)

    @bp.route("/admin/zync_challenges/api/jobs/<int:job_id>", methods=["GET"])
    @admins_only
    def get_job(job_id):
        job = ZyncJob.query.filter_by(id=job_id).first()
        if not job:
            return _error("Job not found", 404)
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0
        return _ok(job.to_dict(offset=offset))

    app.register_blueprint(bp)
