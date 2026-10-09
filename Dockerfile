# iot-bot v2 image for the Raspberry Pi (Ubuntu 16.04 armhf host, Docker 20.10.7).
# Build and run with deploy/docker.sh; see README "Docker on the Pi".
# Works with the legacy builder (no BuildKit features) because that is what Docker 20.10 uses by default.

FROM python:3.13-slim AS build
# cffi has no armv7 wheel and compiles here; cryptography ships one (needs glibc >= 2.31, the image has 2.41)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.10.2
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first so code changes do not rebuild cffi
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY iotbot ./iotbot
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
# uid 1001 = johnson on the Pi, so files written to the mounted /data stay owned by that user
RUN useradd --uid 1001 --no-create-home --shell /usr/sbin/nologin iotbot
COPY --from=build /app/.venv /app/.venv
COPY commands.json /app/commands.json
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    COMMANDS_PATH=/app/commands.json
USER 1001
WORKDIR /data
ENTRYPOINT ["iotbot", "--env-file", "/data/.env"]
