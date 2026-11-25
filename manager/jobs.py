"""Background jobs: long operations (builds, ingests...) run outside the HTTP
request and write their log to the database, where any CTFd worker can read
it back for the admin page.

Only one job runs at a time, across all CTFd workers: a job holds the row of
zync_lock, taken with one conditional UPDATE (atomic in the database). A job
whose heartbeat stops (worker restarted, container stopped) loses the lock
after STALE_AFTER and is reported as interrupted.

Job bodies call log.checkpoint() between steps: it stops the job (raising
JobStopped) when an admin cancelled it, or when it lost the lock (it stalled
long enough for another job to take over), so two jobs never keep working on
CTFd at the same time.
"""
import json
import threading
import time
import traceback
from datetime import datetime, timedelta

from CTFd.models import db
from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

HEARTBEAT_SECONDS = 10
STALE_AFTER = timedelta(seconds=60)
MAX_LOG_CHARS = 2_000_000
FLUSH_SECONDS = 1.0

ACTIVE = ("queued", "running", "cancelling")
KEEP_JOBS = 200


class ZyncJob(db.Model):
    __tablename__ = "zync_jobs"

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(32), nullable=False)
    repo = db.Column(db.String(255))
    params = db.Column(db.Text)
    status = db.Column(db.String(16), default="queued")
    summary = db.Column(db.Text)
    # MEDIUMTEXT on MariaDB: a plain TEXT column stops at 64 KB
    log = db.Column(db.Text(16_777_215), default="")
    created_by = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    heartbeat_at = db.Column(db.DateTime)

    def effective_status(self) -> str:
        if self.status in ACTIVE:
            beat = self.heartbeat_at or self.created_at
            if beat and datetime.utcnow() - beat > STALE_AFTER:
                return "interrupted"
        return self.status

    def to_dict(self, offset: int = 0, with_log: bool = True) -> dict:
        data = {
            "id": self.id,
            "action": self.action,
            "repo": self.repo,
            "params": json.loads(self.params or "{}"),
            "status": self.effective_status(),
            "summary": json.loads(self.summary) if self.summary else None,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }
        if with_log:
            log = self.log or ""
            data["log"] = log[offset:] if offset <= len(log) else ""
            data["log_length"] = len(log)
        return data


def _iso(value):
    return value.isoformat() + "Z" if value else None


class ZyncLock(db.Model):
    """Single row (id 1): the job that may run, and its last heartbeat."""

    __tablename__ = "zync_lock"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, nullable=True)
    heartbeat_at = db.Column(db.DateTime)


_lock = ZyncLock.__table__


def _acquire(job_id: int):
    """Take the lock for job_id. Returns (acquired, previous job id)."""
    now = datetime.utcnow()
    try:
        with db.engine.begin() as conn:
            conn.execute(insert(_lock).values(id=1, job_id=None, heartbeat_at=now))
    except IntegrityError:
        pass  # the row exists
    with db.engine.begin() as conn:
        previous = conn.execute(select(_lock.c.job_id).where(_lock.c.id == 1)).scalar()
        result = conn.execute(
            update(_lock)
            .where(_lock.c.id == 1, or_(_lock.c.job_id.is_(None), _lock.c.heartbeat_at < now - STALE_AFTER))
            .values(job_id=job_id, heartbeat_at=now)
        )
        return result.rowcount == 1, previous


def _beat(conn, job_id: int) -> bool:
    """Renew the lock; False when job_id no longer holds it."""
    result = conn.execute(update(_lock).where(_lock.c.id == 1, _lock.c.job_id == job_id)
                          .values(heartbeat_at=datetime.utcnow()))
    return result.rowcount == 1


def _release(job_id: int):
    with db.engine.begin() as conn:
        conn.execute(update(_lock).where(_lock.c.id == 1, _lock.c.job_id == job_id).values(job_id=None))


def active_job():
    """The job holding the lock, if its heartbeat is fresh.

    The lock is read on a connection of its own: the ORM session may be in a
    transaction that began before another worker took the lock, and MariaDB's
    REPEATABLE READ would keep showing the lock as it was then."""
    with db.engine.connect() as conn:
        row = conn.execute(select(_lock.c.job_id, _lock.c.heartbeat_at).where(_lock.c.id == 1)).first()
    if not row or row.job_id is None or not row.heartbeat_at:
        return None
    if datetime.utcnow() - row.heartbeat_at > STALE_AFTER:
        return None
    return ZyncJob.query.filter_by(id=row.job_id).first()


class JobBusy(Exception):
    pass


