# Syntax: https://docs.docker.com/reference/dockerfile/

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Python dependencies first for better layer caching. Browser + system deps
# are installed together so both the API (search) and the scrape worker
# (browser pool) can run in the same image.
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && python -m playwright install-deps chromium

# Application code (all packages: search + scrape module).
COPY app ./app
COPY drivers ./drivers
COPY schemas ./schemas
COPY parsers ./parsers
COPY fetchers ./fetchers
COPY browser_pool ./browser_pool
COPY ops ./ops
COPY services ./services
COPY routers ./routers

# Persistent cache database lives in a mounted volume.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000
EXPOSE 8765

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]