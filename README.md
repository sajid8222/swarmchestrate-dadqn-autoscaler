# DA-DQN Autoscaler — Decentralized

A **decentralized** Deep-Q-Network autoscaler for microservices. One trained DQN
agent per microservice runs as its **own Kubernetes Deployment**, observes its
service plus its **upstream callers** (cascade graph), and scales only itself.
No central coordinator, no single point of failure.

Tested on [Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo)
running in a k3s cluster with Istio ambient mesh and Prometheus.

**👉 New to this repo? Start with [docs/QUICKSTART.md](docs/QUICKSTART.md)** — full step-by-step setup, deploy, load-test, and troubleshooting in one document.

---

## Architecture (V4)

```
External traffic ─► Istio Gateway (NodePort 30193) ─► Online Boutique
                                                      (frontend → 7 backend gRPC services)

10 DA-DQN agents (1 Deployment each, scheduled across workers):

  dadqn-frontend                ─► scales deploy/frontend
  dadqn-cartservice             ─► scales deploy/cartservice
  dadqn-productcatalogservice   ─► scales deploy/productcatalogservice
  dadqn-currencyservice         ─► scales deploy/currencyservice
  dadqn-recommendationservice   ─► scales deploy/recommendationservice
  dadqn-checkoutservice         ─► scales deploy/checkoutservice
  dadqn-adservice               ─► scales deploy/adservice
  dadqn-shippingservice         ─► scales deploy/shippingservice
  dadqn-paymentservice          ─► scales deploy/paymentservice
  dadqn-emailservice            ─► scales deploy/emailservice
```

Each agent reads from **Prometheus** (Istio mesh metrics) on a 30 s sample cycle
and decides every 15 s. Observation = own metrics + **upstream caller** features
+ frontend p95 SLA signal + time fraction. See [docs/architecture.md](docs/architecture.md).

---

## Repository layout

```
dadqn-autoscaler/
├── dadqn_v3/                       # Python agent package (all V4 patches baked in)
│   ├── service_graph.py            # CASCADE redesign — callers as neighbours
│   ├── config.py                   # SCALABLE_SERVICES, NORM_BOUNDS, etc.
│   ├── reward_v2.py                # SLA + resource + cascade reward
│   ├── agents/                     # DQN agent + multi-agent system
│   ├── environments/
│   │   ├── service_agent_env.py    # UTILIZATION-feature observation
│   │   ├── metrics_collector_v3.py # Prometheus-only metrics (no kubectl)
│   │   └── multi_agent_env_cluster_v3.py
│   └── baselines/dadqn_serve.py    # in-cluster serve loop
│
├── manifests/                      # 10 per-service Deployments + RBAC + configmap
│   ├── 01-serviceaccount.yaml
│   ├── 02-rbac.yaml
│   ├── 03-configmap.yaml           # PROMETHEUS_URL, sample/decision intervals
│   └── 04-deployment.yaml          # 10 Deployments, one per service
│
├── models/sla_v1/                  # 10 SLA-retrained DQN models (sim_cascade_v4/final)
│   └── *_dqn.zip
│
├── locust/wiki_locustfile.py       # CSV-driven load generator
├── workloads/rps-{200,400,600}.csv # load profiles
│
├── scripts/
│   ├── apply_dadqn.sh              # deploy 10 agents + locust-shim (Step 3A)
│   ├── preflight.sh                # refresh waypoints + verify shim (run before each load test)
│   └── monitor_cluster.sh          # live cluster monitor (pod counts, gateway p95, decisions)
│
├── docs/architecture.md            # observation layout, cascade graph, dims
├── Dockerfile                      # builds the agent image
└── requirements.txt
```

---

## Quick start (cluster already has Boutique + Istio + Prometheus)

The agent image **`proactivellmbasedproject/dadqn-autoscaler:v4-decentralized`** has all V4 patches baked in — no ConfigMap workarounds needed.

**Step 1 — Stage models on every worker node** (DA-DQN pods mount `/tmp/sla_v1/` via hostPath):

```bash
for worker in <worker-ip-1> <worker-ip-2> ...; do
  ssh ubuntu@$worker 'mkdir -p /tmp/sla_v1'
  scp models/sla_v1/*.zip ubuntu@$worker:/tmp/sla_v1/
done
```

**Step 2 — Apply the agents:**

```bash
kubectl apply -f manifests/
kubectl -n default wait --for=condition=Available deploy -l app=dadqn-autoscaler --timeout=180s
```

That's it — 10 decentralized per-service agents are now running and scaling Boutique. Or use the helper:

```bash
bash scripts/apply_dadqn.sh
```

**Step 3 — Watch decentralized scaling decisions:**

```bash
# Each agent prints decisions for ITS OWN service only — proof of decentralization
kubectl -n default logs deploy/dadqn-frontend  -f &
kubectl -n default logs deploy/dadqn-cartservice -f
```

---

## End-to-end setup (from empty VMs)

