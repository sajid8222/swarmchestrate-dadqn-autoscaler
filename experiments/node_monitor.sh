#!/usr/bin/env bash
# Continuous node-level CPU+MEM% monitor.
# Polls Prometheus (via the port-forward on $NODE_IP) every $INTERVAL seconds,
# appends one row per sample to results/node_metrics/node_util.csv.
#
# Started alongside run_4way.sh. Sliced by run boundaries (master.log timestamps)
# at plot time.

set -euo pipefail
NODE_IP="${NODE_IP:-3.239.119.17}"
PEM="${PEM:-/home/sajid_40020095/Sajid_node_USA_4.pem}"
INTERVAL="${INTERVAL:-5}"
OUT="${OUT:-/home/sajid_40020095/swarmchestrate-dadqn-autoscaler/experiments/results/node_metrics/node_util.csv}"
SSH="ssh -i $PEM -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o ServerAliveInterval=15 ubuntu@$NODE_IP"

mkdir -p "$(dirname "$OUT")"
if [ ! -f "$OUT" ]; then
  echo "ts_unix,ts_iso,cpu_pct,mem_pct,load1,mem_used_gib,mem_total_gib" > "$OUT"
fi

while true; do
  res=$($SSH 'set -e
    CPU=$(curl -sG --data-urlencode "query=100 - (avg(irate(node_cpu_seconds_total{mode=\"idle\"}[1m])) * 100)" \
           http://127.0.0.1:9090/api/v1/query | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[\"data\"][\"result\"][0][\"value\"][1] if d[\"data\"][\"result\"] else \"nan\")")
    MEM=$(curl -sG --data-urlencode "query=(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100" \
           http://127.0.0.1:9090/api/v1/query | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[\"data\"][\"result\"][0][\"value\"][1] if d[\"data\"][\"result\"] else \"nan\")")
    LOAD=$(curl -sG --data-urlencode "query=node_load1" \
           http://127.0.0.1:9090/api/v1/query | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[\"data\"][\"result\"][0][\"value\"][1] if d[\"data\"][\"result\"] else \"nan\")")
    MU=$(curl -sG --data-urlencode "query=(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / 1073741824" \
           http://127.0.0.1:9090/api/v1/query | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[\"data\"][\"result\"][0][\"value\"][1] if d[\"data\"][\"result\"] else \"nan\")")
    MT=$(curl -sG --data-urlencode "query=node_memory_MemTotal_bytes / 1073741824" \
           http://127.0.0.1:9090/api/v1/query | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[\"data\"][\"result\"][0][\"value\"][1] if d[\"data\"][\"result\"] else \"nan\")")
    echo "${CPU},${MEM},${LOAD},${MU},${MT}"' 2>/dev/null) || res="nan,nan,nan,nan,nan"

  ts_unix=$(date +%s)
  ts_iso=$(date -Iseconds)
  echo "${ts_unix},${ts_iso},${res}" >> "$OUT"
  sleep "$INTERVAL"
done
