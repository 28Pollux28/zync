"""Ingest, sync and delete against a real CTFd, with a fake Galvanize."""
import pytest

from CTFd.models import ChallengeFiles, Challenges, ChallengeTopics, Flags, Hints, Tags, db
from CTFd.plugins.zync.manager import actions, catalog, ctfd_ops

INTRO = {
    "name": "Intro", "category": "Misc", "type": "standard", "value": 50, "description": "Welcome",
    "attempts": 3, "connection_info": "nc host 1337",
    "flags": [" FLAG{intro} ", {"type": "regex", "content": "FLAG\\{intro-[0-9]+\\}", "data": "case_insensitive"}],
    "files": ["notes.txt"],
    "hints": ["Free hint", {"key": "h1", "title": "Paid", "content": "costly", "cost": 10},
              {"key": "h2", "content": "gated", "cost": 5, "requirements": ["h1"]}],
    "tags": ["easy", "intro"], "topics": ["basics"],
}
FOLLOWUP = {"name": "Followup", "category": "Misc", "type": "standard", "value": 100, "flags": ["F{f}"],
            "requirements": {"prerequisites": ["Intro"], "anonymize": True}}
DYN = {"name": "Dyn", "category": "Crypto", "type": "dynamic", "value": 500, "minimum": 50, "decay": 20,
       "flags": ["F{d}"], "requirements": ["Followup"]}


def entries(galvanize, repo="Repo"):
    return catalog.load(galvanize, repo)[0]


def entry(galvanize, name):
    return next(e for e in entries(galvanize) if e.name == name)


@pytest.fixture
def repo(galvanize):
    galvanize.add("Repo", "misc", "intro", INTRO, files={"files/notes.txt": "some notes"})
    galvanize.add("Repo", "misc", "followup", FOLLOWUP)
    galvanize.add("Repo", "crypto", "dyn", DYN)
    return galvanize


def ingest(app, galvanize, log, keys=None, dry_run=False):
    with app.app_context():
        return actions.ingest(log, "Repo", keys=keys, dry_run=dry_run)


# ── Ingest ───────────────────────────────────────────────────────────────────

def test_ingest_creates_everything_in_dependency_order(app, repo, log):
    result = ingest(app, repo, log)
    assert result["failed"] == [], log.text()
    assert result["installed"] == ["Misc/Intro", "Misc/Followup", "Crypto/Dyn"]

    with app.app_context():
        intro = Challenges.query.filter_by(name="Intro").one()
        assert intro.state == "visible"
        assert (intro.value, intro.max_attempts, intro.connection_info) == (50, 3, "nc host 1337")
        flags = {(f.type, f.content, f.data) for f in Flags.query.filter_by(challenge_id=intro.id)}
        assert flags == {("static", "FLAG{intro}", None), ("regex", "FLAG\\{intro-[0-9]+\\}", "case_insensitive")}
        files = ChallengeFiles.query.filter_by(challenge_id=intro.id).all()
        assert [f.location.split("/")[-1] for f in files] == ["notes.txt"]
        hints = Hints.query.filter_by(challenge_id=intro.id).order_by(Hints.id).all()
        assert [h.content for h in hints] == ["Free hint", "costly", "gated"]
        assert hints[2].requirements == {"prerequisites": [hints[1].id]}
        assert sorted(t.value for t in Tags.query.filter_by(challenge_id=intro.id)) == ["easy", "intro"]
        assert ChallengeTopics.query.filter_by(challenge_id=intro.id).count() == 1

        followup = Challenges.query.filter_by(name="Followup").one()
        assert followup.requirements == {"prerequisites": [intro.id], "anonymize": True}
        dyn = Challenges.query.filter_by(name="Dyn").one()
        assert (dyn.type, dyn.initial, dyn.minimum, dyn.decay) == ("dynamic", 500, 50, 20)
        assert dyn.requirements["prerequisites"] == [followup.id]
        # Each challenge is linked to its definition file
        assert catalog.sources_by_challenge()[intro.id] == "Repo/misc/intro/challenge.yml"


def test_ingest_skips_existing(app, repo, log):
    ingest(app, repo, log)
    result = ingest(app, repo, log)
    assert result["installed"] == []
    assert len(result["existing"]) == 3
    with app.app_context():
        assert Challenges.query.count() == 3


def test_same_name_in_two_categories_are_two_challenges(app, galvanize, log):
    galvanize.add("Repo", "web", "login", {"name": "login", "category": "Web", "flags": ["a"]})
    galvanize.add("Repo", "pwn", "login", {"name": "login", "category": "Pwn", "flags": ["b"]})
    result = ingest(app, galvanize, log)
    assert sorted(result["installed"]) == ["Pwn/login", "Web/login"]
    with app.app_context():
        assert Challenges.query.filter_by(name="login").count() == 2


