{{- define "shop.labels" -}}
app.kubernetes.io/part-of: shop
app.kubernetes.io/managed-by: {{ .Release.Service }}
clustermotion.io/color: {{ .Values.color }}
{{- end }}

{{- define "shop.image" -}}
{{ .root.Values.image.registry }}/clustermotion/{{ .name }}:{{ .root.Values.image.tag }}
{{- end }}

{{- define "shop.otherColor" -}}
{{- if eq .Values.color "blue" }}green{{ else }}blue{{ end -}}
{{- end }}

{{- define "shop.awsEnv" -}}
- name: AWS_REGION
  value: {{ .Values.awsRegion | quote }}
- name: CLUSTER_NAME
  value: {{ .Values.color | quote }}
{{- end }}

{{- define "shop.dbEnv" -}}
- name: DB_HOST
  value: {{ .Values.db.host | quote }}
- name: DB_NAME
  value: {{ .Values.db.name | quote }}
- name: DB_USER
  valueFrom:
    secretKeyRef: {name: orders-db-credentials, key: username}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef: {name: orders-db-credentials, key: password}
{{- end }}
