"""Mirrors Celery WORKER process log records into the `worker_log_entries`
Postgres table so the admin UI can tail worker logs without docker.sock or
kubectl -- neither is available to the backend in the EKS/k8s deployment
(and the worker Deployment runs N replicas behind an HPA, so there is no
single node-local log file to tail even with host access; see
charts/paddledoc/templates/worker-deployment.yaml). Read side is
GET /api/v1/auth/admin/worker-logs in app/api/auth.py.

Wiring: importing this module (from app/workers/celery_app.py) connects
`_attach_db_log_handler` to celery's `after_setup_logger` signal, which
fires once in the worker's MAIN process after Celery finishes configuring
the root logger -- NOT at import time. Attaching a handler any earlier
would be silently dropped: `worker_hijack_root_logger` (default True)
replaces the root logger's handlers during that same setup step. Because
this happens before the prefork pool forks its child processes, every
child (including ones spawned later by --max-tasks-per-child recycling)
inherits the handler via the fork's memory copy -- no per-child wiring
needed. With --pool=solo/threads (no fork at all) the same handler just
runs directly in the single process.

Fork safety for the DB connection itself: this module owns a dedicated
engine (deliberately NOT app.database.session.engine) with
os.register_at_fork(after_in_child=...) so a connection the main process
happens to have checked out at fork time is never reused by a forked
child -- two processes sharing one TCP socket silently corrupts the
postgres wire protocol. See SQLAlchemy's "Using Connection Pools with
Multiprocessing or os.fork()".
"""

import logging
import os
import random
import socket
import threading
import traceback
from datetime import datetime, timezone

from celery._state import get_current_task
from celery.signals import after_setup_logger
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.models import WorkerLogEntry

_MESSAGE_MAX_CHARS = 4000
_EXC_TEXT_MAX_CHARS = 8000
# Amortizes the retention-cap DELETE across ~200 writes instead of running
# it on every single insert.
_PRUNE_PROBABILITY = 1 / 200

_engine = create_engine(settings.database_url, future=True, pool_size=2, max_overflow=2, pool_pre_ping=True)
_SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)

if hasattr(os, 'register_at_fork'):  # not available on Windows; worker only ever runs on Linux containers
    os.register_at_fork(after_in_child=lambda: _engine.dispose(close=False))

# Pod name in k8s (k8s sets the pod name as the container hostname by
# default), container hostname in compose. Read once -- stable for the
# process lifetime on both.
_WORKER_NAME = socket.gethostname()

_reentrant = threading.local()


class WorkerLogDBHandler(logging.Handler):
    """Persists records at self.level+ to worker_log_entries.

    Never raises and never blocks task processing on a DB hiccup: emit()
    swallows every exception via handleError(), and drops re-entrant calls
    (e.g. the DB write below itself logging something, such as a
    SQLAlchemy pool warning) instead of recursing.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_reentrant, 'active', False):
            return
        _reentrant.active = True
        try:
            self._write(record)
        except Exception:
            self.handleError(record)
        finally:
            _reentrant.active = False

    def _write(self, record: logging.LogRecord) -> None:
        task = get_current_task()
        task_id = getattr(task.request, 'id', None) if task is not None else None
        task_name = getattr(task, 'name', None) if task is not None else None

        exc_text = None
        if record.exc_info:
            exc_text = ''.join(traceback.format_exception(*record.exc_info))[:_EXC_TEXT_MAX_CHARS]

        db = _SessionLocal()
        try:
            db.add(WorkerLogEntry(
                created_at=datetime.now(timezone.utc),
                level=record.levelname,
                logger_name=record.name,
                worker_name=_WORKER_NAME,
                task_id=task_id,
                task_name=task_name,
                message=record.getMessage()[:_MESSAGE_MAX_CHARS],
                exc_text=exc_text,
            ))
            db.commit()
        finally:
            db.close()

        if random.random() < _PRUNE_PROBABILITY:
            self._prune(cap=settings.worker_log_retention_max_rows)

    @staticmethod
    def _prune(cap: int) -> None:
        if cap <= 0:
            return
        db = _SessionLocal()
        try:
            # Find the created_at of the cap-th newest row, then delete
            # everything older -- a single indexed range delete, no COUNT(*)
            # table scan needed.
            cutoff_row = db.execute(
                select(WorkerLogEntry.created_at)
                .order_by(WorkerLogEntry.created_at.desc())
                .offset(cap)
                .limit(1)
            ).first()
            if cutoff_row is None:
                return  # fewer than `cap` rows exist -- nothing to prune
            db.execute(delete(WorkerLogEntry).where(WorkerLogEntry.created_at < cutoff_row[0]))
            db.commit()
        finally:
            db.close()


@after_setup_logger.connect
def _attach_db_log_handler(logger: logging.Logger, loglevel: int | None = None, **kwargs) -> None:
    level = getattr(logging, settings.worker_log_capture_level.upper(), logging.INFO)

    # By the time this fires, Celery's setup_logging_subsystem has already
    # done `logger.setLevel(loglevel)` (logger is the root logger) and
    # attached its console/stream handler with no level of its own (NOTSET
    # -- see celery.app.log.Logging.setup_handlers, which never calls
    # handler.setLevel). Every app.* module logger is also NOTSET and
    # inherits its effective level from that root, so on its own a capture
    # level more verbose than --loglevel is a no-op: records below
    # --loglevel are never even constructed. To make WORKER_LOG_CAPTURE_LEVEL
    # actually independent of --loglevel as documented, lower the root
    # floor to the capture level -- but pin the handler(s) Celery just
    # attached to the original --loglevel first, since an unpinned (NOTSET)
    # handler would otherwise start printing the newly unblocked records to
    # the console too.
    worker_level = loglevel if loglevel is not None else logger.level
    if worker_level and level < worker_level:
        for handler in logger.handlers:
            if handler.level == logging.NOTSET:
                handler.setLevel(worker_level)
        logger.setLevel(level)

    logger.addHandler(WorkerLogDBHandler(level=level))