def test_ingest_adopts_a_challenge_created_by_hand(app, repo, log):
    with app.app_context():
        db.session.add(Challenges(name="Intro", category="Misc", value=1, type="standard", state="hidden"))
        db.session.commit()
    result = ingest(app, repo, log, keys=["Repo/misc/intro/challenge.yml"])
    assert result["existing"] == ["Misc/Intro"]
    with app.app_context():
        actions.sync(log, "Repo", keys=["Repo/misc/intro/challenge.yml"])
        intro = Challenges.query.filter_by(name="Intro").one()
        assert intro.value == 50, "synced from the definition"
        assert intro.state == "hidden", "sync never changes the state"


def test_install_failure_rolls_back(app, repo, log, monkeypatch):
    repo.files.clear()  # notes.txt missing in Galvanize
    result = ingest(app, repo, log, keys=["Repo/misc/intro/challenge.yml"])
    assert result["failed"] == ["Misc/Intro"]
    assert "File notes.txt: not found" in log.text("ERROR")

    # A failure after the challenge was created: it is removed again
    repo.files["Repo/misc/intro/files/notes.txt"] = b"notes"
    monkeypatch.setattr(ctfd_ops, "set_topics", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    result = ingest(app, repo, log, keys=["Repo/misc/intro/challenge.yml"])
    assert result["failed"] == ["Misc/Intro"]
    with app.app_context():
        assert Challenges.query.count() == 0
        assert catalog.ZyncChallengeSource.query.count() == 0


def test_missing_requirement_fails_before_creating(app, repo, log):
    result = ingest(app, repo, log, keys=["Repo/misc/followup/challenge.yml"])
    assert result["failed"] == ["Misc/Followup"]
    assert "'Intro' (Repo/misc/intro/challenge.yml) is not in CTFd yet" in log.text("ERROR")
    with app.app_context():
        assert Challenges.query.count() == 0


def test_ambiguous_requirement_is_refused(app, galvanize, log):
    galvanize.add("Other", "web", "login", {"name": "login", "category": "Web", "flags": ["a"]})
    galvanize.add("Other", "pwn", "login", {"name": "login", "category": "Pwn", "flags": ["b"]})
    galvanize.add("Repo", "misc", "next", {"name": "Next", "category": "Misc", "flags": ["c"], "requirements": ["login"]})
    with app.app_context():
        actions.ingest(log, "Other")
    result = ingest(app, galvanize, log)
    assert result["failed"] == ["Misc/Next"]
    assert "ambiguous" in log.text("ERROR")


def test_dry_run_changes_nothing_and_reports_every_problem(app, galvanize, log):
    galvanize.add("Repo", "misc", "ok", {"name": "Ok", "category": "Misc", "flags": ["a"]})
    galvanize.add("Repo", "misc", "badflag", {"name": "Bad flag", "category": "Misc", "flags": [{"type": "nope", "content": "x"}]})
    galvanize.add("Repo", "misc", "badhint", {"name": "Bad hint", "category": "Misc",
                                              "hints": [{"content": "x", "requirements": ["missing"]}]})
    galvanize.add("Repo", "misc", "nofile", {"name": "No file", "category": "Misc", "files": ["gone.txt"]})
    galvanize.add("Repo", "misc", "escape", {"name": "Escape", "category": "Misc", "files": ["../../../etc/passwd"]})
    result = ingest(app, galvanize, log, dry_run=True)
    assert result["installed"] == ["Misc/Ok"]
    assert sorted(result["failed"]) == ["Misc/Bad flag", "Misc/Bad hint", "Misc/Escape", "Misc/No file"]
    with app.app_context():
        assert Challenges.query.count() == 0


def test_circular_dependencies_are_reported(app, galvanize, log):
    galvanize.add("Repo", "a", "a", {"name": "A", "category": "X", "requirements": ["B"]})
    galvanize.add("Repo", "b", "b", {"name": "B", "category": "X", "requirements": ["A"]})
    result = ingest(app, galvanize, log)
    assert sorted(result["failed"]) == ["X/A", "X/B"]
    assert "Circular dependency" in log.text("ERROR")


def test_created_hidden_until_complete(app, repo, log, monkeypatch):
    states = []
    original = ctfd_ops.add_flags

    def spy(data, challenge_id, log_):
        states.append(Challenges.query.filter_by(id=challenge_id).one().state)
        return original(data, challenge_id, log_)

    monkeypatch.setattr(ctfd_ops, "add_flags", spy)
    ingest(app, repo, log, keys=["Repo/misc/intro/challenge.yml"])
    assert states == ["hidden"]


# ── Sync ─────────────────────────────────────────────────────────────────────

def test_sync_without_changes_keeps_everything(app, repo, log):
    ingest(app, repo, log)
    with app.app_context():
        intro = Challenges.query.filter_by(name="Intro").one()
        before = ({f.id for f in Flags.query.filter_by(challenge_id=intro.id)},
                  [h.id for h in Hints.query.filter_by(challenge_id=intro.id).order_by(Hints.id)],
                  [f.id for f in ChallengeFiles.query.filter_by(challenge_id=intro.id)])
    repo.downloads.clear()
    with app.app_context():
        result = actions.sync(log, "Repo")
        assert result["failed"] == [] and result["requirements_failed"] == [], log.text()
        intro = Challenges.query.filter_by(name="Intro").one()
        after = ({f.id for f in Flags.query.filter_by(challenge_id=intro.id)},
                 [h.id for h in Hints.query.filter_by(challenge_id=intro.id).order_by(Hints.id)],
                 [f.id for f in ChallengeFiles.query.filter_by(challenge_id=intro.id)])
    assert after == before
    assert repo.downloads == [], "unchanged files are not downloaded again"
    assert repo.checksums == ["Repo/misc/intro/files/notes.txt"], "only the resolved file is hashed, on sync"


def test_sync_applies_changes_in_place(app, repo, log):
    ingest(app, repo, log)
    d = repo.definition("Intro")
    d["value"] = 75
    d["flags"] = ["FLAG{intro}", "FLAG{v2}"]
    d["hints"][1]["content"] = "costly v2"
    d["tags"] = ["medium"]
    del d["attempts"], d["connection_info"]
    d["extra"] = {"state": "hidden", "name": "Hacked", "attribution": "someone"}
    repo.files["Repo/misc/intro/files/notes.txt"] = b"new notes"
    with app.app_context():
        intro = Challenges.query.filter_by(name="Intro").one()
        intro_id = intro.id
        kept_flag = Flags.query.filter_by(challenge_id=intro_id, content="FLAG{intro}").one().id
        hint_ids = [h.id for h in Hints.query.filter_by(challenge_id=intro_id).order_by(Hints.id)]
        actions.sync(log, "Repo", keys=["Repo/misc/intro/challenge.yml"])

        intro = Challenges.query.filter_by(id=intro_id).one()
        assert (intro.value, intro.max_attempts, intro.connection_info) == (75, 0, None), "removed fields are cleared"
        assert intro.name == "Intro" and intro.state == "visible", "extra cannot change identity or state"
        assert intro.attribution == "someone", "other extra fields pass through"
        flags = Flags.query.filter_by(challenge_id=intro_id).all()
        assert {f.content for f in flags} == {"FLAG{intro}", "FLAG{v2}"}
        assert kept_flag in {f.id for f in flags}
        hints = Hints.query.filter_by(challenge_id=intro_id).order_by(Hints.id).all()
        assert [h.id for h in hints] == hint_ids, "hints keep their IDs (players' unlocks)"
        assert hints[1].content == "costly v2"
        assert [t.value for t in Tags.query.filter_by(challenge_id=intro_id)] == ["medium"]
        sha = [f.sha1sum for f in ChallengeFiles.query.filter_by(challenge_id=intro_id)]
        assert len(sha) == 1 and sha[0] == repo.file_checksum("Repo/misc/intro/files/notes.txt")["sha1"]


def test_sync_follows_a_renamed_challenge(app, repo, log):
    ingest(app, repo, log)
    repo.definition("Intro")["name"] = "Introduction"
    repo.definitions[0]["name"] = "Introduction"
    with app.app_context():
        result = actions.sync(log, "Repo")
        assert result["failed"] == [], log.text()
        assert Challenges.query.filter_by(name="Introduction").count() == 1
        assert Challenges.query.filter_by(name="Intro").count() == 0
        assert Challenges.query.count() == 3, "no duplicate"


def test_sync_requirements_second_pass(app, repo, log):
    ingest(app, repo, log)
    del repo.definition("Followup")["requirements"]
    with app.app_context():
        actions.sync(log, "Repo")
        assert Challenges.query.filter_by(name="Followup").one().requirements is None


def test_sync_refuses_circular_requirements(app, galvanize, log):
    """Regression: sync set A requires B and B requires A (success), so
    neither could ever be unlocked; ingest refused the same definitions."""
    galvanize.add("Repo", "a", "a", {"name": "A", "category": "X", "flags": ["a"]})
    galvanize.add("Repo", "b", "b", {"name": "B", "category": "X", "flags": ["b"]})
    ingest(app, galvanize, log)
    galvanize.definition("A")["requirements"] = ["B"]
    galvanize.definition("B")["requirements"] = ["A"]
    with app.app_context():
        dry = actions.sync(log, "Repo", dry_run=True)
        assert sorted(dry["requirements_failed"]) == ["X/A", "X/B"] and dry["status"] == "warning"
        result = actions.sync(log, "Repo")
        assert sorted(result["requirements_failed"]) == ["X/A", "X/B"], log.text()
        assert result["status"] == "warning", "not success"
        assert "Circular requirements: 'A' → 'B' → 'A'" in log.text("ERROR")
        assert {c.name: c.requirements for c in Challenges.query.all()} == {"A": None, "B": None}, \
            "the challenges keep their requirements"
        # Whatever the order
        result = actions.sync(log, "Repo", keys=["Repo/b/b/challenge.yml", "Repo/a/a/challenge.yml"])
        assert sorted(result["requirements_failed"]) == ["X/A", "X/B"]


def test_sync_refuses_a_cycle_through_ctfd(app, repo, log):
    # Followup requires Intro and Dyn requires Followup in CTFd: Intro may
    # not require Dyn, even when only Intro is synced
    ingest(app, repo, log)
    repo.definition("Intro")["requirements"] = ["Dyn"]
    with app.app_context():
        result = actions.sync(log, "Repo", keys=keys("Intro"))
        assert result["requirements_failed"] == ["Misc/Intro"]
        assert "Circular requirements: 'Intro' → 'Dyn' → 'Followup' → 'Intro'" in log.text("ERROR")
        assert Challenges.query.filter_by(name="Intro").one().requirements is None
        # Dropping a requirement in the same sync breaks the cycle
        del repo.definition("Followup")["requirements"]
        result = actions.sync(log, "Repo")
        assert result["requirements_failed"] == [], log.text("ERROR")
        dyn_id = Challenges.query.filter_by(name="Dyn").one().id
        assert Challenges.query.filter_by(name="Intro").one().requirements["prerequisites"] == [dyn_id]


def test_sync_a_refused_challenge_keeps_requirements_that_may_close_another_cycle(app, galvanize, log):
    """Regression: the cycles were looked for once, in the requirements as
    the definitions set them. A refused challenge keeps CTFd's, and another
    challenge could then close a cycle with them: CTFd has A requires C; the
    definitions say A requires B, B requires A (refused: A keeps C) and C
    requires A, which was accepted, leaving A ↔ C in CTFd."""
    galvanize.add("Repo", "a", "a", {"name": "A", "category": "X", "flags": ["a"], "requirements": ["C"]})
    galvanize.add("Repo", "b", "b", {"name": "B", "category": "X", "flags": ["b"]})
    galvanize.add("Repo", "c", "c", {"name": "C", "category": "X", "flags": ["c"]})
    ingest(app, galvanize, log)
    galvanize.definition("A")["requirements"] = ["B"]
    galvanize.definition("B")["requirements"] = ["A"]
    galvanize.definition("C")["requirements"] = ["A"]
    with app.app_context():
        ids = {c.name: c.id for c in Challenges.query.all()}
        dry = actions.sync(log, "Repo", dry_run=True)
        assert sorted(dry["requirements_failed"]) == ["X/A", "X/B", "X/C"], log.text()
        result = actions.sync(log, "Repo")
        assert sorted(result["requirements_failed"]) == ["X/A", "X/B", "X/C"], "the dry run says what the sync does"
        graph = ctfd_ops.requirement_graph()
        assert not any(ctfd_ops.requirement_cycle(graph, i) for i in ids.values()), graph
        assert {c.name: (c.requirements or {}).get("prerequisites") for c in Challenges.query.all()} == \
            {"A": [ids["C"]], "B": None, "C": None}, "the refused challenges keep their requirements"
        errors = log.text("ERROR")
        assert "Circular requirements: 'C' → 'A' → 'C'. 'C' keeps its current requirements (none)" in errors
        assert "'A' keeps its current requirements ('C')" in errors


def test_sync_does_not_refuse_the_unchanged_member_of_a_cycle(app, galvanize, log):
    # A requires B in CTFd and in its definition; B now requires A: only B,
    # whose requirements change, is refused (refusing A would change nothing)
    galvanize.add("Repo", "a", "a", {"name": "A", "category": "X", "flags": ["a"]})
    galvanize.add("Repo", "b", "b", {"name": "B", "category": "X", "flags": ["b"]})
    ingest(app, galvanize, log)
    galvanize.definition("A")["requirements"] = ["B"]
    with app.app_context():
        assert actions.sync(log, "Repo")["requirements_failed"] == []
    galvanize.definition("B")["requirements"] = ["A"]
    with app.app_context():
        assert actions.sync(log, "Repo", dry_run=True)["requirements_failed"] == ["X/B"]
        result = actions.sync(log, "Repo")
        assert result["requirements_failed"] == ["X/B"], log.text("ERROR")
        assert "Circular requirements: 'B' → 'A' → 'B'. 'B' keeps its current requirements (none)" in log.text("ERROR")
        assert Challenges.query.filter_by(name="B").one().requirements is None


def test_sync_reports_a_cycle_already_in_ctfd(app, galvanize, log):
    galvanize.add("Repo", "a", "a", {"name": "A", "category": "X", "flags": ["a"]})
    galvanize.add("Repo", "b", "b", {"name": "B", "category": "X", "flags": ["b"]})
    ingest(app, galvanize, log)
    with app.app_context():
        a, b = Challenges.query.filter_by(name="A").one(), Challenges.query.filter_by(name="B").one()
        a.requirements, b.requirements = {"prerequisites": [b.id]}, {"prerequisites": [a.id]}
        db.session.commit()
    galvanize.definition("A")["requirements"] = ["B"]
    galvanize.definition("B")["requirements"] = ["A"]
    with app.app_context():
        result = actions.sync(log, "Repo")
        assert sorted(result["requirements_failed"]) == ["X/A", "X/B"]
        assert "Circular requirements, already in CTFd: 'A' → 'B' → 'A'" in log.text("ERROR")


def test_requirement_cycle():
    graph = {1: [2], 2: [3, 4], 3: [], 4: [1], 5: [5]}
    assert ctfd_ops.requirement_cycle(graph, 1) == [1, 2, 4, 1]
    assert ctfd_ops.requirement_cycle(graph, 3) == []
    assert ctfd_ops.requirement_cycle(graph, 5) == [5, 5]
    assert ctfd_ops.requirement_cycle({1: [2], 2: [3], 3: [2]}, 1) == [], "a cycle not through the start"


def test_sync_reports_challenges_not_in_ctfd(app, repo, log):
    with app.app_context():
        result = actions.sync(log, "Repo")
    assert len(result["missing"]) == 3


# ── Delete ───────────────────────────────────────────────────────────────────

def keys(*names):
    paths = {"Intro": "Repo/misc/intro/challenge.yml", "Followup": "Repo/misc/followup/challenge.yml",
             "Dyn": "Repo/crypto/dyn/challenge.yml"}
    return [paths[n] for n in names]


def test_delete_refuses_prerequisites_of_remaining_challenges(app, repo, log):
    ingest(app, repo, log)
    with app.app_context():
        result = actions.delete(log, "Repo", keys=keys("Intro"))
        assert result["failed"] == ["Misc/Intro"]
        # Intro + Followup: Dyn still requires Followup, so neither can go
        result = actions.delete(log, "Repo", keys=keys("Intro", "Followup"))
        assert sorted(result["failed"]) == ["Misc/Followup", "Misc/Intro"]
        assert Challenges.query.count() == 3


def test_delete_all_and_force(app, repo, log):
    ingest(app, repo, log)
    with app.app_context():
        result = actions.delete(log, "Repo", keys=keys("Intro"), force=True)
        assert result["deleted"] == ["Misc/Intro"]
        result = actions.delete(log, "Repo")
        assert sorted(result["deleted"]) == ["Crypto/Dyn", "Misc/Followup"]
        assert Challenges.query.count() == 0
        assert Flags.query.count() == 0 and Hints.query.count() == 0
        assert catalog.ZyncChallengeSource.query.count() == 0


def test_delete_dry_run(app, repo, log):
    ingest(app, repo, log)
    with app.app_context():
        result = actions.delete(log, "Repo", dry_run=True)
        assert len(result["deleted"]) == 3
        assert Challenges.query.count() == 3


def test_challenges_whose_definition_is_gone_can_still_be_deleted(app, repo, log):
    ingest(app, repo, log)
    repo.definitions.clear()  # e.g. the repository was removed from Galvanize (404)
    with app.app_context():
        orphans = catalog.orphan_entries("Repo", set())
        assert len(orphans) == 3
        assert actions.sync(log, "Repo")["status"] == "failed", "only delete works without the repository"
        result = actions.delete(log, "Repo")
        assert len(result["deleted"]) == 3
        assert Challenges.query.count() == 0
        assert "Repository 'Repo' is no longer in Galvanize" in log.text("WARNING")


def test_listing_when_galvanize_does_not_answer(app, repo, log):
    """Regression: with Galvanize unreachable, every challenge of the
    repository was listed as gone from it (orphan), without an error."""
    from tests.helpers import login_as_user

    ingest(app, repo, log)
    repo.unreachable = True
    resp = login_as_user(app, name="admin", password="password").get("/admin/zync_challenges/api/challenges?repo=Repo")
    assert resp.status_code == 502
    assert "Cannot reach Galvanize" in resp.get_json()["error"]


def test_listing_of_a_repository_gone_from_galvanize(app, repo, log):
    from tests.helpers import login_as_user

    ingest(app, repo, log)
    repo.definitions.clear()
    rows = login_as_user(app, name="admin", password="password").get("/admin/zync_challenges/api/challenges?repo=Repo").get_json()["data"]["challenges"]
    assert len(rows) == 3 and all(r["orphan"] for r in rows), "its CTFd challenges are listed, to be deleted"


def test_delete_leaves_other_challenges_alone(app, repo, log):
    ingest(app, repo, log)
    with app.app_context():
        db.session.add(Challenges(name="Manual", category="Misc", value=1, type="standard", state="visible"))
        db.session.commit()
        actions.delete(log, "Repo")
        assert [c.name for c in Challenges.query.all()] == ["Manual"]


# ── Rebuild + ingest ─────────────────────────────────────────────────────────

def test_all_builds_then_ingests_only_what_built(app, galvanize, log, monkeypatch):
    monkeypatch.setattr(actions, "BUILD_POLL_SECONDS", 0)
    build = {"kind": "image", "images": ["x"]}
    galvanize.add("Repo", "web", "ok", {"name": "Ok", "category": "Web", "type": "zync", "flags": ["a"]}, build=build)
    galvanize.add("Repo", "web", "ko", {"name": "Ko", "category": "Web", "type": "zync", "flags": ["b"]}, build=build)
    galvanize.add("Repo", "misc", "static", {"name": "Static", "category": "Misc", "flags": ["c"]})
    galvanize.build_result = {"built": ["Repo/web/ok/challenge.yml"], "failed": ["Repo/web/ko/challenge.yml"],
                              "skipped": []}
    with app.app_context():
        result = actions.all_(log, "Repo", force=True)
    assert galvanize.builds[0]["no_cache"] is True
    assert galvanize.builds[0]["challenges"] == [{"path": "Repo/web/ok/challenge.yml"},
                                                 {"path": "Repo/web/ko/challenge.yml"}]
    assert result["build_failed"] == ["Web/Ko"]
    assert sorted(result["installed"]) == ["Misc/Static", "Web/Ok"]
    assert "Galvanize: building" in log.text()


def test_all_sends_every_deployable_challenge_and_skips_refused_builds(app, galvanize, log, monkeypatch):
    # Galvanize decides what to build: a refused plan comes back failed, a
    # pulled image skipped, and what it does not report is taken as failed
    monkeypatch.setattr(actions, "BUILD_POLL_SECONDS", 0)
    galvanize.add("Repo", "web", "ok", {"name": "Ok", "category": "Web", "type": "zync", "flags": ["a"]},
                  build={"kind": "image", "images": ["ok"]})
    galvanize.add("Repo", "web", "evil", {"name": "Evil", "category": "Web", "type": "zync", "flags": ["b"]},
                  build={"kind": "none", "images": ["traefik"], "refused": True,
                         "reason": "refused: traefik is a platform image"})
    galvanize.add("Repo", "web", "pulled", {"name": "Pulled", "category": "Web", "type": "zync", "flags": ["c"]})
    galvanize.add("Repo", "misc", "static", {"name": "Static", "category": "Misc", "flags": ["d"]})
    with app.app_context():
        dry = actions.all_(log, "Repo", dry_run=True)
        assert dry["build_failed"] == ["Web/Evil"] and "Web/Evil" not in dry["installed"]
        assert not galvanize.builds, "a dry run builds nothing"
        result = actions.all_(log, "Repo")
        assert sorted(r["path"] for r in galvanize.builds[0]["challenges"]) == [
            "Repo/web/evil/challenge.yml", "Repo/web/ok/challenge.yml", "Repo/web/pulled/challenge.yml"]
        assert result["build_failed"] == ["Web/Evil"]
        assert sorted(result["installed"]) == ["Misc/Static", "Web/Ok", "Web/Pulled"]
        assert not Challenges.query.filter_by(name="Evil").first()

        galvanize.build_result = {"built": [], "failed": [], "skipped": []}  # e.g. Galvanize lost the result
        galvanize.add("Repo", "web", "new", {"name": "New", "category": "Web", "type": "zync", "flags": ["e"]},
                      build={"kind": "image", "images": ["new"]})
        assert "Web/New" in actions.all_(log, "Repo")["build_failed"]


def test_all_ingests_nothing_built_by_a_failed_or_lost_build(app, galvanize, log, monkeypatch):
    # A challenge requiring one whose build failed is not ingested either, and
    # a build operation that failed without a result counts as failed
    monkeypatch.setattr(actions, "BUILD_POLL_SECONDS", 0)
    galvanize.add("Repo", "web", "ko", {"name": "Ko", "category": "Web", "type": "zync", "flags": ["a"]},
                  build={"kind": "image", "images": ["ko"]})
    galvanize.add("Repo", "misc", "next", {"name": "Next", "category": "Misc", "flags": ["b"], "requirements": ["Ko"]})
    galvanize.build_result = {"built": [], "failed": ["Repo/web/ko/challenge.yml"], "skipped": []}
    with app.app_context():
        result = actions.all_(log, "Repo")
        assert result["build_failed"] == ["Web/Ko"]
        assert result["installed"] == [] and result["failed"] == ["Misc/Next"]
        assert "Requirement 'Ko'" in log.text("ERROR")
        assert Challenges.query.count() == 0

        galvanize.operation = lambda op_id, offset=0: {"id": op_id, "status": "failed", "lines": [], "line_count": 0}
        assert actions.all_(log, "Repo")["build_failed"] == ["Web/Ko"]
        assert Challenges.query.count() == 0


# ── Review fixes (2026-11-25) ────────────────────────────────────────────────

def test_zync_definition_galvanize_cannot_deploy_is_refused(app, galvanize, log):
    # e.g. a second repository declaring the same category/name: Galvanize
    # deploys the first one only
    galvanize.add("Repo", "web", "dup", {"name": "Dup", "category": "Web", "type": "zync", "flags": ["a"]},
                  deployable=False)
    result = ingest(app, galvanize, log)
    assert result["failed"] == ["Web/Dup"]
    assert "Galvanize cannot deploy" in log.text("ERROR")
    with app.app_context():
        assert Challenges.query.count() == 0


def test_invalid_state_is_refused(app, galvanize, log):
    galvanize.add("Repo", "misc", "typo", {"name": "Typo", "category": "Misc", "flags": ["a"], "state": "hiden"})
    galvanize.add("Repo", "misc", "locked", {"name": "Locked", "category": "Misc", "flags": ["a"], "state": "locked"})
    result = ingest(app, galvanize, log)
    assert result["failed"] == ["Misc/Typo"]
    assert "Invalid state 'hiden'" in log.text("ERROR")
    with app.app_context():
        assert Challenges.query.filter_by(name="Locked").one().state == "locked"


def test_requirement_homonyms_in_the_repository_are_ambiguous(app, galvanize, log):
    galvanize.add("Repo", "web", "intro", {"name": "Intro", "category": "Web", "flags": ["a"]})
    galvanize.add("Repo", "pwn", "intro", {"name": "Intro", "category": "Pwn", "flags": ["b"]})
    galvanize.add("Repo", "misc", "next", {"name": "Next", "category": "Misc", "flags": ["c"], "requirements": ["Intro"]})
    galvanize.add("Repo", "misc", "exact", {"name": "Exact", "category": "Misc", "flags": ["d"],
                                            "requirements": ["Pwn/Intro"]})
    result = ingest(app, galvanize, log)
    assert result["failed"] == ["Misc/Next"]
    assert "ambiguous" in log.text("ERROR") and "Category/Name" in log.text("ERROR")
    with app.app_context():
        exact = Challenges.query.filter_by(name="Exact").one()
        pwn = Challenges.query.filter_by(name="Intro", category="Pwn").one()
        assert exact.requirements["prerequisites"] == [pwn.id]


def test_requirement_of_the_repository_is_never_taken_from_elsewhere(app, galvanize, log):
    # A challenge of another repository has the name, but the repository's own
    # definition of that name is not in CTFd yet
    galvanize.add("Other", "misc", "base", {"name": "Base", "category": "Misc", "flags": ["x"]})
    with app.app_context():
        actions.ingest(log, "Other")
    galvanize.add("Repo", "misc", "base", {"name": "Base", "category": "Misc", "flags": ["y"]})
    galvanize.add("Repo", "misc", "next", {"name": "Next", "category": "Misc", "flags": ["z"], "requirements": ["Base"]})
    result = ingest(app, galvanize, log, keys=["Repo/misc/next/challenge.yml"])
    assert result["failed"] == ["Misc/Next"]
    assert "not in CTFd yet" in log.text("ERROR")


def test_moved_definition_keeps_its_challenge(app, repo, log):
    ingest(app, repo, log)
    with app.app_context():
        intro_id = Challenges.query.filter_by(name="Intro").one().id
    item = next(i for i in repo.definitions if i["name"] == "Intro")
    item["path"], item["dir"] = "Repo/misc/intro-v2/challenge.yml", "Repo/misc/intro-v2"
    repo.files["Repo/misc/intro-v2/files/notes.txt"] = b"some notes"
    result = ingest(app, repo, log, dry_run=True)
    assert "Misc/Intro" in result["existing"]
    result = ingest(app, repo, log)
    assert "Misc/Intro" in result["existing"], "not installed a second time"
    assert "Followed its move" in log.text("WARNING")
    with app.app_context():
        assert Challenges.query.filter_by(name="Intro").count() == 1
        source = catalog.ZyncChallengeSource.query.filter_by(challenge_id=intro_id).one()
        assert source.path == "Repo/misc/intro-v2/challenge.yml"


def test_interrupted_install_is_redone(app, repo, log, monkeypatch):
    # The worker "dies" after the challenge was created: nothing rolls back
    class Died(BaseException):
        pass

    original = ctfd_ops.set_tags
    monkeypatch.setattr(ctfd_ops, "set_tags", lambda *a: (_ for _ in ()).throw(Died()))
    with pytest.raises(Died):
        ingest(app, repo, log, keys=["Repo/misc/intro/challenge.yml"])
    with app.app_context():
        assert catalog.is_installing(entry(repo, "Intro"))
        first_id = Challenges.query.filter_by(name="Intro").one().id
    monkeypatch.setattr(ctfd_ops, "set_tags", original)
    with app.app_context():
        result = actions.sync(log, "Repo", keys=["Repo/misc/intro/challenge.yml"])
    assert result["failed"] == ["Misc/Intro"] and "interrupted install" in log.text("ERROR")
    result = ingest(app, repo, log, keys=["Repo/misc/intro/challenge.yml"])
    assert result["installed"] == ["Misc/Intro"]
    with app.app_context():
        intro = Challenges.query.filter_by(name="Intro").one()
        assert intro.state == "visible"
        assert Challenges.query.count() == 1
    assert f"(#{first_id}) was left incomplete by an interrupted install" in log.text("WARNING")
    with app.app_context():
        intro = Challenges.query.filter_by(name="Intro").one()
        assert not catalog.is_installing(entry(repo, "Intro"))
        assert Tags.query.filter_by(challenge_id=intro.id).count() == 2


def test_repository_changing_during_the_job_is_reported(app, repo, log):
    repo.commits = {"Repo": ["abc1234", "def5678"]}
    ingest(app, repo, log)
    assert "changed during the job (abc1234 → def5678)" in log.text("WARNING")


def test_checkpoint_stops_the_job_and_cancels_the_build(app, galvanize, log, monkeypatch):
    from CTFd.plugins.zync.manager.jobs import JobStopped

    monkeypatch.setattr(actions, "BUILD_POLL_SECONDS", 0)
    galvanize.add("Repo", "web", "ok", {"name": "Ok", "category": "Web", "type": "zync", "flags": ["a"]},
                  build={"kind": "image", "images": ["x"]})
    galvanize.operation = lambda op_id, offset=0: {"id": op_id, "status": "running", "lines": [], "line_count": 0}
    calls = {"n": 0}

    def checkpoint():
        calls["n"] += 1
        if calls["n"] > 2:
            raise JobStopped("Cancelled", "cancelled")

    log.checkpoint = checkpoint
    with app.app_context(), pytest.raises(JobStopped):
        actions.all_(log, "Repo")
    assert galvanize.cancelled == ["op-1"]
    with app.app_context():
        assert Challenges.query.count() == 0


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_build_payload():
    p = ctfd_ops.build_payload({"name": "N", "category": "C", "type": "dynamic", "value": "300", "attempts": 2,
                                "extra": {"id": 9, "type": "standard", "next_id": 1, "logic": "all"}})
    assert (p["initial"], p["minimum"], p["decay"], p["value"]) == (300, 100, 450, 300)
    assert p["max_attempts"] == 2 and p["type"] == "dynamic"
    assert "id" not in p and "next_id" not in p and p["logic"] == "all"
    with pytest.raises(ctfd_ops.OpError):
        ctfd_ops.build_payload({"name": "N"})
    with pytest.raises(ctfd_ops.OpError):
        ctfd_ops.build_payload({"name": "N", "category": "C", "value": "lots"})
    update = ctfd_ops.build_payload({"name": "N", "category": "C"}, for_update=True)
    assert update["connection_info"] is None and update["max_attempts"] == 0


def test_git_lfs_pointers_are_not_published(app, galvanize, log):
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 536870912\n"
    galvanize.add("Repo", "forensic", "heap", {"name": "Heap", "category": "Forensic", "flags": ["x"],
                                               "files": ["dump.raw"]}, files={"dump.raw": pointer})
    result = ingest(app, galvanize, log)
    assert result["failed"] == ["Forensic/Heap"]
    assert "dump.raw: a Git LFS pointer" in log.text("ERROR"), "Galvanize's error is reported"
    with app.app_context():
        assert Challenges.query.count() == 0, "rolled back"
