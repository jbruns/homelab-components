# Stdlib-only Python on distroless: no shell, no package manager, runs as nonroot (65532).
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:f47f5807043391605425f2bd162b25f50cebdc90640bacc804e0177608fe5c65
COPY src/ /app/
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 9100
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["/usr/bin/python3", "-m", "template_exporter", "healthcheck"]
ENTRYPOINT ["/usr/bin/python3", "-m", "template_exporter"]
