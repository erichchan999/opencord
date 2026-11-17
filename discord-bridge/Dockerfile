FROM python:3.13-slim-trixie AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY *.py ./

# Unit tests. Not part of the deployed image; run them with
#   docker build --target test --progress plain discord-bridge
FROM base AS test
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
RUN python -m pytest -q tests

FROM base

# State (thread map, settings, open questions) lives on the bridge-data volume,
# owned by the app user. The UID stays 1000 so it keeps owning the files there.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin bridge \
 && mkdir -p /data && chown bridge:bridge /data

USER bridge
CMD ["python", "bridge.py"]