This walks from **3+ empty Linux VMs** to **DA-DQN scaling Boutique under load**.
Tested on AWS t2.large / t3.medium (Ubuntu 22.04 / 24.04 / 26.04).

### 1. Install k3s

**On the control-plane node:**

```bash
curl -sfL https://get.k3s.io | sh -s - server \
    --node-taint CriticalAddonsOnly=true:NoSchedule \
    --tls-san <CP-PRIVATE-IP>
sudo cat /var/lib/rancher/k3s/server/node-token   # copy this
```

> The `NoSchedule` taint keeps workload pods OFF the CP. Boutique + DA-DQN
> agents land only on workers — clean separation of concerns.

**On each worker:**

```bash
curl -sfL https://get.k3s.io | K3S_URL=https://<CP-PRIVATE-IP>:6443 \
    K3S_TOKEN=<token-from-CP> sh -
```

**Verify on CP:**

```bash
sudo chmod 644 /etc/rancher/k3s/k3s.yaml
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes        # all Ready (CP shows control-plane role, workers <none>)
```

> **Networking note:** if VMs are in different security groups (AWS etc.) you must
> open intra-VPC traffic. The k3s agent needs to reach CP on TCP 6443, and the
> mesh needs flannel VXLAN (UDP 8472) and kubelet (TCP 10250) between all nodes.

### 2. Install Istio ambient + Gateway API CRDs

```bash
# Gateway API CRDs
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.0/standard-install.yaml

# istioctl 1.23.2
curl -fsSL https://github.com/istio/istio/releases/download/1.23.2/istio-1.23.2-linux-amd64.tar.gz | tar -xz
sudo install -m 0755 istio-1.23.2/bin/istioctl /usr/local/bin/

# Install ambient profile (k3s-specific CNI paths)
# Path depends on k3s version:
#   v1.30 and earlier: /var/lib/rancher/k3s/data/current/bin
#   v1.31+:            /var/lib/rancher/k3s/data/cni
istioctl install -y --set profile=ambient \
    --set values.cni.cniBinDir=/var/lib/rancher/k3s/data/cni \
    --set values.cni.cniConfDir=/var/lib/rancher/k3s/agent/etc/cni/net.d \
    --set values.global.platform=k3s
```

If the ztunnel pods fail with `failed to find plugin "istio-cni" in path [...]`,
check the error's path and use that as `cniBinDir`.

### 3. Install Prometheus + tell it to scrape Istio

```bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install prom prometheus-community/kube-prometheus-stack \
    -n monitoring --create-namespace \
    --set grafana.enabled=false --set alertmanager.enabled=false \
    --set prometheus.prometheusSpec.retention=2h \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
    --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false

# Apply scrape monitors for Istio (NOT shipped by default in kube-prometheus-stack)
kubectl apply -f - <<'YAML'
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata: {name: istio-waypoints, namespace: monitoring}
spec:
  namespaceSelector: {any: true}
  selector:
    matchLabels: {gateway.istio.io/managed: istio.io-mesh-controller}
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

# Port-forward so DA-DQN agents (in-cluster) AND local scripts can query
nohup kubectl -n monitoring port-forward svc/prom-kube-prometheus-stack-prometheus \
      9090:9090 --address 127.0.0.1 >/tmp/prompf.log 2>&1 &
```

### 4. Deploy Online Boutique + ambient enrollment

```bash
kubectl apply -n default -f \
    https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml

kubectl label namespace default istio.io/dataplane-mode=ambient --overwrite

# Disable the built-in loadgenerator (we use Locust externally)
kubectl -n default scale deploy loadgenerator --replicas=0

for d in $(kubectl -n default get deploy -o name); do
  kubectl -n default rollout status "$d" --timeout=360s
done
```

### 5. Add waypoints (per-service L7 metrics) + Istio Gateway for external traffic

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

# Replace the boutique's NodePort with an Istio Gateway (so external traffic
# enters via the mesh and we see real client-side p95)
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

# Apply ingress gateway scrape monitor + force frontend to use new waypoints
kubectl apply -f - <<'YAML'
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata: {name: istio-ingress-gateways, namespace: monitoring}
spec:
  namespaceSelector: {any: true}
  selector: {matchLabels: {istio.io/gateway-name: frontend-gateway}}
  podMetricsEndpoints:
  - {path: /stats/prometheus, interval: 10s, relabelings: [
      {action: keep, sourceLabels: [__meta_kubernetes_pod_container_port_name], regex: "http-envoy-prom"}]}
YAML
kubectl -n default rollout restart deploy frontend
```

Verify:

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://<CP-PUBLIC-IP>:30193/
# expect HTTP 200
```

### 6. Deploy DA-DQN agents

**Stage models on every worker** (DA-DQN pods mount them via hostPath):

```bash
for w in <worker-ip-1> <worker-ip-2> ...; do
  ssh ubuntu@$w 'mkdir -p /tmp/sla_v1'
  scp models/sla_v1/*.zip ubuntu@$w:/tmp/sla_v1/
done
```

