FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# OS deps (curl for healthchecks; ca-certificates for HTTPS to Locust/Prom)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# CPU-only torch first (saves ~3 GB vs default CUDA build); the next pip
# install sees torch already satisfies torch>=2.0,<3.0 and skips it.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch
RUN pip install -r requirements.txt

# Source code (must come AFTER deps for cache reuse)
COPY dadqn_v3 ./dadqn_v3

# Trained model weights — baked into the image so the container is
# self-contained. MODEL_DIR points here; override via env var to use a
# different path (e.g. for newer weights mounted from a ConfigMap or PV).
COPY models/sla_v1/ /app/models/sla_v1/
ENV MODEL_DIR=/app/models/sla_v1

# Mount point for output CSVs (overridden via Volume in Deployment)
RUN mkdir -p /data
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "dadqn_v3.baselines.dadqn_serve"]
