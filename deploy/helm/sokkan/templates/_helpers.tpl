{{/* Names -------------------------------------------------------------------------------- */}}
{{- define "sokkan.fullname" -}}
{{- if contains "sokkan" .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-sokkan" .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "sokkan.labels" -}}
app.kubernetes.io/name: sokkan
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "sokkan.selector" -}}
app.kubernetes.io/name: sokkan
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "sokkan.image" -}}
{{- $repo := .image.repository -}}
{{- $reg := .root.Values.global.imageRegistry -}}
{{- $first := first (splitList "/" $repo) -}}
{{- if and $reg (not (or (contains "." $first) (contains ":" $first))) -}}
{{- $repo = printf "%s/%s" $reg $repo -}}
{{- end -}}
{{- printf "%s:%s" $repo (default .root.Chart.AppVersion .image.tag | toString) -}}
{{- end -}}

{{- define "sokkan.dataClaim" -}}
{{- default (printf "%s-data" (include "sokkan.fullname" .)) .Values.persistence.existingClaim -}}
{{- end -}}

{{- define "sokkan.secretName" -}}
{{- default (printf "%s-env" (include "sokkan.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "sokkan.dbSecretName" -}}
{{- if .Values.database.existingSecret -}}{{ .Values.database.existingSecret }}
{{- else -}}{{ printf "%s-db" (include "sokkan.fullname" .) }}{{- end -}}
{{- end -}}

{{/* Pod-level security context: restricted profile; uid/gid/fsGroup omitted when null
     (OpenShift assigns them from the namespace range). */}}
{{- define "sokkan.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- with .Values.podSecurity.runAsUser }}
runAsUser: {{ . }}
{{- end }}
{{- with .Values.podSecurity.runAsGroup }}
runAsGroup: {{ . }}
{{- end }}
{{- with .Values.podSecurity.fsGroup }}
fsGroup: {{ . }}
{{- end }}
{{- end -}}

{{- define "sokkan.containerSecurityContext" -}}
allowPrivilegeEscalation: false
privileged: false
runAsNonRoot: true
readOnlyRootFilesystem: {{ .readOnly | default false }}
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "sokkan.pullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Secrets provider (3.3): instance id, OpenBao address, whether the api mounts its SA token */}}
{{- define "sokkan.secretsInstance" -}}
{{- default .Release.Name .Values.secrets.instanceId -}}
{{- end -}}

{{- define "sokkan.openbaoAddr" -}}
{{- if .Values.openbao.address -}}{{ .Values.openbao.address }}
{{- else if .Values.openbao.enabled -}}
{{- printf "%s://%s-openbao.%s.svc:8200" (ternary "https" "http" (ne .Values.openbao.tls.secretName "")) (include "sokkan.fullname" .) .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- define "sokkan.apiNeedsSAToken" -}}
{{- if or (eq .Values.runner.mode "kubernetes") (eq .Values.secrets.provider "kubernetes") (and (eq .Values.secrets.provider "openbao") (eq .Values.openbao.auth.method "kubernetes")) -}}true{{- else -}}false{{- end -}}
{{- end -}}
