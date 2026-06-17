FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Runtime libraries required by OpenCV/PaddleOCR inside slim images.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		libglib2.0-0 \
		libsm6 \
		libxext6 \
		libxrender1 \
		libx11-6 \
		libxcb1 \
		libgl1 \
	&& rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Required by PaddleOCR doc parser engines (PP-StructureV3).
RUN pip install --no-cache-dir onnxruntime

# PaddleOCR PP-StructureV3 for CPU-native document parsing (macOS Silicon + amd64).
RUN pip install --no-cache-dir "paddleocr[doc-parser]"

COPY app /app/app

CMD ["sh", "-c", "celery -A app.workers.tasks worker --loglevel=${CELERY_LOG_LEVEL:-info} --concurrency=${CELERY_WORKER_CONCURRENCY:-1} --prefetch-multiplier=${CELERY_PREFETCH_MULTIPLIER:-1} -Ofair --max-tasks-per-child=${CELERY_MAX_TASKS_PER_CHILD:-5}"]
