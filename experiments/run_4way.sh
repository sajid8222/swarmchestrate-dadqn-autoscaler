#!/usr/bin/env bash
# 4-way autoscaler comparison: KHPA / DAS / CustomDAS / DA-DQN @ 5 RPS levels.
# Runs locust on the laptop (this machine) hitting the cluster at $NODE_IP:30193.
# Drives KHPA/DAS/CustomDAS via the autoscaler-server REST API on $NODE_IP:31879.
# Deploys our dockerized DA-DQN (proactivellmbasedproject/dadqn-autoscaler:v1)
# via kubectl apply for its run.
#
# All output CSVs + plots end up under experiments/results/.

set -euo pipefail

# ─── config ──────────────────────────────────────────────────────────────
NODE_IP="${NODE_IP:-3.239.119.17}"
PEM="${PEM:-/home/sajid_40020095/Sajid_node_USA_4.pem}"
DURATION_S="${DURATION_S:-600}"      # 10 min per run (matches reference)
SLEEP_BETWEEN_S="${SLEEP_BETWEEN_S:-45}"
RESET_SETTLE_S="${RESET_SETTLE_S:-15}"

EXP_DIR=/home/sajid_40020095/swarmchestrate-dadqn-autoscaler/experiments
PROJECT_DIR=/home/sajid_40020095/swarmchestrate-dadqn-autoscaler
RESULTS=$EXP_DIR/results
LOCUST_DIR=$RESULTS/locust
MESH_DIR=$RESULTS/mesh_metrics
AGENT_DIR=$RESULTS/agent_logs
LOG_DIR=$RESULTS/run_logs
mkdir -p "$LOCUST_DIR" "$MESH_DIR" "$AGENT_DIR" "$LOG_DIR"

VENV_BIN=/home/sajid_40020095/ze_autoscalling/Swarmchestrate-Decentralised-Auto-Scaling/dadqn-release/.venv/bin
SSH="ssh -i $PEM -o ConnectTimeout=10 -o StrictHostKeyChecking=no ubuntu@$NODE_IP"
SERVER_URL="http://$NODE_IP:31879"
BOUTIQUE_URL="http://$NODE_IP:30193"
PROM_URL_INCLUSTER="http://prom-kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090/api/v1/query"

read -ra WORKLOADS <<< "${WORKLOADS_OVERRIDE:-rps-0 rps-100 rps-200 rps-400 rps-600}"
read -ra AUTOSCALERS <<< "${AUTOSCALERS_OVERRIDE:-khpa das customdas dadqn}"
BOUTIQUE_SVCS=(adservice cartservice checkoutservice currencyservice emailservice frontend paymentservice productcatalogservice recommendationservice shippingservice)

log() { printf '\n\033[1;36m>>> %s\033[0m\n' "$*" | tee -a "$LOG_DIR/master.log"; }
err() { printf '\n\033[1;31m!!! %s\033[0m\n' "$*" | tee -a "$LOG_DIR/master.log" >&2; }

# ─── helpers ─────────────────────────────────────────────────────────────
reset_cluster() {
  log "reset: scale boutique to 1, delete leftover autoscalers"
  $SSH 'set +e; export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  for s in adservice cartservice checkoutservice currencyservice emailservice \
           frontend paymentservice productcatalogservice recommendationservice shippingservice; do
    sudo k3s kubectl -n default scale deploy "$s" --replicas=1 >/dev/null 2>&1
  done
  for svc in adservice cartservice checkoutservice currencyservice emailservice \
             frontend paymentservice productcatalogservice recommendationservice \
             redis-cart shippingservice; do
    sudo k3s kubectl -n default delete svc "customdas-autoscaler-$svc" >/dev/null 2>&1
  done
  sudo k3s kubectl -n default delete deploy dadqn-autoscaler >/dev/null 2>&1
  sudo k3s kubectl -n default delete cm dadqn-config >/dev/null 2>&1
  sudo k3s kubectl -n default delete role dadqn-autoscaler-role >/dev/null 2>&1
  sudo k3s kubectl -n default delete rolebinding dadqn-autoscaler-rb >/dev/null 2>&1
  sudo k3s kubectl -n default delete sa dadqn-autoscaler >/dev/null 2>&1
  exit 0'
  sleep "$RESET_SETTLE_S"
}

start_locust() {
  local workload="$1" tag="$2" ts="$3"
  local prefix="onlineboutique_${workload}_${tag}_all_${ts}"
  log "locust start: $workload via $tag → $prefix (${DURATION_S}s)"
  cd "$EXP_DIR" && \
    WORKLOAD_CSV="workloads/${workload}.csv" \
    DURATION_S="$DURATION_S" SPAWN_RATE=20 SCALE_FACTOR=1 \
    PATH="$VENV_BIN:$PATH" nohup locust \
      -f wiki_locustfile.py \
      --host "$BOUTIQUE_URL" --run-time "${DURATION_S}s" \
      --autostart --autoquit 5 \
      --web-host 0.0.0.0 --web-port 8089 \
      --csv "$LOCUST_DIR/${prefix}" --only-summary \
      > "$LOG_DIR/locust_${workload}_${tag}.log" 2>&1 &
  LOCUST_PID=$!
  echo "  locust PID=$LOCUST_PID"
}

