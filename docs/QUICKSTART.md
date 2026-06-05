# Quickstart — DA-DQN V4

End-to-end guide from `git clone` to a working experiment with Locust. If you already have a k3s cluster running Online Boutique + Istio ambient + Prometheus, skip to **§3**.

---

## 0. Prerequisites

| Need | Why |
|---|---|
| Linux VMs (≥3 nodes, ≥2 vCPU, ≥4 GB each) | Run k3s cluster |
| SSH access to all nodes | Setup + scp models |
| `kubectl` on the machine you'll run from | Apply manifests |
| `locust` Python pkg | Generate load (laptop or jump-box) |
| Open inter-node ports: 6443/tcp, 8472/udp, 10250/tcp | k3s + flannel + kubelet |
| Open external NodePort 30193/tcp from laptop | So Locust can hit gateway |

---

## 1. Clone the repo

```bash
git clone https://github.com/sajid8222/swarmchestrate-dadqn-autoscaler.git
cd swarmchestrate-dadqn-autoscaler

git log --oneline -3
# Top commit must be 41fbd29 or later
```

Layout:

```
dadqn_v3/        Python agent package (V4 patches baked in)
manifests/       4 YAMLs (SA, RBAC, configmap, 10 Deployments)
models/sla_v1/   10 trained DQN model zips (~1 MB total)
locust/          load generator script
workloads/       4 CSV load profiles (rps-200, 400, 600, c2)
scripts/         apply_dadqn.sh, monitor.sh, preflight_full.sh
docs/            architecture.md, QUICKSTART.md (this file)
Dockerfile       optional: rebuild your own image
```

---

## 2. Cluster setup (skip if cluster already has Boutique + Istio + Prometheus)

### 2A. Install k3s

**On the control-plane node:**

```bash
curl -sfL https://get.k3s.io | sh -s - server \
    --node-taint CriticalAddonsOnly=true:NoSchedule \
    --tls-san <CP-PRIVATE-IP>

sudo chmod 644 /etc/rancher/k3s/k3s.yaml
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

sudo cat /var/lib/rancher/k3s/server/node-token   # copy this
```

> The `NoSchedule` taint keeps boutique + DA-DQN pods off the control-plane. They land only on workers — clean separation.

**On each worker:**

```bash
curl -sfL https://get.k3s.io | \
    K3S_URL=https://<CP-PRIVATE-IP>:6443 \
    K3S_TOKEN=<token-from-CP> sh -
```

Verify on CP: `kubectl get nodes` → all Ready.

### 2B. Install Istio ambient mesh

```bash
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.0/standard-install.yaml

curl -fsSL https://github.com/istio/istio/releases/download/1.23.2/istio-1.23.2-linux-amd64.tar.gz | tar -xz
sudo install -m 0755 istio-1.23.2/bin/istioctl /usr/local/bin/

# CNI path depends on k3s version:
#   v1.30 and earlier: /var/lib/rancher/k3s/data/current/bin
#   v1.31+:            /var/lib/rancher/k3s/data/cni
istioctl install -y --set profile=ambient \
    --set values.cni.cniBinDir=/var/lib/rancher/k3s/data/cni \
    --set values.cni.cniConfDir=/var/lib/rancher/k3s/agent/etc/cni/net.d \
    --set values.global.platform=k3s

kubectl -n istio-system rollout status deploy/istiod      --timeout=180s
kubectl -n istio-system rollout status ds/ztunnel         --timeout=180s
kubectl -n istio-system rollout status ds/istio-cni-node  --timeout=180s
```

> If ztunnel fails with `failed to find plugin "istio-cni" in path [...]`, take the path from the error and use that as `cniBinDir`.

### 2C. Install Prometheus + add Istio scrape monitors

```bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install prom prometheus-community/kube-prometheus-stack \
    -n monitoring --create-namespace \
    --set grafana.enabled=false --set alertmanager.enabled=false \
    --set prometheus.prometheusSpec.retention=2h \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
    --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false

# kube-prometheus-stack does NOT scrape Istio by default — apply these 3 monitors:
kubectl apply -f - <<'YAML'
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata: {name: istio-waypoints, namespace: monitoring}
spec:
  namespaceSelector: {any: true}
  selector: {matchLabels: {gateway.istio.io/managed: istio.io-mesh-controller}}
  podMetricsEndpoints:
  - path: /stats/prometheus
    interval: 15s
    relabelings:
    - action: keep
      sourceLabels: [__meta_kubernetes_pod_container_port_name]
      regex: 'http-envoy-prom'
---
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata: {name: istio-ztunnel, namespace: monitoring}
spec:
  namespaceSelector: {matchNames: [istio-system]}
  selector: {matchLabels: {app: ztunnel}}
  podMetricsEndpoints:
  - {port: ztunnel-stats, path: /metrics, interval: 15s}
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata: {name: istiod, namespace: monitoring}
spec:
  namespaceSelector: {matchNames: [istio-system]}
  selector: {matchLabels: {app: istiod}}
  endpoints:
  - {port: http-monitoring, interval: 15s}
YAML

nohup kubectl -n monitoring port-forward svc/prom-kube-prometheus-stack-prometheus \
      9090:9090 --address 127.0.0.1 >/tmp/prompf.log 2>&1 &
```

### 2D. Deploy Online Boutique + ambient enrollment

```bash
kubectl apply -n default -f \
    https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml

kubectl label namespace default istio.io/dataplane-mode=ambient --overwrite

# Disable built-in loadgenerator (we use Locust externally)
kubectl -n default scale deploy loadgenerator --replicas=0

for d in $(kubectl -n default get deploy -o name); do
  kubectl -n default rollout status "$d" --timeout=360s
done
```

### 2E. Add waypoints (for L7 per-service latency)

```bash
for pair in adservice:ad-waypoint cartservice:cart-waypoint \
            checkoutservice:chk-waypoint currencyservice:cur-waypoint \
            emailservice:email-waypoint frontend:fe-waypoint \
            paymentservice:pay-waypoint productcatalogservice:prod-waypoint \
            recommendationservice:rec-waypoint shippingservice:ship-waypoint; do
  svc=${pair%:*}; wp=${pair#*:}
  istioctl waypoint apply -n default --name "$wp" --enroll-namespace=false
  kubectl -n default label svc "$svc" istio.io/use-waypoint="$wp" --overwrite
done

kubectl -n default get gateway.gateway.networking.k8s.io
# All 10 must show PROGRAMMED=True
```

### 2F. Replace NodePort with Istio Gateway

```bash
kubectl -n default delete svc frontend-external --ignore-not-found

kubectl apply -f - <<'YAML'
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata: {name: frontend-gateway, namespace: default}
spec:
  gatewayClassName: istio
  listeners:
  - {name: http, port: 80, protocol: HTTP, allowedRoutes: {namespaces: {from: Same}}}
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: {name: frontend-route, namespace: default}
spec:
  parentRefs: [{name: frontend-gateway}]
  rules:
  - matches: [{path: {type: PathPrefix, value: /}}]
    backendRefs: [{name: frontend, port: 80}]
YAML

kubectl -n default patch svc frontend-gateway-istio --type=json -p='[
  {"op":"replace","path":"/spec/ports","value":[
    {"name":"status-port","port":15021,"protocol":"TCP","targetPort":15021},
    {"name":"http","port":80,"protocol":"TCP","targetPort":80,"nodePort":30193}
  ]},{"op":"replace","path":"/spec/type","value":"NodePort"}]'

kubectl -n default rollout restart deploy frontend

curl -s -o /dev/null -w "gateway HTTP %{http_code}\n" http://<CP-PUBLIC-IP>:30193/
# Expect: HTTP 200
```

---

## 3. Deploy DA-DQN agents

