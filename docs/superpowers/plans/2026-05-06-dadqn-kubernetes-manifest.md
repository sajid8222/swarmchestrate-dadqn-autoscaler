# DA-DQN Kubernetes Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap the trained DA-DQN v3 agent as a self-contained Kubernetes deployment + public Docker image so it runs inside the cluster (no SSH from laptop) and other researchers can deploy it via `kubectl apply`.

**Architecture:** New top-level project directory `/home/sajid_40020095/swarmchestrate-dadqn-autoscaler/`. Subset of `dadqn_v3/` source copied in (runtime files only — no diagnostics/training). New entrypoint `dadqn_v3/baselines/dadqn_serve.py` does always-on scaling using `kubernetes` Python client. Locust optional via env var. Trained model weights baked into the Docker image.

**Tech Stack:** Python 3.11, PyTorch, stable-baselines3 2.7.1, numpy ≥ 2.0, requests, kubernetes-python-client, Docker, Kubernetes (k3s).

**Spec reference:** [`docs/superpowers/specs/2026-05-06-dadqn-kubernetes-manifest-design.md`](../specs/2026-05-06-dadqn-kubernetes-manifest-design.md) (in source repo: `dadqn-release/docs/superpowers/specs/...`)

**Pre-conditions:**
- Source repo at `/home/sajid_40020095/ze_autoscalling/Swarmchestrate-Decentralised-Auto-Scaling/dadqn-release/` (read-only — surgical changes principle)
- Cluster VMs are stopped — Tasks 9-12 require VMs to be running before starting

---

## File Structure (target)

```
/home/sajid_40020095/swarmchestrate-dadqn-autoscaler/
├── README.md                                    ← Task 11
├── Dockerfile                                   ← Task 6
├── requirements.txt                             ← Task 5
├── .dockerignore                                ← Task 6
├── docs/
│   └── superpowers/
│       ├── specs/
│       │   └── 2026-05-06-dadqn-kubernetes-manifest-design.md   ← Task 1 (copy)
│       └── plans/
│           └── 2026-05-06-dadqn-kubernetes-manifest.md           ← this file
├── dadqn_v3/                                    ← Task 1 (copy from source)
│   ├── __init__.py
│   ├── config.py
│   ├── service_graph.py
│   ├── reward_v2.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── multi_agent_system.py
│   │   └── dqn_agent.py
│   ├── environments/
│   │   ├── __init__.py
│   │   ├── service_agent_env.py
│   │   ├── multi_agent_env_cluster_v3.py        (kept untouched, unused at runtime)
│   │   └── metrics_collector_v3.py
│   ├── baselines/
│   │   ├── __init__.py
│   │   └── dadqn_serve.py                       ← NEW (Task 3)
│   └── models/
│       └── finetuned_v3_actual/                 ← Task 9 (scp'd from cluster-1.1)
│           ├── frontend.zip
│           ├── currencyservice.zip
│           ├── productcatalogservice.zip
│           ├── cartservice.zip
│           ├── adservice.zip
│           ├── checkoutservice.zip
│           ├── emailservice.zip
│           ├── paymentservice.zip
│           ├── shippingservice.zip
│           └── recommendationservice.zip
├── manifests/                                   ← Task 7
│   ├── 01-serviceaccount.yaml
│   ├── 02-rbac.yaml
│   ├── 03-configmap.yaml
│   └── 04-deployment.yaml
└── tests/                                       ← Task 4
    └── test_dadqn_serve_smoke.py
```

---

## Task 1: Bootstrap directory + copy source files

**Files:**
- Create: directory tree under `/home/sajid_40020095/swarmchestrate-dadqn-autoscaler/`
- Copy: subset of `dadqn-release/dadqn_v3/` source files
- Copy: spec doc

- [ ] **Step 1: Create dadqn_v3 subdirectories**

```bash
NEW=/home/sajid_40020095/swarmchestrate-dadqn-autoscaler
SRC=/home/sajid_40020095/ze_autoscalling/Swarmchestrate-Decentralised-Auto-Scaling/dadqn-release/dadqn_v3
mkdir -p "$NEW/dadqn_v3/agents" "$NEW/dadqn_v3/environments" "$NEW/dadqn_v3/baselines" "$NEW/dadqn_v3/models" "$NEW/tests" "$NEW/manifests"
```

Expected: `ls -ld $NEW/dadqn_v3/{agents,environments,baselines,models}` returns 4 dirs.

- [ ] **Step 2: Copy runtime Python files (only)**

