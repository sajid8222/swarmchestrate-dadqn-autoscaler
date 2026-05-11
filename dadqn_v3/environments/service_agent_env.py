"""
Per-service agent environment + SharedState.

Each agent observes:
  - Own 7 metrics (from CSV/cluster)
  - Decay-weighted neighbor features (Paper 1)
  - Frontend latency (shared SLA signal)
  - Time fraction
"""

import numpy as np
import gymnasium as gym
from gymnasium import spaces

from dadqn_v3.config import (
    NUM_ACTIONS, STEPS_PER_EPISODE, SCALABLE_SERVICES, ALL_SERVICES,
    MIN_REPLICAS, MAX_REPLICAS, NORM_BOUNDS,
)
from dadqn_v3.service_graph import (
    get_observation_dim, KAPPA_NEIGHBOR_MAP, DECAY_WEIGHTS,
)


class SharedState:
    """Global state — updated by coordinator, read by agents."""

    def __init__(self):
        self.max_steps: int = STEPS_PER_EPISODE
        self.reset()

    def reset(self):
        self.metrics: dict[str, dict] = {
            svc: {"num_pods": MIN_REPLICAS,
                   "cpu_usage": 0.0, "mem_usage": 0.0,
                   "traffic_in": 0.0, "traffic_out": 0.0, "latency": 0.0}
            for svc in ALL_SERVICES
        }
        self.replicas: dict[str, int] = {svc: MIN_REPLICAS for svc in SCALABLE_SERVICES}
        self.current_step: int = 0
        self.frontend_latency_ms: float = 0.0

    def update(self, metrics: dict[str, dict], replicas: dict[str, int], step: int):
        self.metrics = metrics
        self.replicas = replicas
        self.current_step = step
        self.frontend_latency_ms = metrics.get("frontend", {}).get("latency", 0.0)


class ServiceAgentEnv(gym.Env):
    """Gymnasium env for a single service's agent.

    Observation layout (6 own metrics — no desired_replicas to avoid KHPA leakage):
      [0]   num_pods (normalized)
      [1]   cpu_usage (normalized)
      [2]   mem_usage (normalized)
      [3]   traffic_in (normalized)
      [4]   traffic_out (normalized)
      [5]   latency (normalized)
      [6..N] per κ-hop neighbor: (latency×ρ^d, cpu×ρ^d, pods×ρ^d)
      [N+1]  frontend latency (if not frontend)
      [-1]   time fraction
    """

    def __init__(self, service_name: str, shared_state: SharedState):
        super().__init__()
        self.service_name = service_name
        self.shared_state = shared_state
        self.kappa_neighbors = KAPPA_NEIGHBOR_MAP[service_name]
        self.decay_weights = DECAY_WEIGHTS[service_name]
        self.is_frontend = (service_name == "frontend")

        obs_dim = get_observation_dim(service_name)
        self.observation_space = spaces.Box(0.0, 1.0, shape=(obs_dim,), dtype=np.float32)
        self.action_space = spaces.Discrete(NUM_ACTIONS)

    def _norm(self, val: float, key: str) -> float:
        mx = NORM_BOUNDS.get(key, 1.0)
        return float(np.clip(val / mx if mx > 0 else 0.0, 0.0, 1.0))

    def build_observation(self) -> np.ndarray:
        obs = []
        m = self.shared_state.metrics.get(self.service_name, {})

        # Own 6 metrics (no desired_replicas — avoids KHPA leakage)
        obs.append(self._norm(m.get("num_pods", 1), "pod_count"))
        obs.append(self._norm(m.get("cpu_usage", 0), "cpu_usage"))
        obs.append(self._norm(m.get("mem_usage", 0), "mem_usage"))
        obs.append(self._norm(m.get("traffic_in", 0), "traffic_in"))
        obs.append(self._norm(m.get("traffic_out", 0), "traffic_out"))
        obs.append(self._norm(m.get("latency", 0), "latency"))

        # κ-hop neighbor features (decay-weighted)
        for neighbor in self.kappa_neighbors:
            w = self.decay_weights.get(neighbor, 0.0)
            nm = self.shared_state.metrics.get(neighbor, {})
            obs.append(self._norm(nm.get("latency", 0), "latency") * w)
            obs.append(self._norm(nm.get("cpu_usage", 0), "cpu_usage") * w)
            obs.append(self._norm(nm.get("num_pods", 1), "pod_count") * w)

        # Frontend latency (shared SLA signal)
        if not self.is_frontend:
            obs.append(self._norm(self.shared_state.frontend_latency_ms, "latency"))

        # Time fraction (normalized by actual episode length, not hardcoded)
        max_steps = getattr(self.shared_state, 'max_steps', STEPS_PER_EPISODE)
        obs.append(min(self.shared_state.current_step / max(max_steps, 1), 1.0))

        return np.array(obs, dtype=np.float32)

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        return self.build_observation(), {}

    def step(self, action):
        obs = self.build_observation()
        done = self.shared_state.current_step >= STEPS_PER_EPISODE
        return obs, 0.0, done, False, {}
