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
