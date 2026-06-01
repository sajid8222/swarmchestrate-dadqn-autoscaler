"""
Live K3s cluster environment for DA-DQN v3 evaluation.

Parallel to MultiAgentEnvironmentV3 (simulator) but uses real kubectl + Prometheus
+ Locust instead of the CSV replay.

Reuses:
  - dadqn.environments.metrics_collector.MetricsCollector (Locust+Prom+kubectl)
  - dadqn_v3.environments.service_agent_env.SharedState
  - dadqn_v3.reward_v2.CascadingRewardCalculator
  - dadqn_v3.config (all constants, SCALABLE_SERVICES, ACTION_TO_DELTA, etc.)

Design choices follow CLAUDE.md:
  - No retry/backoff on kubectl/Prometheus failures — log and continue.
  - No deviation from v3 observation structure (22-dim for frontend).
  - Same budget enforcement, priority order, and cooldown as simulator.
"""

import logging
import os
import subprocess
import time

import numpy as np

from dadqn_v3.config import (
    SCALABLE_SERVICES, MIN_REPLICAS, MAX_REPLICAS,
    MAX_TOTAL_PODS, ACTION_TO_DELTA, HOLD_ACTION,
    SLA_LATENCY_MS, SERVICE_RESOURCES,
)
from dadqn_v3.environments.service_agent_env import SharedState
from dadqn_v3.reward_v2 import CascadingRewardCalculator
from dadqn_v3.service_graph import KAPPA_NEIGHBOR_MAP, DECAY_WEIGHTS

logger = logging.getLogger(__name__)

NAMESPACE = os.environ.get("K8S_NAMESPACE", "default")
KUBECONFIG = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
DECISION_INTERVAL_SEC = int(os.environ.get("DECISION_INTERVAL_SEC", "180"))

PRIORITY_ORDER = [
    "frontend", "recommendationservice", "currencyservice",
    "productcatalogservice", "cartservice", "checkoutservice",
    "adservice", "shippingservice", "paymentservice", "emailservice",
]


