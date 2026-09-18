# ITCS355 Lab 1 — training image
#
# Pin the official image index; build with --platform linux/amd64.
# Verified 2026-09-08: Python 3.11.16, slim-trixie, with an amd64 manifest.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependencies first so this layer caches independently of your source.
COPY requirements.txt ./
# Reject package artifacts that do not match the lock file's hashes.
RUN pip install --require-hashes --prefix=/install -r requirements.txt


FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime

# Non-root. A training container has no reason to run as root, and graders check.
RUN useradd --create-home --uid 10001 runner
ENV PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=20260101 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Remove base packages before copying their locked replacements.
RUN python -m pip uninstall -y packaging setuptools

COPY --from=builder /install /usr/local
RUN python -m pip check
WORKDIR /app
COPY --chown=runner:runner src/ ./src/
COPY --chown=runner:runner cloudlayer/ ./cloudlayer/
COPY --chown=runner:runner scripts/ ./scripts/

USER runner

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
LABEL org.opencontainers.image.revision=${GIT_COMMIT}

# Credentials NEVER enter an image layer. They arrive at runtime from SECRET_STORE_PATH
# or from the platform's identity. If you find yourself adding an ARG for a key, stop.
ENTRYPOINT ["python", "-m", "src.train"]
CMD ["--n-estimators", "200", "--max-depth", "8"]
