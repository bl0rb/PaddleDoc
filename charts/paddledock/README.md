# PaddleDock Helm Chart

This chart deploys PaddleDock in Kubernetes with a queue-style topology:

- `frontend` (Next.js)
- `backend` (FastAPI)
- `worker` (Celery)
- external PostgreSQL (required)
- optional bundled `redis`
- optional pre-install/pre-upgrade migration hook job

## PaddleDock HA Queue Profile

This chart includes a PaddleDock HA queue profile for production-oriented deployments:

- scale API (`backend.replicaCount`) and workers (`worker.replicaCount`) independently
- autoscale each with HPA
- run migrations once via a Helm hook job (`migrationJob.enabled: true`)
- no enterprise license feature flags

## Quick Start

```bash
helm upgrade --install paddledock ./charts/paddledock \
  --namespace paddledock --create-namespace
```

## Production-like Example

```bash
helm upgrade --install paddledock ./charts/paddledock \
  --namespace paddledock --create-namespace \
  -f ./charts/paddledock/examples/paddledock-ha-queue-oss.yaml
```

## Important Notes

1. If `persistence.enabled=true`, your StorageClass should support `ReadWriteMany` so backend and worker can access shared files.
2. Set `frontend.apiUrl` to a browser-reachable backend URL (usually your backend ingress host).
3. PostgreSQL must be external. Configure `database.*` and provide `database.passwordSecret`.
4. Default mode runs Alembic in backend startup (`backend.runAlembicOnStartup=true`).
5. For multi-replica backend setups, prefer `migrationJob.enabled=true` with `backend.runAlembicOnStartup=false`.

## Database Configuration (External Only)

This chart uses an external-database pattern and supports only external PostgreSQL:

```yaml
database:
  type: postgresdb
  useExternal: true
  host: "your-postgres-host.com"
  port: 5432
  database: paddledock
  schema: "public"
  user: paddledock
  passwordSecret:
    name: "paddledock-db-secret"
    key: "password"
```

Example secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: paddledock-db-secret
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
| `frontend.image.repository` | string | `ghcr.io/bl0rb/paddledock-frontend` |
| `frontend.image.tag` | string | `latest` |
| `frontend.image.pullPolicy` | string | `IfNotPresent` |
| `frontend.service.type` | string | `ClusterIP` |
| `frontend.service.port` | int | `3000` |
| `frontend.apiUrl` | string | `http://localhost:8000` |
| `frontend.resources` | map | `{}` |
| `frontend.nodeSelector` | map | `{}` |
| `frontend.tolerations` | list | `[]` |
| `frontend.affinity` | map | `{}` |
| `backend.enabled` | bool | `true` |
| `backend.replicaCount` | int | `1` |
| `backend.image.repository` | string | `ghcr.io/bl0rb/paddledock-backend` |
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
| `worker.enabled` | bool | `true` |
| `worker.replicaCount` | int | `1` |
| `worker.image.repository` | string | `ghcr.io/bl0rb/paddledock-worker` |
| `worker.image.tag` | string | `latest` |
| `worker.image.pullPolicy` | string | `IfNotPresent` |
| `worker.paddleDefaultProfile` | string | `ppocrv6_tiny` |
| `worker.resources` | map | `{}` |
| `worker.nodeSelector` | map | `{}` |
| `worker.tolerations` | list | `[]` |
| `worker.affinity` | map | `{}` |
| `migrationJob.enabled` | bool | `false` |
| `migrationJob.backoffLimit` | int | `2` |
| `persistence.enabled` | bool | `true` |
| `persistence.storageClassName` | string | `""` |
| `persistence.accessModes` | list | `[ReadWriteMany]` |
| `persistence.size` | string | `20Gi` |
| `persistence.existingClaim` | string | `""` |
| `database.type` | string | `postgresdb` |
| `database.useExternal` | bool | `true` |
| `database.host` | string | `""` |
| `database.port` | int | `5432` |
| `database.database` | string | `paddledock` |
| `database.schema` | string | `public` |
| `database.user` | string | `paddledock` |
| `database.passwordSecret.name` | string | `""` |
| `database.passwordSecret.key` | string | `password` |
| `redis.enabled` | bool | `true` |
| `redis.image.repository` | string | `redis` |
| `redis.image.tag` | string | `7` |
| `redis.image.pullPolicy` | string | `IfNotPresent` |
| `redis.host` | string | `""` |
| `redis.port` | int | `6379` |
| `redis.resources` | map | `{}` |
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
| `ingress.frontend.hosts` | list | `[{host: paddledock.local, paths:[{path:/, pathType:Prefix}]}]` |
| `ingress.frontend.tls` | list | `[]` |
| `ingress.backend.enabled` | bool | `false` |
| `ingress.backend.className` | string | `""` |
| `ingress.backend.annotations` | map | `{}` |
| `ingress.backend.hosts` | list | `[{host: api.paddledock.local, paths:[{path:/, pathType:Prefix}]}]` |
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
    repository: ghcr.io/bl0rb/paddledock-frontend
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

backend:
  enabled: true
  replicaCount: 1
  image:
    repository: ghcr.io/bl0rb/paddledock-backend
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

worker:
  enabled: true
  replicaCount: 1
  image:
    repository: ghcr.io/bl0rb/paddledock-worker
    tag: "latest"
    pullPolicy: IfNotPresent
  paddleDefaultProfile: ppocrv6_tiny
  resources: {}
  nodeSelector: {}
  tolerations: []
  affinity: {}

migrationJob:
  enabled: false
  backoffLimit: 2

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
  database: paddledock
  schema: public
  user: paddledock
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
      - host: paddledock.local
        paths:
          - path: /
            pathType: Prefix
    tls: []
  backend:
    enabled: false
    className: ""
    annotations: {}
    hosts:
      - host: api.paddledock.local
        paths:
          - path: /
            pathType: Prefix
    tls: []
```
