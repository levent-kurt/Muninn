# Syntax: https://docs.docker.com/reference/dockerfile/
#
# One image serves two roles: the API gateway and the scrape worker. They are
# the same code, started with a different command (see docker-compose.yml).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Chromium and its OS libraries first, so the (slow) browser download is not
# invalidated by every source change. --with-deps already installs the system
# packages, so a separate `playwright install-deps` call is not needed.
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

# Application code. .dockerignore keeps .git, .venv, tests, caches and local
# working documents out of the build context.
COPY . .

# Run unprivileged. Chromium is launched with --no-sandbox (see
# SCRAPE_NO_SANDBOX), so the container's user must not be root either.
RUN useradd --create-home --uid 10001 muninn \
    && mkdir -p /app/data \
    && chown -R muninn:muninn /app
USER muninn

# Persistent cache database and worker log live in a mounted volume.
VOLUME ["/app/data"]

# 8000 = API gateway, 8765 = scrape worker (internal only; not published).
EXPOSE 8000 8765

# Liveness without curl (python:slim does not ship it).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
