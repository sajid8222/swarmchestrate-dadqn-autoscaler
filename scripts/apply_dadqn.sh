#!/usr/bin/env bash
# Apply the DA-DQN per-service agents to a cluster that already has
# Boutique + Istio ambient + Prometheus running (see README.md).
#
# Usage:
#   bash scripts/apply_dadqn.sh
#
# Pre-reqs:
#   - $KUBECONFIG points at the target cluster (or run from a node with k3s)
#   - Models already staged on each worker at /tmp/sla_v1/*.zip
#   - You're in the repo root (so the relative paths resolve)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "[1/4] Create / refresh the 3 patch ConfigMaps (mounted via subPath over the image)..."
for pair in "mc-v3-patch:dadqn_v3/environments/metrics_collector_v3.py" \
            "sg-v2-patch:dadqn_v3/service_graph.py" \
            "sae-v4-patch:dadqn_v3/environments/service_agent_env.py"; do
  cm=${pair%:*}
  src=${pair#*:}
  base=$(basename "$src")
  kubectl -n default delete configmap "$cm" --ignore-not-found
  kubectl -n default create configmap "$cm" --from-file="$base=$src"
done

echo
echo "[2/4] Apply RBAC + ConfigMap + 10 per-service Deployments..."
kubectl apply -f manifests/

echo
echo "[3/4] Wait for all 10 agents to be Ready..."
kubectl -n default wait --for=condition=Available deploy -l app=dadqn-autoscaler --timeout=180s

echo
echo "[4/4] Show pod placement + a sample agent decision..."
kubectl -n default get pods -l app=dadqn-autoscaler -o wide
echo
echo "Sample agent logs (frontend):"
kubectl -n default logs deploy/dadqn-frontend --tail=15

echo
echo "Done. Watch decisions in real-time with:"
echo "  bash scripts/monitor.sh"
echo "or per-service:"
echo "  kubectl -n default logs deploy/dadqn-frontend -f"
