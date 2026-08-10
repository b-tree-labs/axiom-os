{{/*
axiom-secrets Helm chart — helpers. Naming + labels mirror the
data_platform / observability charts; plus address/seal helpers for the
OpenBao server and the extension wiring.
*/}}

{{- define "axiom-secrets.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "axiom-secrets.fullname" -}}
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

{{- define "axiom-secrets.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "axiom-secrets.labels" -}}
helm.sh/chart: {{ include "axiom-secrets.chart" . }}
{{ include "axiom-secrets.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: axiom-secrets
{{- end }}

{{- define "axiom-secrets.selectorLabels" -}}
app.kubernetes.io/name: {{ include "axiom-secrets.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "axiom-secrets.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "axiom-secrets.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* http when TLS is disabled, https otherwise. */}}
{{- define "axiom-secrets.scheme" -}}
{{- if .Values.server.listener.tlsDisable }}http{{ else }}https{{ end }}
{{- end }}

{{/* In-cluster client address — the value of AXIOM_OPENBAO_URL. */}}
{{- define "axiom-secrets.address" -}}
{{ include "axiom-secrets.scheme" . }}://{{ include "axiom-secrets.fullname" . }}:{{ .Values.service.port }}
{{- end }}

{{/* api_addr in the HCL config (advertised address of this node). */}}
{{- define "axiom-secrets.apiAddr" -}}
{{ include "axiom-secrets.scheme" . }}://{{ include "axiom-secrets.fullname" . }}:{{ .Values.service.port }}
{{- end }}

{{/* cluster_addr for the raft backend (per-pod DNS, cluster port 8201). */}}
{{- define "axiom-secrets.clusterAddr" -}}
{{ include "axiom-secrets.scheme" . }}://{{ include "axiom-secrets.fullname" . }}-0.{{ include "axiom-secrets.fullname" . }}-internal:8201
{{- end }}
