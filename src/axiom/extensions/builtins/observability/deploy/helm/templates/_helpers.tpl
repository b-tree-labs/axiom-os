{{/*
axiom-observability Helm chart — helpers.
*/}}

{{- define "axiom-observability.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "axiom-observability.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "axiom-observability.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "axiom-observability.labels" -}}
helm.sh/chart: {{ include "axiom-observability.chart" . }}
{{ include "axiom-observability.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: axiom-observability
{{- end }}

{{- define "axiom-observability.selectorLabels" -}}
app.kubernetes.io/name: {{ include "axiom-observability.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "axiom-observability.postgresHost" -}}
{{- if eq .Values.postgres.mode "external" -}}
{{- .Values.postgres.external.host -}}
{{- else -}}
{{- printf "%s-postgres" (include "axiom-observability.fullname" .) -}}
{{- end -}}
{{- end }}

{{- define "axiom-observability.clickhouseHost" -}}
{{- if eq .Values.clickhouse.mode "external" -}}
{{- .Values.clickhouse.external.url -}}
{{- else -}}
{{- printf "http://%s-clickhouse:8123" (include "axiom-observability.fullname" .) -}}
{{- end -}}
{{- end }}

{{- define "axiom-observability.langfuseEnv" -}}
- name: DATABASE_URL
  value: "postgresql://{{ .Values.postgres.internal.username }}:$(POSTGRES_PASSWORD)@{{ include "axiom-observability.postgresHost" . }}:5432/{{ .Values.postgres.internal.database }}"
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "axiom-observability.fullname" . }}-postgres
      key: password
- name: CLICKHOUSE_URL
  value: {{ include "axiom-observability.clickhouseHost" . | quote }}
- name: CLICKHOUSE_USER
  value: {{ .Values.clickhouse.internal.username | quote }}
- name: CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "axiom-observability.fullname" . }}-clickhouse
      key: password
- name: SALT
  valueFrom:
    secretKeyRef:
      name: {{ include "axiom-observability.fullname" . }}-langfuse
      key: salt
- name: NEXTAUTH_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "axiom-observability.fullname" . }}-langfuse
      key: nextauthSecret
- name: ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "axiom-observability.fullname" . }}-langfuse
      key: encryptionKey
- name: NEXTAUTH_URL
  value: "http://{{ include "axiom-observability.fullname" . }}-web:{{ .Values.service.port }}"
- name: TELEMETRY_ENABLED
  value: "false"
{{- end }}
