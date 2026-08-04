# PaddleDoc Helm Chart

This chart deploys PaddleDoc in Kubernetes with a queue-style topology:

- `frontend` (Next.js)
- `backend` (FastAPI)
- `worker` (Celery)
- external PostgreSQL (required)
- optional bundled `redis`
- optional pre-install/pre-upgrade migration hook job

## PaddleDoc HA Queue Profile

This chart includes a PaddleDoc HA queue profile for production-oriented deployments:

- scale API (`backend.replicaCount`) and workers (`worker.replicaCount`) independently
- autoscale each with HPA
- run migrations once via a Helm hook job (`migrationJob.enabled: true`)
- no enterprise license feature flags

## Quick Start

```bash
helm upgrade --install PaddleDoc ./charts/paddledoc \
  --namespace PaddleDoc --create-namespace
```

Install from GHCR OCI registry:

```bash
helm install PaddleDoc oci://ghcr.io/bl0rb/charts/paddledoc --version 0.2.0 \
  --namespace PaddleDoc --create-namespace
```

## Production-like Example

```bash
helm upgrade --install PaddleDoc ./charts/paddledoc \
  --namespace PaddleDoc --create-namespace \
  -f ./charts/paddledoc/examples/paddledoc-ha-queue-oss.yaml
```

## Small Kubernetes Example (CPU + External PostgreSQL)

This example is for Kubernetes clusters (including lightweight k3s on NAS
hardware). It is not for standalone NAS Docker deployments.

Keep one replica per component, use conservative resources, and default to a
CPU OCR profile.

```bash
helm upgrade --install PaddleDoc ./charts/paddledoc \
  --namespace PaddleDoc --create-namespace \
  -f ./charts/paddledoc/examples/nas-cpu-external-postgres.yaml
```

## Important Notes

1. If `persistence.enabled=true`, your StorageClass should support `ReadWriteMany` so backend and worker can access shared files. With `persistence.enabled=false`, backend and worker fall back to per-pod `emptyDir` volumes at the storage path (non-persistent, not shared between pods) so they keep working as non-root.
2. Set `frontend.apiUrl` to a browser-reachable backend URL (usually your backend ingress host).
3. PostgreSQL must be external. Configure `database.*` and provide `database.passwordSecret`.
4. Default mode runs Alembic in backend startup (`backend.runAlembicOnStartup=true`).
5. For multi-replica backend setups, prefer `migrationJob.enabled=true` with `backend.runAlembicOnStartup=false`.
6. OCR profile defaults to CPU-safe `ppocrv6_tiny`; switch to a GPU-oriented profile only when your cluster nodes provide NVIDIA runtime/device plugin.

## Running without shared storage

Since app v1.0.12, `persistence.enabled=false` is a fully supported deployment
mode, not just a fallback: each `backend` and `worker` pod gets its own
private, non-persistent `emptyDir` at the storage path, and no volume is
shared between pods. This works because uploads, OCR results, and editor
versions are all persisted to PostgreSQL (as the single source of truth for
shared artifacts) rather than to the filesystem — the `emptyDir` is only used
as transient scratch space during a job's processing.

Practical implications:

- No `ReadWriteMany` StorageClass is required; `persistence.enabled=false`
  works on any cluster, including single-node/NAS setups with only
  `ReadWriteOnce` storage.
- Backend and worker pods can scale, restart, and reschedule independently
  without losing or diverging on uploaded files or results.
- If you front the backend with an ingress, the ingress controller's
  request body-size limit must be raised to cover `backend.maxUploadBytes`
  (or the app default of 100 MiB if unset). For ingress-nginx, set:

  ```yaml
  ingress:
    backend:
      annotations:
        nginx.ingress.kubernetes.io/proxy-body-size: "100m"
  ```

  Keep this value at least as large as `backend.maxUploadBytes` so large
  uploads aren't rejected by the ingress controller before they reach the
  backend.

## Scaling Logic

This chart supports two scaling modes for backend and worker:

1. Manual replicas:
  - Set `autoscaling.backend.enabled=false` and `autoscaling.worker.enabled=false`
  - Use `backend.replicaCount` and `worker.replicaCount`
2. HPA-managed replicas:
  - Set `autoscaling.backend.enabled=true` and/or `autoscaling.worker.enabled=true`
  - Deployments start at `minReplicas`
  - HPA scales up to `maxReplicas` based on CPU target

When HPA is enabled, deployment `replicas` is automatically aligned to `minReplicas`.

