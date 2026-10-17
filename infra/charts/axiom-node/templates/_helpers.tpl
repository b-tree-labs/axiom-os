{{/* One image reference for every role: digest wins over tag. */}}
{{- define "axiom-node.image" -}}
{{- if .Values.image.digest -}}{{ .Values.image.repository }}@{{ .Values.image.digest }}{{- else -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}
{{- end -}}

{{- define "axiom-node.labels" -}}
app.kubernetes.io/name: axiom-node
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .role }}
{{- with .slot }}
axiom.io/slot: {{ . }}
{{- end }}
{{- end -}}

{{/*
Resources come from the profile; a role may override. Realtime roles get
guaranteed resources (requests = limits, so they are the last to be evicted);
background roles request half and are throttled first.
*/}}
{{- define "axiom-node.resources" -}}
{{- if eq (toString .cfg.resources) "none" }}
{{- /* No requests or limits: a laptop's development database sized by the machine. */}}
{{- else }}
{{- $p := index .root.Values.resources .root.Values.profile -}}
{{- $r := default $p .cfg.resources -}}
{{- if eq (default "background" .priority) "realtime" }}
resources:
  requests: {cpu: {{ $r.cpu | quote }}, memory: {{ $r.memory | quote }}}
  limits: {cpu: {{ $r.cpu | quote }}, memory: {{ $r.memory | quote }}}
{{- else }}
resources:
  requests: {cpu: {{ $r.cpu | quote }}, memory: {{ $r.memory | quote }}}
  limits: {memory: {{ $r.memory | quote }}}
{{- end }}
{{- end }}
{{- end -}}

{{- define "axiom-node.priorityClass" -}}
{{ printf "%s-%s" .root.Release.Name (default "background" .priority) }}
{{- end -}}

{{/*
A role that runs the runtime image: one replica (one writer on its volume),
packages installed once into /data by the entrypoint, probes, limits.
Called with dict "root" . "role" name "cfg" values "command" list "port" int
"probePath" string "priority" realtime|background "extraPorts" list of
{name, port}.
*/}}
{{- define "axiom-node.workload" -}}
{{- $fullname := printf "%s-%s" .root.Release.Name .role -}}
{{- if .slot }}{{- $fullname = printf "%s-%s" $fullname .slot -}}{{- end }}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ $fullname }}
  labels: {{- include "axiom-node.labels" . | nindent 4 }}
  annotations:
    axiom.io/role: {{ .role | quote }}
    axiom.io/priority: {{ default "background" .priority | quote }}
    {{- with .cfg.egress }}
    axiom.io/egress: {{ toJson . | quote }}
    {{- end }}
    {{- with .cfg.listen }}
    axiom.io/listen: {{ toJson . | quote }}
    {{- end }}
    {{- if and .port .cfg.publish (not .slot) }}
    axiom.io/publish: {{ printf "%s:%v" .cfg.publish .port | quote }}
    {{- end }}
    {{- with .slot }}
    axiom.io/slot: {{ . | quote }}
    {{- end }}
