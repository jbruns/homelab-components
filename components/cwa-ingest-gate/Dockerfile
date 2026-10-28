# Built on the pinned Calibre-Web Automated image (upstreams.yaml; keep the two pins together), not distroless: the
# gate drives the calibredb that writes the Library, and only the same calibre as the app's may write its
# metadata.db. Its s6 init is replaced: this image runs one process, nonroot by default; Compose sets the Library's
# owner as the user.
FROM crocodilestick/calibre-web-automated:v4.0.8@sha256:5e00373854247750cc3e4479b492ae09293ff5e06ed10177f226634d97888679
COPY src/ /gate/
# calibredb ships in /app/calibre, which only the s6 init puts on PATH; its settings go to a writable temp folder.
ENV PATH=/app/calibre:$PATH CALIBRE_CONFIG_DIRECTORY=/tmp/calibre-config \
    PYTHONPATH=/gate PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 9715
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python3", "-m", "cwa_ingest_gate", "healthcheck"]
ENTRYPOINT ["python3", "-m", "cwa_ingest_gate"]
CMD []
