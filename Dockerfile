# syntax=docker/dockerfile:1.17
#
# The ASPEN-NET transcription service: the official Hermes Agent image, plus
# faster-whisper and yt-dlp in their own Python 3.12 venv, plus the
# aspen-net-skills skills baked in read-only.
#
# Skills come from the `skills-repo` stage: a git clone of
# quantum-aspen/aspen-net-skills at SKILLS_REF. To build against a local
# checkout instead (e.g. to try skill edits before pushing them):
#   docker build --build-context skills-repo=../aspen-net-skills .

ARG SKILLS_REF=main

FROM ghcr.io/astral-sh/uv:0.11.19@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 AS uv

FROM scratch AS skills-repo
ARG SKILLS_REF
ADD https://github.com/quantum-aspen/aspen-net-skills.git#${SKILLS_REF} /

FROM nousresearch/hermes-agent:v0.22.5@sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/transcriber/python \
    UV_PROJECT_ENVIRONMENT=/opt/transcriber/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /opt/transcriber/app

# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python 3.12

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Root-owned and read-only to the hermes user, so the agent (or Hermes'
# skill curator) can't modify the skills it runs.
COPY --from=skills-repo /skills /opt/transcriber/skills
RUN chmod -R a+rX,go-w /opt/transcriber/skills

# Our venv after Hermes' shim, ahead of Hermes' venv: `transcriber`, `yt-dlp`
# and python3 resolve to ours; `hermes` stays Hermes'.
ENV PATH=/opt/hermes/bin:/opt/transcriber/.venv/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    TRANSCRIBER_DATA=/opt/data/transcriber \
    SKILLS_DIR=/opt/transcriber/skills

# The image's entrypoint (s6 /init) fixes /opt/data ownership, then runs
# this as the hermes user. Keep it: it also reaps orphaned processes.
WORKDIR /opt/data
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s \
    CMD ["/opt/transcriber/.venv/bin/python", "-c", "import os,sys,time; sys.exit(time.time() - os.path.getmtime('/opt/data/transcriber/heartbeat') > 180)"]
CMD ["transcriber", "serve"]
