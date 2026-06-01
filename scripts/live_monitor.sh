#!/usr/bin/env bash
# Live mesh traffic + waypoint CPU + node monitor. Refreshes every 5 s.
# Open in another terminal:  bash experiments/live_monitor.sh
#
# Set SLEEP=2 for faster refresh.
#
# Columns:
#   HTTP_RPM / HTTP_mean / HTTP_p95  — mesh-visible HTTP traffic to this service.
#                                      Note: frontend is exposed via NodePort, so
#                                      external HTTP from Locust bypasses the
#                                      mesh and HTTP_* will show "-" for it.
#   gRPC_RPM / gRPC_mean / gRPC_p95  — internal (pod-to-pod) gRPC traffic.
#                                      This is what shows backend services are busy.
#   CPU_m / MEM_Mi / PODS            — pod usage + replica count.
# SLA reference: 500 ms (compare against HTTP_p95 / gRPC_p95).
set -u
SLEEP="${SLEEP:-5}"
NODE_IP="${NODE_IP:-34.204.199.255}"
PEM="${PEM:-/home/sajid_40020095/Sajid_node_USA_4.pem}"
SSH="ssh -i $PEM -o ConnectTimeout=6 -o StrictHostKeyChecking=no -o ServerAliveInterval=20 ubuntu@$NODE_IP"
SERVICES="frontend currencyservice productcatalogservice cartservice adservice checkoutservice emailservice paymentservice shippingservice recommendationservice"