spec:
  serviceName: {{ $fullname }}
  replicas: 1
  podManagementPolicy: OrderedReady
  updateStrategy: {type: RollingUpdate}
  selector:
    matchLabels: {{- include "axiom-node.labels" . | nindent 6 }}
  template:
    metadata:
      labels: {{- include "axiom-node.labels" . | nindent 8 }}
    spec:
      {{- if .cfg.hostNetwork }}
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      {{- end }}
      priorityClassName: {{ include "axiom-node.priorityClass" . }}
      {{- $watch := default list .cfg.watchVolumes }}
      {{- if or .slot $watch }}
      # Both slots mount the role's one data volume, and a watched volume is
      # mounted where it lives, so these pods share a node.
      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            {{- if .slot }}
            - topologyKey: kubernetes.io/hostname
              labelSelector:
                matchLabels:
                  app.kubernetes.io/instance: {{ .root.Release.Name }}
                  app.kubernetes.io/component: {{ .role }}
            {{- end }}
            {{- range $watch }}
            - topologyKey: kubernetes.io/hostname
              labelSelector:
                matchLabels:
                  app.kubernetes.io/instance: {{ $.root.Release.Name }}
                  app.kubernetes.io/component: {{ .role }}
            {{- end }}
      {{- end }}
      securityContext: {runAsUser: {{ .root.Values.runAsUser }}, runAsNonRoot: true, fsGroup: {{ .root.Values.runAsUser }}}
      {{- $pk0 := default .root.Values.packages .cfg.packages }}
      {{- if .cfg.init }}
      initContainers:
        - name: init
          image: {{ include "axiom-node.image" .root | quote }}
          args: {{ toJson .cfg.init }}
          # Start in the role's own writable volume: tools that default to a
          # path relative to where they start (a collector's journal) work.
          workingDir: /data
          env:
            - {name: AXIOM_PACKAGES, value: {{ $pk0.pin | quote }}}
            - {name: AXIOM_PACKAGES_VERSION, value: {{ $pk0.version | quote }}}
            - {name: AXIOM_NODE_CONFIG, value: /tmp/axiom-node.toml}
            - {name: AXIOM_NODE_TOML, value: {{ include "axiom-node.nodeToml" (dict "root" $.root "cfg" .cfg) | quote }}}
            {{- range $k, $v := .cfg.env }}
            - {name: {{ $k }}, value: {{ $v | quote }}}
            {{- end }}
          {{- if .cfg.secretFrom }}
          envFrom: [{secretRef: {name: {{ .cfg.secretFrom }}}}]
          {{- end }}
          # The init sees what the role sees: a package or a file it installs
          # may come from the role's config.
          volumeMounts:
            - {name: data, mountPath: /data}
            {{- if .cfg.configFrom }}
            - {name: config, mountPath: /config, readOnly: true}
            {{- end }}
            {{- range .cfg.extraConfig }}
            - {name: {{ .name }}, mountPath: {{ .path }}, readOnly: true}
            {{- end }}
      {{- end }}
      containers:
        - name: {{ .role }}
          image: {{ include "axiom-node.image" .root | quote }}
          imagePullPolicy: {{ .root.Values.image.pullPolicy }}
          args: {{ toJson .command }}
          workingDir: /data
          {{- $pk := mergeOverwrite (deepCopy .root.Values.packages) (default dict .cfg.packages) (default dict .slotPackages) }}
          env:
            - {name: AXIOM_PACKAGES, value: {{ $pk.pin | quote }}}
            - {name: AXIOM_PACKAGES_VERSION, value: {{ $pk.version | quote }}}
            - {name: AXIOM_NODE_CONFIG, value: /tmp/axiom-node.toml}
            - {name: AXIOM_NODE_TOML, value: {{ include "axiom-node.nodeToml" (dict "root" $.root "cfg" .cfg) | quote }}}
          {{- with $watch }}
            - {name: AXIOM_DISK_WATCH, value: {{ include "axiom-node.diskWatch" . | quote }}}
          {{- end }}
            {{- if .slot }}
            - {name: AXIOM_ROOT, value: /pkg}
            - {name: AXIOM_SLOT, value: {{ .slot | quote }}}
            {{- end }}
            {{- range $k, $v := .cfg.env }}
            - {name: {{ $k }}, value: {{ $v | quote }}}
            {{- end }}
          {{- if .cfg.secretFrom }}
          envFrom:
            - secretRef: {name: {{ .cfg.secretFrom }}}
          {{- end }}
          {{- if or .port .extraPorts }}
          ports:
            {{- if .port }}
            - {name: http, containerPort: {{ .port }}}
            {{- end }}
            {{- range .extraPorts }}
            - {name: {{ .name }}, containerPort: {{ .port }}}
            {{- end }}
          {{- end }}
          {{- if .probePath }}
          startupProbe:
            httpGet: {path: {{ .probePath }}, port: {{ .port }}}
            periodSeconds: 10
            failureThreshold: 60     # first boot installs the packages
          livenessProbe:
            httpGet: {path: {{ .probePath }}, port: {{ .port }}}
            periodSeconds: 30
          readinessProbe:
            httpGet: {path: {{ .probePath }}, port: {{ .port }}}
            periodSeconds: 10
          {{- end }}
          {{- include "axiom-node.resources" . | nindent 10 }}
          volumeMounts:
            - {name: data, mountPath: /data}
            {{- if .slot }}
            - {name: pkg, mountPath: /pkg}
            {{- end }}
            {{- if .cfg.configFrom }}
            - {name: config, mountPath: /config, readOnly: true}
            {{- end }}
            {{- range .cfg.extraConfig }}
            - {name: {{ .name }}, mountPath: {{ .path }}, readOnly: true}
            {{- end }}
            {{- range $watch }}
            - {name: watch-{{ .label }}, mountPath: /watch/{{ .label }}, readOnly: true}
            {{- end }}
        {{- range .cfg.companions }}
        - name: {{ .name }}
          image: {{ include "axiom-node.image" $.root | quote }}
          args: {{ toJson .command }}
          workingDir: /data
          env:
            - {name: AXIOM_PACKAGES, value: {{ $pk0.pin | quote }}}
            - {name: AXIOM_PACKAGES_VERSION, value: {{ $pk0.version | quote }}}
            - {name: AXIOM_NODE_CONFIG, value: /tmp/axiom-node.toml}
            - {name: AXIOM_NODE_TOML, value: {{ include "axiom-node.nodeToml" (dict "root" $.root "cfg" $.cfg) | quote }}}
            {{- range $k, $v := $.cfg.env }}
            - {name: {{ $k }}, value: {{ $v | quote }}}
            {{- end }}
          {{- if $.cfg.secretFrom }}
          envFrom: [{secretRef: {name: {{ $.cfg.secretFrom }}}}]
          {{- end }}
          resources: {requests: {cpu: {{ default "50m" .cpu | quote }}, memory: {{ default "64Mi" .memory | quote }}}}
          volumeMounts:
            - {name: data, mountPath: /data}
            {{- if $.cfg.configFrom }}
            - {name: config, mountPath: /config, readOnly: true}
            {{- end }}
            {{- range $.cfg.extraConfig }}
            - {name: {{ .name }}, mountPath: {{ .path }}, readOnly: true}
            {{- end }}
        {{- end }}
      {{- if or .cfg.configFrom .dataClaim .cfg.extraConfig $watch }}
      volumes:
        {{- with .dataClaim }}
        - name: data
          persistentVolumeClaim: {claimName: {{ . }}}
        {{- end }}
        {{- if .cfg.configFrom }}
        - name: config
          configMap: {name: {{ .cfg.configFrom }}}
        {{- end }}
        {{- range .cfg.extraConfig }}
        - name: {{ .name }}
          configMap: {name: {{ .from }}}
        {{- end }}
        {{- range $watch }}
        {{- $target := index $.root.Values.roles .role }}
        - name: watch-{{ .label }}
          persistentVolumeClaim:
            claimName: {{ if dig "blueGreen" "enabled" false $target }}{{ printf "%s-%s-data" $.root.Release.Name .role }}{{ else }}{{ printf "data-%s-%s-0" $.root.Release.Name .role }}{{ end }}
            readOnly: true
        {{- end }}
      {{- end }}
  volumeClaimTemplates:
    {{- if .slot }}
    - metadata: {name: pkg}
      spec:
        accessModes: [ReadWriteOnce]
        resources: {requests: {storage: 2Gi}}
    {{- else }}
    - metadata: {name: data}
      spec:
        accessModes: [ReadWriteOnce]
        resources: {requests: {storage: {{ .cfg.storage }}}}
    {{- end }}
{{- if and (or .port .extraPorts) (not .slot) }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $fullname }}
  labels: {{- include "axiom-node.labels" . | nindent 4 }}
spec:
  selector: {{- include "axiom-node.labels" . | nindent 4 }}
  ports:
    {{- if .port }}
    - {name: http, port: {{ .port }}, targetPort: http}
    {{- end }}
    {{- range .extraPorts }}
    - {name: {{ .name }}, port: {{ .port }}, targetPort: {{ .name }}}
    {{- end }}
{{- end }}
{{- end -}}


{{/*
The node's declaration (ADR-164), written by the entrypoint to
$AXIOM_NODE_CONFIG in every container that runs Axiom, so what a role's values
say about agents is what the process enforces, not only what the disclosure
says. A role may widen or narrow the agent policy for itself (`agents`).
*/}}
{{- define "axiom-node.nodeToml" -}}
[node]
role = {{ .root.Values.node.role | quote }}
functions = {{ toJson (default list .root.Values.node.functions) }}
agents = {{ default .root.Values.node.agents .cfg.agents | quote }}
maintenance = {{ .root.Values.node.maintenance | quote }}
{{- end -}}


{{- define "axiom-node.diskWatch" -}}
{{- $items := list -}}
{{- range . }}{{- $items = append $items (printf "%s=/watch/%s" .label .label) -}}{{- end -}}
{{- join "," $items -}}
{{- end -}}
