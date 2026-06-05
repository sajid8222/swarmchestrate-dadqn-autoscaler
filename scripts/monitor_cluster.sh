#!/usr/bin/env bash
# Live cluster monitor.
#
# Run modes (auto-detected):
#   1) On the CP node:   bash scripts/monitor_cluster.sh
#   2) From laptop:      CP=<ip> PEM=~/.ssh/<key>.pem bash scripts/monitor_cluster.sh
#
# Shows every 10 s:
#   1. Pod counts per service
#   2. Pod distribution across workers
#   3. Gateway aggregate (via locust-shim — what agents see)
#   4. Per-service gRPC + HTTP latency p95 and RPM (from Prometheus)
#   5. Last 5 dadqn-frontend decisions

set -u
NS=${NS:-default}
SERVICES=(frontend recommendationservice currencyservice productcatalogservice cartservice adservice checkoutservice emailservice paymentservice shippingservice)

# --- mode detection -----------------------------------------------------
# If $CP is set we treat it as laptop mode and wrap every kubectl/curl in ssh.
# Otherwise we assume we're ON the CP and run kubectl + ClusterIP curls directly.
if [[ -n "${CP:-}" ]]; then
  PEM=${PEM:-$HOME/.ssh/cluster3.pem}
  if [[ ! -r "$PEM" ]]; then
    echo "ERROR: PEM not readable at $PEM — set PEM=<path> and retry." >&2
    exit 1
  fi
  KC() { ssh -i "$PEM" -o BatchMode=yes -o ConnectTimeout=5 ubuntu@$CP "sudo kubectl -n $NS $*"; }
  KCJ() { ssh -i "$PEM" -o BatchMode=yes -o ConnectTimeout=5 ubuntu@$CP "sudo kubectl -n $NS $*"; }
  REMOTE_CURL() { ssh -i "$PEM" -o BatchMode=yes -o ConnectTimeout=5 ubuntu@$CP "$*"; }
  echo "(laptop mode: SSH to $CP via $PEM)"
else
  KC() { sudo kubectl -n "$NS" "$@"; }
  KCJ() { sudo kubectl -n "$NS" "$@"; }
  REMOTE_CURL() { eval "$*"; }
  echo "(CP mode: running kubectl locally on $(hostname))"
fi