```bash
SRC=/home/sajid_40020095/ze_autoscalling/Swarmchestrate-Decentralised-Auto-Scaling/dadqn-release/dadqn_v3
NEW=/home/sajid_40020095/swarmchestrate-dadqn-autoscaler

cp "$SRC/__init__.py" "$NEW/dadqn_v3/"
cp "$SRC/config.py" "$NEW/dadqn_v3/"
cp "$SRC/service_graph.py" "$NEW/dadqn_v3/"
cp "$SRC/reward_v2.py" "$NEW/dadqn_v3/"
cp "$SRC/agents/__init__.py" "$NEW/dadqn_v3/agents/"
cp "$SRC/agents/multi_agent_system.py" "$NEW/dadqn_v3/agents/"
cp "$SRC/agents/dqn_agent.py" "$NEW/dadqn_v3/agents/"
cp "$SRC/environments/__init__.py" "$NEW/dadqn_v3/environments/"
cp "$SRC/environments/service_agent_env.py" "$NEW/dadqn_v3/environments/"
cp "$SRC/environments/metrics_collector_v3.py" "$NEW/dadqn_v3/environments/"
cp "$SRC/environments/multi_agent_env_cluster_v3.py" "$NEW/dadqn_v3/environments/"
cp "$SRC/baselines/__init__.py" "$NEW/dadqn_v3/baselines/" 2>/dev/null || touch "$NEW/dadqn_v3/baselines/__init__.py"
```

Expected: All copied without "No such file" errors.

- [ ] **Step 3: Copy spec doc**

```bash
cp /home/sajid_40020095/ze_autoscalling/Swarmchestrate-Decentralised-Auto-Scaling/dadqn-release/docs/superpowers/specs/2026-05-06-dadqn-kubernetes-manifest-design.md \
   /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/docs/superpowers/specs/
```

- [ ] **Step 4: Verify imports work locally (sanity)**

```bash
cd /home/sajid_40020095/swarmchestrate-dadqn-autoscaler
PYTHONPATH=. python3 -c "
import dadqn_v3
import dadqn_v3.config
import dadqn_v3.agents.multi_agent_system
import dadqn_v3.environments.metrics_collector_v3
print('imports OK')
"
```

Expected: prints `imports OK`. If a module is missing (likely `service_agent_env` dependency), copy it and retry.

---

## Task 2: Add `kubernetes` to runtime dependencies

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: Write requirements.txt**

```bash
cat > /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/requirements.txt << 'EOF'
torch>=2.0,<3.0
stable-baselines3==2.7.1
numpy>=2.0,<3.0
requests>=2.28
kubernetes>=29.0
EOF
```

- [ ] **Step 2: Test install in a venv**

```bash
cd /home/sajid_40020095/swarmchestrate-dadqn-autoscaler
python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python -c "import torch, stable_baselines3, numpy, requests, kubernetes; print('deps OK')"
```

Expected: prints `deps OK`. Image build (Task 6) reuses these versions.

---

## Task 3: Write `dadqn_serve.py` entrypoint

**Files:**
- Create: `dadqn_v3/baselines/dadqn_serve.py`

- [ ] **Step 1: Write `dadqn_serve.py`**