**Apply the agent manifests:**

```bash
kubectl apply -f manifests/
kubectl -n default wait --for=condition=Available deploy -l app=dadqn-autoscaler --timeout=180s
kubectl -n default get pods -l app=dadqn-autoscaler -o wide
```

The manifest uses public image `proactivellmbasedproject/dadqn-autoscaler:v4-decentralized` which already includes all V4 patches (cascade graph, utilization features, Prometheus-only metrics, NORM_BOUNDS tuning). No ConfigMap mounts required. If you want to rebuild from source, see the `Dockerfile`.

### 7. Run a load test

On any machine that can reach the cluster (laptop or a node):

```bash
pip install locust pandas

WORKLOAD_CSV=workloads/rps-200.csv DURATION_S=600 SPAWN_RATE=10 \
  locust -f locust/wiki_locustfile.py \
         --host http://<CP-PUBLIC-IP>:30193 \
         --web-host 0.0.0.0 --web-port 8089 \
         --autostart --autoquit 5
```

Watch agents react:

```bash
bash scripts/monitor_cluster.sh
# or per-service:
kubectl -n default logs deploy/dadqn-frontend  -f
```

You should see lines like:

```
decision: scaled to {'frontend': 9}
decision: scaled to {'cartservice': 6}
decision: scaled to {'productcatalogservice': 9}
```

Each agent only writes its own service — **proof of decentralization**.

---

## Configuration

`manifests/03-configmap.yaml` exposes these env vars (override per environment):

| Variable | Default | Meaning |
|---|---|---|
| `PROMETHEUS_URL` | in-cluster Prometheus svc | Where agents query metrics |
| `PROM_RATE_WINDOW` | `1m` | PromQL rate window |
| `K8S_NAMESPACE` | `default` | Boutique namespace |
| `SAMPLE_INTERVAL_SEC` | `30` | How often agents collect metrics |
| `DECISION_INTERVAL_SEC` | `15` | How often agents pick an action |
| `SCALE_DOWN_COOLDOWN_SEC` | `30` | Min sec between scale-up → scale-down |
| `LOCUST_URL` | unset | If set, used for frontend client-side p95 |

Per-pod startup patches in `manifests/04-deployment.yaml` further set:
`MAX_REPLICAS=9`, `MAX_TOTAL_PODS=90`, `NORM_BOUNDS[cpu_usage]=3000`,
`NORM_BOUNDS[latency]=1500`, `ALL_SERVICES` to the full 10+redis-cart list.

---

## Verifying decentralization

```bash
# 10 pods, one per service
kubectl -n default get pods -l app=dadqn-autoscaler -o custom-columns=POD:.metadata.name,SVC:.metadata.labels.svc

# Each pod has its own MY_SERVICE env
for svc in frontend cartservice currencyservice; do
  POD=$(kubectl -n default get pod -l app=dadqn-autoscaler,svc=$svc -o jsonpath='{.items[0].metadata.name}')
  echo "$POD: MY_SERVICE=$(kubectl -n default exec $POD -- printenv MY_SERVICE)"
done

# Each agent's log shows decisions for ITS OWN service ONLY
for svc in frontend cartservice productcatalogservice; do
  POD=$(kubectl -n default get pod -l app=dadqn-autoscaler,svc=$svc -o jsonpath='{.items[0].metadata.name}')
  echo "--- $svc agent ---"
  kubectl -n default logs $POD --tail=5 | grep decision
done
```

---

## Training methodology

Models in `models/sla_v1/` were trained against the V4 cascade reward
(SLA + resource utilization + cascade neighbour signal) using simulator
closed-loop episodes built from real Boutique observation data. Details in
[docs/architecture.md](docs/architecture.md). The reward weights, observation
layout, and sim training script are reproducible — open an issue if you want
to retrain on your own cluster's data distribution.

---

## Repro tips & gotchas

- **t3.medium / t2.large clusters work**, but smaller workers (≤ 4 GiB RAM)
  may not have enough capacity for the agents' chosen replica counts. You'll
  see pods in `Pending` state — that's a hardware ceiling, not an agent bug.
- **`preflight.sh` before every load test**: Istio waypoint pods accumulate
  stale Envoy connection state over hours, and Prometheus can lose the Istio
  scrape across a VM restart. Either condition silently inflates gateway p95
  to ~10 s. The preflight script refreshes waypoints + restarts Prometheus +
  verifies the locust-shim Service in ~30 s.
- **Image**: the deployed agent uses `proactivellmbasedproject/dadqn-autoscaler:v4-decentralized` which has every V4 patch baked in (cascade `service_graph.py`, utilization-feature `service_agent_env.py`, Prometheus-only `metrics_collector_v3.py`, NORM_BOUNDS tuned for cluster-2 sized hardware). No ConfigMap subPath mounts are needed. Rebuild from `Dockerfile` if you want a custom image; tag and update `manifests/04-deployment.yaml`.

## License

MIT.