# --- one-time service IP lookup (ClusterIPs are routable from any k3s node) -
SHIM_IP=$(KC get svc locust-shim -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
PROM_IP=$(sudo kubectl -n monitoring get svc prom-kube-prometheus-stack-prometheus -o jsonpath='{.spec.clusterIP}' 2>/dev/null \
          || (ssh -i "${PEM:-}" -o BatchMode=yes ubuntu@${CP:-localhost} \
              "sudo kubectl -n monitoring get svc prom-kube-prometheus-stack-prometheus -o jsonpath='{.spec.clusterIP}'" 2>/dev/null))

if [[ -z "$SHIM_IP" ]]; then
  echo "WARN: locust-shim service not found in ns/$NS — gateway aggregate will be empty."
  echo "       run 'kubectl apply -f manifests/' to deploy it."
fi
if [[ -z "$PROM_IP" ]]; then
  echo "WARN: prometheus svc not found in ns/monitoring — per-service metrics will be empty."
fi
echo "  shim ClusterIP = ${SHIM_IP:-<missing>}:8089"
echo "  prom ClusterIP = ${PROM_IP:-<missing>}:9090"
sleep 2

# helper: extract a scalar from a Prom JSON response (returns "0" on miss/NaN)
prom_scalar() {
  python3 -c "import sys,json
try:
    d=json.load(sys.stdin); r=d.get('data',{}).get('result',[]);
    v = r[0]['value'][1] if r else '0'
    print('0' if v in ('NaN', '+Inf', '-Inf', None) else f'{float(v):.0f}')
except Exception:
    print('0')"
}

while :; do
  echo ""
  echo "================================================================================"
  echo " DA-DQN cluster monitor — $(date +%H:%M:%S)"
  echo "================================================================================"

  echo "--- pod counts (per service) ---"
  KC get deploy "${SERVICES[@]}" \
      -o custom-columns=NAME:.metadata.name,REPL:.status.replicas --no-headers 2>/dev/null

  echo
  echo "--- pod distribution (which worker hosts each service's pods) ---"
  KCJ get pods -o json 2>/dev/null | python3 -c "
import sys, json
from collections import defaultdict
try:
    data = json.load(sys.stdin)
except Exception as e:
    print(f'  (no data: {e})'); sys.exit()
dist = defaultdict(lambda: defaultdict(int))
TRACKED = {'frontend','recommendationservice','currencyservice','productcatalogservice','cartservice','adservice','checkoutservice','emailservice','paymentservice','shippingservice'}
for p in data.get('items', []):
    if p.get('status',{}).get('phase') != 'Running': continue
    name = p['metadata']['name']
    svc = name.rsplit('-',2)[0]
    if svc not in TRACKED: continue
    node = p['spec'].get('nodeName','?')
    short = node.replace('ip-172-31-','w-')
    dist[svc][short] += 1
nodes = sorted({n for s in dist.values() for n in s})
if not nodes:
    print('  (no service pods running)'); sys.exit()
print(f\"  {'SERVICE':<24}\" + ''.join(f'{n:<11}' for n in nodes) + 'TOTAL')
for svc in sorted(dist):
    row, total = '', 0
    for n in nodes:
        c = dist[svc][n]
        row += f'{c:<11}' if c else f'{\"·\":<11}'
        total += c
    print(f'  {svc:<24}{row}{total}')
"

  echo
  echo "--- gateway aggregate (via shim — what the agent sees) ---"
  if [[ -n "$SHIM_IP" ]]; then
    REMOTE_CURL "curl -s --max-time 4 http://$SHIM_IP:8089/stats/requests" 2>/dev/null \
      | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin); s = d['stats'][0]
    print(f\"  users={d.get('user_count',0):>4}  state={d.get('state','?'):<8}  rps={s.get('current_rps',0):>6}  avg_ms={s.get('avg_response_time',0):>6}  p95_ms={s.get('current_response_time_percentile_95',0):>6}\")
except Exception as e:
    print(f'  (shim error: {e})')
"
  else
    echo "  (shim ClusterIP unknown — skipping)"
  fi

  echo
  echo "--- per-service latency (p95 ms) + traffic (rpm) — from Prometheus ---"
  printf "  %-22s %10s %10s %10s %10s\n" "service" "grpc_p95" "http_p95" "grpc_rpm" "http_rpm"
  if [[ -n "$PROM_IP" ]]; then
    for svc in "${SERVICES[@]}"; do
      Q1="histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{destination_service_name=\"$svc\",request_protocol=\"grpc\"}[1m])) by (le))"
      Q2="histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{destination_service_name=\"$svc\",request_protocol=\"http\"}[1m])) by (le))"
      Q3="sum(rate(istio_requests_total{destination_service_name=\"$svc\",request_protocol=\"grpc\"}[1m]))*60"
      Q4="sum(rate(istio_requests_total{destination_service_name=\"$svc\",request_protocol=\"http\"}[1m]))*60"
      g95=$(REMOTE_CURL "curl -s -G 'http://$PROM_IP:9090/api/v1/query' --data-urlencode 'query=$Q1' --max-time 3" 2>/dev/null | prom_scalar)
      h95=$(REMOTE_CURL "curl -s -G 'http://$PROM_IP:9090/api/v1/query' --data-urlencode 'query=$Q2' --max-time 3" 2>/dev/null | prom_scalar)
      grpm=$(REMOTE_CURL "curl -s -G 'http://$PROM_IP:9090/api/v1/query' --data-urlencode 'query=$Q3' --max-time 3" 2>/dev/null | prom_scalar)
      hrpm=$(REMOTE_CURL "curl -s -G 'http://$PROM_IP:9090/api/v1/query' --data-urlencode 'query=$Q4' --max-time 3" 2>/dev/null | prom_scalar)
      printf "  %-22s %10s %10s %10s %10s\n" "$svc" "${g95:-0}" "${h95:-0}" "${grpm:-0}" "${hrpm:-0}"
    done
  else
    echo "  (prom ClusterIP unknown — skipping)"
  fi

  echo
  echo "--- last 5 dadqn-frontend decisions ---"
  KC logs deploy/dadqn-frontend --tail=20 2>/dev/null \
    | grep -E "decision|idle auto-scaledown" | tail -5

  sleep 10
done
