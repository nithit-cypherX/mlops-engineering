# Lab 5 pipeline image, adapted from Lab 2 at 18f4ad4.
# Build context: lab/lab5/. Target platform: linux/amd64.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY requirements.txt ./
RUN pip install --require-hashes --prefix=/install -r requirements.txt

FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime

RUN useradd --create-home --uid 10001 runner
ENV PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=20260101 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Retain Lab 2's fix for conflicting base-image package metadata.
RUN python -m pip uninstall -y packaging setuptools
COPY --from=builder /install /usr/local
RUN python -m pip check
WORKDIR /app
COPY --chown=runner:runner src/ ./src/
COPY --chown=runner:runner cloudlayer/ ./cloudlayer/
COPY --chown=runner:runner scripts/ ./scripts/
USER runner

# A declared revision, not proof of image contents. No credential build arguments.
ARG GIT_COMMIT
ENV GIT_COMMIT=${GIT_COMMIT}
RUN python -c "import os, re; assert re.fullmatch(r'[0-9a-f]{40}', os.environ.get('GIT_COMMIT', '')), 'GIT_COMMIT must be a full lowercase Git SHA'"
LABEL org.opencontainers.image.revision=${GIT_COMMIT}

# The pipeline chooses the step command. Starting the image alone does no work.
ENTRYPOINT []
CMD ["python", "--help"]
