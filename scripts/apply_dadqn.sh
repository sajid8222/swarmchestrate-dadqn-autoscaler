#!/usr/bin/env bash
# Apply the DA-DQN per-service decentralized agents.
#
# This is a 1-step deploy — the agent image
# (proactivellmbasedproject/dadqn-autoscaler:v4-decentralized) already has all
# V4 patches baked in. No ConfigMap subPath workarounds needed.
#
# Pre-reqs:
#   - Cluster has: k3s + Istio ambient + Prometheus + Boutique + waypoints + Gateway:30193
#   - Image (v4-decentralized) has models baked in — no host staging needed
#   - $KUBECONFIG points at the target cluster
#   - You're running from the repo root

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "[1/3] Apply SA + RBAC + ConfigMap + 10 per-service Deployments..."
kubectl apply -f manifests/

echo
echo "[2/3] Wait for all 10 agents to be Ready..."
kubectl -n default wait --for=condition=Available deploy -l app=dadqn-autoscaler --timeout=180s

echo
echo "[3/3] Show pod placement + a sample agent decision..."
kubectl -n default get pods -l app=dadqn-autoscaler -o wide
echo
echo "Sample agent log (frontend, last 15 lines):"
kubectl -n default logs deploy/dadqn-frontend --tail=15

echo
echo "Done. Watch decisions in real-time with:"
echo "  bash scripts/monitor.sh"
echo "or per-service:"
echo "  kubectl -n default logs deploy/dadqn-frontend -f"
