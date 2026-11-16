FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY bridge.py opencode.py ./

# State (thread -> session map) lives on a volume owned by the app user.
# UID 1000 matches the owner of the existing bridge-data volume.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin bridge \
 && mkdir -p /data && chown bridge:bridge /data

USER bridge
CMD ["python", "bridge.py"]
