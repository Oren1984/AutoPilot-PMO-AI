# AutoPilot PMO AI - single image used by the three Compose services
# (demo enterprise app, agent backend with headless Chromium, Streamlit console).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 1) dependencies (cached layer): install from pyproject with a stub package
# (README.md is stubbed so documentation edits do not invalidate this layer)
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py README.md \
    && pip install ".[ui]" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# 2) application code
COPY app ./app
COPY ui ./ui
RUN pip install --no-deps . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data && chown appuser:appuser /data

USER appuser
EXPOSE 8000 8100 8501
CMD ["uvicorn", "app.api.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
