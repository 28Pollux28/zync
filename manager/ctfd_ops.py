"""Install, sync and delete challenges in CTFd from their definition.

Port of ctfd-management's modules/ctfd/{challenge,resources}.sh. Instead of
calling CTFd's REST API with a token, it runs the code the API runs
(challenge type classes, models, upload helpers) inside CTFd. Definitions
and files come from Galvanize.
"""
import posixpath

from flask import current_app, request
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from CTFd.cache import clear_challenges, clear_standings
from CTFd.models import (
    ChallengeFiles,
    Challenges,
    ChallengeTopics,
    Flags,
    Hints,
    Tags,
    Topics,
    db,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES
from CTFd.plugins.flags import FLAG_CLASSES
from CTFd.schemas.challenges import ChallengeSchema
from CTFd.utils.uploads import delete_file, upload_file

from .catalog import (Entry, ZyncChallengeSource, is_installing, link, linked_challenge, requirement_anonymize,
                      requirement_prereqs)

EXISTS = "exists"
STATES = ("visible", "hidden", "locked")
# `extra` passes plugin fields through, but not the ones that define the
# challenge's identity or its visibility
PROTECTED_EXTRA = {"id", "type", "name", "category", "state", "requirements", "next_id"}


class OpError(Exception):
    """An operation failed; the message is shown in the job log."""


def clear_caches():
    clear_standings()
    clear_challenges()


def _chal_class(type_name: str):
    try:
        return CHALLENGE_CLASSES[type_name]
    except KeyError:
        raise OpError(f"Unknown challenge type '{type_name}' (installed: {', '.join(sorted(CHALLENGE_CLASSES))})")


def _with_request(payload: dict, fn):
    """Run fn() inside a fake request whose JSON body is payload: challenge
    type classes read their input from flask.request, as in the API."""
    with current_app.test_request_context(method="POST", json=payload):
        return fn(request)


# ── Payload ──────────────────────────────────────────────────────────────────

def _number(value, field):
    if isinstance(value, bool):
        raise OpError(f"'{field}' must be a number")
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise OpError(f"'{field}' must be a number (got {value!r})")
    return int(number) if number.is_integer() else number


def _or(value, default):
    return default if value is None else value


def build_payload(data: dict, for_update: bool = False) -> dict:
    name, category = data.get("name"), data.get("category")
    if not name:
        raise OpError("Challenge name is required")
    if not category:
        raise OpError("Challenge category is required")

    ctype = data.get("type") or "standard"
    payload = {
        "name": str(name),
        "category": str(category),
        "description": str(data.get("description") or ""),
        "value": _number(_or(data.get("value"), 100), "value"),
        "type": ctype,
    }
    for field in ("connection_info", "attribution"):
        if data.get(field):
            payload[field] = str(data[field])
        elif for_update:
            payload[field] = None  # removed from the definition: cleared in CTFd
    if data.get("attempts") not in (None, ""):
        payload["max_attempts"] = int(_number(data["attempts"], "attempts"))
    elif for_update:
        payload["max_attempts"] = 0

    if ctype == "dynamic":
        payload["initial"] = _number(_or(data.get("value"), 500), "value")
        payload["minimum"] = _number(_or(data.get("minimum"), 100), "minimum")
        payload["decay"] = _number(_or(data.get("decay"), 450), "decay")

    extra = data.get("extra")
    if isinstance(extra, dict):
        payload.update({k: v for k, v in extra.items() if k not in PROTECTED_EXTRA})
    return payload


def _validate(payload: dict):
    result = ChallengeSchema().load(payload)
    if result.errors:
        raise OpError(f"Invalid challenge data: {result.errors}")


# ── Flags ────────────────────────────────────────────────────────────────────

def desired_flags(data: dict) -> list:
    """Normalised like CTFd's API: static and regex flags are stripped."""
    flags = []
    for flag in data.get("flags") or []:
        if flag is None:
            continue
        if isinstance(flag, dict):
            content = flag.get("content", flag.get("flag", ""))
            item = {
                "type": str(flag.get("type") or "static"),
                "content": "" if content is None else str(content),
                "data": "" if flag.get("data") is None else str(flag.get("data")),
            }
        else:
            item = {"type": "static", "content": str(flag), "data": ""}
        if item["type"] not in FLAG_CLASSES:
            raise OpError(f"Unknown flag type '{item['type']}' (known: {', '.join(sorted(FLAG_CLASSES))})")
        if item["type"] in ("static", "regex"):
            item["content"] = item["content"].strip()
        flags.append(item)
    return flags


def _flag_key(flag) -> tuple:
    if isinstance(flag, dict):
        return (flag["type"] or "static", flag["content"] or "", flag["data"] or "")
    return (flag.type or "static", flag.content or "", flag.data or "")


def _create_flag(challenge_id: int, flag: dict):
    db.session.add(Flags(challenge_id=challenge_id, type=flag["type"], content=flag["content"],
                         data=flag["data"] or None))


def add_flags(data, challenge_id, log):
    flags = desired_flags(data)
    for flag in flags:
        _create_flag(challenge_id, flag)
    db.session.commit()
    log.debug(f"Added {len(flags)} flag(s)")


def sync_flags(data, challenge_id, log):
    """New flags are created before stale ones are deleted, so the challenge
    always has a valid flag; unchanged flags are left alone."""
    want = desired_flags(data)
    existing = Flags.query.filter_by(challenge_id=challenge_id).all()
    have = {_flag_key(f) for f in existing}
    need = {_flag_key(f) for f in want}

    added = [f for f in want if _flag_key(f) not in have]
    for flag in added:
        _create_flag(challenge_id, flag)
    db.session.commit()

    removed = [f for f in existing if _flag_key(f) not in need]
    for flag in removed:
        db.session.delete(flag)
    db.session.commit()
    log.debug(f"Flags: {len(added)} added, {len(removed)} removed")


# ── Files (from Galvanize) ───────────────────────────────────────────────────

def resolved_files(entry: Entry) -> list:
    """Paths (relative to Galvanize's challenge_dir) of the files the
    definition declares, as Galvanize found them."""
    paths = []
    for f in entry.files:
        if f.get("error") or not f.get("path"):
            raise OpError(f"File {f.get('declared')}: {f.get('error') or 'not found'}")
        paths.append(f["path"])
    return paths


def _upload(client, path: str, challenge_id: int):
    downloaded = client.download(path)
    if downloaded is None:
        raise OpError(f"File disappeared from Galvanize: {path}")
    tmp, _ = downloaded
    with tmp:
        upload_file(file=FileStorage(stream=tmp, filename=posixpath.basename(path)), challenge=challenge_id,
                    type="challenge")


def upload_files(paths, challenge_id, client, log):
    for path in paths:
        _upload(client, path, challenge_id)
        log.debug(f"Uploaded: {posixpath.basename(path)}")


def sync_files(entry, challenge_id, client, log):
    """A CTFd file is kept when its name and SHA-1 match a declared file, so
    unchanged files keep their download URL and are not downloaded again
    (Galvanize hashes only the files that have a CTFd file of their name).
    New files are uploaded before stale ones are deleted."""
    existing = ChallengeFiles.query.filter_by(challenge_id=challenge_id).all()
    used, to_upload = set(), []
    for path in resolved_files(entry):
        name = secure_filename(posixpath.basename(path))
        same_name = [f for f in existing if f.id not in used and (f.location or "").split("/")[-1] == name]
        match = None
        if same_name:
            checksum = client.file_checksum(path)
            if checksum is None:
                raise OpError(f"File disappeared from Galvanize: {path}")
            match = next((f for f in same_name if f.sha1sum == checksum["sha1"]), None)
        if match:
            used.add(match.id)
        else:
            to_upload.append(path)
    for path in to_upload:
        _upload(client, path, challenge_id)
        log.debug(f"Uploaded: {posixpath.basename(path)}")
    stale = [f.id for f in existing if f.id not in used]
    for file_id in stale:
        delete_file(file_id)
    log.debug(f"Files: {len(to_upload)} uploaded, {len(stale)} removed, {len(used)} unchanged")


# ── Hints ────────────────────────────────────────────────────────────────────
#
# Players' unlocks point at hint IDs, so hints are updated in place: the Nth
# declared hint reuses the Nth existing hint (by ID). A gated hint (with
# `requirements` naming other hints' `key`) gets its prerequisites before its
# content is written.

def _normalise_hint(entry) -> dict:
    if isinstance(entry, dict):
        content = entry.get("content", entry.get("hint", ""))
        return {
            "content": "" if content is None else str(content),
            "title": str(entry.get("title") or ""),
            "cost": int(_number(entry.get("cost") or 0, "hint cost")),
            "key": entry.get("key"),
            "requires": [str(k) for k in (entry.get("requirements") or [])],
        }
    return {"content": str(entry), "title": "", "cost": 0, "key": None, "requires": []}


def declared_hints(data) -> list:
    hints = [_normalise_hint(h) for h in (data.get("hints") or []) if h is not None]
    keys = {str(h["key"]) for h in hints if h["key"] is not None}
    for h in hints:
        for k in h["requires"]:
            if k not in keys:
                raise OpError(f"Hint prerequisite key '{k}' is not defined among this challenge's hints")
    return hints


def apply_hints(data, challenge_id, log, existing=None) -> list:
    """Returns the existing hints that were not reused."""
    existing = list(existing or [])
    declared = declared_hints(data)

    ids_by_key, rows = {}, []
    for i, hint in enumerate(declared):
        if i < len(existing):
            row, reused = existing[i], True
        else:
            row = Hints(challenge_id=challenge_id, title=hint["title"], cost=hint["cost"],
                        content="" if hint["requires"] else hint["content"])
            db.session.add(row)
            db.session.flush()
            reused = False
        rows.append((row, hint, reused))
        if hint["key"] is not None:
            ids_by_key[str(hint["key"])] = row.id
    db.session.commit()

    for row, hint, reused in rows:
        prereqs = [ids_by_key[k] for k in hint["requires"]]
        if not reused and not prereqs:
            continue
        row.title, row.cost = hint["title"], hint["cost"]
        row.requirements = {"prerequisites": prereqs}
        if prereqs:
            db.session.commit()  # prerequisites enforced before the content is readable
        row.content = hint["content"]
        db.session.commit()

    log.debug(f"Hints: {len(declared)} declared")
    return existing[len(declared):]


def sync_hints(data, challenge_id, log):
    existing = Hints.query.filter_by(challenge_id=challenge_id).order_by(Hints.id).all()
    for hint in apply_hints(data, challenge_id, log, existing):
        log.warning(f"Deleted hint #{hint.id} (removed from challenge.yml; players who unlocked it lose access)")
        db.session.delete(hint)
    db.session.commit()


# ── Tags and topics (no player state: cleared and recreated) ─────────────────

def _strings(data, key) -> list:
    values = data.get(key) or []
    return [str(v) for v in values if v is not None] if isinstance(values, list) else []


def set_tags(data, challenge_id, log):
    Tags.query.filter_by(challenge_id=challenge_id).delete()
    tags = _strings(data, "tags")
    for value in tags:
        db.session.add(Tags(challenge_id=challenge_id, value=value))
    db.session.commit()
    log.debug(f"Tags: {len(tags)}")


def set_topics(data, challenge_id, log):
    ChallengeTopics.query.filter_by(challenge_id=challenge_id).delete()
    topics = _strings(data, "topics")
    for value in topics:
        topic = Topics.query.filter_by(value=value).first()
        if topic is None:
            topic = Topics(value=value)
            db.session.add(topic)
            db.session.flush()
        db.session.add(ChallengeTopics(challenge_id=challenge_id, topic_id=topic.id))
    db.session.commit()
    log.debug(f"Topics: {len(topics)}")


# ── Requirements ─────────────────────────────────────────────────────────────

class RequirementResolver:
    """Resolves a prerequisite name to one CTFd challenge.

    A name is looked up among the definitions of the repository first:
    several definitions of that name are ambiguous ("Category/Name" picks
    one), and one not ingested yet is an error, never a CTFd challenge of the
    same name from elsewhere. Only a name no definition of the repository has
    falls back to CTFd, where it must be unique."""

    def __init__(self, entries):
        self.by_name = {}
        self.by_label = {}
        for e in entries:
            self.by_name.setdefault(e.name, []).append(e)
            self.by_label.setdefault(e.label, []).append(e)

    def resolve(self, name):
        found = self.by_name.get(name) or self.by_label.get(name) or []
        if len(found) > 1:
            raise OpError(f"Requirement '{name}' is ambiguous: " + ", ".join(e.label for e in found)
                          + " (write it Category/Name)")
        if found:
            chal = linked_challenge(found[0])
            if not chal:
                raise OpError(f"Requirement '{name}' ({found[0].path}) is not in CTFd yet: ingest it first")
            return [chal.id]
        matches = Challenges.query.filter_by(name=name).all()
        if len(matches) > 1:
            raise OpError(f"Requirement '{name}' is ambiguous: {len(matches)} CTFd challenges have that name")
        if not matches:
            raise OpError(f"Requirement '{name}' not found in CTFd: ingest it first")
        return [matches[0].id]


def resolve_requirements(data, resolver, challenge_id=None) -> list:
    ids = []
    for req in requirement_prereqs(data):
        if isinstance(req, bool):
            raise OpError(f"Invalid requirement: {req!r}")
        if isinstance(req, int):
            if not Challenges.query.filter_by(id=req).first():
                raise OpError(f"Requirement ID {req} not found in CTFd")
            found = [req]
        else:
            found = resolver.resolve(str(req))
        for resolved in found:
            if challenge_id is not None and resolved == challenge_id:
                raise OpError("A challenge cannot require itself")
            if resolved not in ids:
                ids.append(resolved)
    return ids


def requirement_graph() -> dict:
    """CTFd's prerequisites: challenge ID → the IDs it requires."""
    return {chal.id: list((chal.requirements or {}).get("prerequisites") or [])
            for chal in Challenges.query.filter(Challenges.requirements.isnot(None)).all()}


def requirement_cycle(graph: dict, start: int) -> list:
    """The IDs of a path start → … → start through graph (challenge ID → IDs
    it requires), [] when start is in no cycle."""
    seen, path = set(), [start]

    def visit(node):
        for nxt in graph.get(node) or []:
            if nxt == start:
                return True
            if nxt in seen:
                continue
            seen.add(nxt)
            path.append(nxt)
            if visit(nxt):
                return True
            path.pop()
        return False

    return path + [start] if visit(start) else []


def set_requirements(data, challenge, resolver, log, clear_when_empty=True):
    ids = resolve_requirements(data, resolver, challenge.id)
    if not ids and not clear_when_empty:
        return
    store_requirements(challenge, ids, requirement_anonymize(data), log)


def store_requirements(challenge, ids, anonymize, log):
    challenge.requirements = {"prerequisites": ids, "anonymize": anonymize} if ids else None
    db.session.commit()
    log.debug(f"Requirements: {ids or 'none'}")


# ── Install / sync / delete ──────────────────────────────────────────────────

def check_deployable(entry: Entry):
    """A zync challenge must be one Galvanize deploys: one it did not index (a
    duplicate of another definition's category/name, or invalid deployment
    settings) would give players another challenge's instance, or none."""
    if entry.type == "zync" and not entry.deployable:
        raise OpError("Galvanize cannot deploy this definition (see the skipped definitions: duplicate "
                      "category/name, or invalid deploy_parameters)")


def check(entry: Entry):
    """Validation that needs no CTFd write (used by dry runs too)."""
    check_deployable(entry)
    state = entry.data.get("state")
    if state is not None and state not in STATES:
        raise OpError(f"Invalid state {state!r}: use {', '.join(STATES)}")
    payload = build_payload(entry.data)
    _chal_class(payload["type"])
    desired_flags(entry.data)
    declared_hints(entry.data)
    resolved_files(entry)
    return payload


def install(entry: Entry, client, resolver, log):
    """Create the challenge. Returns its ID, or EXISTS when it is already in
    CTFd. The challenge stays hidden until everything is attached, and is
    deleted again on failure."""
    data = entry.data
    payload = check(entry)
    existing = linked_challenge(entry)
    if existing and is_installing(entry):
        # An install died halfway (worker restarted): finish it from scratch
        log.warning(f"'{existing.name}' (#{existing.id}) was left incomplete by an interrupted install: "
                    f"deleted and installed again")
        delete(existing, log, force=True)
        existing = None
    if existing:
        return EXISTS

    files = resolved_files(entry)
    resolve_requirements(data, resolver)
    wanted_state = str(data.get("state") or "visible")
    payload["state"] = "hidden"
    _validate(payload)
    chal_class = _chal_class(payload["type"])

    challenge = _with_request(payload, chal_class.create)
    challenge_id = challenge.id
    link(entry, challenge_id, installing=True)
    log.debug(f"Created challenge #{challenge_id}")

    step = "flags"
    try:
        add_flags(data, challenge_id, log)
        step = "files"
        upload_files(files, challenge_id, client, log)
        step = "hints"
        apply_hints(data, challenge_id, log)
        step = "tags"
        set_tags(data, challenge_id, log)
        step = "topics"
        set_topics(data, challenge_id, log)
        step = "requirements"
        set_requirements(data, Challenges.query.filter_by(id=challenge_id).first(), resolver, log,
                         clear_when_empty=False)
        step = "state"
        chal = Challenges.query.filter_by(id=challenge_id).first()
        chal.state = wanted_state
        db.session.commit()
        link(entry, challenge_id, installing=False)
    except Exception as e:
        db.session.rollback()
        log.error(f"Failed to attach {step}: {e} — rolling back")
        chal = Challenges.query.filter_by(id=challenge_id).first()
        if chal:
            delete(chal, log, force=True)
            log.warning(f"Rolled back: '{payload['name']}' removed from CTFd")
        raise OpError(f"Install failed at step '{step}'")
    finally:
        clear_caches()
    return challenge_id


def sync(entry: Entry, client, log) -> int:
    """Update an existing challenge in place (its ID never changes, so other
    challenges' prerequisites stay valid). The state is never changed, so a
    hidden challenge is not leaked. Requirements are set by
    sync_requirements in a second pass."""
    data = entry.data
    check_deployable(entry)
    payload = build_payload(data, for_update=True)
    desired_flags(data)
    declared_hints(data)
    challenge = linked_challenge(entry)
    if not challenge:
        raise OpError("Not in CTFd: ingest it first")
    if is_installing(entry):
        raise OpError("Left incomplete by an interrupted install: ingest it again")
    link(entry, challenge.id)

    wanted_type = payload.pop("type")
    if wanted_type != challenge.type:
        log.warning(f"Type changed ({challenge.type} → {wanted_type}): CTFd cannot convert a challenge, "
                    f"delete and ingest it again to change its type")
    _validate(payload)
    chal_class = _chal_class(challenge.type)
    challenge_id = challenge.id
    _with_request(payload, lambda req: chal_class.update(challenge, req))

    problems = []
    for label, fn in (
        ("flags", lambda: sync_flags(data, challenge_id, log)),
        ("hints", lambda: sync_hints(data, challenge_id, log)),
        ("tags", lambda: set_tags(data, challenge_id, log)),
        ("topics", lambda: set_topics(data, challenge_id, log)),
        ("files", lambda: sync_files(entry, challenge_id, client, log)),
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — keep syncing the other parts
            db.session.rollback()
            problems.append(label)
            log.warning(f"Failed to sync {label}: {e}")
    clear_caches()
    if problems:
        raise OpError(f"Partially synced (failed: {', '.join(problems)})")
    return challenge_id


def sync_requirements(entry: Entry, planned, log):
    """Set the requirements plan_requirements planned for entry (raises its
    error)."""
    if isinstance(planned, Exception):
        raise planned
    challenge = linked_challenge(entry)
    if not challenge:
        raise OpError("Not in CTFd while setting requirements")
    store_requirements(challenge, planned, requirement_anonymize(entry.data), log)


def plan_requirements(entries, resolver) -> dict:
    """The requirements a sync sets: entry path → the prerequisite IDs, or
    the OpError that refuses them (the challenge keeps its current ones).

    CTFd accepts circular requirements (A requires B, B requires A), and the
    challenges would never unlock: requirements that would close a cycle, in
    CTFd's prerequisites as the sync leaves them, are refused. A refused
    challenge keeps CTFd's requirements, which may close another cycle, so
    the check is repeated until nothing more is refused, and the sync never
    leaves a new cycle, whatever the order of the definitions. Only the
    challenges whose requirements change are refused: keeping a challenge's
    current requirements changes nothing. A cycle already in CTFd that the
    definitions keep is reported on its challenges."""
    current = requirement_graph()
    planned, results = {}, {}
    for entry in entries:
        challenge = linked_challenge(entry)
        if not challenge:
            results[entry.path] = OpError("Not in CTFd while setting requirements")
            continue
        try:
            planned[entry.path] = (challenge.id, resolve_requirements(entry.data, resolver, challenge.id))
        except OpError as e:
            results[entry.path] = e  # stays as it is in CTFd
    refused = {}  # path → (cycle, the graph it was found in)
    while True:
        graph = dict(current)
        graph.update({cid: ids for path, (cid, ids) in planned.items() if path not in refused})
        found = {}
        for path, (cid, ids) in planned.items():
            if path in refused or set(ids) == set(current.get(cid) or []):
                continue
            cycle = requirement_cycle(graph, cid)
            if cycle:
                found[path] = cycle
        if not found:
            break
        refused.update(found)
    names = {c.id: c.name for c in Challenges.query.with_entities(Challenges.id, Challenges.name).all()}

    def show(ids):
        return " → ".join(f"'{names.get(i, i)}'" for i in ids)

    for path, (cid, ids) in planned.items():
        if path in refused:
            kept = current.get(cid) or []
            results[path] = OpError(
                f"Circular requirements: {show(refused[path])}. '{names.get(cid, cid)}' keeps its current "
                f"requirements ({', '.join(repr(names.get(i, i)) for i in kept) or 'none'})")
            continue
        cycle = requirement_cycle(graph, cid)
        if cycle:
            # Unchanged, in a cycle CTFd already has
            results[path] = OpError(f"Circular requirements, already in CTFd: {show(cycle)}")
            continue
        results[path] = ids
    return results


def dependents(challenge_id: int, excluded=()) -> list:
    """Challenges listing challenge_id among their prerequisites."""
    found = []
    for chal in Challenges.query.filter(Challenges.requirements.isnot(None)).all():
        if chal.id == challenge_id or chal.id in excluded:
            continue
        prereqs = (chal.requirements or {}).get("prerequisites") or []
        if challenge_id in prereqs:
            found.append(chal)
    return found


def delete(challenge, log, force=False, excluded=()):
    """Delete a challenge with its flags, files, hints, tags, solves...

    Refused when other challenges require it, since CTFd keeps prerequisites
    as bare IDs: deleting it would silently unlock them. force overrides.
    """
    deps = dependents(challenge.id, excluded)
    if deps:
        names = ", ".join(f"'{d.name}'" for d in deps)
        if not force:
            raise OpError(f"Prerequisite of {names}: remove those requirements first, or use force")
        log.warning(f"'{challenge.name}' is a prerequisite of {names} (deleted anyway: force)")
    challenge_id = challenge.id
    ChallengeTopics.query.filter_by(challenge_id=challenge_id).delete()
    db.session.commit()
    _chal_class(challenge.type).delete(challenge)
    # The link goes last: if the deletion fails, the challenge keeps it
    ZyncChallengeSource.query.filter_by(challenge_id=challenge_id).delete()
    db.session.commit()
    clear_caches()
