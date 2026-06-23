{{/*
axiom-data-platform Helm chart — helpers.
Common labels + naming + the dataPlatform env block used by webserver,
daemon, and the init job.
*/}}

{{- define "axiom-data-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "axiom-data-platform.fullname" -}}
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

{{- define "axiom-data-platform.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "axiom-data-platform.labels" -}}
helm.sh/chart: {{ include "axiom-data-platform.chart" . }}
{{ include "axiom-data-platform.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: axiom-data-platform
{{- end }}

{{- define "axiom-data-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "axiom-data-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Postgres host + DSN — switches on `postgres.mode`.
External points at the operator-supplied service; internal at the
chart's own StatefulSet.
*/}}
{{- define "axiom-data-platform.postgresHost" -}}
{{- if eq .Values.database.mode "external" -}}
{{- .Values.database.external.host -}}
{{- else -}}
{{- printf "%s-postgres" (include "axiom-data-platform.fullname" .) -}}
{{- end -}}
{{- end }}

{{- define "axiom-data-platform.postgresPort" -}}
{{- if eq .Values.database.mode "external" -}}{{ .Values.database.external.port }}{{- else -}}5432{{- end -}}
{{- end }}

{{- define "axiom-data-platform.postgresDatabase" -}}
{{- if eq .Values.database.mode "external" -}}{{ .Values.database.external.database }}{{- else -}}{{ .Values.database.internal.database }}{{- end -}}
{{- end }}

{{- define "axiom-data-platform.postgresUsername" -}}
{{- if eq .Values.database.mode "external" -}}{{ .Values.database.external.username }}{{- else -}}{{ .Values.database.internal.username }}{{- end -}}
{{- end }}

{{- define "axiom-data-platform.postgresSecret" -}}
{{- if eq .Values.database.mode "external" -}}
{{- .Values.database.external.passwordSecret -}}
{{- else -}}
{{- printf "%s-postgres" (include "axiom-data-platform.fullname" .) -}}
{{- end -}}
{{- end }}

{{- define "axiom-data-platform.ragDsn" -}}
postgresql://{{ include "axiom-data-platform.postgresUsername" . }}:$(POSTGRES_PASSWORD)@{{ include "axiom-data-platform.postgresHost" . }}:{{ include "axiom-data-platform.postgresPort" . }}/{{ include "axiom-data-platform.postgresDatabase" . }}
{{- end }}

{{/*
Common env block for dagster pods + init job. Bidirectional A2A note:
the install + diagnose CLI also read these names — if you rename, the
diagnose checks won't find them in the running pods.
*/}}
{{- define "axiom-data-platform.commonEnv" -}}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "axiom-data-platform.postgresSecret" . }}
      key: password
- name: DAGSTER_PG_USER
  value: {{ include "axiom-data-platform.postgresUsername" . | quote }}
- name: DAGSTER_PG_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "axiom-data-platform.postgresSecret" . }}
      key: password
- name: DAGSTER_PG_HOST
  value: {{ include "axiom-data-platform.postgresHost" . | quote }}
- name: DAGSTER_PG_PORT
  value: {{ include "axiom-data-platform.postgresPort" . | quote }}
- name: DAGSTER_PG_DB
  value: {{ .Values.database.dagsterDatabase | quote }}
# Source-kind specifics (Box folder id, GDrive drive id, S3 bucket, …)
# are NOT in env. They live in connector TOMLs under
# `$AXI_STATE/plinth/connectors/`; the Dagster sensor reads them from
# there and dispatches to the kind's SourceKindProvider at runtime.
- name: DP1_BRONZE_ROOT
  value: {{ .Values.bronze.mountPath | quote }}
- name: DP1_PROVENANCE_RULES_FILE
  value: "/etc/axiom/rules/rules.toml"
- name: DP1_RAG_DSN
  value: {{ include "axiom-data-platform.ragDsn" . | quote }}
{{- end }}
