"""V4-patched metrics_collector_v3.py — all Prometheus, no kubectl subprocess.

Fixes vs base (per fresh_starts study memory):
  1. CPU/MEM/pods now read from Prometheus (kube-state-metrics + cadvisor) — the
     dadqn image has NO kubectl binary so _kubectl_top returned all zeros, killing
     resource-target scaling. Replaces _kubectl_top subprocess kubectl calls.
  2. Per-service LATENCY = P95 histogram (matches what the v4 model was trained on,
     not the MEAN that the base file used).
  3. Frontend latency falls back to GATEWAY P95 directly (instead of needing the
     external p95_shim served via Locust JSON).

Pod regex `<svc>-[a-z0-9]+-[a-z0-9]+` matches ReplicaSet-managed pods only — avoids
counting `frontend-gateway-istio-...` (more segments) as frontend pods.
"""
import logging
import os
import requests

from dadqn_v3.config import ALL_SERVICES, MIN_REPLICAS  # noqa: F401

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
PROM_RATE_WINDOW = os.environ.get("PROM_RATE_WINDOW", "1m")
NAMESPACE = os.environ.get("K8S_NAMESPACE", "default")
LOCUST_URL = os.environ.get("LOCUST_URL", "").strip()


class MetricsCollectorV3:

    def _prom_query(self, query: str) -> float:
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": query}, timeout=5)
            results = resp.json().get("data", {}).get("result", [])
            if results:
                val = float(results[0]["value"][1])
                if val != val:  # NaN
                    return 0.0
                return val
        except Exception as e:
            logger.debug(f"Prometheus query failed: {e}")
        return 0.0

    def _get_locust_p95(self) -> float:
        if not LOCUST_URL:
            return 0.0
        try:
            resp = requests.get(f"{LOCUST_URL}/stats/requests", timeout=3)
            data = resp.json()
            top = data.get("current_response_time_percentiles", {}) or {}
            p95 = top.get("response_time_percentile_0.95")
            if p95 is not None and p95 > 0:
                return float(p95)
            for entry in data.get("stats", []):
                if entry.get("name") == "Aggregated":
                    p95 = entry.get("response_time_percentile_0.95", 0.0)
                    if p95 is not None and p95 > 0:
                        return float(p95)
        except Exception as e:
            logger.debug(f"Locust query failed: {e}")
        return 0.0

    def _gateway_p95_ms(self) -> float:
        return self._prom_query(
            f'histogram_quantile(0.95, sum by(le)(rate('
            f'istio_request_duration_milliseconds_bucket{{'
            f'source_workload=~".*-gateway-istio",destination_workload="frontend"'
            f'}}[{PROM_RATE_WINDOW}])))')

    def get_service_metrics(self, svc: str) -> dict:
        # ── Latency ──
        if svc == "frontend":
            # Trained signal: client-side gateway p95 (or Locust shim if set)
            lat = self._get_locust_p95()
            if lat <= 0:
                lat = self._gateway_p95_ms()
                if lat > 0:
                    logger.warning(f"Locust unavailable, using Gateway p95 ({lat:.1f}ms)")
        else:
            # V4 PATCH: per-service P95 (NOT mean — the model was trained on p95)
            lat = self._prom_query(
                f'histogram_quantile(0.95, sum by(le)(rate('
                f'istio_request_duration_milliseconds_bucket{{'
                f'destination_workload="{svc}"}}[{PROM_RATE_WINDOW}])))')

        # ── Traffic (req/min) ──
        traffic_in = self._prom_query(
            f'sum(rate(istio_requests_total{{destination_workload="{svc}"}}[{PROM_RATE_WINDOW}]))*60')
        traffic_out = self._prom_query(
            f'sum(rate(istio_requests_total{{source_workload="{svc}"}}[{PROM_RATE_WINDOW}]))*60')

        # ── Resource (Prometheus-based; image has no kubectl) ──
        cpu, mem, pods = self._prom_resources(svc)

        return {
            "num_pods": pods,
            "cpu_usage": cpu,
            "mem_usage": mem,
            "traffic_in": traffic_in,
            "traffic_out": traffic_out,
            "latency": lat,
        }

    def _prom_resources(self, svc: str) -> tuple:
        # Pod regex `<svc>-[a-z0-9]+-[a-z0-9]+` — RS-managed pods only.
        # Excludes `frontend-gateway-istio-...` (more segments) etc.
        pod_re = f'{svc}-[a-z0-9]+-[a-z0-9]+'

        cpu_m = self._prom_query(
            f'sum(rate(container_cpu_usage_seconds_total{{'
            f'namespace="{NAMESPACE}",pod=~"{pod_re}",'
            f'container!="POD",container!=""'
            f'}}[{PROM_RATE_WINDOW}])) * 1000')

        mem_mi = self._prom_query(
            f'sum(container_memory_working_set_bytes{{'
            f'namespace="{NAMESPACE}",pod=~"{pod_re}",'
            f'container!="POD",container!=""'
            f'}}) / (1024*1024)')

        pods = self._prom_query(
            f'count(kube_pod_info{{namespace="{NAMESPACE}",pod=~"{pod_re}"}})')

        return cpu_m, mem_mi, max(int(pods) if pods > 0 else 1, MIN_REPLICAS)
