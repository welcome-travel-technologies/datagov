# The Document Analyzer: luccy's UNCHANGED RAG app, served live from DataGov's Qdrant.
#
# Base: the luccy image, as built from luccy's own serving/deploy/Dockerfile. LUCCY_IMAGE MUST
# be built from the luccy branch feat/engine-whole-pages: that branch leaves every original
# luccy file unchanged and adds document_explorer/stitch.py (stitch_document, normalize), the
# shared stitcher this adapter rebuilds whole pages with. The build checks it: a luccy image
# without the stitcher, or whose luccy code differs from the version pinned in
# luccy_qdrant/pins.py, fails to build (the RUN below), so a mismatch never reaches a host.
#   docker build --build-arg LUCCY_IMAGE=ghcr.io/welcome-travel-technologies/luccy@sha256:<digest> analyzer/
#
# Nothing in /app changes. The package goes to /opt/datagov-analyzer; the hooks it installs
# live in memory only (luccy_qdrant/hooks.py, STORAGE_MAP.md). No page, index or catalog file
# exists in this container: do not mount luccy's /data. The only writable state is luccy's
# operational log folder (answer records that feedback and /records look up, chats, notes,
# page errors, the spend ledger); it is not document data and belongs on a named volume at
# /app/serving/logs (SERVING_LOG_DIR).
#
# Commands (no ENTRYPOINT, so compose overrides CMD for one-off jobs):
#   python -m luccy_qdrant.serve     the web app: luccy's gunicorn command on the hooked app
#   python -m luccy_qdrant.sync      DataGov corpus export -> luccy payload + luccy_kw alias
#   python -m luccy_qdrant.hooks     check the pins against this image's luccy (--app: and install them)
ARG LUCCY_IMAGE=luccy:local
FROM ${LUCCY_IMAGE}

COPY luccy_qdrant /opt/datagov-analyzer/luccy_qdrant

# /opt/datagov-analyzer for the adapter, /app for luccy (the image's editable install puts it
# on the path too; listing it keeps that true for any interpreter flag). luccy's own knobs the
# live corpus needs: the stemming keyword leg and Qdrant meaning search (both luccy defaults).
# Two answers at once (luccy's own default of 4 peaked above the analyzer's 6 GiB); a
# deployment may set its own.
ENV PYTHONPATH=/opt/datagov-analyzer:/app \
    LUCCY_KW_STEM=1 \
    LUCCY_VECTOR_STORE=qdrant \
    LUCCY_MAX_CONCURRENT_ANSWERS=2 \
    SERVING_LOG_DIR=/app/serving/logs

# The pins, the stitcher and the hooks on luccy's app, checked once here (no Qdrant, no key).
RUN python -m luccy_qdrant.hooks --app

VOLUME ["/app/serving/logs"]

# luccy's HEALTHCHECK (python urlopen on http://127.0.0.1:$PORT/health, start period 300 s)
# is inherited unchanged; /health is the live one and answers 503 with its reason (e.g.
# "luccy_kw alias missing: run analyzer-sync") until the first sync has run.
CMD ["python", "-m", "luccy_qdrant.serve"]
