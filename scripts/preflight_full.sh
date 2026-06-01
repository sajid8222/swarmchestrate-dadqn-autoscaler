#!/usr/bin/env bash
NODE_IP=3.233.220.50
PEM=/home/sajid_40020095/Sajid_node_USA_4.pem
SSH="ssh -i $PEM -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes ubuntu@$NODE_IP"
log(){ echo "[pf $(date +%T)] $*"; }

log "wait SSH"; for i in $(seq 1 60); do $SSH 'echo ok' >/dev/null 2>&1 && break; sleep 10; done
log "wait nodes Ready"; for i in $(seq 1 60); do n=$($SSH 'sudo k3s kubectl get nodes --no-headers 2>/dev/null' 2>/dev/null|grep -c " Ready "); log "ready=$n/5"; [ "$n" -ge 5 ]&&break; sleep 10; done
log "wait boutique deploys"; for i in $(seq 1 60); do nr=$($SSH 'sudo k3s kubectl get deploy -n default --no-headers 2>/dev/null' 2>/dev/null|awk '{split($2,a,"/"); if(a[1]!=a[2])c++}END{print c+0}'); log "notready=$nr"; [ "$nr" = "0" ]&&break; sleep 10; done

log "gateway probe"; code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://$NODE_IP:30193/")
if [ "$code" != "200" ]; then
  log "gateway $code -> fix: restart gateway + delete default pods"
  $SSH 'sudo k3s kubectl -n default rollout restart deploy/frontend-gateway-istio; sudo k3s kubectl -n default delete pods --all --grace-period=5 >/dev/null 2>&1'
  for i in $(seq 1 40); do nr=$($SSH 'sudo k3s kubectl get pods -n default --no-headers 2>/dev/null'|awk '{split($2,a,"/"); if(a[1]!=a[2])c++}END{print c+0}'); [ "$nr" = "0" ]&&break; sleep 10; done
  for i in $(seq 1 30); do code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://$NODE_IP:30193/"); log "gw=$code"; [ "$code" = "200" ]&&break; sleep 10; done
fi
log "gateway now $code"

log "fix Prometheus istio scraping (restart operator+server+pf)"
$SSH 'sudo k3s kubectl -n monitoring rollout restart deploy/prom-kube-prometheus-stack-operator >/dev/null 2>&1
sudo k3s kubectl -n monitoring delete pod prometheus-prom-kube-prometheus-stack-prometheus-0 --grace-period=10 >/dev/null 2>&1
sudo k3s kubectl -n monitoring rollout status statefulset/prometheus-prom-kube-prometheus-stack-prometheus --timeout=180s >/dev/null 2>&1
sudo fuser -k 9090/tcp >/dev/null 2>&1; sleep 2
nohup sudo k3s kubectl -n monitoring port-forward svc/prom-kube-prometheus-stack-prometheus 9090:9090 --address 127.0.0.1 >/tmp/prompf.log 2>&1 & sleep 6; echo pf-up'
log "seed traffic + verify istio_requests_total"
( for i in $(seq 1 300); do curl -s -o /dev/null --max-time 3 "http://$NODE_IP:30193/"; done ) &
for i in $(seq 1 20); do c=$($SSH 'curl -s "http://127.0.0.1:9090/api/v1/query?query=count(istio_requests_total)" 2>/dev/null'|grep -oE "\"[0-9]+\"\]"|tr -dc 0-9); log "istio series=$c"; [ -n "$c" ]&&[ "$c" -gt 0 ]&&{ log "PROM OK"; break; }; sleep 10; done
log "scaling critical Istio waypoints (prevents prod-waypoint saturation seen at >=400rps)"
$SSH '"'"'for w in "prod-waypoint:3" "cur-waypoint:2" "rec-waypoint:2" "cart-waypoint:2"; do
  d=${w%:*}; n=${w#*:};
  sudo k3s kubectl -n default scale deploy $d --replicas=$n >/dev/null 2>&1
  sudo k3s kubectl -n default rollout status deploy/$d --timeout=60s >/dev/null 2>&1
done
echo "[pf] waypoints: $(sudo k3s kubectl -n default get deploy -l service.istio.io/canonical-name --no-headers | awk "{print \$1\"=\"\$2}" | tr "\n" " ")"'"'"'
log "PREFLIGHT DONE (gateway=$code)"
