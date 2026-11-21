{{/* The SeaweedFS subchart's resource name prefix (its own fullname rule). */}}
{{- define "axiom-cluster.seaweedfsName" -}}
{{- $sw := default dict .Values.seaweedfs -}}
{{- if $sw.fullnameOverride -}}{{ $sw.fullnameOverride }}
{{- else if contains "seaweedfs" .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-seaweedfs" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end -}}
{{- end -}}