## Pod security / PSA

All templates (`frontend`, `backend`, `worker`, `redis`, and the
`migrationJob` hook) set restricted-compliant `securityContext` defaults, so
this chart passes the Pod Security Admission `restricted:latest` profile
out of the box on a namespace that enforces it:

- pod-level: `runAsNonRoot: true`, an explicit `runAsUser`/`runAsGroup`, and
  `seccompProfile.type: RuntimeDefault`
- container-level: `allowPrivilegeEscalation: false` and
  `capabilities.drop: ["ALL"]`

Defaults per component:

| Component | `runAsUser`/`runAsGroup` | `fsGroup` | Notes |
|---|---|---|---|
| `frontend` | 1000/1000 | - | matches the `node` uid in the `node:26-alpine` image |
| `backend` | 1000/1000 | 1000 (`fsGroupChangePolicy: OnRootMismatch`) | `fsGroup` keeps the storage PVC writable for a non-root user |
| `worker` | 1000/1000 | 1000 (`fsGroupChangePolicy: OnRootMismatch`) | same as backend; also runs the OCR model cache (see below) |
| `redis` | 999/999 | - | 999 is the `redis` user baked into the official `redis:7` image |
| `migrationJob` | 1000/1000 | 1000 (`fsGroupChangePolicy: OnRootMismatch`) | runs the backend image, so it mirrors `backend`'s context |

Every value is overridable, e.g. `frontend.podSecurityContext`,
`backend.containerSecurityContext`, `worker.podSecurityContext`,
`redis.containerSecurityContext`, `migrationJob.podSecurityContext`, etc.
`readOnlyRootFilesystem` is intentionally left unset because the backend and
worker containers write to `/tmp` and application directories at runtime.

### Worker model cache

PaddleOCR downloads model weights at runtime into `$HOME/.paddlex` and
`$HOME/.paddleocr`. Because the worker container now runs as a non-root user
(uid 1000) without a writable home directory baked into the image, the chart
mounts a writable `emptyDir` at `/home/paddledoc` and sets `HOME` to that
path so model downloads succeed:

- `worker.modelCache.enabled` (default `true`) adds the `model-cache`
  `emptyDir` volume and mount, and sets `HOME=/home/paddledoc` on the worker
  container. Set to `false` if you bake models into the image or provide
  your own writable `$HOME` some other way.
- `worker.modelCache.sizeLimit` (default `""`, unlimited) sets the
  `emptyDir.sizeLimit`; leave empty to let the node decide.

This volume is independent of `persistence.enabled` (which controls the
shared `/app/backend/storage` PVC) — it is added whenever
`worker.modelCache.enabled` is `true`, regardless of the persistence setting.

### Redis authentication

Redis is both the Celery broker/result backend and reachable from every
backend/worker pod, so it should not sit open on the network. Wiring is
optional/back-compatible:

- **Bundled redis** (`redis.enabled=true`, the default): `redis.auth.enabled`
  defaults to `true`. The chart generates a password into a
  `<release>-redis-auth` Secret the first time it's installed (preserved
  across upgrades via `lookup`, so an upgrade doesn't rotate the password and
  break existing broker connections), passes it to the bundled redis
  container as `--requirepass`, and wires the same value into backend/worker/
  the migration job's `REDIS_URL` via a `REDIS_PASSWORD` env + Kubernetes
  `$(REDIS_PASSWORD)` substitution (the same pattern already used for
  `DATABASE_URL`/`DATABASE_PASSWORD`) — so the password itself is never
  duplicated as a literal string anywhere in the rendered manifests.
- **External redis** (`redis.enabled=false`): auth stays off by default
  (identical unauthenticated `REDIS_URL` to before this chart supported
  auth) unless you explicitly opt in by setting `redis.auth.password` or
  `redis.auth.existingSecret`.
- `redis.auth.existingSecret` points at a secret you manage yourself
  (key `redis.auth.existingSecretKey`, default `password`). It must already
  exist before `helm install`/`upgrade` — its value is read via `lookup`,
  which can't see a secret created earlier in the same install.
- `redis.auth.password` is a plaintext override (mainly for local/CI use);
  it takes priority over both `existingSecret` and auto-generation.

To disable auth entirely (e.g. a trusted, network-isolated redis), set
`redis.auth.enabled=false`.

### Authentication

Every UI/API action requires login (there is no anonymous or job-password-only
access path). Wiring:

- **`auth.required`** (default `true`): the chart fails `helm template`/
  `install`/`upgrade` immediately with a clear message if no `SECRET_KEY` is
  configured, rather than deploying pods that crash-loop at startup. Set
  `auth.secretKey.existingSecret` (name + `existingSecretKey` of a
  pre-existing Secret you manage) or `auth.secretKey.value` (plaintext,
  local/CI use — the chart then creates and manages a `<release>-auth`
  Secret from it; prefer `existingSecret` for real deployments so the key
  doesn't live in plaintext values/Helm release history). Unlike the bundled
  redis password, `SECRET_KEY` is **never auto-generated** — it must be
  supplied explicitly, since a value that silently changed between renders
  would invalidate every session and break decryption of stored OIDC client
  secrets. `SECRET_KEY` is wired into `backend`, `worker` (parity — it
  imports the same config module), and the migration Job (`alembic/env.py`
  also imports it). Set `auth.required=false` only for trusted,
  non-production use; the app itself still fails fast at its own startup for
  any non-sqlite database if `SECRET_KEY` ends up unset.
- **`auth.publicApiUrl`**: base URL the backend is externally reachable at,
  used to build the OIDC `redirect_uri`
  (`{publicApiUrl}/api/v1/auth/oidc/{slug}/callback`). Defaults to a URL
  derived from `ingress.backend.hosts[0]` (`https://` if `ingress.backend.tls`
  is set, else `http://`) when `ingress.backend.enabled=true`, otherwise
  `http://localhost:<backend.service.port>`. Set it explicitly if your
  externally visible hostname differs from the ingress host configured here
  (e.g. a separate DNS/load-balancer front-end), or OIDC login redirects will
  target the wrong host.
- **First run**: visit `/setup` on the frontend to bootstrap the first admin
  account. `GET /auth/setup-status` reports whether setup is still needed;
  `POST /auth/setup` is allowed exactly once and permanently rejects further
  attempts once an admin exists.
- **Upgrading an existing install**: after upgrading to an auth-enabled
  version, every previously existing job has no owner and is visible to
  admins only until an admin explicitly bulk-assigns ownership
  (`POST /auth/admin/jobs/claim-ownerless`); regular users won't see legacy
  jobs until then.

### Celery task time limits

`worker.celery.taskSoftTimeLimitSeconds` / `worker.celery.taskTimeLimitSeconds`
(defaults 1500s/1800s) bound how long a single OCR task may run before Celery
raises `SoftTimeLimitExceeded` inside it (soft) or SIGKILLs the worker child
(hard) — the job is then marked `FAILED` rather than hanging a worker slot
indefinitely. `worker.celery.visibilityTimeoutSeconds` (default 1800s) is the
matching Redis broker `visibility_timeout`; it must stay `>=` the hard limit
so a still-running long task isn't considered lost and redelivered to another
worker before its own hard limit fires (`app/workers/celery_app.py` also
clamps this defensively even if misconfigured here).

## Database Configuration (External Only)

This chart uses an external-database pattern and supports only external PostgreSQL:

```yaml
database:
  type: postgresdb
  useExternal: true
  host: "your-postgres-host.com"
  port: 5432
  database: paddledoc
  schema: "public"
  user: paddledoc
  passwordSecret:
    name: "paddledoc-db-secret"
    key: "password"
```

Example secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: paddledoc-db-secret
type: Opaque
stringData:
  password: "change-me"
```

## Full Configuration Reference (All Values)

The following list contains all configurable parameters currently supported by this chart (from `values.yaml`).

| Key | Type | Default |
|---|---|---|
| `nameOverride` | string | `""` |
| `fullnameOverride` | string | `""` |
| `imagePullSecrets` | list | `[]` |
| `serviceAccount.create` | bool | `true` |
| `serviceAccount.name` | string | `""` |
| `serviceAccount.annotations` | map | `{}` |
| `global.podAnnotations` | map | `{}` |
| `global.podLabels` | map | `{}` |
| `frontend.enabled` | bool | `true` |
| `frontend.replicaCount` | int | `1` |
| `frontend.image.repository` | string | `ghcr.io/bl0rb/PaddleDoc-frontend` |
| `frontend.image.tag` | string | `latest` |
| `frontend.image.pullPolicy` | string | `IfNotPresent` |
| `frontend.service.type` | string | `ClusterIP` |
| `frontend.service.port` | int | `3000` |
| `frontend.apiUrl` | string | `http://localhost:8000` |
| `frontend.resources` | map | `{}` |
| `frontend.nodeSelector` | map | `{}` |
| `frontend.tolerations` | list | `[]` |
| `frontend.affinity` | map | `{}` |
| `frontend.podSecurityContext` | map | `{runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, seccompProfile: {type: RuntimeDefault}}` |
| `frontend.containerSecurityContext` | map | `{allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}` |
| `backend.enabled` | bool | `true` |
| `backend.replicaCount` | int | `1` |
| `backend.image.repository` | string | `ghcr.io/bl0rb/PaddleDoc-backend` |
| `backend.image.tag` | string | `latest` |
| `backend.image.pullPolicy` | string | `IfNotPresent` |
| `backend.service.type` | string | `ClusterIP` |
| `backend.service.port` | int | `8000` |
| `backend.corsOrigins` | string | `["http://localhost:3000"]` |
| `backend.runAlembicOnStartup` | bool | `true` |
| `backend.maxUploadBytes` | string | `""` |
| `backend.resources` | map | `{}` |
| `backend.nodeSelector` | map | `{}` |
| `backend.tolerations` | list | `[]` |
| `backend.affinity` | map | `{}` |
| `backend.podSecurityContext` | map | `{runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, fsGroupChangePolicy: OnRootMismatch, seccompProfile: {type: RuntimeDefault}}` |
| `backend.containerSecurityContext` | map | `{allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}` |
| `worker.enabled` | bool | `true` |
| `worker.replicaCount` | int | `1` |
| `worker.image.repository` | string | `ghcr.io/bl0rb/PaddleDoc-worker` |
| `worker.image.tag` | string | `latest` |
| `worker.image.pullPolicy` | string | `IfNotPresent` |
| `worker.paddleDefaultProfile` | string | `ppocrv6_tiny` |
| `worker.resources` | map | `{}` |
| `worker.nodeSelector` | map | `{}` |
| `worker.tolerations` | list | `[]` |
| `worker.affinity` | map | `{}` |
| `worker.podSecurityContext` | map | `{runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, fsGroupChangePolicy: OnRootMismatch, seccompProfile: {type: RuntimeDefault}}` |
| `worker.containerSecurityContext` | map | `{allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}` |
| `worker.modelCache.enabled` | bool | `true` |
| `worker.modelCache.sizeLimit` | string | `""` |
| `worker.celery.taskSoftTimeLimitSeconds` | int | `1500` |
| `worker.celery.taskTimeLimitSeconds` | int | `1800` |
| `worker.celery.visibilityTimeoutSeconds` | int | `1800` |
| `auth.required` | bool | `true` |
| `auth.secretKey.existingSecret` | string | `""` |
| `auth.secretKey.existingSecretKey` | string | `secret-key` |
| `auth.secretKey.value` | string | `""` |
| `auth.publicApiUrl` | string | `""` |
| `migrationJob.enabled` | bool | `false` |
| `migrationJob.backoffLimit` | int | `2` |
| `migrationJob.podSecurityContext` | map | `{runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, fsGroupChangePolicy: OnRootMismatch, seccompProfile: {type: RuntimeDefault}}` |
| `migrationJob.containerSecurityContext` | map | `{allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}` |
| `persistence.enabled` | bool | `true` |
| `persistence.storageClassName` | string | `""` |
| `persistence.accessModes` | list | `[ReadWriteMany]` |
| `persistence.size` | string | `20Gi` |
| `persistence.existingClaim` | string | `""` |
| `database.type` | string | `postgresdb` |
| `database.useExternal` | bool | `true` |
| `database.host` | string | `""` |
| `database.port` | int | `5432` |
| `database.database` | string | `paddledoc` |
| `database.schema` | string | `public` |
| `database.user` | string | `paddledoc` |
| `database.passwordSecret.name` | string | `""` |
| `database.passwordSecret.key` | string | `password` |
| `redis.enabled` | bool | `true` |
| `redis.image.repository` | string | `redis` |
| `redis.image.tag` | string | `7` |
| `redis.image.pullPolicy` | string | `IfNotPresent` |
| `redis.host` | string | `""` |
| `redis.port` | int | `6379` |
| `redis.auth.enabled` | bool | `true` |
| `redis.auth.existingSecret` | string | `""` |
| `redis.auth.existingSecretKey` | string | `password` |
| `redis.auth.password` | string | `""` |
| `redis.resources` | map | `{}` |
| `redis.podSecurityContext` | map | `{runAsNonRoot: true, runAsUser: 999, runAsGroup: 999, seccompProfile: {type: RuntimeDefault}}` |
| `redis.containerSecurityContext` | map | `{allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}` |
| `autoscaling.backend.enabled` | bool | `false` |
| `autoscaling.backend.minReplicas` | int | `1` |
| `autoscaling.backend.maxReplicas` | int | `5` |
| `autoscaling.backend.targetCPUUtilizationPercentage` | int | `70` |
| `autoscaling.worker.enabled` | bool | `false` |
| `autoscaling.worker.minReplicas` | int | `1` |
| `autoscaling.worker.maxReplicas` | int | `10` |
| `autoscaling.worker.targetCPUUtilizationPercentage` | int | `75` |
| `ingress.frontend.enabled` | bool | `false` |
| `ingress.frontend.className` | string | `""` |
| `ingress.frontend.annotations` | map | `{}` |
| `ingress.frontend.hosts` | list | `[{host: PaddleDoc.local, paths:[{path:/, pathType:Prefix}]}]` |
| `ingress.frontend.tls` | list | `[]` |
| `ingress.backend.enabled` | bool | `false` |
| `ingress.backend.className` | string | `""` |
| `ingress.backend.annotations` | map | `{}` |
| `ingress.backend.hosts` | list | `[{host: api.PaddleDoc.local, paths:[{path:/, pathType:Prefix}]}]` |
| `ingress.backend.tls` | list | `[]` |

## Full Values Example

For convenience, here is the current complete default values file:

```yaml
nameOverride: ""
fullnameOverride: ""

imagePullSecrets: []

serviceAccount:
  create: true
  name: ""
  annotations: {}

global:
  podAnnotations: {}
  podLabels: {}

frontend:
  enabled: true
  replicaCount: 1
  image:
    repository: ghcr.io/bl0rb/paddledoc-frontend
    tag: "latest"
    pullPolicy: IfNotPresent
  service:
    type: ClusterIP
    port: 3000
  apiUrl: http://localhost:8000
  resources: {}
  nodeSelector: {}
  tolerations: []
  affinity: {}
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containerSecurityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]

backend:
  enabled: true
  replicaCount: 1
  image:
    repository: ghcr.io/bl0rb/paddledoc-backend
    tag: "latest"
    pullPolicy: IfNotPresent
  service:
    type: ClusterIP
    port: 8000
  corsOrigins: '["http://localhost:3000"]'
  runAlembicOnStartup: true
  maxUploadBytes: ""
  resources: {}
  nodeSelector: {}
  tolerations: []
  affinity: {}
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile:
      type: RuntimeDefault
  containerSecurityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]

worker:
  enabled: true
  replicaCount: 1
  image:
    repository: ghcr.io/bl0rb/paddledoc-worker
    tag: "latest"
    pullPolicy: IfNotPresent
  paddleDefaultProfile: ppocrv6_tiny
  resources: {}
  nodeSelector: {}
  tolerations: []
  affinity: {}
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile:
      type: RuntimeDefault
  containerSecurityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]
  modelCache:
    enabled: true
    sizeLimit: ""

migrationJob:
  enabled: false
  backoffLimit: 2
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile:
      type: RuntimeDefault
  containerSecurityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]

persistence:
  enabled: true
  storageClassName: ""
  accessModes:
    - ReadWriteMany
  size: 20Gi
  existingClaim: ""

database:
  type: postgresdb
  useExternal: true
  host: ""
  port: 5432
  database: paddledoc
  schema: public
  user: paddledoc
  passwordSecret:
    name: ""
    key: password

redis:
  enabled: true
  image:
    repository: redis
    tag: "7"
    pullPolicy: IfNotPresent
  host: ""
  port: 6379
  resources: {}
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 999
    runAsGroup: 999
    seccompProfile:
      type: RuntimeDefault
  containerSecurityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]

autoscaling:
  backend:
    enabled: false
    minReplicas: 1
    maxReplicas: 5
    targetCPUUtilizationPercentage: 70
  worker:
    enabled: false
    minReplicas: 1
    maxReplicas: 10
    targetCPUUtilizationPercentage: 75

ingress:
  frontend:
    enabled: false
    className: ""
    annotations: {}
    hosts:
      - host: paddledoc.local
        paths:
          - path: /
            pathType: Prefix
    tls: []
  backend:
    enabled: false
    className: ""
    annotations: {}
    hosts:
      - host: api.paddledoc.local
        paths:
          - path: /
            pathType: Prefix
    tls: []
```
