# The ASDT layer processes (dt_network.ingestor, data_mgmt.pipeline,
# reactive.rule_engine, ...). One image, the layer chosen by `command:`.
#
# Deliberately leaner than twin-api.Dockerfile: no langchain, which is most of
# that image's 659 MB and is only needed by the intelligent layer.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY docker/requirements-layers.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY config/        /app/config/
COPY shared/        /app/shared/
COPY data_layer/    /app/data_layer/
COPY data_mgmt/     /app/data_mgmt/
COPY dt_network/    /app/dt_network/
COPY physical/      /app/physical/
COPY reactive/      /app/reactive/
COPY service_layer/ /app/service_layer/
COPY simulation/    /app/simulation/

# Overridden per service. The ingestor is the one that must run for readings to
# reach InfluxDB at all.
CMD ["python", "-m", "dt_network.ingestor"]