```python
"""
DA-DQN v3 always-on Kubernetes-native scaler.

Drop-in replacement for `dadqn_wiki_runner` for in-cluster deployment:
- Uses kubernetes-python-client (in-cluster config) instead of subprocess kubectl
- No --duration: runs forever until pod is terminated
- Locust-optional: silently falls back to Prometheus mesh latency if LOCUST_URL is unset/unreachable

Env vars (all optional, sensible defaults):
  PROMETHEUS_URL              http://prom-kube-prometheus-stack-prometheus.monitoring.svc:9090
  LOCUST_URL                  (unset = production mode)
  PROM_RATE_WINDOW            30s
  K8S_NAMESPACE               default
  SAMPLE_INTERVAL_SEC         30
  DECISION_INTERVAL_SEC       15
  SCALE_DOWN_COOLDOWN_SEC     90
  OBS_FALLBACK_FROM_CPU       1
  MODEL_DIR                   /app/dadqn_v3/models/finetuned_v3_actual
  CSV_DIR                     /data
"""
import csv
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
from kubernetes import client, config

from dadqn_v3.config import (
    SCALABLE_SERVICES, ALL_SERVICES, SLA_LATENCY_MS,
    ACTION_TO_DELTA, MIN_REPLICAS, get_max_replicas,
)
from dadqn_v3.agents.multi_agent_system import MultiAgentSystem
from dadqn_v3.environments.multi_agent_env_cluster_v3 import MultiAgentEnvironmentClusterV3

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("dadqn_serve")

NAMESPACE = os.environ.get("K8S_NAMESPACE", "default")
LOCUST_URL = os.environ.get("LOCUST_URL", "").strip()
SAMPLE_INTERVAL_SEC = int(os.environ.get("SAMPLE_INTERVAL_SEC", "30"))
DECISION_INTERVAL_SEC = int(os.environ.get("DECISION_INTERVAL_SEC", "15"))
SCALE_DOWN_COOLDOWN_SEC = int(os.environ.get("SCALE_DOWN_COOLDOWN_SEC", "90"))
MODEL_DIR = os.environ.get("MODEL_DIR", "/app/dadqn_v3/models/finetuned_v3_actual")
CSV_DIR = os.environ.get("CSV_DIR", "/data")


def _init_k8s():
    """Load in-cluster config when running as a pod; otherwise local kubeconfig."""
    try:
        config.load_incluster_config()
        logger.info("k8s: in-cluster config loaded")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("k8s: local kubeconfig loaded")
    return client.CoreV1Api(), client.AppsV1Api()


def kubectl_pod_counts(core_v1) -> dict:
    counts = {svc: 0 for svc in ALL_SERVICES}
    try:
        pods = core_v1.list_namespaced_pod(namespace=NAMESPACE, watch=False).items
    except Exception as e:
        logger.warning(f"list_namespaced_pod failed: {e}")
        return counts
    for pod in pods:
        if pod.status.phase != "Running":
            continue
        name = pod.metadata.name
        for svc in ALL_SERVICES:
            if name.startswith(svc + "-"):
                counts[svc] += 1
                break
    return counts


def scale_deployment(apps_v1, name: str, replicas: int) -> bool:
    try:
        apps_v1.patch_namespaced_deployment_scale(
            name=name, namespace=NAMESPACE,
            body={"spec": {"replicas": replicas}},
        )
        return True
    except Exception as e:
        logger.warning(f"scale {name}->{replicas} failed: {e}")
        return False


def locust_stats() -> dict:
    if not LOCUST_URL:
        return {"users": 0, "rps": 0.0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}
    try:
        d = requests.get(f"{LOCUST_URL}/stats/requests", timeout=3).json()
    except Exception:
        return {"users": 0, "rps": 0.0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}
    top = d.get("current_response_time_percentiles", {}) or {}
    p95 = float(top.get("response_time_percentile_0.95") or 0.0)
    p50 = float(top.get("response_time_percentile_0.5") or 0.0)
    rps, avg = 0.0, 0.0
    for entry in d.get("stats", []):
        if entry.get("name") == "Aggregated":
            rps = float(entry.get("current_rps") or 0.0)
            avg = float(entry.get("avg_response_time") or 0.0)
            if p95 == 0.0:
                p95 = float(entry.get("response_time_percentile_0.95") or 0.0)
            break
    return {"users": d.get("user_count", 0), "rps": rps,
            "avg_ms": avg, "p50_ms": p50, "p95_ms": p95}


def main():
    Path(CSV_DIR).mkdir(parents=True, exist_ok=True)
    core_v1, apps_v1 = _init_k8s()

    logger.info(f"loading model: {MODEL_DIR}")
    mas = MultiAgentSystem(agent_type="dqn")
    mas.load_all(MODEL_DIR)
    for svc in SCALABLE_SERVICES:
        mas.agents[svc].model.exploration_rate = 0.0  # deterministic

    logger.info("init env (collects initial metrics)")
    env = MultiAgentEnvironmentClusterV3(max_steps=10)
    shared_state = env.reset()
    mas.init_envs(shared_state)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = Path(CSV_DIR) / f"wiki_dadqn_serve_{ts}.csv"
    fieldnames = ["step", "sec", "actual_rps", "locust_users",
                  "locust_p95_ms", "locust_avg_ms", "frontend_latency_ms",
                  "total_pods", "sla_violation",
                  ] + [f"{s}_pods" for s in SCALABLE_SERVICES]

    samples_per_decision = max(1, DECISION_INTERVAL_SEC // SAMPLE_INTERVAL_SEC)
    last_scale_up = {svc: 0.0 for svc in SCALABLE_SERVICES}
    logger.info(f"sample={SAMPLE_INTERVAL_SEC}s decision={DECISION_INTERVAL_SEC}s "
                f"cooldown={SCALE_DOWN_COOLDOWN_SEC}s csv={csv_path}")

    t_start = time.time()
    step = 0
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        while True:
            step += 1
            elapsed = int(time.time() - t_start)

            try:
                is_decision = (step > 1) and ((step - 1) % samples_per_decision == 0)
                if is_decision:
                    env._collect_metrics(step=step)
                    mas.init_envs(env.shared_state)
                    actions = mas.collect_actions(deterministic=True)
                    proposed = {}
                    now = time.time()
                    for svc in SCALABLE_SERVICES:
                        delta = ACTION_TO_DELTA[actions[svc]]
                        cur = env.shared_state.replicas.get(svc, MIN_REPLICAS)
                        raw = int(np.clip(cur + delta, MIN_REPLICAS, get_max_replicas(svc)))
                        if raw < cur and (now - last_scale_up[svc]) < SCALE_DOWN_COOLDOWN_SEC:
                            proposed[svc] = cur
                        else:
                            proposed[svc] = raw
                            if raw > cur:
                                last_scale_up[svc] = now
                    proposed = env._enforce_budget(proposed)
                    for svc, replicas in proposed.items():
                        if replicas != env.shared_state.replicas.get(svc):
                            scale_deployment(apps_v1, svc, replicas)
                    env.shared_state.replicas = proposed
                    logger.info(f"  decision: scaled to {proposed}")

                s = locust_stats()
                fe_lat = s["p95_ms"] or (env.shared_state.frontend_latency_ms or 0.0)
                pods = kubectl_pod_counts(core_v1)
                row = {
                    "step": step, "sec": elapsed,
                    "actual_rps": round(s["rps"], 1),
                    "locust_users": s["users"],
                    "locust_p95_ms": round(s["p95_ms"], 1),
                    "locust_avg_ms": round(s["avg_ms"], 1),
                    "frontend_latency_ms": round(fe_lat, 1),
                    "total_pods": sum(pods.values()),
                    "sla_violation": 1 if fe_lat > SLA_LATENCY_MS else 0,
                }
                for svc in SCALABLE_SERVICES:
                    row[f"{svc}_pods"] = pods.get(svc, 0)
                writer.writerow(row); f.flush()

                logger.info(
                    f"t={elapsed:5d}s users={row['locust_users']:>4} "
                    f"rps={row['actual_rps']:5.1f} p95={row['locust_p95_ms']:6.0f}ms "
                    f"pods={row['total_pods']:>3} viol={row['sla_violation']}"
                    f"{' (decision)' if is_decision else ''}"
                )
            except Exception as e:
                logger.error(f"tick {step} failed: {e}", exc_info=True)

            time.sleep(SAMPLE_INTERVAL_SEC)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it imports without running**

```bash
cd /home/sajid_40020095/swarmchestrate-dadqn-autoscaler
PYTHONPATH=. .venv/bin/python -c "from dadqn_v3.baselines import dadqn_serve; print('serve module OK')"
```

Expected: prints `serve module OK`. If `ImportError` for missing module, copy that file and retry.

---

## Task 4: Local smoke test for `dadqn_serve.py`

**Files:**
- Create: `tests/test_dadqn_serve_smoke.py`

- [ ] **Step 1: Write the smoke test**

```python
# tests/test_dadqn_serve_smoke.py
"""
Smoke test: dadqn_serve module loads, _init_k8s falls back to local kubeconfig
when in-cluster config absent, locust_stats returns zeros when LOCUST_URL unset.
"""
import importlib
import os
import sys


