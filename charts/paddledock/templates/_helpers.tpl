{{- define "paddledock.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "paddledock.fullname" -}}
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

{{- define "paddledock.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "paddledock.labels" -}}
helm.sh/chart: {{ include "paddledock.chart" . }}
app.kubernetes.io/name: {{ include "paddledock.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "paddledock.selectorLabels" -}}
app.kubernetes.io/name: {{ include "paddledock.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "paddledock.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "paddledock.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "paddledock.redisHost" -}}
{{- if .Values.redis.enabled -}}
{{- printf "%s-redis" (include "paddledock.fullname" .) -}}
{{- else -}}
{{- required "redis.host is required when redis.enabled=false" .Values.redis.host -}}
{{- end -}}
{{- end -}}
