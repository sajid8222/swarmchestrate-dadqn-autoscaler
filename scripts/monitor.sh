#!/usr/bin/env bash
# Cluster-2 live monitor — appends to scrollback instead of clearing.
# Each refresh prints a banner with timestamp, so you can scroll back through history.
set -u
PROM=http://127.0.0.1:9090/api/v1/query
SLEEP="${SLEEP:-10}"

q() {
  curl -sG --data-urlencode "query=$1" "$PROM" 2>/dev/null \
    | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)["data"]["result"]
  print(float(d[0]["value"][1]) if d else 0)
except Exception:
  print(0)'
}

while true; do
  echo
  echo "================================================================================"
  printf "║ %s   (refresh every %ss — Ctrl+C to stop)\n" "$(date)" "$SLEEP"
  echo "================================================================================"
  echo
  echo "── Gateway (what users see) ──"
  P95=$(q 'histogram_quantile(0.95, sum by(le)(rate(istio_request_duration_milliseconds_bucket{source_workload=~".*gateway.*"}[1m])))')
  RPS=$(q 'sum(rate(istio_requests_total{source_workload=~".*gateway.*"}[1m]))')
  P99=$(q 'histogram_quantile(0.99, sum by(le)(rate(istio_request_duration_milliseconds_bucket{source_workload=~".*gateway.*"}[1m])))')
  printf "  RPS=%-6.1f   p95=%-7.1fms   p99=%-7.1fms   SLA=500ms\n" "$RPS" "$P95" "$P99"
  echo
  echo "── Frontend agent (last 5 lines) ──"
  kubectl -n default logs deploy/dadqn-frontend --tail=5 2>/dev/null | grep -E 't=|decision' | sed 's/^/  /'
  echo
  echo "── Boutique replicas (WANT/READY) ──"
  kubectl -n default get deploy -o custom-columns='NAME:.metadata.name,WANT:.spec.replicas,READY:.status.readyReplicas' \
    | grep -vE 'NAME|dadqn-|waypoint|loadgenerator|gateway' | sort | awk '{ printf "  %-22s %3s / %s\n", $1, $2, $3 }'
  echo
  echo "── Per-worker placement ──"
  for node in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
    is_cp=$(kubectl get node "$node" -o jsonpath='{.spec.taints[?(@.key=="CriticalAddonsOnly")].effect}')
    [ -n "$is_cp" ] && tag=" (CP)" || tag=""
    dadqn=$(kubectl -n default get pods -l app=dadqn-autoscaler -o jsonpath="{range .items[?(@.spec.nodeName=='$node')]}{.metadata.labels.svc}{','}{end}" | sed 's/,$//')
    bout=$(kubectl -n default get pods --field-selector="spec.nodeName=$node" -o jsonpath='{range .items[*]}{.metadata.labels.app}{","}{end}' \
      | tr ',' '\n' | grep -E '^(adservice|cartservice|checkoutservice|currencyservice|emailservice|frontend|paymentservice|productcatalogservice|recommendationservice|shippingservice|redis-cart)$' \
      | tr '\n' ',' | sed 's/,$//')
    printf "  %-22s dadqn=[%s]  boutique=[%s]\n" "$node$tag" "${dadqn:--}" "${bout:--}"
  done
  sleep "$SLEEP"
done
