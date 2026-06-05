#!/usr/bin/env bash
# Pre-test preflight — run on the CP node before any load test.
#
# What this fixes:
#   1. Stale Envoy state in long-running Istio waypoint pods.
#      Symptom: gateway p95 stuck at 8–10s, frontend logs "context canceled"
#      from gRPC calls to backends, even though backend pods have idle CPU.
#      Cause: connection state accumulates inside the waypoint pod over hours.
#      Fix: delete-and-recreate every waypoint pod.
#
#   2. Prometheus losing the Istio scrape after a cluster restart.
#      Symptom: shim returns rps=0 even while Locust drives real traffic;
#      agents see idle and force MIN_REPLICAS via the built-in override.
#      Fix: restart Prometheus + verify istio_requests_total has data.
#
#   3. ConfigMap LOCUST_URL must point at the in-cluster shim Service.
#      Symptom: agents read from empty URL → rps=0 → idle override fires.
#      Fix: ensure the value matches the locust-shim Service DNS.
#
# Run from anywhere on the CP:
#     bash scripts/preflight.sh
# Use --skip-prom to skip the Prom restart if it's already healthy.
set -uo pipefail
NS=${NS:-default}

cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*"; }

cyan "==[1/4]== refreshing all Istio waypoints (clears stale Envoy state)"
WAYPOINTS=$(kubectl -n "$NS" get pod -l gateway.istio.io/managed=istio.io-mesh-controller -o name 2>/dev/null)
if [ -z "$WAYPOINTS" ]; then
  red   "  no waypoint pods found — has Istio been installed in $NS?"
else
  echo "$WAYPOINTS" | xargs -r kubectl -n "$NS" delete --force --grace-period=0 2>&1 | tail -3
  green "  refreshed $(echo "$WAYPOINTS" | wc -l) waypoint pods"
fi
echo

cyan "==[2/4]== restarting Prometheus (recovers Istio scrape after VM restart)"
if [[ "${1:-}" != "--skip-prom" ]]; then
  PROM_POD=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o name 2>/dev/null | head -1)
  if [ -n "$PROM_POD" ]; then
    kubectl -n monitoring delete "$PROM_POD" --force --grace-period=0 2>&1 | tail -1
    green "  Prometheus restarting — istio scrape will re-establish in ~30s"
  else
    red   "  Prometheus pod not found in monitoring namespace — skipping"
  fi
else
  echo "  skipped (--skip-prom)"
fi
echo

cyan "==[3/4]== verifying ConfigMap LOCUST_URL points at locust-shim Service"
LOCUST_URL=$(kubectl -n "$NS" get configmap dadqn-config -o jsonpath='{.data.LOCUST_URL}' 2>/dev/null)
EXPECTED="http://locust-shim.$NS.svc.cluster.local:8089"
if [ "$LOCUST_URL" = "$EXPECTED" ]; then
  green "  LOCUST_URL ok: $LOCUST_URL"
else
  red   "  LOCUST_URL=$LOCUST_URL"
  red   "  expected: $EXPECTED"
  red   "  re-apply manifests with: kubectl apply -f manifests/"
fi
echo

cyan "==[4/4]== verifying locust-shim Deployment is Ready and reachable"
READY=$(kubectl -n "$NS" get deploy locust-shim -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
if [ "$READY" = "1" ]; then
  green "  locust-shim deployment Ready (1/1)"
  # in-cluster reachability check from one of the dadqn pods
  AGENT=$(kubectl -n "$NS" get pod -l app=dadqn-autoscaler,svc=frontend -o name 2>/dev/null | head -1)
  if [ -n "$AGENT" ]; then
    OUT=$(kubectl -n "$NS" exec "$AGENT" -- curl -s --max-time 4 \
            "http://locust-shim.$NS.svc.cluster.local:8089/stats/requests" 2>/dev/null | head -c 200)
    if [ -n "$OUT" ]; then
      green "  shim reachable from agent: $OUT"
    else
      red   "  shim NOT reachable from agent — check Service + Deployment"
    fi
  fi
else
  red   "  locust-shim not Ready — apply 05-locust-shim.yaml or check pod logs"
fi
echo
green "Preflight done. Wait ~30s for Prometheus to start scraping Istio, then start Locust."
