FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

# Runtime libraries required by OpenCV/PaddleOCR inside slim images.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		build-essential \
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

# Required by the OpenAI vision pipeline to render PDF pages to images.
RUN pip install --no-cache-dir pypdfium2

# PaddlePaddle GPU framework (3.2.1+, required by PaddleOCR-VL-1.6).
# Uses --extra-index-url so PyPI remains the primary index for other packages (Pillow etc.)
# while paddlepaddle-gpu is resolved from PaddlePaddle's CUDA 12.6 index.
# At runtime, paddle.is_compiled_with_cuda() returns True when the container is started
# with NVIDIA device reservation (docker-compose.gpu.yml).
RUN pip install --no-cache-dir paddlepaddle-gpu==3.2.1 \
    --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/

# PaddleOCR PP-StructureV3 and PaddleOCR-VL for document parsing.
RUN pip install --no-cache-dir "paddleocr[doc-parser]"

COPY app /app/app

CMD ["sh", "-c", "celery -A app.workers.tasks worker --loglevel=${CELERY_LOG_LEVEL:-info} --pool=${CELERY_WORKER_POOL:-prefork} --concurrency=${CELERY_WORKER_CONCURRENCY:-1} --prefetch-multiplier=${CELERY_PREFETCH_MULTIPLIER:-1} -Ofair --max-tasks-per-child=${CELERY_MAX_TASKS_PER_CHILD:-5}"]
