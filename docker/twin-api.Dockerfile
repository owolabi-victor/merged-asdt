# Twin API (service_layer.twin_api).
#
# Replaces the compose service that ran `pip install ... && python ...` inside
# `command:` on a bare python:3.11-slim. That re-downloaded every package on
# each container start - slow, and unreproducible, because nothing was pinned.
# Dependencies are resolved once here, into the image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copied before the source so a code change does not re-run the install layer.
COPY docker/requirements-twin-api.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# The same four trees the compose service bind-mounted read-only.
COPY service_layer/ /app/service_layer/
COPY shared/        /app/shared/
COPY ui/            /app/ui/
COPY intelligent/   /app/intelligent/

EXPOSE 8080
CMD ["python", "-c", "import uvicorn; uvicorn.run('service_layer.twin_api:app', host='0.0.0.0', port=8080)"]
