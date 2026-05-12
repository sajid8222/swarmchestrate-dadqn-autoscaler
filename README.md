# Swarmchestrate DA-DQN Autoscaler

A trained **Decentralised Autoscaling agent** (DA-DQN v3 — one Deep Q-Network per microservice, multi-agent) packaged as a Docker image + Kubernetes manifests. Drop it into a k3s cluster running [Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo) and the agent scales the deployments automatically based on Istio mesh metrics and (optionally) Locust client-side p95.

**Public image:** [`proactivellmbasedproject/dadqn-autoscaler:v2`](https://hub.docker.com/r/proactivellmbasedproject/dadqn-autoscaler)
(v1 is also available; v2 is recommended — see [what's new](#whats-new-in-v2).)

The agent does **not** retrain in production. The trained Q-network weights for all 10 services are baked into the image.

## What's new in v2

- **Production-realistic latency source.** When the external load tester is not directly reachable from inside the cluster (the normal production case), the agent now reads end-to-end client p95 from the **Istio ingress Gateway**'s `istio_request_duration_milliseconds` histogram. This is the canonical observability source-of-truth and matches what the trained Q-networks expect. *Before:* fallback queried backend-call mean latency and under-reported by ~100× on single-node setups, leaving the agent passive.
- **Cooldown 30 s** (was 90 s) — matches the reference DAS/CustomDAS framework cooldown so comparisons are apples-to-apples.

## Reproducing the 4-way comparison

The [`experiments/`](experiments/) directory ships everything to reproduce the KHPA / DAS / CustomDAS / DA-DQN sweep:

```bash
cd experiments
NODE_IP=<your-cluster-ip> PEM=<your-ssh-key.pem> bash preflight.sh    # 10-check sanity
NODE_IP=<your-cluster-ip> PEM=<your-ssh-key.pem> bash run_4way.sh     # ~4 hours full sweep
python plot_rps_4way.py                                                # 6-panel comparison plot
```

`live_monitor.sh` (per-service mesh + node CPU/MEM, 5 s refresh) and `node_monitor.sh` (continuous CSV) ship alongside.

---

## TL;DR

If you already have a k3s cluster with Online Boutique + Istio ambient mesh + Prometheus running, you can deploy the agent in **3 commands** (see [Quick start](#quick-start-3-commands-cluster-already-set-up)).

If you are starting from scratch (just have a few Linux VMs), follow the **[End-to-end guide](#end-to-end-guide-from-zero-to-autoscaling)** below.

---

## End-to-end guide (from zero to autoscaling)

This walks a beginner from "I have 3 empty Linux VMs" to "DA-DQN is autoscaling Online Boutique under load". Tested on AWS `t2.large` (Ubuntu 24.04).

### Step 0 — What you need before starting

| On your laptop | On each VM |
|---|---|
| SSH access to the VMs | Ubuntu 22.04 / 24.04 (or any systemd Linux) |
| `kubectl` installed | 2 vCPU, 4 GB RAM minimum, 30 GB disk |
| `git` installed | Outbound internet (to pull k3s, Helm, Istio, images) |

> **Networking:** if the VMs are in different security groups (e.g. AWS), open these ports between them: **TCP 6443** (k3s API), **UDP 8472** (flannel VXLAN), **TCP 10250** (kubelet). Otherwise workers will loop on `failed to get CA certs`.

For the rest of this guide I'll assume:
- `node1` = k3s server (control plane + workload)
- `node2`, `node3` = k3s workers

Use whatever hostnames or SSH aliases you like — just replace them in the commands.

---

### Step 1 — Install k3s on the cluster

**On `node1` (k3s server):**

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.30.4+k3s1 sh -s - server \
    --write-kubeconfig-mode 644 \
    --disable traefik

# Grab the node-join token
sudo cat /var/lib/rancher/k3s/server/node-token
# Copy the printed token — you'll need it on the workers.
```

**On `node2` and `node3` (k3s workers):**

```bash
NODE1_IP=<private IP of node1>
TOKEN=<token from previous step>

curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.30.4+k3s1 \
    K3S_URL="https://${NODE1_IP}:6443" \
    K3S_TOKEN="${TOKEN}" sh -s - agent
```

**Verify on `node1`:**

```bash
sudo k3s kubectl get nodes
```

You should see all 3 nodes `Ready`:

```
NAME    STATUS   ROLES                  AGE   VERSION
node1   Ready    control-plane,master   2m    v1.30.4+k3s1
node2   Ready    <none>                 30s   v1.30.4+k3s1
node3   Ready    <none>                 25s   v1.30.4+k3s1
```

**Export kubeconfig** (so the rest of the guide works without `sudo k3s`):

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes
```

---

### Step 2 — Install `istioctl` and Helm on `node1`

```bash
# istioctl 1.23.2
cd /tmp
curl -fsSL https://github.com/istio/istio/releases/download/1.23.2/istio-1.23.2-linux-amd64.tar.gz -o istio.tgz
tar -xzf istio.tgz
sudo install -m 0755 istio-1.23.2/bin/istioctl /usr/local/bin/istioctl
rm -rf istio.tgz istio-1.23.2/
istioctl version --remote=false   # should print 1.23.2

# Helm
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version --short
```

---

### Step 3 — Install Istio ambient mesh

```bash
# Gateway API CRDs (needed for waypoints)
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.0/standard-install.yaml

# Istio ambient profile — k3s requires custom CNI paths
istioctl install -y --set profile=ambient \
    --set values.cni.cniBinDir=/var/lib/rancher/k3s/data/current/bin \
    --set values.cni.cniConfDir=/var/lib/rancher/k3s/agent/etc/cni/net.d \
    --set values.global.platform=k3s

# Wait until the 3 ambient components are Ready
kubectl -n istio-system rollout status deploy/istiod         --timeout=180s
kubectl -n istio-system rollout status ds/ztunnel            --timeout=180s
kubectl -n istio-system rollout status ds/istio-cni-node     --timeout=180s
```

> **Why those flags?** Vanilla `istioctl install --set profile=ambient` writes the CNI to `/opt/cni/...`, which k3s does not use. The flags above point Istio to k3s's actual CNI directories — without them `istio-cni-node` will be `Running` but install no network rules, and the mesh will silently fail to capture traffic.

---

### Step 4 — Install Prometheus and tell it to scrape Istio

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install prom prometheus-community/kube-prometheus-stack \
    -n monitoring --create-namespace \
    --set grafana.enabled=false \
    --set alertmanager.enabled=false \
    --set prometheus.prometheusSpec.retention=2h \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
    --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false

kubectl -n monitoring rollout status \
    statefulset/prometheus-prom-kube-prometheus-stack-prometheus --timeout=300s
```

**Critical:** `kube-prometheus-stack` does NOT scrape Istio by default — you have to add three scrape rules manually:

```bash
cat <<'YAML' | kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata: { name: istio-waypoints, namespace: monitoring }
spec:
  namespaceSelector: { any: true }
  selector:
    matchLabels: { gateway.istio.io/managed: istio.io-mesh-controller }
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
metadata: { name: istio-ztunnel, namespace: monitoring }
spec:
  namespaceSelector: { matchNames: [istio-system] }
  selector: { matchLabels: { app: ztunnel } }
  podMetricsEndpoints:
  - { port: ztunnel-stats, path: /metrics, interval: 15s }
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata: { name: istiod, namespace: monitoring }
spec:
  namespaceSelector: { matchNames: [istio-system] }
  selector: { matchLabels: { app: istiod } }
  endpoints:
  - { port: http-monitoring, interval: 15s }
YAML
```

**Verify Prometheus is scraping Istio:**

```bash
kubectl -n monitoring port-forward svc/prom-kube-prometheus-stack-prometheus 9090:9090 &
curl -s 'http://localhost:9090/api/v1/query?query=count(\{__name__=~"istio.*"\})' | head -c 200
kill %1
```

You should see a non-zero `value`. If it returns `0`, the PodMonitors didn't match — re-check `kubectl -n monitoring get podmonitor`.

---

### Step 5 — Deploy Online Boutique into the ambient mesh

```bash
# 1) apply the upstream manifests in the default namespace
kubectl apply -n default -f \
    https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml

# 2) enroll the namespace in the ambient mesh (L4 mTLS + metrics, zero config)
kubectl label namespace default istio.io/dataplane-mode=ambient --overwrite

# 3) wait for the deployments to come up (~3-6 min)
for d in $(kubectl -n default get deploy -o name); do
  kubectl -n default rollout status "$d" --timeout=360s
done

# 4) get the frontend NodePort so you can hit the app
kubectl -n default get svc frontend
```

Hit `http://<any-node-public-ip>:<frontend-NodePort>` in a browser — you should see the Online Boutique shop. **If this works, the mesh is healthy.**

**Add L7 waypoints for all 10 services** (needed so the agent gets per-service HTTP/gRPC latency, not just TCP byte counts):

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

kubectl -n default get gateways
# All 10 should show: PROGRAMMED=True
```

> **Why the naming?** The waypoint name **must not** match an existing Service name. If you name a waypoint `frontend` while a Service `frontend` exists, the Gateway controller tries to bind to that Service's port 15008 (which doesn't exist) and you get `AddressNotUsable`. Convention: `<svc>-waypoint`.

**Expose frontend via Istio ingress Gateway** (so external HTTP enters the mesh and the agent sees real end-to-end p95). Without this, external NodePort traffic bypasses the mesh and the agent's `frontend_latency_ms` only reflects backend hops (~10 ms, way under-reporting reality).

```bash
# 1) replace the NodePort frontend-external Service with an Istio Gateway
kubectl -n default delete svc frontend-external --ignore-not-found

cat <<'YAML' | kubectl apply -f -
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata: { name: frontend-gateway, namespace: default }
spec:
  gatewayClassName: istio
  listeners:
  - { name: http, port: 80, protocol: HTTP, allowedRoutes: { namespaces: { from: Same } } }
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: frontend-route, namespace: default }
spec:
  parentRefs: [{ name: frontend-gateway }]
  rules:
  - matches: [{ path: { type: PathPrefix, value: / } }]
    backendRefs: [{ name: frontend, port: 80 }]
YAML

# 2) patch the auto-created Gateway Service to NodePort 30193 (your old port)
kubectl -n default patch svc frontend-gateway-istio --type=json -p='[
  {"op":"replace","path":"/spec/ports","value":[
    {"name":"status-port","port":15021,"protocol":"TCP","targetPort":15021},
    {"name":"http","port":80,"protocol":"TCP","targetPort":80,"nodePort":30193}
  ]}]'

# 3) tell Prometheus to scrape the ingress Gateway's Envoy stats
cat <<'YAML' | kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata: { name: istio-ingress-gateways, namespace: monitoring }
spec:
  namespaceSelector: { any: true }
  selector:
    matchLabels: { istio.io/gateway-name: frontend-gateway }
  podMetricsEndpoints:
  - { path: /stats/prometheus, interval: 10s, relabelings: [
      { action: keep, sourceLabels: [__meta_kubernetes_pod_container_port_name], regex: "http-envoy-prom" }
    ]}
YAML

# 4) force frontend to re-establish gRPC connections through the new waypoints
#    (frontend pods that started BEFORE waypoints were applied use long-lived
#     gRPC clients that skip the waypoint and emit no per-service mesh metrics)
kubectl -n default rollout restart deploy frontend
```

---

### Step 6 — Deploy the DA-DQN autoscaler (the part this repo is about)

```bash
# back on your laptop:
git clone https://github.com/sajid8222/swarmchestrate-dadqn-autoscaler.git
cd swarmchestrate-dadqn-autoscaler

# point kubectl at the k3s cluster (copy kubeconfig from node1, replace 127.0.0.1 with node1's public IP)
scp node1:/etc/rancher/k3s/k3s.yaml ./kubeconfig.yaml
sed -i "s|server: https://127.0.0.1:6443|server: https://<node1-public-ip>:6443|" ./kubeconfig.yaml
export KUBECONFIG=$PWD/kubeconfig.yaml

# apply the 4 manifests
kubectl apply -f manifests/
kubectl wait --for=condition=Ready pod -l app=dadqn-autoscaler --timeout=60s
kubectl logs -f deployment/dadqn-autoscaler
```

Within ~60 seconds you should see:

```
loading model: /app/dadqn_v3/models/finetuned_v3_actual
sample=30s decision=15s cooldown=90s csv=/data/wiki_dadqn_serve_<ts>.csv
t=    0s users=   0 rps=  0.0 p95=     0ms pods= 11 viol=0
  decision: scaled to {'frontend': 1, 'currencyservice': 1, ...}
```

That's the agent sampling Prometheus every 30 s and issuing scaling decisions every 15 s — **running inside the cluster**, no SSH, no laptop kubectl needed once it's deployed.

---

### Step 7 — Generate load with Locust and watch it autoscale

On any node (the worker is a good choice), install Locust and run a CSV-driven workload:

```bash
pip install locust
git clone https://github.com/sajid8222/swarmchestrate-dadqn-autoscaler.git ~/dadqn   # for the workloads/
cd ~/dadqn

CSV_PATH=workloads/rps-600.csv TIME_MINUTE=10 SPAWN_RATE=20 \
  locust -f wiki_locustfile.py \
         --host http://localhost:<frontend-NodePort> \
         --autostart --autoquit 5 \
         --web-host 0.0.0.0 --web-port 8089
```

While it runs, watch the agent in another terminal:

```bash
kubectl logs -f deployment/dadqn-autoscaler

# in a second terminal — watch pod counts grow under load and shrink after
watch -n2 'kubectl -n default get deploy'
```

You should see:
- `frontend` replicas jump from 1 → 2 or 3 as RPS climbs.
- `recommendationservice` / `productcatalogservice` follow.
- After Locust finishes, replicas slowly scale back to 1.

---

### Step 8 — Pull out the experiment data

```bash
POD=$(kubectl get pod -l app=dadqn-autoscaler -o jsonpath='{.items[0].metadata.name}')

# CSV (one row per 30 s sample)
kubectl cp default/$POD:/data ./agent_data/

# full agent log
kubectl logs deployment/dadqn-autoscaler --tail=10000 > agent.log
```

The CSV columns: `step, sec, rps, locust_users, locust_p95_ms, frontend_latency_ms, total_pods, sla_violation, <service>_pods × 10`.

You're done — that's a complete autoscaling experiment.

---

## Quick start (3 commands, cluster already set up)

If your cluster already has Online Boutique + Istio ambient + Prometheus:

```bash
git clone https://github.com/sajid8222/swarmchestrate-dadqn-autoscaler.git
cd swarmchestrate-dadqn-autoscaler
kubectl apply -f manifests/
kubectl wait --for=condition=Ready pod -l app=dadqn-autoscaler --timeout=60s
kubectl logs -f deployment/dadqn-autoscaler
```

