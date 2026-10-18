# The restic CLI, copied from the same pinned image the Contract Tests run (upstreams.yaml); keep the two together.
FROM restic/restic:0.19.0@sha256:7f44e0057b82348597568ea209360762d0b38f8e1dbc8ad859661ac1055e45f2 AS restic

# Stdlib-only Python on distroless: no shell, no package manager, runs as nonroot (65532).
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:f47f5807043391605425f2bd162b25f50cebdc90640bacc804e0177608fe5c65
COPY --from=restic /usr/bin/restic /usr/bin/restic
COPY src/ /app/
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8002
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["/usr/bin/python3", "-m", "backup_coverage", "healthcheck"]
ENTRYPOINT ["/usr/bin/python3", "-m", "backup_coverage"]
