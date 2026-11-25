"""Challenge definitions, as Galvanize lists them, and their link to CTFd."""
import posixpath
from dataclasses import dataclass, field
from datetime import datetime

from CTFd.models import Challenges, db
from sqlalchemy import text


@dataclass
class Entry:
    """One challenge definition file of a Galvanize repository."""

    path: str  # definition file, relative to Galvanize's challenge_dir: the stable identity
    repo: str
    dir: str
    category: str
    name: str
    type: str
    deployable: bool
    build: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    files: list = field(default_factory=list)  # declared files, as Galvanize resolved them
    commit: str = None  # repository commit when Galvanize listed the definition

    @property
    def key(self) -> str:
        return self.path

    @property
    def folder(self) -> str:
        return posixpath.basename(self.dir)

    @property
    def label(self) -> str:
        return f"{self.category}/{self.name}"

    def requirement_names(self) -> list:
        return [r for r in requirement_prereqs(self.data) if isinstance(r, str)]

    @classmethod
    def from_api(cls, item: dict):
        return cls(path=item["path"], repo=item.get("repository", ""), dir=item["dir"], category=item["category"],
                   name=item["name"], type=item["type"], deployable=bool(item.get("deployable")),
                   build=item.get("build") or {}, data=item.get("definition") or {},
                   files=item.get("files") or [], commit=item.get("commit"))


def requirement_prereqs(data: dict) -> list:
    """`requirements` is either a list or {prerequisites: [...], anonymize: bool}."""
    reqs = data.get("requirements") or []
    if isinstance(reqs, dict):
        reqs = reqs.get("prerequisites") or []
    return [r for r in reqs if r is not None and r != ""] if isinstance(reqs, list) else []


def requirement_anonymize(data: dict) -> bool:
    reqs = data.get("requirements")
    return bool(reqs.get("anonymize", False)) if isinstance(reqs, dict) else False


def load(client, repo):
    """(entries, skipped) of a repository."""
    listing = client.challenges(repo)
    entries = [Entry.from_api(item) for item in listing.get("challenges") or []]
    return entries, listing.get("skipped") or []


def commit_of(entries):
    """The repository commit Galvanize gave with these entries, if any."""
    return next((e.commit for e in entries if e.commit), None)


def current_commit(client, repo):
    """The repository's commit now (from Galvanize's listing)."""
    items = client.challenges(repo).get("challenges") or []
    return next((i.get("commit") for i in items if i.get("commit")), None)


# ── Link between a definition and its CTFd challenge ────────────────────────

class ZyncChallengeSource(db.Model):
    """Which definition file a CTFd challenge was ingested from.

    The definition path is the identity: two challenges may share a name in
    different categories, and a renamed challenge stays the same challenge.
    """

    __tablename__ = "zync_challenge_sources"

    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True)
    path = db.Column(db.String(512), unique=True, nullable=False)
    repo = db.Column(db.String(255))
    # Set while an install attaches flags, files...: still set afterwards
    # means the worker died in the middle, and the challenge is incomplete
    installing = db.Column(db.Boolean, nullable=False, default=False, server_default=text("0"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def _source(entry: Entry):
    return ZyncChallengeSource.query.filter_by(path=entry.path).first()


def linked_challenge(entry: Entry):
    """The CTFd challenge of a definition: the one linked to its path, else
    one with the same name and category that no other definition owns (a
    challenge ingested by the old script or by hand), which gets adopted.
    Read-only: a link whose challenge is gone is replaced by the next link()."""
    source = _source(entry)
    if source:
        chal = Challenges.query.filter_by(id=source.challenge_id).first()
        if chal:
            return chal
    candidates = Challenges.query.filter_by(name=entry.name, category=entry.category).order_by(Challenges.id).all()
    if not candidates:
        return None
    owned = {s.challenge_id for s in ZyncChallengeSource.query.filter(
        ZyncChallengeSource.challenge_id.in_([c.id for c in candidates])).all()}
    for chal in candidates:
        if chal.id not in owned:
            return chal
    return None


def is_installing(entry: Entry) -> bool:
    """The definition's challenge was left incomplete by an interrupted install."""
    source = _source(entry)
    return bool(source and source.installing)


def link(entry: Entry, challenge_id: int, installing: bool = False):
    source = _source(entry)
    if source and source.challenge_id != challenge_id:
        db.session.delete(source)
        db.session.flush()
        source = None
    if not source:
        ZyncChallengeSource.query.filter_by(challenge_id=challenge_id).delete()
        db.session.add(ZyncChallengeSource(challenge_id=challenge_id, path=entry.path, repo=entry.repo,
                                           installing=installing))
    else:
        source.repo = entry.repo
        source.installing = installing
    db.session.commit()


def relink_moved(entries, log, dry_run=False) -> dict:
    """Follow definitions that moved inside their repository (folder or file
    renamed): a definition without a link takes over the link of a challenge
    of the same repository, name and category whose definition path Galvanize
    no longer lists. entries must be every entry of the repositories involved.
    Returns {new path: challenge} for what was (or, in a dry run, would be)
    relinked."""
    known = {e.path for e in entries}
    moved = {}
    by_repo = {}
    for e in entries:
        by_repo.setdefault(e.repo, []).append(e)
    for repo, repo_entries in by_repo.items():
        orphans = [s for s in ZyncChallengeSource.query.filter_by(repo=repo).all() if s.path not in known]
        if not orphans:
            continue
        challenges = {c.id: c for c in Challenges.query.filter(
            Challenges.id.in_([s.challenge_id for s in orphans])).all()}
        for entry in repo_entries:
            if _source(entry):
                continue
            matches = [s for s in orphans if (c := challenges.get(s.challenge_id))
                       and c.name == entry.name and c.category == entry.category]
            same_name = [e for e in repo_entries if e.name == entry.name and e.category == entry.category]
            if len(matches) != 1 or len(same_name) != 1:
                continue  # ambiguous: left alone (ingest would then create a new challenge)
            source = matches[0]
            chal = challenges[source.challenge_id]
            log.warning(f"[{entry.label}] {'Would follow' if dry_run else 'Followed'} its move: "
                        f"{source.path} → {entry.path} (#{chal.id})")
            moved[entry.path] = chal
            orphans.remove(source)
            if not dry_run:
                source.path = entry.path
                db.session.commit()
    return moved


def sources_by_challenge() -> dict:
    return {s.challenge_id: s.path for s in ZyncChallengeSource.query.all()}


def orphan_entries(repo, known_paths) -> list:
    """Entries for the CTFd challenges ingested from `repo` whose definition
    Galvanize no longer lists (removed or renamed file, removed repository),
    so that they can still be deleted."""
    out = []
    for source in ZyncChallengeSource.query.filter_by(repo=repo).all():
        if source.path in known_paths:
            continue
        chal = Challenges.query.filter_by(id=source.challenge_id).first()
        if chal:
            out.append(Entry(path=source.path, repo=repo, dir=posixpath.dirname(source.path), category=chal.category,
                             name=chal.name, type=chal.type, deployable=False))
    return out


def linked_repos() -> list:
    return sorted({s.repo for s in ZyncChallengeSource.query.all() if s.repo is not None})
