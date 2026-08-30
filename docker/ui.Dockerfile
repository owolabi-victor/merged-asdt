# Streamlit dashboards. One image serves both portals; they differ only in the
# script and port, which compose supplies as build args.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY docker/requirements-ui.txt /tmp/requirements.txt
# --timeout/--retries: PyPI reads time out on slow links, and a droplet
# build should not fail on one stalled download.
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r /tmp/requirements.txt

COPY ui/ /app/ui/

# Which portal this image runs. Overridden per service in compose.
ARG APP_SCRIPT=ui/app_scientist.py
ARG APP_PORT=8501
ENV APP_SCRIPT=${APP_SCRIPT} APP_PORT=${APP_PORT}

EXPOSE ${APP_PORT}
CMD ["sh", "-c", "streamlit run \"$APP_SCRIPT\" --server.port \"$APP_PORT\" --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false"]
