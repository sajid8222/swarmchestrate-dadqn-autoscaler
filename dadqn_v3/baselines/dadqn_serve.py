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