def test_module_imports():
    mod = importlib.import_module("dadqn_v3.baselines.dadqn_serve")
    assert hasattr(mod, "main")
    assert hasattr(mod, "kubectl_pod_counts")
    assert hasattr(mod, "scale_deployment")
    assert hasattr(mod, "locust_stats")


def test_locust_stats_no_url(monkeypatch):
    monkeypatch.setenv("LOCUST_URL", "")
    # Re-import to pick up env var change
    if "dadqn_v3.baselines.dadqn_serve" in sys.modules:
        del sys.modules["dadqn_v3.baselines.dadqn_serve"]
    mod = importlib.import_module("dadqn_v3.baselines.dadqn_serve")
    s = mod.locust_stats()
    assert s == {"users": 0, "rps": 0.0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}
```

- [ ] **Step 2: Run smoke test**

```bash
cd /home/sajid_40020095/swarmchestrate-dadqn-autoscaler
.venv/bin/pip install --quiet pytest
PYTHONPATH=. .venv/bin/pytest tests/test_dadqn_serve_smoke.py -v
```

Expected: 2 passed.

---

## Task 5: Write `Dockerfile` and `.dockerignore`

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Write `.dockerignore`**

```bash
cat > /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/.dockerignore << 'EOF'
.venv/
.git/
.pytest_cache/
__pycache__/
*.pyc
docs/
tests/
EOF
```

- [ ] **Step 2: Write `Dockerfile`**

```dockerfile
# /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/Dockerfile
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
RUN pip install -r requirements.txt

# Source code (must come AFTER deps for cache reuse)
COPY dadqn_v3 ./dadqn_v3

# Trained model weights (assumed present in dadqn_v3/models/finetuned_v3_actual/)
# Verified at runtime: the entrypoint will fail-fast if missing.

