from celery import Celery

from app.core.config import settings

celery_app = Celery('paddle_web_pipeline_worker', broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
	task_serializer='json',
	result_serializer='json',
	accept_content=['json'],
	worker_concurrency=max(1, int(settings.worker_concurrency)),
	worker_prefetch_multiplier=1,
	task_acks_late=True,
	task_reject_on_worker_lost=True,
	task_track_started=True,
	broker_transport_options={
		# Ensure lost worker messages are re-delivered in a predictable window.
		'visibility_timeout': 600,
	},
)
