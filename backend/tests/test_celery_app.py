"""Step 4 ride-along: Celery task hard/soft time limits + the matching
broker visibility_timeout.

Long CPU-bound OCR tasks shouldn't hang a worker slot forever, and if
Redis's visibility_timeout is shorter than the hard time limit a
still-running task can be considered lost and redelivered to another
worker -- duplicating the OCR work -- before the original attempt's hard
limit even fires. These tests assert both are actually wired into
celery_app.conf, and that the invariant (visibility_timeout >= hard limit)
holds even if someone misconfigures the two independently via env/settings.
"""

import importlib

from app.core.config import settings


def _reload_celery_app():
    """celery_app.py reads settings.celery_* at import time, so exercising a
    different configuration requires a fresh import rather than reusing the
    module-level `celery_app` singleton other modules already imported."""
    import app.workers.celery_app as celery_app_module

    return importlib.reload(celery_app_module)


def test_celery_conf_has_soft_and_hard_time_limits() -> None:
    celery_app_module = _reload_celery_app()

    conf = celery_app_module.celery_app.conf
    assert conf.task_soft_time_limit == settings.celery_task_soft_time_limit_seconds
    assert conf.task_time_limit == settings.celery_task_time_limit_seconds


def test_celery_conf_soft_limit_is_less_than_hard_limit_by_default() -> None:
    celery_app_module = _reload_celery_app()

    conf = celery_app_module.celery_app.conf
    assert conf.task_soft_time_limit < conf.task_time_limit


def test_broker_visibility_timeout_at_least_matches_hard_time_limit() -> None:
    celery_app_module = _reload_celery_app()

    conf = celery_app_module.celery_app.conf
    visibility_timeout = conf.broker_transport_options['visibility_timeout']
    assert visibility_timeout >= conf.task_time_limit


def test_visibility_timeout_is_clamped_up_if_configured_below_hard_limit(monkeypatch) -> None:
    # A misconfiguration (e.g. someone bumps the hard limit in one place but
    # forgets the broker visibility_timeout) must not silently reintroduce
    # the "task redelivered mid-flight" bug -- celery_app.py should clamp it
    # rather than trust the configured value blindly.
    monkeypatch.setattr(settings, 'celery_task_time_limit_seconds', 3600)
    monkeypatch.setattr(settings, 'celery_task_soft_time_limit_seconds', 3000)
    monkeypatch.setattr(settings, 'celery_broker_visibility_timeout_seconds', 600)

    celery_app_module = _reload_celery_app()

    conf = celery_app_module.celery_app.conf
    assert conf.task_time_limit == 3600
    assert conf.broker_transport_options['visibility_timeout'] >= 3600


def test_task_acks_late_and_reject_on_worker_lost_still_set() -> None:
    # Pre-existing crash-recovery semantics (Step 4 must not regress these):
    # a worker killed mid-task should not silently ack the message.
    celery_app_module = _reload_celery_app()

    conf = celery_app_module.celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