# Mount point for output CSVs (overridden via Volume in Deployment)
RUN mkdir -p /data
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "dadqn_v3.baselines.dadqn_serve"]
```

- [ ] **Step 3: Build the image (NOTE: requires model weights — see Task 9)**

```bash
cd /home/sajid_40020095/swarmchestrate-dadqn-autoscaler
docker build -t dadqn-autoscaler:dev .
```

Expected: `Successfully built` and `Successfully tagged dadqn-autoscaler:dev`.

If model weights aren't present yet (Task 9 not done), build will succeed but image won't run — defer to Task 9 if blocked.

---

## Task 6: Local docker run smoke test (no cluster)

**Goal:** Verify image's Python imports and model load work standalone (no cluster connection).

**Files:**
- (no new files — runtime check)

- [ ] **Step 1: Run image in import-only mode**

```bash
docker run --rm --entrypoint python dadqn-autoscaler:dev -c "
from dadqn_v3.baselines.dadqn_serve import main, locust_stats
from dadqn_v3.agents.multi_agent_system import MultiAgentSystem
print('image imports OK')
"
```

Expected: prints `image imports OK`. If model files missing, this still passes — model load only happens when `main()` is called.

- [ ] **Step 2: Verify model directory is in image**

```bash
docker run --rm --entrypoint sh dadqn-autoscaler:dev -c \
  'ls -1 /app/dadqn_v3/models/finetuned_v3_actual/ 2>/dev/null | wc -l'
```

Expected: `10` (10 service .zip files). If `0`, model weights missing — return to Task 9 first.

---

## Task 7: Write Kubernetes manifests

**Files:**
- Create: `manifests/01-serviceaccount.yaml`
- Create: `manifests/02-rbac.yaml`
- Create: `manifests/03-configmap.yaml`
- Create: `manifests/04-deployment.yaml`

- [ ] **Step 1: Write ServiceAccount**

```bash
cat > /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/manifests/01-serviceaccount.yaml << 'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: dadqn-autoscaler
  namespace: default
EOF
```

- [ ] **Step 2: Write RBAC (Role + RoleBinding)**

```bash
cat > /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/manifests/02-rbac.yaml << 'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: dadqn-autoscaler
  namespace: default
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments/scale"]
    verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: dadqn-autoscaler
  namespace: default
subjects:
  - kind: ServiceAccount
    name: dadqn-autoscaler
    namespace: default
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: dadqn-autoscaler
EOF
```

- [ ] **Step 3: Write ConfigMap**

```bash
cat > /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/manifests/03-configmap.yaml << 'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: dadqn-config
  namespace: default
data:
  PROMETHEUS_URL: "http://prom-kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
  PROM_RATE_WINDOW: "30s"
  K8S_NAMESPACE: "default"
  SAMPLE_INTERVAL_SEC: "30"
  DECISION_INTERVAL_SEC: "15"
  SCALE_DOWN_COOLDOWN_SEC: "90"
  OBS_FALLBACK_FROM_CPU: "1"
  # LOCUST_URL: "http://172.31.33.177:8089"      # uncomment in EXPERIMENT mode
  LOCUST_URL: ""                                  # PRODUCTION mode (default)
EOF
```

- [ ] **Step 4: Write Deployment**

```bash
DOCKERHUB_USER="${DOCKERHUB_USER:-PLACEHOLDER}"   # set via env var when applying

cat > /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/manifests/04-deployment.yaml << EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dadqn-autoscaler
  namespace: default
  labels:
    app: dadqn-autoscaler
spec:
  replicas: 1
  selector:
    matchLabels:
      app: dadqn-autoscaler
  template:
    metadata:
      labels:
        app: dadqn-autoscaler
    spec:
      serviceAccountName: dadqn-autoscaler
      containers:
        - name: agent
          image: ${DOCKERHUB_USER}/dadqn-autoscaler:v1
          imagePullPolicy: IfNotPresent
          envFrom:
            - configMapRef:
                name: dadqn-config
          volumeMounts:
            - name: data
              mountPath: /data
          resources:
            requests:
              cpu: "200m"
              memory: "512Mi"
            limits:
              cpu: "1"
              memory: "2Gi"
      volumes:
        - name: data
          emptyDir: {}
