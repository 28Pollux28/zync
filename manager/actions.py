"""Job bodies: what each CTFd action of the challenge manager does.

They follow ctfd-management's challenges.sh (ingest, sync, all) and add
delete. Definitions and files come from Galvanize; image builds are
Galvanize operations, which "all" starts and waits for.
"""
import functools
import time
from dataclasses import dataclass

from CTFd.models import db

from . import catalog, ctfd_ops
from .galvanize import GalvanizeClient, GalvanizeError

BUILD_POLL_SECONDS = 2
# Longest wait for a Galvanize build (Galvanize stops each challenge's build
# after build.timeout; this only bounds a Galvanize that never answers)
BUILD_WAIT_SECONDS = 6 * 3600
POLL_RETRIES = 5


def _checkpoint(log):
    """Stop if the job was cancelled or lost its lock (see jobs.checkpoint)."""
    checkpoint = getattr(log, "checkpoint", None)
    if checkpoint:
        checkpoint()


@dataclass
class Selection:
    """The definitions a job works on, from one Galvanize listing."""

    repo: str
    keys: list  # definition paths selected, None for all
    entries: list  # the selected ones
    all_entries: list  # every definition of the repository
    commit: str = None  # repository commit of the listing


class RepoNotFound(Exception):
    pass


def _select(log, client, repo, keys, missing_ok=False):
    try:
        all_entries, skipped = catalog.load(client, repo)
    except GalvanizeError as e:
        if e.status != 404:
            raise
        # A repository removed from Galvanize: its CTFd challenges can still
        # be deleted (see delete)
        if not missing_ok or repo not in catalog.linked_repos():
            raise RepoNotFound(f"Repository '{repo}' not found in Galvanize") from e
        log.warning(f"Repository '{repo}' is no longer in Galvanize")
        all_entries, skipped = [], []
    if keys is None:
        entries = all_entries
    else:
        wanted = set(keys)
        entries = [e for e in all_entries if e.key in wanted]
    scope = "all challenges" if keys is None else f"{len(entries)} selected challenge(s)"
    log.info(f"Repository '{repo}': {scope}")
    if keys is not None and len(entries) < len(set(keys)):
        log.warning(f"{len(set(keys)) - len(entries)} selected challenge(s) are no longer in Galvanize's index")
    if keys is None:
        for sk in skipped:
            log.warning(f"Galvanize skipped {sk.get('path')}: {sk.get('reason')}")
    return Selection(repo, keys, entries, all_entries, catalog.commit_of(all_entries))


def _warn_if_changed(log, client, repo, before):
    try:
        after = catalog.current_commit(client, repo)
    except GalvanizeError:
        return
    if before and after and before != after:
        log.warning(f"Repository '{repo}' changed during the job ({before} → {after}): some challenges may "
                    f"come from the new commit, run sync again")


def job(fn=None, *, missing_ok=False):
    """A job action, on the definitions Galvanize lists when it starts, with a
    warning when the repository moved to another commit while it ran. A
    repository Galvanize does not have fails the job, unless missing_ok and
    challenges ingested from it are still in CTFd."""
    if fn is None:
        return functools.partial(job, missing_ok=missing_ok)

    @functools.wraps(fn)
    def run(log, repo, keys=None, force=False, dry_run=False):
        client = GalvanizeClient.from_config()
        try:
            selection = _select(log, client, repo, keys, missing_ok)
        except RepoNotFound as e:
            log.error(str(e))
            return {"status": "failed"}
        result = fn(log, client, selection, force, dry_run)
        _warn_if_changed(log, client, repo, selection.commit)
        return result
    return run


def _report(log, title, groups, dry_run=False):
    log.info(f"── {title}{' (dry run: nothing was changed)' if dry_run else ''} ──")
    for label, names, level in groups:
        if names:
            getattr(log, level)(f"{label}: {len(names)}")
            for name in names:
                log.info(f"  - {name}")


def _status(*failure_lists):
    return "warning" if any(failure_lists) else "success"


# ── Ingest ───────────────────────────────────────────────────────────────────

