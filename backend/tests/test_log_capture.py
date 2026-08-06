"""Proves WORKER_LOG_CAPTURE_LEVEL does what config.py/values.yaml/
docker-compose.yml document: a capture floor independent of the worker's
--loglevel, able to go MORE verbose than --loglevel without also making
the console noisier.

Exercises `_attach_db_log_handler` (the `after_setup_logger` receiver in
app.workers.log_capture) the same way Celery's own
`Logging.setup_logging_subsystem` invokes it -- see
celery.app.log.Logging.setup_logging_subsystem / _configure_logger /
setup_handlers, which by the time the signal fires has already done
`root.setLevel(loglevel)` and attached a level-less (NOTSET) console/stream
handler to the root logger.

A throwaway named logger stands in for the real root logger so these tests
don't mutate process-wide logging state (pytest's own log capture included);
Python's logging hierarchy works the same way for any dotted name, not just
the unnamed root, so `<fake_root>.child` faithfully models how a real
`app.workers.tasks` (NOTSET, propagate=True) logger inherits its effective
level from the actual root.
"""

import logging

from sqlalchemy import select

from app.core.config import settings
from app.models.models import WorkerLogEntry
from app.workers import log_capture as log_capture_module
from tests.conftest import TestingSessionLocal

_FAKE_ROOT_NAME = 'test_log_capture_fake_root'


def _fake_root() -> tuple[logging.Logger, logging.StreamHandler]:
    """A fresh stand-in root logger with one NOTSET console/stream handler,
    matching exactly what Celery's setup_handlers() leaves behind right
    before after_setup_logger fires."""
    logger = logging.getLogger(_FAKE_ROOT_NAME)
    logger.handlers.clear()
    logger.propagate = False  # this IS the top of its own hierarchy in the test
    logger.setLevel(logging.INFO)
    console_handler = logging.StreamHandler()
    logger.addHandler(console_handler)
    return logger, console_handler


def test_capture_more_verbose_than_loglevel_lowers_root_and_pins_console(monkeypatch):
    logger, console_handler = _fake_root()
    monkeypatch.setattr(settings, 'worker_log_capture_level', 'DEBUG')

    log_capture_module._attach_db_log_handler(logger=logger, loglevel=logging.INFO)

    assert logger.level == logging.DEBUG
    # was NOTSET before the call -- must be pinned to the original
    # --loglevel, not left to inherit the newly-lowered root level.
    assert console_handler.level == logging.INFO
    db_handlers = [h for h in logger.handlers if isinstance(h, log_capture_module.WorkerLogDBHandler)]
    assert len(db_handlers) == 1
    assert db_handlers[0].level == logging.DEBUG


def test_capture_no_more_verbose_than_loglevel_leaves_root_and_console_untouched(monkeypatch):
    logger, console_handler = _fake_root()
    monkeypatch.setattr(settings, 'worker_log_capture_level', 'WARNING')

    log_capture_module._attach_db_log_handler(logger=logger, loglevel=logging.INFO)

    assert logger.level == logging.INFO  # untouched -- --loglevel is already verbose enough
    assert console_handler.level == logging.NOTSET  # never pinned -- nothing to protect against

    db_handlers = [h for h in logger.handlers if isinstance(h, log_capture_module.WorkerLogDBHandler)]
    assert db_handlers[0].level == logging.WARNING


def test_capture_equal_to_loglevel_leaves_root_and_console_untouched(monkeypatch):
    logger, console_handler = _fake_root()
    monkeypatch.setattr(settings, 'worker_log_capture_level', 'INFO')

    log_capture_module._attach_db_log_handler(logger=logger, loglevel=logging.INFO)

    assert logger.level == logging.INFO
    assert console_handler.level == logging.NOTSET


def test_debug_capture_reaches_db_handler_while_console_stays_at_info(monkeypatch):
    """End to end: capture=DEBUG + loglevel=INFO must let a DEBUG record
    from an app.* logger (a) actually get constructed and land in
    worker_log_entries, and (b) never reach the console handler pinned at
    INFO -- the exact bug: previously the DB handler's own level=DEBUG was
    irrelevant because the record never got past the root logger's INFO
    floor to reach any handler at all."""
    logger, console_handler = _fake_root()
    monkeypatch.setattr(settings, 'worker_log_capture_level', 'DEBUG')
    # Redirect the module's real (module-owned, non-test) DB engine at the
    # shared sqlite test DB so the write is verifiable without touching the
    # on-disk paddledoc.db the module normally writes to.
    monkeypatch.setattr(log_capture_module, '_SessionLocal', TestingSessionLocal)

    log_capture_module._attach_db_log_handler(logger=logger, loglevel=logging.INFO)

    child_name = f'{_FAKE_ROOT_NAME}.app.workers.tasks'
    child = logging.getLogger(child_name)
    child.setLevel(logging.NOTSET)
    child.propagate = True
    child.handlers.clear()
    try:
        child.debug('debug marker %s', 'from worker task')
        child.info('info marker')
    finally:
        child.handlers.clear()

    db = TestingSessionLocal()
    try:
        rows = db.execute(
            select(WorkerLogEntry).where(WorkerLogEntry.logger_name == child_name)
        ).scalars().all()
    finally:
        db.close()

    levels_seen = {row.level for row in rows}
    assert 'DEBUG' in levels_seen, 'DEBUG record from an app.* logger did not land in worker_log_entries'
    assert 'INFO' in levels_seen
    debug_row = next(row for row in rows if row.level == 'DEBUG')
    assert 'debug marker' in debug_row.message

    # And it must not have leaked out to the pinned console handler.
    assert console_handler.level == logging.INFO
