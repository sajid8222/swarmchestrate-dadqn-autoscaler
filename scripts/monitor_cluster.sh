#!/usr/bin/env bash
# Live cluster monitor — run in your own terminal:
#   bash /home/sajid_40020095/monitor_cluster.sh
#
# Shows:
#   1. Pod counts per service (replicas)
#   2. Pod distribution across workers (which worker hosts which pods)
#   3. Gateway-level RPS / users / p95 (via shim, what agents see)
#   4. Per-service gRPC + HTTP latency p95 and RPM (from Prometheus)
#   5. Last 5 dadqn-frontend decisions

PEM=/home/sajid_40020095/Sajid_node_USA_4.pem
CP=98.81.58.218

SERVICES=(frontend recommendationservice currencyservice productcatalogservice cartservice adservice checkoutservice emailservice paymentservice shippingservice)

while :; do
  echo ""
  echo ""
  echo "================================================================================"
  echo " DA-DQN cluster monitor — $(date +%H:%M:%S)"
  echo "================================================================================"

  echo "--- pod counts (per service) ---"
  ssh -i $PEM -o BatchMode=yes -o ConnectTimeout=5 ubuntu@$CP "
    sudo kubectl -n default get deploy ${SERVICES[*]} \
      -o custom-columns=NAME:.metadata.name,REPL:.status.replicas --no-headers 2>/dev/null
  "

  echo
  echo "--- pod distribution (which worker hosts each service's pods) ---"
  ssh -i $PEM -o BatchMode=yes -o ConnectTimeout=5 ubuntu@$CP 'sudo kubectl -n default get pods -o json 2>/dev/null' | python3 -c "
import sys, json
from collections import defaultdict
try:
    data = json.load(sys.stdin)
except Exception as e:
    print(f'  (no data: {e})')
    sys.exit()
# map service -> {worker: count}
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
print(f\"  {'SERVICE':<24}\" + ''.join(f'{n:<11}' for n in nodes) + 'TOTAL')
for svc in sorted(dist):
    row = ''
    total = 0
    for n in nodes:
        c = dist[svc][n]
        row += f'{c:<11}' if c else f'{\"·\":<11}'
        total += c
    print(f'  {svc:<24}{row}{total}')
"

  echo
  echo "--- gateway aggregate (via shim — what the agent sees) ---"
  ssh -i $PEM -o BatchMode=yes -o ConnectTimeout=4 ubuntu@$CP \
    'curl -s http://localhost:8089/stats/requests --max-time 4 2>/dev/null' \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    s = d['stats'][0]
    print(f\"  users={d.get('user_count',0):>4}  state={d.get('state','?'):<8}  rps={s.get('current_rps',0):>6}  avg_ms={s.get('avg_response_time',0):>6}  p95_ms={s.get('current_response_time_percentile_95',0):>6}\")
except Exception as e:
    print(f'  (shim error: {e})')
"

  echo
  echo "--- per-service latency (p95 ms) + traffic (rpm) — from Prometheus ---"
  printf "  %-22s %10s %10s %10s %10s\n" "service" "grpc_p95" "http_p95" "grpc_rpm" "http_rpm"
  for svc in "${SERVICES[@]}"; do
    metrics=$(ssh -i $PEM -o BatchMode=yes -o ConnectTimeout=5 ubuntu@$CP "
      g95=\$(curl -s -G 'http://localhost:9090/api/v1/query' --data-urlencode \"query=histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{destination_service_name=\\\"$svc\\\",request_protocol=\\\"grpc\\\"}[1m])) by (le))\" --max-time 3 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get(\"data\",{}).get(\"result\",[]); v=r[0][\"value\"][1] if r else \"0\"; print(\"0\" if v in (\"NaN\",None) else f\"{float(v):.0f}\")' 2>/dev/null || echo 0)
      h95=\$(curl -s -G 'http://localhost:9090/api/v1/query' --data-urlencode \"query=histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{destination_service_name=\\\"$svc\\\",request_protocol=\\\"http\\\"}[1m])) by (le))\" --max-time 3 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get(\"data\",{}).get(\"result\",[]); v=r[0][\"value\"][1] if r else \"0\"; print(\"0\" if v in (\"NaN\",None) else f\"{float(v):.0f}\")' 2>/dev/null || echo 0)
      grpm=\$(curl -s -G 'http://localhost:9090/api/v1/query' --data-urlencode \"query=sum(rate(istio_requests_total{destination_service_name=\\\"$svc\\\",request_protocol=\\\"grpc\\\"}[1m]))*60\" --max-time 3 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get(\"data\",{}).get(\"result\",[]); v=r[0][\"value\"][1] if r else \"0\"; print(f\"{float(v):.0f}\" if v not in (\"NaN\",None) else \"0\")' 2>/dev/null || echo 0)
      hrpm=\$(curl -s -G 'http://localhost:9090/api/v1/query' --data-urlencode \"query=sum(rate(istio_requests_total{destination_service_name=\\\"$svc\\\",request_protocol=\\\"http\\\"}[1m]))*60\" --max-time 3 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get(\"data\",{}).get(\"result\",[]); v=r[0][\"value\"][1] if r else \"0\"; print(f\"{float(v):.0f}\" if v not in (\"NaN\",None) else \"0\")' 2>/dev/null || echo 0)
      echo \"\$g95 \$h95 \$grpm \$hrpm\"
    " 2>/dev/null)
    read g95 h95 grpm hrpm <<< "$metrics"
    printf "  %-22s %10s %10s %10s %10s\n" "$svc" "${g95:-0}" "${h95:-0}" "${grpm:-0}" "${hrpm:-0}"
  done

  echo
  echo "--- last 5 dadqn-frontend decisions ---"
  ssh -i $PEM -o BatchMode=yes -o ConnectTimeout=4 ubuntu@$CP '
    sudo kubectl -n default logs deploy/dadqn-frontend --tail=10 2>/dev/null \
      | grep -E "decision|idle auto-scaledown" | tail -5
  '

  sleep 10
done
