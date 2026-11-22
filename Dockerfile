# Same base image the commits were labeled in (its Python decides which tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV SKIP_INTEGRATION=0 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The exact packages of the labeling environment, so tests behave as labeled.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, from its own repository.
RUN python -c "import urllib.request,tarfile,io; \
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(d)).extractall('/app', filter='tar')" \
 && mv /app/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /app/sqlglot

COPY selector.py map.json.gz lines.json.gz /app/

# The run's disk is read-only: precompile so imports never recompile.
RUN (python -m compileall -q /app/sqlglot /app/selector.py || true) \
 && chmod -R a+rX /app

WORKDIR /app/sqlglot
CMD ["python", "/app/selector.py"]
