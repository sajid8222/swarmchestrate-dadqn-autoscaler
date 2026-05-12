#!/usr/bin/env bash
# Preflight for single-node 4-way comparison experiment.
# Run from laptop. Exits non-zero on any failure.
set -euo pipefail

NODE_IP="${NODE_IP:-3.239.119.17}"
PEM="${PEM:-/home/sajid_40020095/Sajid_node_USA_4.pem}"
SSH="ssh -i $PEM -o ConnectTimeout=8 -o StrictHostKeyChecking=no ubuntu@$NODE_IP"
BOUTIQUE_URL="http://$NODE_IP:30193"
SERVER_URL="http://$NODE_IP:31879"

log() { printf '\033[1;36m[preflight] %s\033[0m\n' "$*"; }
err() { printf '\033[1;31m[preflight ERROR] %s\033[0m\n' "$*" >&2; }

# ── 1. ≥1 node Ready ─────────────────────────────────────────────────────
ready=$($SSH 'sudo k3s kubectl get nodes --no-headers' 2>/dev/null \
  | awk '$2=="Ready"{c++}END{print c+0}')
[ "$ready" -lt 1 ] && { err "expected ≥1 node Ready, got $ready"; exit 1; }
log "$ready node(s) Ready ✓"

# ── 2. No pods in bad state ──────────────────────────────────────────────
bad=$($SSH 'sudo k3s kubectl get pods -A --no-headers' 2>/dev/null \
  | awk '$4 ~ /CrashLoop|CreateContainerError|ImagePullBackOff|ErrImagePull/' | wc -l)
[ "$bad" -gt 0 ] && {
  err "$bad pod(s) in bad state:"
  $SSH 'sudo k3s kubectl get pods -A' | grep -E "CrashLoop|CreateContainerError|ImagePullBackOff|ErrImagePull" >&2
  exit 1
}
log "no pods in bad state ✓"

# ── 3. autoscaler-server reachable (from laptop) ─────────────────────────
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$SERVER_URL/healthz" 2>/dev/null || echo 000)
[ "$code" = "000" ] && { err "autoscaler-server unreachable at $SERVER_URL"; exit 1; }
log "autoscaler-server reachable HTTP $code ✓"

# ── 4. Boutique frontend reachable on NodePort 30193 ─────────────────────
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BOUTIQUE_URL/" 2>/dev/null || echo 000)
[ "$code" != "200" ] && { err "boutique frontend HTTP $code at $BOUTIQUE_URL"; exit 1; }
log "boutique frontend HTTP $code ✓"

# ── 5. All 10 waypoints Ready ────────────────────────────────────────────
wp=$($SSH 'sudo k3s kubectl get pods -n default --no-headers' 2>/dev/null \
  | grep waypoint | grep -c " 1/1 ")
[ "$wp" -lt 10 ] && {
  err "expected ≥10 waypoints Ready, got $wp"
  $SSH 'sudo k3s kubectl -n default get pods | grep -i waypoint' >&2
  exit 1
}
log "$wp waypoints Ready ✓"

# ── 6. All 10 services labeled with correct waypoint ─────────────────────
declare -A SVC_WP=(
  [adservice]=ad-waypoint [cartservice]=cart-waypoint
  [checkoutservice]=chk-waypoint [currencyservice]=cur-waypoint
  [emailservice]=email-waypoint [frontend]=fe-waypoint
  [paymentservice]=pay-waypoint [productcatalogservice]=prod-waypoint
  [recommendationservice]=rec-waypoint [shippingservice]=ship-waypoint
)
miss=0
for svc in "${!SVC_WP[@]}"; do
  expected="${SVC_WP[$svc]}"
  got=$($SSH "sudo k3s kubectl get svc $svc -o jsonpath='{.metadata.labels.istio\.io/use-waypoint}'" 2>/dev/null)
  if [ "$got" != "$expected" ]; then
    err "$svc: expected use-waypoint=$expected, got '${got:-MISSING}'"
    miss=$((miss + 1))
  fi
done
[ "$miss" -gt 0 ] && exit 1
log "all 10 services labeled with correct waypoint ✓"

# ── 7. 4 Istio scrape rules present ──────────────────────────────────────
for cr in 'podmonitor istio-waypoints' 'podmonitor istio-ztunnel' \
          'podmonitor istio-gateways' 'servicemonitor istiod'; do
  cnt=$($SSH "sudo k3s kubectl -n monitoring get $cr --no-headers 2>/dev/null" | wc -l)
  [ "$cnt" -lt 1 ] && { err "missing $cr in monitoring/"; exit 1; }
done
log "4 Istio scrape CRs present ✓"

# ── 8. Prometheus reachable + scraping istio_* ───────────────────────────
istio_metric_count=$($SSH 'curl -sG --data-urlencode "query=count({__name__=~\"istio.*\"})" http://127.0.0.1:9090/api/v1/query' 2>/dev/null \
  | grep -oE '"[0-9]+"' | tail -1 | tr -d '"')
[ -z "$istio_metric_count" ] && { err "Prometheus port-forward dead — re-run pf"; exit 1; }
[ "$istio_metric_count" -lt 20 ] && { err "only $istio_metric_count istio_* metrics — scraping incomplete"; exit 1; }
log "Prometheus scraping $istio_metric_count istio_* metrics ✓"

# ── 9. autoscaler-server can reach Prometheus in-cluster ─────────────────
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$SERVER_URL/monitor/status" 2>/dev/null || echo 000)
[ "$code" = "000" ] && { err "autoscaler-server /monitor/status failed"; exit 1; }
log "autoscaler-server /monitor/status HTTP $code ✓"

# ── 10. DA-DQN docker image accessible from cluster ──────────────────────
img=$($SSH 'sudo k3s ctr images ls 2>/dev/null | grep dadqn-autoscaler' | head -1 || true)
if [ -z "$img" ]; then
  log "DA-DQN image not pre-pulled — pulling now (~1.3 GB)"
  $SSH 'sudo k3s ctr images pull docker.io/proactivellmbasedproject/dadqn-autoscaler:v1' 2>&1 | tail -2
fi
log "DA-DQN image available ✓"

echo
log "============================================="
log "PREFLIGHT OK — all 10 checks passed"
log "============================================="
