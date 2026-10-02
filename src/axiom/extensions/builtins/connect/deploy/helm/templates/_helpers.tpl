{{- define "axi-presence.slug" -}}
{{- required "slug is required (k8s-safe per-principal name, e.g. axi-bens)" .Values.slug -}}
{{- end -}}

{{- define "axi-presence.secretName" -}}
{{- .Values.secretName | default (include "axi-presence.slug" .) -}}
{{- end -}}
