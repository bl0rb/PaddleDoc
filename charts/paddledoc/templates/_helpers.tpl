{{- define "paddledoc.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "paddledoc.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "paddledoc.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "paddledoc.labels" -}}
helm.sh/chart: {{ include "paddledoc.chart" . }}
app.kubernetes.io/name: {{ include "paddledoc.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "paddledoc.selectorLabels" -}}
app.kubernetes.io/name: {{ include "paddledoc.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "paddledoc.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "paddledoc.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "paddledoc.redisHost" -}}
{{- if .Values.redis.enabled -}}
{{- printf "%s-redis" (include "paddledoc.fullname" .) -}}
{{- else -}}
{{- required "redis.host is required when redis.enabled=false" .Values.redis.host -}}
{{- end -}}
{{- end -}}

{{/*
Redis auth is optional/back-compatible: it's effectively on only when either
(a) we're managing the bundled redis (redis.enabled=true) -- so we always
have somewhere to point --requirepass at -- or (b) the operator explicitly
opted in for an external redis by setting redis.auth.password or
redis.auth.existingSecret. This keeps existing external-redis deployments
(redis.enabled=false, no auth fields set) rendering byte-for-byte the same
unauthenticated REDIS_URL as before, even though redis.auth.enabled
defaults to true.
*/}}
{{- define "paddledoc.redisAuthEffective" -}}
{{- if not .Values.redis.auth.enabled -}}
{{- else if .Values.redis.enabled -}}
true
{{- else if .Values.redis.auth.password -}}
true
{{- else if .Values.redis.auth.existingSecret -}}
true
{{- end -}}
{{- end -}}

{{- define "paddledoc.redisAuthSecretName" -}}
{{- if .Values.redis.auth.existingSecret -}}
{{- .Values.redis.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-redis-auth" (include "paddledoc.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "paddledoc.redisAuthSecretKey" -}}
{{- default "password" .Values.redis.auth.existingSecretKey -}}
{{- end -}}

{{/*
Only used inside templates/redis-secret.yaml, and only when we own the
secret (redis.auth.existingSecret is empty) -- so this is evaluated exactly
once per release, never duplicated across files. Everything else (redis
requirepass, backend/worker/migration REDIS_PASSWORD) reads the resulting
value back via secretKeyRef rather than recomputing it, so a fresh
randAlphaNum on every `include` call can't produce mismatched passwords
across resources.
*/}}
{{- define "paddledoc.redisGeneratedPassword" -}}
{{- if .Values.redis.auth.password -}}
{{- .Values.redis.auth.password -}}
{{- else -}}
{{- $secretName := printf "%s-redis-auth" (include "paddledoc.fullname" .) -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace $secretName -}}
{{- if $existing -}}
{{- index $existing.data "password" | b64dec -}}
{{- else -}}
{{- randAlphaNum 32 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
REDIS_URL as an env `value:`, relying on Kubernetes' $(VAR) env-substitution
(the same trick backend/worker/migration-job already use for
DATABASE_URL/$(DATABASE_PASSWORD)) so the real password -- read from a
Secret via a sibling REDIS_PASSWORD env entry -- is spliced in by the
kubelet, never re-derived here.
*/}}
{{- define "paddledoc.redisUrl" -}}
{{- if include "paddledoc.redisAuthEffective" . -}}
redis://:$(REDIS_PASSWORD)@{{ include "paddledoc.redisHost" . }}:{{ .Values.redis.port }}/0
{{- else -}}
redis://{{ include "paddledoc.redisHost" . }}:{{ .Values.redis.port }}/0
{{- end -}}
{{- end -}}

{{/*
True/non-empty when a SECRET_KEY source is actually configured, i.e. when
auth.secretKey.existingSecret or auth.secretKey.value is set. Used both to
gate whether the SECRET_KEY env entry is rendered and, combined with
auth.required, to `fail` template rendering with a clear message instead of
shipping pods that crash-loop at startup (see app/core/config.py's own
fail-fast for the same condition).
*/}}
{{- define "paddledoc.secretKeyEffective" -}}
{{- if .Values.auth.secretKey.existingSecret -}}
true
{{- else if .Values.auth.secretKey.value -}}
true
{{- end -}}
{{- end -}}

{{- define "paddledoc.secretKeySecretName" -}}
{{- if .Values.auth.secretKey.existingSecret -}}
{{- .Values.auth.secretKey.existingSecret -}}
{{- else -}}
{{- printf "%s-auth" (include "paddledoc.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "paddledoc.secretKeySecretKey" -}}
{{- if .Values.auth.secretKey.existingSecret -}}
{{- default "secret-key" .Values.auth.secretKey.existingSecretKey -}}
{{- else -}}
secret-key
{{- end -}}
{{- end -}}

{{/*
The fail-fast guard shared by backend/worker/migration-job: SECRET_KEY is
required by the app itself for any non-sqlite deployment, and this chart
only ever deploys against external PostgreSQL (see database.useExternal),
so auth.required=true (the default) turns a missing SECRET_KEY into an
immediate, clear `helm template`/`install` failure rather than a runtime
crash-loop discovered later. Usage: {{ include "paddledoc.requireSecretKey" . }}
*/}}
{{- define "paddledoc.requireSecretKey" -}}
{{- if and .Values.auth.required (not (include "paddledoc.secretKeyEffective" .)) -}}
{{- fail "auth.required=true (the default) but no SECRET_KEY is configured. Set auth.secretKey.existingSecret (name of a pre-existing Secret) or auth.secretKey.value. SECRET_KEY signs session cookies/OIDC state and derives the OIDC client-secret encryption key. Set auth.required=false only for trusted, non-production use -- the app itself still fails fast at startup for any non-sqlite database." -}}
{{- end -}}
{{- end -}}

{{/*
PUBLIC_API_URL: the base URL the backend is externally reachable at, used to
build the OIDC redirect_uri. Explicit auth.publicApiUrl always wins; else
derive from ingress.backend.hosts[0] (https if ingress.backend.tls is set,
else http) when ingress.backend.enabled; else fall back to
http://localhost:<backend.service.port>, matching app/core/config.py's own
default so a bare `helm template` (no ingress, no override) still renders a
sensible value.
*/}}
{{- define "paddledoc.publicApiUrl" -}}
{{- if .Values.auth.publicApiUrl -}}
{{- .Values.auth.publicApiUrl -}}
{{- else if and .Values.ingress.backend.enabled .Values.ingress.backend.hosts -}}
{{- $scheme := "http" -}}
{{- if .Values.ingress.backend.tls -}}
{{- $scheme = "https" -}}
{{- end -}}
{{- printf "%s://%s" $scheme (index .Values.ingress.backend.hosts 0).host -}}
{{- else -}}
{{- printf "http://localhost:%v" .Values.backend.service.port -}}
{{- end -}}
{{- end -}}