def sort_by_dependencies(entries):
    """Order entries so that each comes after the batch challenges it requires
    (by name; a name shared by several entries depends on all of them).
    Raises on circular dependencies."""
    by_name = {}
    for e in entries:
        by_name.setdefault(e.name, []).append(e)
    state, ordered, stack = {}, [], []

    def visit(entry):
        key = entry.key
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            raise ctfd_ops.OpError("Circular dependency: " + " → ".join([e.label for e in stack] + [entry.label]))
        state[key] = 1
        stack.append(entry)
        for dep in entry.requirement_names():
            for target in by_name.get(dep, []):
                if target is not entry:
                    visit(target)
        stack.pop()
        state[key] = 2
        ordered.append(entry)

    for entry in entries:
        visit(entry)
    return ordered


def _ingest(log, client, entries, dry_run, resolver_entries=None):
    installed, existing, failed = [], [], []
    try:
        ordered = sort_by_dependencies(entries)
    except ctfd_ops.OpError as e:
        log.error(str(e))
        return {"installed": [], "existing": [], "failed": [e.label for e in entries]}
    if any(e.requirement_names() for e in ordered):
        log.info("Install order resolved from the challenges' requirements")
    resolver = ctfd_ops.RequirementResolver(resolver_entries or entries)

    moved = catalog.relink_moved(resolver_entries or entries, log, dry_run)
    total = len(ordered)
    for i, entry in enumerate(ordered, 1):
        _checkpoint(log)
        log.info(f"[{i}/{total}] {entry.label} ({entry.path})")
        try:
            if dry_run:
                ctfd_ops.check(entry)
                if catalog.is_installing(entry):
                    log.warning(f"[{entry.label}] Left incomplete by an interrupted install: would install it again")
                    installed.append(entry.label)
                elif catalog.linked_challenge(entry) or entry.path in moved:
                    log.warning(f"[{entry.label}] Already in CTFd (use sync to update it)")
                    existing.append(entry.label)
                else:
                    log.info(f"[{entry.label}] Would install")
                    installed.append(entry.label)
                continue
            result = ctfd_ops.install(entry, client, resolver, log)
        except Exception as e:  # noqa: BLE001 — next challenge
            db.session.rollback()
            log.error(f"[{entry.label}] {'Invalid' if dry_run else 'Failed to install'}: {e}")
            failed.append(entry.label)
            continue
        if result == ctfd_ops.EXISTS:
            log.warning(f"[{entry.label}] Already in CTFd (use sync to update it)")
            existing.append(entry.label)
        else:
            log.success(f"[{entry.label}] Installed as #{result}")
            installed.append(entry.label)
    return {"installed": installed, "existing": existing, "failed": failed}


@job
def ingest(log, client, sel, force=False, dry_run=False):
    result = _ingest(log, client, sel.entries, dry_run, sel.all_entries)
    _report(log, "Ingest summary", [("Installed", result["installed"], "success"),
                                    ("Already in CTFd (skipped)", result["existing"], "warning"),
                                    ("Failed", result["failed"], "error")], dry_run)
    result["status"] = _status(result["failed"])
    return result


# ── Rebuild + ingest ─────────────────────────────────────────────────────────

def _wait_operation(log, client, op):
    """Follow a Galvanize operation, copying its log, until it finishes.
    Cancelling the job cancels the operation; a Galvanize that stops
    answering, or an operation running past BUILD_WAIT_SECONDS, fails it."""
    offset, errors = 0, 0
    deadline = time.monotonic() + BUILD_WAIT_SECONDS
    levels = {"debug": log.debug, "info": log.info, "success": log.success, "warning": log.warning, "error": log.error}
    while True:
        try:
            _checkpoint(log)
        except Exception:
            try:
                client.cancel(op["id"])
                log.warning(f"Cancelled Galvanize operation {op['id']}")
            except GalvanizeError as e:
                log.warning(f"Could not cancel Galvanize operation {op['id']}: {e}")
            raise
        try:
            current = client.operation(op["id"], offset)
            errors = 0
        except GalvanizeError as e:
            errors += 1
            if e.status == 404 or errors > POLL_RETRIES:
                log.error(f"Lost track of Galvanize operation {op['id']}: {e}")
                return {"id": op["id"], "status": "failed", "result": {}}
            log.warning(f"Galvanize did not answer ({e}), retrying")
            time.sleep(BUILD_POLL_SECONDS)
            continue
        for line in current.get("lines") or []:
            levels.get(line.get("level"), log.info)(f"Galvanize: {line.get('message')}")
        offset = current.get("line_count", offset)
        if current.get("status") != "running":
            return current
        if time.monotonic() > deadline:
            log.error(f"Galvanize operation {op['id']} still running after {BUILD_WAIT_SECONDS // 3600} h: cancelled")
            try:
                client.cancel(op["id"])
            except GalvanizeError:
                pass
            return {"id": op["id"], "status": "failed", "result": current.get("result") or {}}
        time.sleep(BUILD_POLL_SECONDS)


