# Build only off the 2GiB ECS, from a pinned official Python image.
# PYTHON_BASE must be python:3.11-slim-bookworm@sha256:<verified digest>.
ARG PYTHON_BASE
FROM ${PYTHON_BASE} AS wheel-builder
WORKDIR /build
COPY packages/engine/pyproject.toml packages/engine/README.md packages/engine/
COPY packages/engine/src/ packages/engine/src/
COPY apps/server/pyproject.toml apps/server/README.md apps/server/
COPY apps/server/src/ apps/server/src/
# Install build frontend from the same hash-locked profile.
COPY deploy/saas/requirements-build.lock /tmp/requirements-build.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-build.lock \
 && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels packages/engine apps/server

ARG PYTHON_BASE
FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    REQPILOT_STORAGE_DIR=/var/lib/reqpilotx \
    REQPILOT_OBJECT_ROOT=/var/lib/reqpilotx \
    REQPILOT_SCRATCH_ROOT=/var/lib/reqpilotx/scratch \
    REQPILOT_FRONTEND_DIR=/opt/reqpilotx/frontend
COPY deploy/saas/requirements-runtime.lock /tmp/requirements-runtime.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-runtime.lock
COPY --from=wheel-builder /wheels/ /tmp/wheels/
RUN python -m pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels /tmp/requirements-runtime.lock \
 && groupadd --gid 10001 reqpilotx \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin reqpilotx
COPY apps/web/dist/ /opt/reqpilotx/frontend/
COPY deploy/saas/entrypoint.py deploy/saas/preflight.py /opt/reqpilotx/deploy/
WORKDIR /opt/reqpilotx
USER 10001:10001
ENTRYPOINT ["python", "/opt/reqpilotx/deploy/entrypoint.py"]
CMD ["api"]
