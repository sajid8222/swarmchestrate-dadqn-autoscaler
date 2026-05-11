"""
Live-cluster metrics collector for DA-DQN v3.

Same logic as dadqn/environments/metrics_collector.py but uses dadqn_v3.config
imports (no dependency on the legacy `decentralized` package).

Returns per-service metrics in the same dict format the simulator uses.
"""

import logging
import os
import subprocess

import requests

from dadqn_v3.config import (
    ALL_SERVICES, MIN_REPLICAS,
)

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
PROM_RATE_WINDOW = os.environ.get("PROM_RATE_WINDOW", "2m")
NAMESPACE = os.environ.get("K8S_NAMESPACE", "default")
KUBECONFIG = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
LOCUST_URL = os.environ.get("LOCUST_URL", "http://localhost:8089")

# Fix-2: when Istio mesh returns 0 (broken/missing waypoints), derive
# traffic_in/latency from kubectl-top CPU and Locust frontend latency.
# Off by default to preserve original behaviour for tests that depend on
# raw mesh data; enabled by setting OBS_FALLBACK_FROM_CPU=1.
OBS_FALLBACK_FROM_CPU = os.environ.get("OBS_FALLBACK_FROM_CPU", "0") == "1"
# Empirical: ~50 RPM per millicore CPU at light boutique loads
CPU_TO_RPM = float(os.environ.get("CPU_TO_RPM", "50"))
# Backend service latency typically 30–60 % of frontend p95 in this app
BACKEND_LAT_FRACTION = float(os.environ.get("BACKEND_LAT_FRACTION", "0.5"))


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

    def get_service_metrics(self, svc: str) -> dict:
        # Latency: Locust p95 for frontend, Prometheus for others
        if svc == "frontend":
            lat = self._get_locust_p95()
            if lat <= 0:
                lat = self._prom_query(
                    f'max(sum by (destination_workload) '
                    f'(rate(istio_request_duration_milliseconds_sum{{'
                    f'source_workload="frontend",destination_workload!="unknown"}}[{PROM_RATE_WINDOW}])) '
                    f'/ sum by (destination_workload) '
                    f'(rate(istio_request_duration_milliseconds_count{{'
                    f'source_workload="frontend",destination_workload!="unknown"}}[{PROM_RATE_WINDOW}])))')
                if lat > 0:
                    logger.warning(f"Locust unavailable, using Prometheus backend latency ({lat:.1f}ms)")
        else:
            lat = self._prom_query(
                f'sum(rate(istio_request_duration_milliseconds_sum{{'
                f'destination_workload="{svc}"}}[{PROM_RATE_WINDOW}]))'
                f'/sum(rate(istio_request_duration_milliseconds_count{{'
                f'destination_workload="{svc}"}}[{PROM_RATE_WINDOW}]))')

        # Traffic
        traffic_in = self._prom_query(
            f'sum(rate(istio_requests_total{{destination_workload="{svc}"}}[{PROM_RATE_WINDOW}]))*60')
        traffic_out = self._prom_query(
            f'sum(rate(istio_requests_total{{source_workload="{svc}"}}[{PROM_RATE_WINDOW}]))*60')

        cpu, mem, pods = self._kubectl_top(svc)

        # Fix-2 fallbacks (env-gated). Triggered only when mesh returned 0,
        # which on this k3s cluster happens for every service except the one
        # that has a working waypoint. Without these fallbacks the agents
        # observe traffic_in=0/latency=0 for ~9 of 10 services and refuse to
        # scale them. Use kubectl-top CPU + Locust frontend latency as proxies.
        if OBS_FALLBACK_FROM_CPU:
            if traffic_in <= 0 and cpu > 0:
                traffic_in = cpu * CPU_TO_RPM
            if traffic_out <= 0 and cpu > 0:
                traffic_out = cpu * CPU_TO_RPM * 0.6
            if svc != "frontend" and lat <= 0:
                fe = self._get_locust_p95()
                if fe > 0:
                    lat = fe * BACKEND_LAT_FRACTION

        return {
            "num_pods": pods,
            "cpu_usage": cpu,
            "mem_usage": mem,
            "traffic_in": traffic_in,
            "traffic_out": traffic_out,
            "latency": lat,
        }

    def _kubectl_top(self, svc: str) -> tuple[float, float, int]:
        cpu_total, mem_total, pod_count = 0.0, 0.0, 0
        env = {"KUBECONFIG": KUBECONFIG, "PATH": "/usr/local/bin:/usr/bin:/bin"}
        try:
            result = subprocess.run(
                ["kubectl", "get", "pod", "-n", NAMESPACE, "--no-headers"],
                capture_output=True, text=True, timeout=10, env=env)
            for line in result.stdout.strip().split("\n"):
                if svc in line and "Running" in line:
                    pod_count += 1

            result = subprocess.run(
                ["kubectl", "top", "pod", "-n", NAMESPACE, "--no-headers"],
                capture_output=True, text=True, timeout=10, env=env)
            for line in result.stdout.strip().split("\n"):
                if svc in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            cpu_total += float(parts[1].replace("m", ""))
                        except ValueError:
                            pass
                        try:
                            mem_total += float(parts[2].replace("Mi", ""))
                        except ValueError:
                            pass
        except Exception as e:
            logger.debug(f"kubectl failed for {svc}: {e}")
        return cpu_total, mem_total, max(pod_count, 1)