@job
def all_(log, client, sel, force=False, dry_run=False):
    """challenges.sh's default action: build the images, then ingest. Every
    deployable challenge selected goes to Galvanize, which builds what there
    is to build; a challenge whose build failed (refused included) is not
    ingested."""
    entries = sel.entries
    deployable = [e for e in entries if e.deployable]
    failed_paths = set()
    if deployable and dry_run:
        for e in deployable:
            if e.build.get("refused"):
                log.error(f"[{e.label}] Build refused by Galvanize: {e.build.get('reason')}")
                failed_paths.add(e.path)
            elif e.build.get("buildable"):
                log.info(f"[{e.label}] Would build {', '.join(e.build.get('images') or [])}")
    elif deployable:
        log.info(f"── Build ({len(deployable)} challenge(s), on Galvanize's hosts) ──")
        refs = [{"path": e.path} for e in deployable]
        op = _wait_operation(log, client, client.build(repo=sel.repo, challenges=refs, no_cache=force))
        result = op.get("result") or {}
        # What Galvanize did not report built or with nothing to build failed
        done = set(result.get("built") or []) | set(result.get("skipped") or [])
        failed_paths = {e.path for e in deployable if e.path not in done}
    else:
        log.info("Nothing to build")
    build_failed = sorted(e.label for e in deployable if e.path in failed_paths)
    if build_failed:
        log.warning("Some builds failed: their challenges are not ingested")

    log.info("── Ingest ──")
    to_ingest = [e for e in entries if e.path not in failed_paths]
    result = _ingest(log, client, to_ingest, dry_run, sel.all_entries)
    result["build_failed"] = build_failed
    _report(log, "Summary", [("Build failed", build_failed, "error"),
                             ("Installed", result["installed"], "success"),
                             ("Already in CTFd (skipped)", result["existing"], "warning"),
                             ("Install failed", result["failed"], "error")], dry_run)
    result["status"] = _status(build_failed, result["failed"])
    return result


# ── Sync ─────────────────────────────────────────────────────────────────────

@job
def sync(log, client, sel, force=False, dry_run=False):
    entries, all_entries = sel.entries, sel.all_entries
    resolver = ctfd_ops.RequirementResolver(all_entries)
    moved = catalog.relink_moved(all_entries, log, dry_run)
    synced, missing, failed, req_failed, done = [], [], [], [], []
    total = len(entries)
    for i, entry in enumerate(entries, 1):
        _checkpoint(log)
        log.info(f"[{i}/{total}] {entry.label} ({entry.path})")
        try:
            if not catalog.linked_challenge(entry) and entry.path not in moved:
                log.warning(f"[{entry.label}] Not in CTFd: ingest it first")
                missing.append(entry.label)
                continue
            if dry_run:
                ctfd_ops.check(entry)
                log.info(f"[{entry.label}] Would sync")
                synced.append(entry.label)
                if catalog.linked_challenge(entry):  # (a moved one is linked by the sync)
                    done.append(entry)
                continue
            ctfd_ops.sync(entry, client, log)
            log.success(f"[{entry.label}] Synced")
            synced.append(entry.label)
            done.append(entry)
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            log.error(f"[{entry.label}] {'Invalid' if dry_run else 'Failed to sync'}: {e}")
            failed.append(entry.label)
            if catalog.linked_challenge(entry):
                done.append(entry)

    # Second pass, once every challenge exists: name → ID resolution of the
    # prerequisites cannot depend on the sync order. The requirements are
    # planned for all the challenges at once, cycles refused (see
    # plan_requirements), so a dry run reports what the sync does.
    if done:
        log.info("Checking requirements..." if dry_run else "Setting requirements...")
        plan = ctfd_ops.plan_requirements(done, resolver)
    for entry in done:
        _checkpoint(log)
        try:
            if dry_run:
                if isinstance(plan[entry.path], Exception):
                    raise plan[entry.path]
                continue
            ctfd_ops.sync_requirements(entry, plan[entry.path], log)
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            log.error(f"[{entry.label}] {'Requirements would be refused' if dry_run else 'Failed to set requirements'}: {e}")
            req_failed.append(entry.label)
    ctfd_ops.clear_caches()

    _report(log, "Sync summary", [("Synced", synced, "success"), ("Not in CTFd", missing, "warning"),
                                  ("Failed", failed, "error"), ("Requirements failed", req_failed, "error")], dry_run)
    return {"synced": synced, "missing": missing, "failed": failed, "requirements_failed": req_failed,
            "status": _status(failed, req_failed)}


