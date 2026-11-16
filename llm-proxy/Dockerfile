FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Standard library only: nothing is installed in the container holding the key.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin llmproxy
WORKDIR /app
COPY server.py ./

USER llmproxy
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"
CMD ["python", "server.py"]