class MultiAgentEnvironmentClusterV3:
    """
    Live cluster env. step() actually scales pods via kubectl and waits
    DECISION_INTERVAL_SEC seconds before sampling metrics.
    """

    def __init__(self, max_steps: int = 10):
        # Import MetricsCollector lazily — it hits Prometheus/kubectl on init
        from dadqn_v3.environments.metrics_collector_v3 import MetricsCollectorV3
        self.metrics_collector = MetricsCollectorV3()
        self.max_steps = max_steps
        self.shared_state = SharedState()
        self.shared_state.max_steps = max_steps
        self.reward_calc = CascadingRewardCalculator()
        self.current_step = 0
        self.reset_on_episode_start = False  # v4 style — continuous across episodes

    # ── Reset ─────────────────────────────────────────────────────────
    def reset(self) -> SharedState:
        self.current_step = 0
        self.shared_state.reset()
        if self.reset_on_episode_start:
            self._scale_all_to_min()
            time.sleep(5)
        self._collect_metrics(step=0)
        return self.shared_state

    # ── Step ──────────────────────────────────────────────────────────
    def step(self, actions: dict) -> tuple[SharedState, dict, bool, dict]:
        # 1. Convert actions to proposed replicas
        proposed = {}
        for svc in SCALABLE_SERVICES:
            delta = ACTION_TO_DELTA[actions[svc]]
            current = self.shared_state.replicas.get(svc, MIN_REPLICAS)
            proposed[svc] = int(np.clip(current + delta, MIN_REPLICAS, MAX_REPLICAS))

        # 2. Budget enforcement
        proposed = self._enforce_budget(proposed)

        # 3. Execute scaling commands on the cluster
        self._apply_scaling(proposed)

        # 3a. Log applied scaling immediately (monitor will see this within ~5-10s)
        abbr = {"frontend": "fe", "currencyservice": "cur",
                "productcatalogservice": "prod", "cartservice": "cart",
                "adservice": "ad", "checkoutservice": "chk",
                "emailservice": "email", "paymentservice": "pay",
                "shippingservice": "ship", "recommendationservice": "rec"}
        pod_str = " ".join(f"{abbr[s]}={proposed[s]}" for s in SCALABLE_SERVICES)
        logger.info(f"  ACTION applied (step {self.current_step + 1}) → "
                    f"total={sum(proposed.values())} [{pod_str}]")

        # 4. Wait for pods to warm up + Prometheus rate window
        time.sleep(DECISION_INTERVAL_SEC)

        # 5. Collect fresh metrics
        self._collect_metrics(step=self.current_step + 1)
        # _collect_metrics sets shared_state.replicas from kubectl — overwrite with our
        # intended allocation so the observation reflects the action, not the
        # possibly-still-warming-up real count.
        self.shared_state.replicas = proposed

        self.current_step += 1

        # 6. Compute rewards (same formula as v3 simulator)
        frontend_lat = self.shared_state.frontend_latency_ms
        rewards = {}
        for svc in SCALABLE_SERVICES:
            m = self.shared_state.metrics.get(svc, {})
            res = SERVICE_RESOURCES.get(svc, {"cpu_limit_m": 200, "mem_limit_mi": 128})
            pods = proposed[svc]

            own_lat = m.get("latency", 0.0)
            traffic_in = m.get("traffic_in", 0.0)
            cpu_util = min(m.get("cpu_usage", 0) / (res["cpu_limit_m"] * max(pods, 1)), 1.0)
            mem_util = min(m.get("mem_usage", 0) / (res["mem_limit_mi"] * max(pods, 1)), 1.0)

            neighbor_lats = {n: self.shared_state.metrics.get(n, {}).get("latency", 0)
                             for n in KAPPA_NEIGHBOR_MAP.get(svc, [])}
            decay_w = DECAY_WEIGHTS.get(svc, {})

            reward, _ = self.reward_calc.compute_reward(
                own_latency_ms=own_lat, traffic_in=traffic_in,
                cpu_util=cpu_util, mem_util=mem_util, pods=pods,
                frontend_latency_ms=frontend_lat,
                neighbor_latencies=neighbor_lats, decay_weights=decay_w,
            )
            rewards[svc] = reward

        done = self.current_step >= self.max_steps
        info = {
            "step": self.current_step,
            "frontend_latency_ms": frontend_lat,
            "total_pods": sum(proposed.values()),
            "replicas": dict(proposed),
            "per_service_metrics": {svc: dict(self.shared_state.metrics.get(svc, {}))
                                    for svc in SCALABLE_SERVICES},
        }
        return self.shared_state, rewards, done, info

    # ── Internal: cluster actions ────────────────────────────────────
    def _apply_scaling(self, proposed: dict):
        env = {"KUBECONFIG": KUBECONFIG, "PATH": "/usr/local/bin:/usr/bin:/bin"}
        current = self.shared_state.replicas
        for svc in SCALABLE_SERVICES:
            target = proposed[svc]
            if target != current.get(svc, MIN_REPLICAS):
                try:
                    subprocess.run(
                        ["kubectl", "scale", f"deploy/{svc}",
                         f"--replicas={target}", "-n", NAMESPACE],
                        capture_output=True, timeout=15, env=env)
                except Exception as e:
                    logger.warning(f"kubectl scale failed for {svc}: {e}")

    def _scale_all_to_min(self):
        env = {"KUBECONFIG": KUBECONFIG, "PATH": "/usr/local/bin:/usr/bin:/bin"}
        for svc in SCALABLE_SERVICES:
            subprocess.run(
                ["kubectl", "scale", f"deploy/{svc}",
                 f"--replicas={MIN_REPLICAS}", "-n", NAMESPACE],
                capture_output=True, timeout=15, env=env)

    def _collect_metrics(self, step: int):
        metrics = {}
        replicas = {}
        for svc in SCALABLE_SERVICES + ["redis-cart"]:
            m = self.metrics_collector.get_service_metrics(svc)
            metrics[svc] = m
            if svc in SCALABLE_SERVICES:
                replicas[svc] = m.get("num_pods", MIN_REPLICAS)
        self.shared_state.update(metrics, replicas, step)

    def _enforce_budget(self, proposed: dict) -> dict:
        total = sum(proposed.values())
        while total > MAX_TOTAL_PODS:
            reduced = False
            for svc in reversed(PRIORITY_ORDER):
                if total <= MAX_TOTAL_PODS:
                    break
                old = self.shared_state.replicas.get(svc, MIN_REPLICAS)
                if proposed[svc] > old:
                    proposed[svc] -= 1
                    total -= 1
                    reduced = True
            if not reduced:
                break
        return proposed