# ── Delete ───────────────────────────────────────────────────────────────────

def delete_entries(log, entries, force, dry_run):
    deleted, missing, failed, found = [], [], [], []
    for entry in entries:
        chal = catalog.linked_challenge(entry)
        if chal:
            found.append((entry, chal))
        else:
            log.debug(f"[{entry.label}] Not in CTFd")
            missing.append(entry.label)
    log.info(f"{len(found)} challenge(s) to delete from CTFd")

    # A challenge can go when everything that requires it goes too. Refusing
    # one may make its own prerequisites undeletable, hence the fixed point.
    deletable = {chal.id for _, chal in found}
    refused = {}
    changed = not force
    while changed:
        changed = False
        for entry, chal in found:
            if chal.id in deletable:
                deps = ctfd_ops.dependents(chal.id, deletable)
                if deps:
                    deletable.discard(chal.id)
                    refused[chal.id] = deps
                    changed = True

    for entry, chal in found:
        _checkpoint(log)
        label = f"'{chal.name}' (#{chal.id})"
        if chal.id in refused:
            log.error(f"[{entry.label}] {'Would be refused' if dry_run else 'Not deleted'}: prerequisite of "
                      + ", ".join(f"'{d.name}'" for d in refused[chal.id]) + " (delete those too, or use force)")
            failed.append(entry.label)
            continue
        if dry_run:
            log.info(f"[{entry.label}] Would delete {label}")
            deleted.append(entry.label)
            continue
        try:
            ctfd_ops.delete(chal, log, force=force, excluded=deletable)
            log.success(f"[{entry.label}] Deleted {label}")
            deleted.append(entry.label)
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            log.error(f"[{entry.label}] Not deleted: {e}")
            failed.append(entry.label)
    if deleted and not dry_run:
        log.info("Instances already started keep running until they expire: "
                 "terminate them from the Zync Dashboard if needed")
    return {"deleted": deleted, "missing": missing, "failed": failed}


@job(missing_ok=True)
def delete(log, client, sel, force=False, dry_run=False):
    entries = sel.entries
    # Challenges whose definition left Galvanize can still be deleted
    orphans = catalog.orphan_entries(sel.repo, {e.path for e in sel.all_entries})
    if sel.keys is not None:
        orphans = [o for o in orphans if o.key in set(sel.keys)]
    if orphans:
        log.info(f"{len(orphans)} challenge(s) whose definition is no longer in Galvanize")
    entries = entries + orphans
    result = delete_entries(log, entries, force, dry_run)
    _report(log, "Delete summary", [("Deleted", result["deleted"], "success"),
                                    ("Not in CTFd", result["missing"], "info"),
                                    ("Failed", result["failed"], "error")], dry_run)
    result["status"] = _status(result["failed"])
    return result


FUNCTIONS = {"ingest": ingest, "sync": sync, "all": all_, "delete": delete}

__all__ = ["FUNCTIONS", "GalvanizeError", "sort_by_dependencies", "delete_entries"]
