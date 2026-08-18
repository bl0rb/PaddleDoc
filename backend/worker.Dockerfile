FROM python:3.13-slim

WORKDIR /app

ARG TARGETARCH

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

# --require-hashes rejects any artifact whose digest is not in the lock;
# regenerate the lock via the command documented in requirements.in.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt

# PaddlePaddle install is architecture-specific:
# - amd64: use CUDA-enabled wheel for GPU deployments.
# - arm64: use CPU wheel (GPU wheel is not published for aarch64).
RUN if [ "$TARGETARCH" = "amd64" ]; then \
			pip install --no-cache-dir paddlepaddle-gpu==3.2.1 \
				--extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/; \
		else \
			pip install --no-cache-dir paddlepaddle==3.2.1; \
		fi

# Worker-only extras, version- and hash-locked (supply-chain hardening):
# - onnxruntime: PaddleOCR doc parser engines (PP-StructureV3)
# - pypdfium2: renders PDF pages to images for the OpenAI vision pipeline
# - paddleocr[doc-parser]: PP-StructureV3 and PaddleOCR-VL document parsing
# --require-hashes rejects any artifact not matching the recorded hashes; the
# lock is regenerated via the command documented in requirements-worker.in.
# Note: like the previous unpinned install, the paddleocr tree pins pyyaml to
# 6.0.2 via paddlex, overriding the 6.0.3 pin from requirements.txt.
COPY requirements-worker.txt /app/requirements-worker.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements-worker.txt

COPY app /app/app

# Non-root runtime user. UID/GID 1000 and the home directory match the Helm
# chart (podSecurityContext runAsUser/runAsGroup 1000, HOME=/home/paddledoc),
# so the image behaves identically under compose and under Kubernetes instead
# of running as root in the one and as 1000 in the other.
RUN groupadd --gid 1000 paddledoc \
	&& useradd --uid 1000 --gid 1000 --home-dir /home/paddledoc --create-home paddledoc \
	&& mkdir -p /app/backend/storage \
	&& chown -R paddledoc:paddledoc /app /home/paddledoc

USER paddledoc

# PaddleOCR downloads model weights at runtime into $HOME/.paddlex and
# $HOME/.paddleocr. Docker does not read the home directory from /etc/passwd,
# so HOME must be set explicitly or it would default to "/" (not writable for
# this user) and every model download would fail.
ENV HOME=/home/paddledoc

CMD ["sh", "-c", "celery -A app.workers.tasks worker --loglevel=${CELERY_LOG_LEVEL:-info} --pool=${CELERY_WORKER_POOL:-prefork} --concurrency=${CELERY_WORKER_CONCURRENCY:-1} --prefetch-multiplier=${CELERY_PREFETCH_MULTIPLIER:-1} -Ofair --max-tasks-per-child=${CELERY_MAX_TASKS_PER_CHILD:-5}"]
