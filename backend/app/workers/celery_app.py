from celery import Celery

from app.core.config import settings

celery_app = Celery('paddle_web_pipeline_worker', broker=settings.redis_url, backend=settings.redis_url)

_soft_time_limit = max(1, int(settings.celery_task_soft_time_limit_seconds))
_hard_time_limit = max(_soft_time_limit, int(settings.celery_task_time_limit_seconds))
# Must be >= the hard time limit: otherwise Redis can consider a still-running
# task's delivery lost and redeliver it to another worker before the hard
# limit even has a chance to kill the original attempt, duplicating OCR work.
_visibility_timeout = max(_hard_time_limit, int(settings.celery_broker_visibility_timeout_seconds))

celery_app.conf.update(
	task_serializer='json',
	result_serializer='json',
	accept_content=['json'],
	worker_concurrency=max(1, int(settings.worker_concurrency)),
	worker_prefetch_multiplier=1,
	task_acks_late=True,
	task_reject_on_worker_lost=True,
	task_track_started=True,
	task_soft_time_limit=_soft_time_limit,
	task_time_limit=_hard_time_limit,
	broker_transport_options={
		# Ensure lost worker messages are re-delivered in a predictable
		# window, and stay >= task_time_limit (see _visibility_timeout above)
		# so in-flight long OCR tasks aren't redelivered/duplicated.
		'visibility_timeout': _visibility_timeout,
	},
)