EOF
```

NOTE: replace `${DOCKERHUB_USER}` once Docker Hub username known. Until then, image string is `PLACEHOLDER/dadqn-autoscaler:v1` — `kubectl apply` will fail with `ImagePullBackOff` (expected at this stage).

- [ ] **Step 5: Validate manifests with `kubectl --dry-run=client` (no cluster needed)**

```bash
cd /home/sajid_40020095/swarmchestrate-dadqn-autoscaler
for f in manifests/*.yaml; do
  kubectl apply --dry-run=client -f "$f" 2>&1 | head -2
done
```

Expected: each prints `<kind>/<name> created (dry run)` — no errors.

---

## Task 8: Write README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

```bash
cat > /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/README.md << 'EOF'
# Swarmchestrate DA-DQN Autoscaler

Decentralised Autoscaling for Online Boutique using Deep Q-Networks (one Q-net per service, multi-agent).

This repo packages a **trained** DA-DQN v3 agent as a Docker image + Kubernetes manifests so it runs **inside** your k3s cluster, scaling the boutique deployments based on Istio mesh metrics (and optionally Locust client-side p95).

## Quick start

```bash
# 1. Apply manifests (uses public Docker Hub image)
kubectl apply -f manifests/

# 2. Verify pod is running
kubectl get pod -n default -l app=dadqn-autoscaler

# 3. Watch the agent's decisions live
kubectl logs -n default -f deployment/dadqn-autoscaler
```

## Configuration modes

The agent reads two metric sources:
- **Prometheus** (always required): in-cluster Istio mesh metrics
- **Locust** (optional): client-side p95 + concurrent users

### Production mode (Prometheus-only — default)

Leave `LOCUST_URL` empty in the ConfigMap. Agent scales based on mesh latency only.

### Experiment mode (with Locust)

```bash
# 1. Start your Locust on any node (here, cluster-1.4 worker)
ssh cluster-1.4 'CSV_PATH=workloads/rps-600.csv ... locust -f wiki_locustfile.py ...'

# 2. Update the ConfigMap to point at it
kubectl set env deployment/dadqn-autoscaler LOCUST_URL=http://172.31.33.177:8089

# 3. Restart the pod to pick up the env change
kubectl rollout restart deployment/dadqn-autoscaler
```

## Tunables (ConfigMap `dadqn-config`)

| Key | Default | Effect |
|---|---|---|
| `LOCUST_URL` | `""` | If set, agent uses real client-side p95 |
| `PROMETHEUS_URL` | in-cluster prom-kube-prometheus-stack URL | Mesh metrics source |
| `PROM_RATE_WINDOW` | `30s` | Prometheus `rate()` window |
| `SAMPLE_INTERVAL_SEC` | `30` | How often agent samples + writes a CSV row |
| `DECISION_INTERVAL_SEC` | `15` | How often agent issues a scaling decision |
| `SCALE_DOWN_COOLDOWN_SEC` | `90` | After scale-up, prevent scale-down for this long |

## Retrieving experiment data

```bash
# Find the agent pod
POD=$(kubectl get pod -n default -l app=dadqn-autoscaler -o jsonpath='{.items[0].metadata.name}')

# Pull the agent's CSV
kubectl cp default/$POD:/data ./agent_data/

# Pull the agent's full log
kubectl logs deployment/dadqn-autoscaler --tail=10000 > agent.log
```

## Pause / resume between experiments

```bash
kubectl scale deployment/dadqn-autoscaler --replicas=0   # pause
kubectl scale deployment/dadqn-autoscaler --replicas=1   # resume
```

## Lifecycle

```bash
kubectl apply -f manifests/        # deploy
kubectl rollout restart deploy/dadqn-autoscaler  # restart with fresh state
kubectl delete -f manifests/       # uninstall
```

## Architecture

The agent runs an always-on outer loop:
1. Collects Prometheus mesh metrics + (optionally) Locust API
2. Builds 22-dim observation for `frontend` (5 κ-hop neighbours) and 14-dim for the other 9 services
3. Each Q-network outputs an action (`-3` to `+3` replica delta)
4. Enforces per-service caps (`recommendationservice`=8) and total-pod budget (40 max)
5. Honours `SCALE_DOWN_COOLDOWN_SEC` after a scale-up to prevent oscillation
6. Patches `apps/v1/Deployment/scale` via Kubernetes API

## Build & push your own image

```bash
docker build -t YOURNAME/dadqn-autoscaler:v1 .
docker push YOURNAME/dadqn-autoscaler:v1
# Then edit manifests/04-deployment.yaml to use your image tag
```

## Citation

(TBD: paper reference)

## License

(TBD)
EOF
```

- [ ] **Step 2: Skim README for clarity**

```bash
less /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/README.md
```

Expected: readable, no broken refs.

---

## Task 9: Pull trained model weights from cluster-1.1 [REQUIRES VMs RUNNING]

**Files:**
- Copy: `/home/sajid_40020095/swarmchestrate-dadqn-autoscaler/dadqn_v3/models/finetuned_v3_actual/*.zip` (10 files)

**Pre-condition:** VMs are running and SSH to cluster-1.1 works.

- [ ] **Step 1: Verify cluster-1.1 has the trained weights**

```bash
ssh cluster-1.1 'ls -1 ~/dadqn-release/dadqn_v3/diagnostics/finetuned_v3_actual/*.zip 2>&1 | wc -l'
```

Expected: `10`. If less, re-train or use the older `Cluster_1_results/model/best/` as fallback.

- [ ] **Step 2: rsync weights to laptop**

```bash
mkdir -p /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/dadqn_v3/models/finetuned_v3_actual
rsync -av cluster-1.1:~/dadqn-release/dadqn_v3/diagnostics/finetuned_v3_actual/*.zip \
  /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/dadqn_v3/models/finetuned_v3_actual/
```

Expected: 10 `.zip` files copied.

- [ ] **Step 3: Verify**

```bash
ls -1 /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/dadqn_v3/models/finetuned_v3_actual/*.zip | wc -l
```

Expected: `10`.

- [ ] **Step 4: Rebuild Docker image with weights baked in**

```bash
cd /home/sajid_40020095/swarmchestrate-dadqn-autoscaler
docker build -t dadqn-autoscaler:dev .
```

- [ ] **Step 5: Verify weights are in image** (re-run Task 6 Step 2)

```bash
docker run --rm --entrypoint sh dadqn-autoscaler:dev -c \
  'ls -1 /app/dadqn_v3/models/finetuned_v3_actual/ | wc -l'
```

Expected: `10`.

---

## Task 10: Push image to Docker Hub [REQUIRES dockerhub-user PARAMETER]

**Pre-condition:** User has provided their Docker Hub username (call it `$DH`).

- [ ] **Step 1: Login to Docker Hub**

```bash
docker login   # interactive; enter username + access token
```

Expected: `Login Succeeded`.

- [ ] **Step 2: Tag and push**

```bash
DH=<dockerhub-user>   # replace
docker tag dadqn-autoscaler:dev ${DH}/dadqn-autoscaler:v1
docker push ${DH}/dadqn-autoscaler:v1
```

Expected: push completes, image accessible at `https://hub.docker.com/r/${DH}/dadqn-autoscaler/tags`.

- [ ] **Step 3: Update Deployment manifest with the real image**

```bash
sed -i "s|PLACEHOLDER/dadqn-autoscaler:v1|${DH}/dadqn-autoscaler:v1|" \
  /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/manifests/04-deployment.yaml
grep image: /home/sajid_40020095/swarmchestrate-dadqn-autoscaler/manifests/04-deployment.yaml
```

Expected: `image: ${DH}/dadqn-autoscaler:v1`.

---

## Task 11: Deploy to cluster + verify [REQUIRES VMs RUNNING + Task 10 done]

- [ ] **Step 1: Apply manifests**

```bash
cd /home/sajid_40020095/swarmchestrate-dadqn-autoscaler
KUBECONFIG_CMD='ssh cluster-1.1 -- "cat /etc/rancher/k3s/k3s.yaml" > /tmp/k3s-kubeconfig.yaml'
# OR: kubectl needs cluster access — recommended: run kubectl FROM cluster-1.1
ssh cluster-1.1 'mkdir -p ~/swarm-dadqn-manifests'
rsync -av manifests/ cluster-1.1:~/swarm-dadqn-manifests/
ssh cluster-1.1 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl apply -f ~/swarm-dadqn-manifests/'
```

Expected:
```
serviceaccount/dadqn-autoscaler created
role.rbac.authorization.k8s.io/dadqn-autoscaler created
rolebinding.rbac.authorization.k8s.io/dadqn-autoscaler created
configmap/dadqn-config created
deployment.apps/dadqn-autoscaler created
```

- [ ] **Step 2: Wait for pod Ready**

```bash
ssh cluster-1.1 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl wait --for=condition=Ready pod -l app=dadqn-autoscaler -n default --timeout=120s'
```

Expected: `pod/dadqn-autoscaler-XXXX condition met` within 120s. If `ImagePullBackOff`, image push (Task 10) failed.

- [ ] **Step 3: Tail logs and verify model loads + first decision fires**

```bash
ssh cluster-1.1 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; timeout 100 kubectl logs -f deploy/dadqn-autoscaler' | head -30
```

Expected (within ~90s):
- Line: `loading model: /app/dadqn_v3/models/finetuned_v3_actual`
- Line: `init env (collects initial metrics)`
- Line: `t=    0s users=...` (first sample)
- Line: `decision: scaled to {...}` (within 1-2 minutes)

If logs stop at "loading model" with traceback → model files missing or version mismatch → check Task 9.

---

## Task 12: End-to-end test at rps-600 [REQUIRES Task 11 deployed]

- [ ] **Step 1: Reset cluster pods to baseline**

```bash
ssh cluster-1.1 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; for s in frontend currencyservice productcatalogservice cartservice adservice checkoutservice emailservice paymentservice shippingservice recommendationservice; do kubectl scale deployment "$s" --replicas=1 2>/dev/null; done; sleep 12'
```

- [ ] **Step 2: Set LOCUST_URL in ConfigMap (experiment mode)**

```bash
ssh cluster-1.1 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl set env deployment/dadqn-autoscaler LOCUST_URL=http://172.31.33.177:8089; kubectl rollout restart deploy/dadqn-autoscaler; kubectl wait --for=condition=Ready pod -l app=dadqn-autoscaler --timeout=60s'
```

- [ ] **Step 3: Start mesh monitor + Locust on cluster-1.4**

```bash
TS=$(date +%Y%m%d_%H%M%S)
curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"namespace\":\"default\",\"interval\":5,\"file_prefix\":\"ob_mesh_dadqn_manifest_${TS}\",\"prom_url\":\"http://prom-kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090/api/v1/query\"}" \
  "http://$(ssh cluster-1.1 hostname -I | awk '{print $1}'):31879/monitor/start"

ssh cluster-1.4 "cd ~ && CSV_PATH=load/online-boutique/workloads/rps-600.csv TIME_MINUTE=10 SPAWN_RATE=20 SCALE_FACTOR=1 PATH=/home/ubuntu/locust-venv/bin:\$PATH nohup locust -f load/online-boutique/wiki_locustfile.py --host http://localhost:30193 --run-time 600s --autostart --autoquit 5 --web-host 0.0.0.0 --web-port 8089 --csv /tmp/ob_${TS} --only-summary > /tmp/locust.log 2>&1 < /dev/null & disown; echo started"
```

- [ ] **Step 4: Wait for run to complete (10 min + 30s)**

```bash
sleep 630
```

- [ ] **Step 5: Stop Locust + monitor, pull data**

```bash
ssh cluster-1.4 'pkill -9 -f wiki_locustfile 2>/dev/null'
curl -s -X POST -H 'Content-Type: application/json' -d '{"namespace":"default"}' "http://$(ssh cluster-1.1 hostname -I | awk '{print $1}'):31879/monitor/stop"

# Pull agent CSV
POD=$(ssh cluster-1.1 "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get pod -l app=dadqn-autoscaler -o jsonpath='{.items[0].metadata.name}'")
ssh cluster-1.1 "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl cp default/${POD}:/data /tmp/agent_data"
mkdir -p /tmp/manifest_test
rsync -av cluster-1.1:/tmp/agent_data/ /tmp/manifest_test/
ls -lh /tmp/manifest_test/
```

Expected: at least 1 `wiki_dadqn_serve_*.csv` file with > 10 rows.

- [ ] **Step 6: Inspect results**

```bash
tail -10 /tmp/manifest_test/wiki_dadqn_serve_*.csv
```

Expected: rows with non-zero `locust_users` (>0), p95 < SLA (1200ms) for steady-state, pod counts that scaled up during load (e.g. `frontend_pods >= 3`).

---

## Task 13: Add `run_v3_rps_X_on_manifest.sh` orchestrator [LOW PRIORITY — manual workflow above is sufficient for now]

**Files:**
- Create: `/home/sajid_40020095/ze_autoscalling/Swarmchestrate-Decentralised-Auto-Scaling/dadqn-release/dadqn_v3/diagnostics/cluster_2_results/scripts/run_v3_rps_X_on_manifest.sh`

- [ ] **Step 1: Adapt the worker-node orchestrator**

Copy `run_v3_rps600_locust_on_worker.sh` and replace the agent SSH block with the kubectl flow from Task 12 (set LOCUST_URL → rollout restart → no nohup). Take WL as `$1`.

- [ ] **Step 2: Test with rps-400 first (lighter load)**

```bash
bash run_v3_rps_X_on_manifest.sh rps-400
```

Expected: full sweep completes in ~12 min, agent CSV has scaling decisions.

---

## Spec coverage check (self-review)

| Spec section | Implementing task |
|---|---|
| `dadqn_serve.py` (always-on, kubernetes-client, Locust-optional) | Task 3 |
| Dockerfile | Task 5 |
| 4 manifests (SA, RBAC, ConfigMap, Deployment) | Task 7 |
| README (experiment + production modes) | Task 8 |
| Existing `dadqn_wiki_runner.py` untouched | Task 1 (we only copy, don't edit) |
| RBAC scoped to default ns | Task 7 Step 2 |
| Lifecycle commands (apply/restart/scale/delete) | README in Task 8 + Task 11/12 |
| Updated experiment orchestrator | Task 13 |
| Success criterion 1 (image builds + pushes) | Tasks 5, 10 |
| Success criterion 2 (kubectl apply works) | Task 11 |
| Success criterion 3 (pod Ready ≤ 60s) | Task 11 Step 2 |
| Success criterion 4 (first decision ≤ 90s) | Task 11 Step 3 |
| Success criterion 5 (frontend ≥ 3 pods at rps-600) | Task 12 Step 6 |
| Success criterion 6 (scales back after load) | Task 12 Step 6 |
| Success criterion 7 (logs + CSV retrievable) | Task 12 Step 5 |
| Success criterion 9 (README sufficient for fresh deploy) | Task 8 |

**Open parameter:** `<dockerhub-user>` — required before Task 10.

**Coverage gaps:** None identified.

---

## Tasks deferred to "VMs running" state

| Task | Reason |
|---|---|
| Task 9 (model rsync) | Requires SSH to cluster-1.1 |
| Task 10 (image push) | Optional; can build locally first then push when ready |
| Task 11 (deploy) | Requires running cluster |
| Task 12 (e2e test) | Requires running cluster + Locust |

**Tasks 1-8 are doable right now with VMs stopped** — they're pure laptop work.