The trained model weights are **baked into the Docker image** at `/app/models/sla_v1/` (see `Dockerfile`'s `COPY models/sla_v1/` step). No manual staging is needed — each agent pod loads its service's `.zip` directly from the image.

> **Heads-up if you're forking with custom models:** rebuild the image (`docker build -t <your-registry>/dadqn-autoscaler:<tag> .`) and update `image:` in `manifests/04-deployment.yaml`. The manifests don't mount any `hostPath` for models — models live entirely inside the image.

### 3A. Apply the agents (single command)

From the repo root, with `KUBECONFIG` pointing at the cluster:

```bash
bash scripts/apply_dadqn.sh
```

The script:

1. `kubectl apply -f manifests/`
2. Waits for all 10 deployments to be Available
3. Prints pod placement + a sample frontend agent log

### 3B. Verify decentralization

```bash
# All 10 pods Running, one per service
kubectl -n default get pods -l app=dadqn-autoscaler -o wide

# Each pod has its own MY_SERVICE env
for svc in frontend cartservice currencyservice; do
  POD=$(kubectl -n default get pod -l app=dadqn-autoscaler,svc=$svc -o jsonpath='{.items[0].metadata.name}')
  echo "$POD: MY_SERVICE=$(kubectl -n default exec $POD -- printenv MY_SERVICE)"
done

# CRITICAL: each agent sees ONLY its own service in SCALABLE_SERVICES
DA=$(kubectl -n default get pod -l app=dadqn-autoscaler,svc=frontend -o jsonpath='{.items[0].metadata.name}')
kubectl -n default exec $DA -- python -c "from dadqn_v3.config import SCALABLE_SERVICES; print(SCALABLE_SERVICES)"
# MUST be ['frontend'] (length 1, not 10)

# Each agent decides for ITS OWN service only
for svc in frontend cartservice productcatalogservice; do
  POD=$(kubectl -n default get pod -l app=dadqn-autoscaler,svc=$svc -o jsonpath='{.items[0].metadata.name}')
  echo "--- $svc agent ---"
  kubectl -n default logs $POD --tail=3 | grep decision
done
# Each line must have only one service in the dict:
#   decision: scaled to {'frontend': 3}              ← correct (decentralized)
# NOT:
#   decision: scaled to {'frontend': 1, 'cart': 1, ...}  ← WRONG (centralized — see Troubleshooting)
```

---

## 4. Locust load test

Run **outside the cluster** (laptop / jump-box). Locust generates external traffic that enters the cluster via the Istio Gateway on NodePort 30193.

### 4A. Install Locust (once)

```bash
pip install locust pandas
# or:
python3 -m venv .venv && source .venv/bin/activate && pip install locust pandas
```

### 4B. Pick a workload profile

| File | Peak users | Approx peak RPS | Use case |
|---|---|---|---|
| `rps-200.csv` | 840 const | ~170 | Light steady load |
| `rps-400.csv` | 1700 const | ~340 | Medium load |
| `rps-600.csv` | 2500 const | ~500 | Heavy / saturation |
| `rps-c2.csv` | 100→500→100 | 30→165 | Gentle ramp (good for small clusters) |

First time on a small cluster (≤4 workers × 4 GB RAM)? Start with `rps-c2.csv`.

### 4C. Run

```bash
cd swarmchestrate-dadqn-autoscaler

WORKLOAD_CSV=workloads/rps-c2.csv \
DURATION_S=1200 \
SPAWN_RATE=10 \
locust -f locust/wiki_locustfile.py \
       --host http://<CP-PUBLIC-IP>:30193 \
       --web-host 0.0.0.0 --web-port 8089 \
       --autostart --autoquit 5
```

Env vars:

- `WORKLOAD_CSV` — which CSV shape to read
- `DURATION_S` — total wall-clock duration. CSV stages are evenly split across this (10 stages × 120s = 1200s here)
- `SPAWN_RATE` — users spawned per second during ramps
- `--autostart` — start the swarm immediately
- `--autoquit 5` — quit 5s after the shape finishes
- `--web-host 0.0.0.0 --web-port 8089` — Web UI at http://localhost:8089

The locustfile drives a realistic user behavior pattern: homepage → browse products → add to cart → view cart → change currency → checkout. Each user waits 5s between actions (or 1–5s if you use `wiki_locustfile_c2.py`).

### 4D. Watch it run

Browser: `http://localhost:8089` — live RPS, p95, users.

In another terminal:

```bash
# Live monitor — gateway p95, replicas, latest agent decisions
bash scripts/monitor.sh   # if you cloned this script, otherwise see scripts/

# Or per-service:
kubectl -n default logs deploy/dadqn-frontend -f
```

### 4E. Expected scaling behavior

| Time | Locust | Boutique | DA-DQN |
|---|---|---|---|
| 0–1m | users 0→100, RPS 0→40 | baseline 1/1 each | mostly HOLD |
| 1–3m | users 100→300 | frontend 2→4, currency 2–3 | +1/+2 actions |
| 3–5m | users 300→500 peak | frontend 5–9, productcatalog 5–9, rec 5–8 | aggressive scale-up |
| 5–7m | hold at 500 | stable | steady state |
| 7–20m | ramp down | scales DOWN | -1/-2 with 30s cooldown |

---

## 5. Cleanup

```bash
kubectl -n default delete -f manifests/

for svc in frontend cartservice currencyservice productcatalogservice recommendationservice adservice checkoutservice emailservice paymentservice shippingservice; do
  kubectl -n default scale deploy $svc --replicas=1
done

# Stop VMs (AWS example)
aws ec2 stop-instances --instance-ids <id1> <id2> ...
```

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Each agent log shows decisions for ALL 10 services in the dict | Per-pod `SCALABLE_SERVICES` patch silently failed; pod is using a stale cached image | `kubectl rollout restart deploy -l app=dadqn-autoscaler`. If still bad, on each worker: `sudo k3s crictl rmi docker.io/proactivellmbasedproject/dadqn-autoscaler:v4-decentralized` to wipe cache, then re-apply manifests. Verify with `kubectl exec ... -- python -c "from dadqn_v3.config import SCALABLE_SERVICES; print(SCALABLE_SERVICES)"` — must show 1 item. |
| `ztunnel` pod stuck `ContainerCreating` with `failed to find plugin "istio-cni" in path [...]` | k3s CNI dir differs from istioctl default | Take the path from the error, re-run `istioctl install` with that as `cniBinDir` |
| `kubectl get nodes` from laptop times out | Cluster API (port 6443) not open from your IP | SSH to CP and run from there, or open SG: `aws ec2 authorize-security-group-ingress --group-id <sg> --protocol tcp --port 6443 --cidr <your-ip>/32` |
| Agents come up but crash with file-not-found loading model | Models not staged on the worker where the pod landed | scp models to **all** workers' `/tmp/sla_v1/` |
| Locust web UI launches but stays `Status: ready` | `--autostart` didn't trigger | Click "Start" in the browser, or add `--users 500 --spawn-rate 10` to the launch command |
| Gateway p95 huge (>2000ms) at peak load | Cluster physical CPU/MEM ceiling | `kubectl top nodes` — workers >80% means hardware limit, not agent bug. Use bigger instances or lower workload |
| `Locust unavailable, using Gateway p95 (...)` in agent log | Normal — agent fell back to Prometheus gateway p95 since `LOCUST_URL` isn't set | Expected. The fallback is functionally equivalent. |

---

## 7. Quick reference

| Action | Command |
|---|---|
| Deploy agents | `bash scripts/apply_dadqn.sh` |
| Run load test | `WORKLOAD_CSV=workloads/rps-c2.csv DURATION_S=1200 locust -f locust/wiki_locustfile.py --host http://<CP>:30193 --web-port 8089 --autostart --autoquit 5` |
| Watch agents | `kubectl -n default logs deploy/dadqn-frontend -f` |
| Stop agents | `kubectl -n default delete -f manifests/` |
| Verify decentralization | `kubectl exec deploy/dadqn-frontend -- python -c "from dadqn_v3.config import SCALABLE_SERVICES; print(SCALABLE_SERVICES)"` → must be `['frontend']` |