class JobStopped(Exception):
    """Raised by checkpoint(): the job must stop (status: cancelled or interrupted)."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def request_cancel(job_id: int) -> bool:
    """Ask a queued or running job to stop at its next checkpoint."""
    with db.engine.begin() as conn:
        result = conn.execute(update(ZyncJob.__table__)
                              .where(ZyncJob.__table__.c.id == job_id,
                                     ZyncJob.__table__.c.status.in_(("queued", "running")))
                              .values(status="cancelling"))
        return result.rowcount == 1


class JobLogger:
    """Collects log lines and flushes them to the job row.

    Writes go through their own connection, so they never commit (or get
    rolled back with) the ORM session the job itself is working in.
    """

    LEVELS = ("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR")

    def __init__(self, job_id: int):
        self.job_id = job_id
        self._pending = []
        self._length = 0
        self._truncated = False
        self._last_flush = 0.0
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._engine = db.engine  # needs the app context: create the logger in the job thread
        self.counts = {level: 0 for level in self.LEVELS}
        self.lost = False  # another job took the lock over

    def _add(self, level: str, message: str):
        with self._lock:
            self.counts[level] += 1
            for line in str(message).splitlines() or [""]:
                text = f"[{level}] {line}\n"
                if self._length + len(text) > MAX_LOG_CHARS:
                    if not self._truncated:
                        self._truncated = True
                        self._pending.append("[WARNING] Log truncated (too long)\n")
                    continue
                self._length += len(text)
                self._pending.append(text)
        if time.monotonic() - self._last_flush >= FLUSH_SECONDS:
            try:
                self.flush(wait=False)
            except Exception:  # noqa: BLE001 — kept pending, the next flush retries
                pass

    def debug(self, msg): self._add("DEBUG", msg)
    def info(self, msg): self._add("INFO", msg)
    def success(self, msg): self._add("SUCCESS", msg)
    def warning(self, msg): self._add("WARNING", msg)
    def error(self, msg): self._add("ERROR", msg)

    def flush(self, wait=True, **fields):
        """Append the pending lines to the job row. Safe from any thread (the
        build pool has no app context, hence the engine kept from __init__);
        flushes are serialised so chunks keep their order."""
        if not self._flush_lock.acquire(blocking=wait):
            return
        try:
            with self._lock:
                chunk = "".join(self._pending)
                self._pending = []
                self._last_flush = time.monotonic()
            values = dict(fields)
            values["heartbeat_at"] = datetime.utcnow()
            if chunk:
                values["log"] = func.coalesce(ZyncJob.__table__.c.log, "") + chunk
            try:
                with self._engine.begin() as conn:
                    conn.execute(update(ZyncJob.__table__)
                                 .where(ZyncJob.__table__.c.id == self.job_id).values(**values))
                    if not _beat(conn, self.job_id):
                        self.lost = True
            except Exception:
                with self._lock:
                    self._pending.insert(0, chunk)
                raise
        finally:
            self._flush_lock.release()

    def checkpoint(self):
        """Stop here if the job was cancelled or lost its lock."""
        self.flush()
        if self.lost:
            raise JobStopped("Another job took the lock over (this one stalled): stopped", "interrupted")
        with self._engine.connect() as conn:
            status = conn.execute(select(ZyncJob.__table__.c.status)
                                  .where(ZyncJob.__table__.c.id == self.job_id)).scalar()
        if status == "cancelling":
            raise JobStopped("Cancelled", "cancelled")


def start_job(app, action: str, repo, params: dict, user: str, func) -> ZyncJob:
    """Create a job row and run func(logger, **params) in the background.

    func returns a summary dict; a "status" key in it (success, warning,
    failed) sets the job status.
    """
    job = ZyncJob(action=action, repo=repo, params=json.dumps(params), status="queued",
                  created_by=user, heartbeat_at=datetime.utcnow(), log="")
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    acquired, previous = _acquire(job_id)
    if not acquired:
        db.session.delete(job)
        db.session.commit()
        running = ZyncJob.query.filter_by(id=previous).first() if previous else None
        raise JobBusy(f"Job #{previous} ({running.action if running else '?'}) is still running")
    if previous and previous != job_id:
        # It held the lock without heartbeat: its worker died
        ZyncJob.query.filter(ZyncJob.id == previous, ZyncJob.status.in_(ACTIVE)).update(
            {"status": "interrupted"}, synchronize_session=False)
    old = [j.id for j in ZyncJob.query.with_entities(ZyncJob.id).order_by(ZyncJob.id.desc()).offset(KEEP_JOBS).all()]
    if old:
        ZyncJob.query.filter(ZyncJob.id.in_(old)).delete(synchronize_session=False)
    db.session.commit()

    thread = threading.Thread(target=_run, args=(app, job_id, func, params), daemon=True,
                              name=f"zync-job-{job_id}")
    thread.start()
    return job


def _run(app, job_id, func, params):
    with app.app_context():
        logger = JobLogger(job_id)
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    logger.flush()
                except Exception:  # noqa: BLE001 — next beat retries
                    pass

        beat = threading.Thread(target=heartbeat, daemon=True, name=f"zync-job-{job_id}-beat")
        logger.flush(status="running", started_at=datetime.utcnow())
        beat.start()

        status, summary = "failed", None
        try:
            summary = func(logger, **params) or {}
            status = summary.pop("status", None) or ("warning" if logger.counts["ERROR"] else "success")
        except JobStopped as e:
            db.session.rollback()
            logger.warning(str(e))
            status = e.status
        except Exception as e:  # noqa: BLE001 — the job must always finish
            db.session.rollback()
            logger.error(f"Unexpected error: {e}")
            logger.debug(traceback.format_exc())
        finally:
            stop.set()
            db.session.remove()
            try:
                logger.flush(status=status, finished_at=datetime.utcnow(),
                             summary=json.dumps(summary) if summary is not None else None)
            finally:
                _release(job_id)