while true; do
  echo
  echo "============================================================================================================================"
  echo "=== $(date '+%T')   Single-node cluster ($NODE_IP) — mesh + waypoint + node ==="
  echo "============================================================================================================================"
  $SSH "
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
    PROM=http://127.0.0.1:9090/api/v1/query
    NS=default
    TOP=\$(sudo k3s kubectl top pod -n \$NS --no-headers 2>/dev/null)
    PODS=\$(sudo k3s kubectl get pod -n \$NS --no-headers 2>/dev/null)

    q() { curl -sG --data-urlencode \"query=\$1\" \$PROM 2>/dev/null | jq -r '.data.result[0].value[1] // \"NaN\"'; }
    norm() { v=\$1; if [ \"\$v\" = NaN ] || [ -z \"\$v\" ]; then echo '-'; else printf '%.1f' \"\$v\"; fi; }

    # ── Node CPU + MEM (overall) ───────────────────────────────────────
    NCPU=\$(q '100 - (avg(irate(node_cpu_seconds_total{mode=\"idle\"}[1m])) * 100)')
    NMEM=\$(q '(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100')
    NLOAD=\$(q 'node_load1')
    TOTAL_PODS=\$(echo \"\$PODS\" | grep -c Running)
    # ── Gateway (client-side end-to-end, what Locust really sees) ────
    GWP95=\$(q 'histogram_quantile(0.95, sum by(le)(rate(istio_request_duration_milliseconds_bucket{source_workload=~\".*-gateway-istio\",destination_workload=\"frontend\"}[1m])))')
    GWRPS=\$(q 'sum(rate(istio_requests_total{source_workload=~\".*-gateway-istio\",destination_workload=\"frontend\"}[1m]))')
    DA_DEP=\$(sudo k3s kubectl -n default get deploy dadqn-autoscaler --no-headers 2>/dev/null | wc -l)
    HPA_CNT=\$(sudo k3s kubectl -n default get hpa --no-headers 2>/dev/null | wc -l)
    SCALER=\$( [ \$DA_DEP -gt 0 ] && echo DA-DQN || ( [ \$HPA_CNT -gt 0 ] && echo \"KHPA(\${HPA_CNT}hpa)\" || echo NONE ) )
    printf 'NODE   CPU=%s%%   MEM=%s%%   load1=%s   pods total=%s   scaler=%s\n' \"\$(norm \"\$NCPU\")\" \"\$(norm \"\$NMEM\")\" \"\$(norm \"\$NLOAD\")\" \"\$TOTAL_PODS\" \"\$SCALER\"
    printf 'CLIENT gw_p95=%sms   gw_RPS=%s   SLA=500ms   (this is what Locust/users see)\n' \"\$(norm \"\$GWP95\")\" \"\$(norm \"\$GWRPS\")\"
    echo

    printf '%-22s | %9s %9s %9s | %9s %9s %9s | %6s %7s %5s\n' \
      SERVICE HTTP_RPM HTTP_mean HTTP_p95 gRPC_RPM gRPC_mean gRPC_p95 CPU_m MEM_Mi PODS
    printf '%s\n' '----------------------------------------------------------------------------------------------------------------------------'
    for svc in $SERVICES; do
      hr=\$(q \"sum(rate(istio_requests_total{request_protocol=\\\"http\\\",destination_workload=\\\"\$svc\\\"}[2m]))*60\")
      hl=\$(q \"sum(rate(istio_request_duration_milliseconds_sum{request_protocol=\\\"http\\\",destination_workload=\\\"\$svc\\\"}[2m]))/sum(rate(istio_request_duration_milliseconds_count{request_protocol=\\\"http\\\",destination_workload=\\\"\$svc\\\"}[2m]))\")
      hp=\$(q \"histogram_quantile(0.95, sum by(le) (rate(istio_request_duration_milliseconds_bucket{request_protocol=\\\"http\\\",destination_workload=\\\"\$svc\\\"}[2m])))\")
      gr=\$(q \"sum(rate(istio_requests_total{request_protocol=\\\"grpc\\\",destination_workload=\\\"\$svc\\\"}[2m]))*60\")
      gl=\$(q \"sum(rate(istio_request_duration_milliseconds_sum{request_protocol=\\\"grpc\\\",destination_workload=\\\"\$svc\\\"}[2m]))/sum(rate(istio_request_duration_milliseconds_count{request_protocol=\\\"grpc\\\",destination_workload=\\\"\$svc\\\"}[2m]))\")
      gp=\$(q \"histogram_quantile(0.95, sum by(le) (rate(istio_request_duration_milliseconds_bucket{request_protocol=\\\"grpc\\\",destination_workload=\\\"\$svc\\\"}[2m])))\")
      cpu=\$(echo \"\$TOP\" | awk -v s=\$svc '\$1 ~ \"^\"s\"-\" {gsub(/m\$/,\"\",\$2); sum+=\$2} END {print sum+0}')
      mem=\$(echo \"\$TOP\" | awk -v s=\$svc '\$1 ~ \"^\"s\"-\" {gsub(/Mi\$/,\"\",\$3); sum+=\$3} END {print sum+0}')
      pods=\$(echo \"\$PODS\" | awk -v s=\$svc '\$1 ~ \"^\"s\"-\" && /Running/' | wc -l)
      printf '%-22s | %9s %9s %9s | %9s %9s %9s | %6s %7s %5s\n' \
        \"\$svc\" \"\$(norm \"\$hr\")\" \"\$(norm \"\$hl\")\" \"\$(norm \"\$hp\")\" \"\$(norm \"\$gr\")\" \"\$(norm \"\$gl\")\" \"\$(norm \"\$gp\")\" \"\$cpu\" \"\$mem\" \"\$pods\"
    done
    echo
    echo 'HTTP_mean/gRPC_mean = arithmetic mean. HTTP_p95/gRPC_p95 = 95th percentile (SLA = 500 ms).'
    echo 'Note: external HTTP from Locust hits frontend via NodePort (not via mesh), so frontend HTTP_* stays \"-\".'
    echo 'End-user p95 (client side): open http://localhost:8089 → Latency tab while Locust is running.'
    echo
    echo '=== Waypoint CPU (out of 2000m limit each) ==='
    sudo k3s kubectl top pod -n \$NS --no-headers 2>/dev/null | grep waypoint | awk '{ printf \"  %-25s %6s %8s\n\", \$1, \$2, \$3 }'
  " 2>/dev/null
  # ── Locust UI status (this laptop) ─────────────────────────────────
  LSTATUS=$(curl -s --max-time 2 "http://localhost:8089/stats/requests" 2>/dev/null \
    | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    print(f\"  users={d.get('user_count','?')}  state={d.get('state','?')}\")
except: print('  no response (segment not running)')" 2>/dev/null)
  [ -z "$LSTATUS" ] && LSTATUS="  port 8089 not listening (between segments OR sweep stopped)"
  echo
  echo "Locust UI: http://localhost:8089  →$LSTATUS"
  sleep "$SLEEP"
done
