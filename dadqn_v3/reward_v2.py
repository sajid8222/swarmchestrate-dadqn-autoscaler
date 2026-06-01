"""
DA-DQN v2 reward: Cascading bottleneck-aware reward.

Key insight from user: "when service gets high RPS, scale that service.
If latency still high, don't scale frontend further — check which service's
gRPC latency is high and scale THAT service."

This translates to a reward where each agent primarily reacts to:
  1. Its OWN backend latency (am I the bottleneck?)
  2. Its OWN RPS/traffic (am I under load?)
  3. Neighbor latencies (is my downstream bottlenecked?)
  4. Global SLA (is the system violating?)

The r_shared (global SLA) weight is reduced and r_own_latency is added.
This teaches agents: "scale up when YOU are the bottleneck, hold when you're not."
"""

import math
from dadqn_v3.config import (
    SLA_LATENCY_MS, MIN_REPLICAS, NORM_BOUNDS,
)


class CascadingRewardCalculator:
    """Reward function that teaches bottleneck-aware scaling.

    Components:
      r_bottleneck (0.35): Am I the bottleneck? Penalize high own latency/RPS
      r_neighbor   (0.25): Are my neighbors bottlenecked? (Paper 1 decay)
      r_shared     (0.25): Is the global SLA violated? (penalty-only)
      r_cost       (0.15): Pod cost penalty (monotone)

    The key change from v1: r_bottleneck replaces r_individual. Instead of
    penalizing utilization outside [0.4-0.7], we penalize high own latency
    and high traffic. This teaches the agent: "if YOUR latency is high,
    scale up. If your latency is fine, hold — even if frontend is struggling."
    """

    # Weights
    W_BOTTLENECK = 0.35
    W_NEIGHBOR = 0.25
    W_SHARED = 0.25
    W_COST = 0.15
    W_RESOURCE = 0.0   # CPU/mem utilization-target term (set >0 to enable; see RESOURCE_TARGET)

    # Resource utilization target (HPA-style): once CPU or mem util crosses this, scale up.
    RESOURCE_TARGET = 0.50
    OVER_PROVISION_W = 0.5   # how hard to penalise util << target (wasted pods); < under-provision

    # Per-service latency thresholds (gRPC backend, much lower than SLA)
    # If a service's own latency exceeds this, it's a bottleneck candidate
    OWN_LATENCY_THRESHOLD_MS = 50.0  # Most backend services run < 10ms
    OWN_LATENCY_CRITICAL_MS = 200.0  # Above this, definitely scale up

    def compute_bottleneck_reward(self, own_latency_ms: float,
                                  traffic_in: float, cpu_util: float,
                                  pods: int) -> float:
        """Am I the bottleneck? Reward based on own service health.

        - Low own latency + low traffic → hold (reward ~0)
        - High own latency + high traffic → scale up (negative reward)
        - Low own latency + high traffic → healthy, slight positive
        """
        # Latency component: penalize high own latency
        if own_latency_ms <= self.OWN_LATENCY_THRESHOLD_MS:
            lat_reward = 0.0  # Healthy
        elif own_latency_ms <= self.OWN_LATENCY_CRITICAL_MS:
            # Linear ramp from 0 to -1
            frac = (own_latency_ms - self.OWN_LATENCY_THRESHOLD_MS) / \
                   (self.OWN_LATENCY_CRITICAL_MS - self.OWN_LATENCY_THRESHOLD_MS)
            lat_reward = -frac
        else:
            # Exponential penalty above critical
            overshoot = min((own_latency_ms - self.OWN_LATENCY_CRITICAL_MS) /
                           self.OWN_LATENCY_CRITICAL_MS, 3.0)
            lat_reward = max(-1.0 * (math.exp(overshoot) - 1.0), -5.0)

        # CPU component: penalize high CPU (near saturation)
        if cpu_util > 0.8:
            cpu_penalty = -2.0 * (cpu_util - 0.8)  # -0.4 at 100%
        elif cpu_util > 0.6:
            cpu_penalty = 0.0  # Healthy range
        else:
            cpu_penalty = 0.0  # Under-utilized but that's ok (cost handles it)

        # Resource penalty (same as v1)
        resource_penalty = -0.1 * (pods - MIN_REPLICAS)

        return lat_reward + cpu_penalty + resource_penalty

    def compute_neighbor_reward(self, neighbor_latencies: dict[str, float],
                                decay_weights: dict[str, float]) -> float:
        """Are my neighbors bottlenecked? (Paper 1 decay-weighted)."""
        if not neighbor_latencies:
            return 0.0
        total = 0.0
        for svc, lat in neighbor_latencies.items():
            w = decay_weights.get(svc, 0.0)
            if w == 0.0:
                continue
            # Penalize neighbor bottlenecks (using own_latency threshold, not SLA)
            if lat <= self.OWN_LATENCY_THRESHOLD_MS:
                total += 0.0  # Neighbor healthy
            else:
                overshoot = min((lat - self.OWN_LATENCY_THRESHOLD_MS) /
                               self.OWN_LATENCY_CRITICAL_MS, 3.0)
                total += w * max(-0.5 * (math.exp(overshoot) - 1.0), -5.0)
        return total / max(len(neighbor_latencies), 1)

    def compute_shared_reward(self, frontend_latency_ms: float) -> float:
        """Global SLA violation penalty (same as v1 but lower weight)."""
        if frontend_latency_ms <= SLA_LATENCY_MS:
            return 0.0
        overshoot = min((frontend_latency_ms - SLA_LATENCY_MS) / SLA_LATENCY_MS, 3.0)
        return max(-1.0 * (math.exp(overshoot) - 1.0), -10.0)

    def compute_cost_reward(self, pods: int, max_replicas: int = 9) -> float:
        """Monotone pod cost penalty."""
        return -pods / max_replicas

    def compute_resource_reward(self, cpu_util: float, mem_util: float) -> float:
        """HPA-style two-sided utilization target. The BINDING resource (max of cpu/mem)
        should sit near RESOURCE_TARGET (~50%):
          - util > target  -> UNDER-provisioned: strong penalty (need more pods; SLA risk).
                              0 at target, -1 at 100%.
          - util < target  -> OVER-provisioned: milder penalty (wasting pods).
                              0 at target, -OVER_PROVISION_W at 0% util.
        This gives the agent a clear gradient to ADD pods under load AND REMOVE pods at idle
        (where util is tiny), so it converges to a load-appropriate count instead of over-
        provisioning. Asymmetric so under-provisioning (SLA) is penalised harder than waste."""
        u = max(cpu_util, mem_util)
        t = self.RESOURCE_TARGET
        if u > t:
            # gradient extends above 100% util (cap -3 at u~2.0) so heavily-loaded services
            # (frontend) keep a scale-up direction instead of a flat -1.
            return -min((u - t) / (1.0 - t), 3.0)
        return -self.OVER_PROVISION_W * ((t - u) / t)

    def compute_reward(self, own_latency_ms: float, traffic_in: float,
                       cpu_util: float, mem_util: float, pods: int,
                       frontend_latency_ms: float,
                       neighbor_latencies: dict = None,
                       decay_weights: dict = None) -> tuple[float, dict]:
        """Full cascading reward."""
        r_bottle = self.compute_bottleneck_reward(
            own_latency_ms, traffic_in, cpu_util, pods)
        r_neighbor = (self.compute_neighbor_reward(neighbor_latencies, decay_weights)
                      if neighbor_latencies and decay_weights else 0.0)
        r_shared = self.compute_shared_reward(frontend_latency_ms)
        r_resource = self.compute_resource_reward(cpu_util, mem_util)
        r_cost = self.compute_cost_reward(pods)

        total = (self.W_BOTTLENECK * r_bottle +
                 self.W_NEIGHBOR * r_neighbor +
                 self.W_SHARED * r_shared +
                 self.W_RESOURCE * r_resource +
                 self.W_COST * r_cost)

        return total, {
            "bottleneck": r_bottle,
            "neighbor": r_neighbor,
            "shared": r_shared,
            "resource": r_resource,
            "cost": r_cost,
            "total": total,
        }