wait_locust_done() {
  local pid="$1"
  if ! wait "$pid" 2>/dev/null; then
    err "locust pid $pid exited non-zero"
  fi
  log "locust done"
}

monitor_start() {
  local file_prefix="$1"
  curl -s -m 20 -X POST -H 'Content-Type: application/json' \
    -d "{\"app\":\"onlineboutique\",\"namespace\":\"default\",\"interval\":5,\"file_prefix\":\"${file_prefix}\",\"prom_url\":\"${PROM_URL_INCLUSTER}\"}" \
    "$SERVER_URL/monitor/start" | tee -a "$LOG_DIR/api.log" >/dev/null
  echo "" >> "$LOG_DIR/api.log"
}

monitor_stop() {
  curl -s -m 20 -X POST "$SERVER_URL/monitor/stop" | tee -a "$LOG_DIR/api.log" >/dev/null
  echo "" >> "$LOG_DIR/api.log"
}

copy_mesh_csvs() {
  log "copy mesh CSVs from autoscaler-server pod"
  $SSH 'set -e; export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  SVR=$(sudo k3s kubectl -n default get pods -l app=autoscaler-server -o jsonpath="{.items[0].metadata.name}")
  rm -rf /tmp/svr_logs; mkdir -p /tmp/svr_logs
  sudo k3s kubectl -n default cp "$SVR:/app/logs/." /tmp/svr_logs/ >/dev/null 2>&1
  ls /tmp/svr_logs | wc -l'
  rsync -e "ssh -i $PEM -o StrictHostKeyChecking=no" -az ubuntu@$NODE_IP:/tmp/svr_logs/ "$MESH_DIR/" >/dev/null
  ls "$MESH_DIR"/*.csv 2>/dev/null | wc -l | xargs -I{} echo "  total mesh CSVs in results: {}"
}

# ─── framework run (KHPA/DAS/CustomDAS via autoscaler-server) ────────────
run_framework() {
  local autoscaler="$1" workload="$2"
  local ts="$(date +%Y%m%d_%H%M%S)"
  local file_prefix="onlineboutique_mesh_metrics_onlineboutique_${workload}_${autoscaler}"

  case "$autoscaler" in
    khpa)
      AC_NAME=default_cpu
      CONFIG='"average_cpu_utilization":50,"min_replicas":1,"max_replicas":8'
      ;;
    das)
      AC_NAME=das
      CONFIG="\"image\":\"zewang42/das-autoscaler:latest\",\"prom_url\":\"$PROM_URL_INCLUSTER\",\"interval\":30,\"cooldown_seconds\":30,\"alpha_down_threshold\":30,\"tau_min\":60,\"tau_max\":80,\"beta_up_threshold\":95,\"min_replicas\":1,\"max_replicas\":8"
      ;;
    customdas)
      AC_NAME=customDAS
      CONFIG="\"image\":\"zewang42/customdas-autoscaler:latest\",\"prom_url\":\"$PROM_URL_INCLUSTER\",\"interval\":30,\"alpha_down_threshold\":30,\"tau_min\":60,\"tau_max\":80,\"beta_up_threshold\":95,\"min_replicas\":1,\"max_replicas\":8,\"p2p_hub_deployment\":\"frontend\",\"p2p_hub_port\":5000"
      ;;
  esac

  log "framework setup: $AC_NAME / $workload (file_prefix=$file_prefix)"
  local payload
  payload=$(cat <<JSON
{"app":"onlineboutique","namespace":"default","workload_name":"$workload","duration_seconds":$DURATION_S,"autoscaler":{"autoscaler_name":"$AC_NAME","config":{$CONFIG}},"monitor":{"interval":5,"prom_url":"$PROM_URL_INCLUSTER","file_prefix":"$file_prefix"}}
JSON
)
  curl -s -m 60 -X POST -H 'Content-Type: application/json' -d "$payload" "$SERVER_URL/experiment/setup" \
    | tee -a "$LOG_DIR/api.log" | head -c 300 ; echo

  start_locust "$workload" "$autoscaler" "$ts"
  wait_locust_done "$LOCUST_PID"

  log "framework cleanup: $AC_NAME"
  curl -s -m 60 -X POST -H 'Content-Type: application/json' \
    -d "{\"app\":\"onlineboutique\",\"namespace\":\"default\",\"autoscaler_name\":\"$AC_NAME\",\"delete_autoscaler\":true,\"stop_monitoring\":true}" \
    "$SERVER_URL/experiment/cleanup" | tee -a "$LOG_DIR/api.log" | head -c 300 ; echo
}

# ─── DA-DQN run (our dockerized image, deployed via manifests/) ──────────
run_dadqn() {
  local workload="$1"
  local ts="$(date +%Y%m%d_%H%M%S)"
  local file_prefix="onlineboutique_mesh_metrics_onlineboutique_${workload}_dadqn"

  log "dadqn monitor start (file_prefix=$file_prefix)"
  monitor_start "$file_prefix"

  log "dadqn: kubectl apply manifests/ from github repo"
  scp -i "$PEM" -o StrictHostKeyChecking=no -q \
    "$PROJECT_DIR"/manifests/*.yaml ubuntu@$NODE_IP:/tmp/
  $SSH 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  sudo k3s kubectl apply -f /tmp/01-serviceaccount.yaml \
                         -f /tmp/02-rbac.yaml \
                         -f /tmp/03-configmap.yaml \
                         -f /tmp/04-deployment.yaml
  sudo k3s kubectl -n default wait --for=condition=Ready pod -l app=dadqn-autoscaler --timeout=90s'

  start_locust "$workload" "dadqn" "$ts"
  wait_locust_done "$LOCUST_PID"

  log "dadqn: copy agent CSV + logs BEFORE deleting pod"
  local agent_dest="$AGENT_DIR/${workload}_${ts}"
  mkdir -p "$agent_dest"
  $SSH 'set -e; export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  POD=$(sudo k3s kubectl -n default get pod -l app=dadqn-autoscaler -o jsonpath="{.items[0].metadata.name}")
  rm -rf /tmp/dadqn_out; mkdir -p /tmp/dadqn_out
  sudo k3s kubectl -n default cp "$POD:/data" /tmp/dadqn_out/ 2>/dev/null || true
  sudo k3s kubectl -n default logs deploy/dadqn-autoscaler --tail=5000 > /tmp/dadqn_out/agent.log 2>&1 || true'
  rsync -e "ssh -i $PEM -o StrictHostKeyChecking=no" -az ubuntu@$NODE_IP:/tmp/dadqn_out/ "$agent_dest/" >/dev/null

  log "dadqn cleanup: delete manifests + stop monitor"
  $SSH 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  sudo k3s kubectl delete -f /tmp/01-serviceaccount.yaml \
                          -f /tmp/02-rbac.yaml \
                          -f /tmp/03-configmap.yaml \
                          -f /tmp/04-deployment.yaml --ignore-not-found=true >/dev/null 2>&1' || true
  monitor_stop
}

# ─── main ────────────────────────────────────────────────────────────────
START_TS="$(date +%Y%m%d_%H%M%S)"
echo "# 4-way sweep start: $START_TS" | tee "$LOG_DIR/master.log"
echo "# duration_per_run=${DURATION_S}s sleep_between=${SLEEP_BETWEEN_S}s" | tee -a "$LOG_DIR/master.log"
echo "# total runs: $((${#WORKLOADS[@]} * ${#AUTOSCALERS[@]})) = $(( ${#WORKLOADS[@]} * ${#AUTOSCALERS[@]} * DURATION_S / 60 )) min runtime + setup" | tee -a "$LOG_DIR/master.log"

log "PREFLIGHT"
bash "$EXP_DIR/preflight.sh" 2>&1 | tee -a "$LOG_DIR/master.log" || { err "preflight FAILED"; exit 1; }

TOTAL=$(( ${#WORKLOADS[@]} * ${#AUTOSCALERS[@]} ))
i=0
for wl in "${WORKLOADS[@]}"; do
  for asc in "${AUTOSCALERS[@]}"; do
    i=$((i + 1))
    log "================== RUN $i / $TOTAL :: $asc @ $wl ($(date '+%H:%M:%S')) =================="
    reset_cluster
    if [ "$asc" = "dadqn" ]; then
      run_dadqn "$wl"
    else
      run_framework "$asc" "$wl"
    fi
    copy_mesh_csvs
    log "run $i complete; sleeping ${SLEEP_BETWEEN_S}s"
    sleep "$SLEEP_BETWEEN_S"
  done
done

log "================== ALL DONE :: $START_TS → $(date +%Y%m%d_%H%M%S) =================="
log "mesh CSVs:    $(ls "$MESH_DIR"/*.csv 2>/dev/null | wc -l)"
log "locust CSVs:  $(ls "$LOCUST_DIR"/*_stats_history.csv 2>/dev/null | wc -l)"
log "agent dirs:   $(ls -d "$AGENT_DIR"/*/ 2>/dev/null | wc -l)"
log "next: python $EXP_DIR/plot_rps_4way.py"
