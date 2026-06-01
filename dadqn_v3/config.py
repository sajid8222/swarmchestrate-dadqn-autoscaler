"""DA-DQN v3 configuration. Trained on closed-loop observer data."""

import os
from pathlib import Path

V3_ROOT = Path(__file__).parent
DATA_DIR = Path(os.path.expanduser("~/dadqn_v3_data"))
MODEL_DIR = V3_ROOT / "models"

# Services
SCALABLE_SERVICES = [
    "frontend", "currencyservice", "productcatalogservice", "cartservice",
    "adservice", "checkoutservice", "emailservice", "paymentservice",
    "shippingservice", "recommendationservice",
]
ALL_SERVICES = SCALABLE_SERVICES + ["redis-cart"]

# Scaling limits
MIN_REPLICAS = 1
MAX_REPLICAS = 9                  # per-service cap (was 8) — user spec: min 1, max 9
MAX_TOTAL_PODS = 90               # non-binding (10 svc x 9); per-service cap + cost govern, not fixed trimming
NUM_ACTIONS = 7  # {-3, -2, -1, 0, +1, +2, +3}
ACTION_TO_DELTA = {0: -3, 1: -2, 2: -1, 3: 0, 4: 1, 5: 2, 6: 3}
HOLD_ACTION = 3

# SLA
SLA_LATENCY_MS = 1200

# Episode
STEPS_PER_EPISODE = 100
EPISODE_LENGTH = 100

# κ-hop neighborhood
KAPPA_HOP = 1
DECAY_RATE = 0.5

# V3 CSV columns per service
V3_METRICS = ["num_pods", "cpu_usage", "mem_usage", "http_rpm", "http_lat_ms", "grpc_rpm", "grpc_lat_ms"]

# Normalization bounds
NORM_BOUNDS = {
    "pod_count": MAX_REPLICAS,
    "cpu_usage": 3000.0,   # was 800 — frontend/rec run 900-2855m; 800 saturated the obs at 1.0
    "mem_usage": 1000.0,
    "http_rpm": 15000.0,
    "http_lat_ms": 5000.0,
    "grpc_rpm": 50000.0,
    "grpc_lat_ms": 5000.0,
    "latency": 5000.0,
    "traffic_in": 50000.0,
    "traffic_out": 50000.0,
}

# Reward weights (V2 cascading)
REWARD_W_BOTTLENECK = 0.35
REWARD_W_NEIGHBOR = 0.25
REWARD_W_SHARED = 0.25
REWARD_W_COST = 0.15

# DQN config
DQN_CONFIG = {
    "learning_rate": 1e-3,
    "buffer_size": 10_000,
    "learning_starts": 500,
    "batch_size": 64,
    "gamma": 0.99,
    "target_update_interval": 100,
    "exploration_fraction": 0.3,
    "exploration_final_eps": 0.05,
    "policy_kwargs": {"net_arch": [64, 64]},
}

# Service resources (for utilization calculation)
SERVICE_RESOURCES = {
    "frontend":              {"cpu_limit_m": 200, "mem_limit_mi": 256},
    "currencyservice":       {"cpu_limit_m": 200, "mem_limit_mi": 128},
    "productcatalogservice": {"cpu_limit_m": 200, "mem_limit_mi": 128},
    "cartservice":           {"cpu_limit_m": 300, "mem_limit_mi": 128},
    "adservice":             {"cpu_limit_m": 300, "mem_limit_mi": 300},
    "checkoutservice":       {"cpu_limit_m": 200, "mem_limit_mi": 128},
    "emailservice":          {"cpu_limit_m": 200, "mem_limit_mi": 128},
    "paymentservice":        {"cpu_limit_m": 200, "mem_limit_mi": 128},
    "shippingservice":       {"cpu_limit_m": 200, "mem_limit_mi": 128},
    "recommendationservice": {"cpu_limit_m": 500, "mem_limit_mi": 1024},
}
