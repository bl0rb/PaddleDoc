FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		build-essential \
	&& rm -rf /var/lib/apt/lists/*

# --require-hashes rejects any artifact whose digest is not in the lock;
# regenerate the lock via the command documented in requirements.in.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt

COPY app /app/app
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY entrypoint.sh /app/entrypoint.sh
RUN tr -d '\r' < /app/entrypoint.sh > /app/entrypoint.sh.tmp && mv /app/entrypoint.sh.tmp /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Non-root runtime user. UID/GID 1000 and the home directory match the Helm
# chart (podSecurityContext runAsUser/runAsGroup 1000, HOME=/home/paddledoc),
# so the image behaves identically under compose and under Kubernetes instead
# of running as root in the one and as 1000 in the other.
RUN groupadd --gid 1000 paddledoc \
	&& useradd --uid 1000 --gid 1000 --home-dir /home/paddledoc --create-home paddledoc \
	&& mkdir -p /app/backend/storage \
	&& chown -R paddledoc:paddledoc /app /home/paddledoc

USER paddledoc

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
