# Octopus in a Linux container (P22). macOS runs natively (MLX, Vision, Keychain, launchd): this image is for
# Linux servers / NAS. Data, config.toml, secrets.toml and the model caches all live in the /data volume.
#
#   docker compose run --rm octopus init      # writes /data/config.toml (data_dir = "/data"; English unless --language zh)
#   docker compose up -d                       # octopus run
#
# Tests in the same Linux environment:  docker build --target test -t octopus-test . && docker run --rm octopus-test

FROM python:3.12-slim AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.7.10 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# dependencies first: this layer is reused while only the code changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# the project's metadata names its readme and license file: the build needs both
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --create-home --uid 1000 octopus && mkdir /data && chown octopus:octopus /data
ENV PATH=/app/.venv/bin:$PATH \
    OCTOPUS_DATA_DIR=/data \
    XDG_CACHE_HOME=/data/.cache \
    HF_HOME=/data/.cache/huggingface \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

FROM base AS test
# git, in this stage only (never in the runtime image): test_export_public.py runs on throwaway repositories;
# test_no_private_data.py's checkout scans still skip, as the build context has no .git
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN uv sync --frozen
COPY tests ./tests
# test_shortcuts.py builds the macOS Shortcuts' plists (plain Python) and checks the signed files are there;
# test_extension_locales.py reads the Chrome extension's messages (P25)
COPY extension/shortcuts ./extension/shortcuts
COPY extension/chrome ./extension/chrome
# test_export_public.py / test_no_private_data.py load scripts/export_public.py
COPY scripts ./scripts
USER octopus
# the conftest gives every test its own HOME; no cache directory under /app (not writable by this user)
CMD ["python", "-m", "pytest", "-q", "-W", "error::RuntimeWarning", "-p", "no:cacheprovider"]

FROM base
USER octopus
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["octopus"]
CMD ["run"]
