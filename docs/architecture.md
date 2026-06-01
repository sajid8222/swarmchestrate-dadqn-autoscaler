# Architecture — DA-DQN V4 Decentralized Autoscaler

## 1. Topology

```
                ┌──────────────────────────────────────────┐
                │  Kubernetes cluster (k3s)                │
                │                                          │
                │  CP (NoSchedule taint)                   │
                │   └─ system pods only (istiod, prom, …)  │
                │                                          │
                │  Worker-1   Worker-2   …   Worker-N      │
                │   │         │              │             │
                │   ▼         ▼              ▼             │
                │  Boutique pods (10 services + redis-cart)│
                │  Waypoint pods (1 per service)           │
                │  DA-DQN agent pods (1 per service)       │
                │                                          │
                └──────────────────────────────────────────┘

Each DA-DQN agent pod:
  └─ Loads ONE model (its own service's *_dqn.zip)
  └─ Reads metrics from Prometheus (per-service P95, RPS, CPU util)
  └─ Decides every 15s: -3 / -2 / -1 / 0 / +1 / +2 / +3 pods
  └─ Calls k8s API to scale ONLY its own Deployment
```

## 2. The cascade service graph

Each service's "neighbours" are its **upstream callers** — the services whose
traffic drives its load. When a caller gets busy/slow, its callees see more
requests and scale ahead of the wave.

```
SERVICE_CALLS (caller → callees):
  frontend         → ad, recommendation, checkout, currency, cart,
                     productcatalog, shipping
  checkoutservice  → cart, currency, productcatalog, shipping, payment, email
  recommendation   → productcatalog

SERVICE_CALLED_BY (callers of each service — these are the "neighbours"):
  frontend                 → [] (entry point; observes own + frontend p95)
  adservice                → [frontend]
  recommendationservice    → [frontend]
  checkoutservice          → [frontend]
  emailservice             → [checkout]
  paymentservice           → [checkout]
  currencyservice          → [frontend, checkout]
  cartservice              → [frontend, checkout]
  shippingservice          → [frontend, checkout]
  productcatalogservice    → [frontend, checkout, recommendation]   # most-shared
```

## 3. Observation layout (per agent)

For service `s`, the observation vector contains:

```
[ own_pods, own_cpu_util, own_mem_util,
  own_traffic_in, own_traffic_out, own_latency,
  for each caller c:  [c_latency × w, c_cpu_util × w, c_pods × w],
  frontend_latency,   # SKIPPED if s == "frontend"
  time_fraction ]
```

`w = 1.0` for direct (1-hop) callers. CPU/MEM are **utilization features**
(`usage / (limit × pods) / 2.0`, clipped to 1.0) so `50 %` utilization → `0.25`
and `≥ 200 %` → `1.0`. This is comparable across services regardless of
absolute CPU limit.

**Resulting dims per service:**

| Service | Callers | Obs dim |
|---|---|---|
| frontend | 0 | **7** (own 6 + time) |
| recommendationservice / checkoutservice / adservice / emailservice / paymentservice | 1 | **11** (own 6 + caller 3 + fe + time) |
| currencyservice / cartservice / shippingservice | 2 | **14** (own 6 + 2×3 + fe + time) |
| productcatalogservice | 3 | **17** (own 6 + 3×3 + fe + time) |

These dims must match `model.observation_space.shape` of the corresponding zip
— verify with `python -c "from stable_baselines3 import DQN; print(DQN.load('models/sla_v1/frontend_dqn.zip').observation_space.shape)"`.

## 4. Metric sources

| Metric | Prometheus query |
|---|---|
| Per-service P95 latency | `histogram_quantile(0.95, sum by(le)(rate(istio_request_duration_milliseconds_bucket{destination_workload="<svc>"}[1m])))` |
| Frontend (client-side) p95 | gateway-side: `source_workload=~".*-gateway-istio",destination_workload="frontend"` |
| Per-service RPS in | `sum(rate(istio_requests_total{destination_workload="<svc>"}[1m]))` |
| Per-service CPU (millicores) | `sum(rate(container_cpu_usage_seconds_total{namespace,pod=~"<svc>-[a-z0-9]+-[a-z0-9]+",container!="POD",container!=""}[1m])) * 1000` |
| Per-service MEM (Mi) | `sum(container_memory_working_set_bytes{namespace,pod=~"<svc>-[a-z0-9]+-[a-z0-9]+"}) / 1Mi` |
| Per-service pod count | `count(kube_pod_info{namespace,pod=~"<svc>-[a-z0-9]+-[a-z0-9]+"})` |

The pod regex `<svc>-[a-z0-9]+-[a-z0-9]+` matches only ReplicaSet-managed pods
of the deployment — it excludes `frontend-gateway-istio-…` (more segments) so
frontend's pod count isn't inflated by the gateway.

## 5. Reward (training only)

Trained against V4 cascade reward:

```
r = wr·r_resource + wn·r_neighbour + wb·r_bottleneck + ws·r_shared
       + cost·r_cost + bcost·r_bcost
```

Weights used for `sim_cascade_v4/final` (the shipped models):
- `wr = 0.30`  resource (HPA-style 50 % target on util)
- `wn = 0.25`  cascade neighbour (driven by caller latency/util/pods)
- `wb = 0.20`  per-service bottleneck (own p95 vs SLA)
- `ws = 0.20`  shared frontend p95 (global SLA term)
- `cost = 0.05` mild cost penalty
- `bcost = 0`  no flat per-pod bottleneck cost
- `NORM_BOUNDS[latency] = 1500` (resolves the 0-1500 ms SLA-relevant band)
- `NORM_BOUNDS[cpu_usage] = 3000` (unsaturates frontend at high util)
- SLA target = `500 ms` (frontend p95)
- Steady-load episodes + difference reward for per-agent credit assignment

## 6. Why decentralized?

| Aspect | Centralized (one pod, all agents) | Decentralized (this) |
|---|---|---|
| Single point of failure | Yes — pod crash = all scaling stops | No — each agent independent |
| Pod count | 1 | 10 |
| Coordination | Shared state in-process | None — each reads Prom independently |
| Algorithm | Same (per-service Q-network) | Same |
| Observation completeness | Full graph (10 services in shared state) | Same — each pod's metric collector reads all 11 |
| k8s topology | nodeSelector pinned | Free scheduling across workers |

The V4 architecture's win: **fault isolation** without sacrificing observation
completeness. Each agent's metric collector reads the full service graph from
Prometheus; only the action surface is per-pod.

## 7. Training data + reproducibility

Models were trained in the closed-loop replay simulator
(`MultiAgentEnvironmentV3`) on dense observation sweeps collected from a real
5-node cluster:
- Loads: 50 / 100 / 150 / 200 / 300 / 600 / 1000 RPS
- Per load: hold steady, sweep critical-path services through pods 1 / 2 / 4 / 6 / 8
- Per-service P95 latency captured (not mean — earlier versions used mean which
  fooled the model because 2 cart pods → 60 ms mean but 216 ms p95)
- Final dataset: ~209 rows, idle→stress contrast

Retraining for a different cluster: collect a fresh dense sweep with the same
methodology, retrain `sim_finetune.py` with the same reward weights and
`--norm-latency 1500`. The sim trainer lives in the fresh_starts study tree
(not shipped here; open an issue if needed).
