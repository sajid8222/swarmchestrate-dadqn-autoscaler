# DA-DQN Kubernetes Manifest — Design

**Date:** 2026-05-06
**Status:** Approved (brainstorming → writing-plans)
**Goal:** Wrap the working DA-DQN v3 agent as a deployable Kubernetes manifest so it runs *inside* the cluster (no laptop SSH), and is also clean enough for other researchers to deploy with `kubectl apply` + a public Docker Hub image.

## Background

Today the DA-DQN v3 agent (`dadqn_v3.baselines.dadqn_wiki_runner`) runs from the laptop or via SSH on `cluster-1.1`, scales boutique deployments using `subprocess.run(["kubectl", "scale", ...])`, and reads metrics from Prometheus + Locust over HTTP.

Works for ad-hoc experiments, but not reproducible — every researcher needs the same `.venv`, same `KUBECONFIG`, same SSH access, and the same orchestrator scripts. The framework's other autoscalers (DAS, CustomDAS) live as containerised Deployments inside the cluster (e.g., `zewang42/das-autoscaler:latest`). DA-DQN should follow the same pattern.

## Architecture

### Components

1. **`dadqn_v3/baselines/dadqn_serve.py`** — *new* entrypoint
   - Always-on outer loop (no `--duration` arg)
   - Replaces `subprocess.run(["kubectl", ...])` with the `kubernetes` Python client (`config.load_incluster_config()` when in-cluster, `load_kube_config()` for local dev)
   - Locust-optional: gracefully degrades to Prometheus-only if `LOCUST_URL` is unset or unreachable (already 80% how `metrics_collector_v3.py` behaves)
   - Logs decisions to stdout (so `kubectl logs` retrieves them)
   - Writes per-decision CSV rows to `/data/wiki_dadqn_<ts>.csv` (mounted `emptyDir`)

2. **`dadqn_v3/deploy/Dockerfile`**
   - Base: `python:3.11-slim`
   - Installs: `torch`, `stable-baselines3==2.7.1`, `numpy>=2.0`, `requests`, `kubernetes`
   - Copies: `dadqn_v3/` package + trained weights from `dadqn_v3/diagnostics/finetuned_v3_actual/`
   - Entrypoint: `python -m dadqn_v3.baselines.dadqn_serve`
   - Image tag: `<dockerhub-user>/dadqn-autoscaler:v1` (open parameter — see end of doc)

3. **Manifests** in `dadqn_v3/deploy/manifests/`
   - `01-serviceaccount.yaml` — `ServiceAccount/dadqn-autoscaler` in `default` ns
   - `02-rbac.yaml` — `Role` + `RoleBinding` (verbs scoped to deployments + pods, see RBAC section)
   - `03-configmap.yaml` — env-var configurables: `LOCUST_URL`, `PROMETHEUS_URL`, `PROM_RATE_WINDOW`, `SAMPLE_INTERVAL_SEC`, `DECISION_INTERVAL_SEC`, `SCALE_DOWN_COOLDOWN_SEC`, `OBS_FALLBACK_FROM_CPU`
   - `04-deployment.yaml` — `Deployment/dadqn-autoscaler`, 1 replica, `restartPolicy: Always`, mounts ConfigMap as env, mounts `emptyDir` at `/data`

4. **`dadqn_v3/deploy/README.md`** — docs for two configurations:
   - **Experiment mode** (with Locust): set `LOCUST_URL` in ConfigMap → `kubectl rollout restart`
   - **Production mode** (Prometheus-only): leave `LOCUST_URL` empty / unset

### What stays unchanged

- `dadqn_v3/baselines/dadqn_wiki_runner.py` — keeps existing experiment scripts working
- `dadqn_v3/agents/`, `dadqn_v3/environments/`, `dadqn_v3/config.py` — reused by both runner and serve
- All training, plotting, observation-extraction code — unrelated, untouched

Per CLAUDE.md "Surgical Changes": new entrypoint, no edits to working code paths.

### Data flow

```
Locust (cluster-1.4)        ──HTTP──┐
Prometheus (cluster-1.1)    ──query─┼─→ MetricsCollectorV3 → MultiAgentSystem → kubernetes-client.scale()
                                    │
                                    └─→ stdout (kubectl logs) + /data/wiki_dadqn_<ts>.csv (kubectl cp)
```

### RBAC scope

Minimum permissions, scoped to `default` namespace only:
- `apiGroups: [""], resources: [pods], verbs: [get, list]`
- `apiGroups: [apps], resources: [deployments], verbs: [get, list, watch]`
- `apiGroups: [apps], resources: [deployments/scale], verbs: [get, patch, update]`

Researchers using a different namespace edit the `Role` + `RoleBinding` `metadata.namespace` field.

### Lifecycle

| Phase | Command |
|---|---|
| Initial deploy | `kubectl apply -f dadqn_v3/deploy/manifests/` |
| Tune params | `kubectl edit cm dadqn-config` + `kubectl rollout restart deploy/dadqn-autoscaler` |
| Pause between experiments | `kubectl scale deploy/dadqn-autoscaler --replicas=0` |
| Resume | `kubectl scale deploy/dadqn-autoscaler --replicas=1` |
| Retrieve logs | `kubectl logs deploy/dadqn-autoscaler --tail=2000` |
| Retrieve CSV | `kubectl cp default/<pod>:/data/wiki_dadqn_<ts>.csv ./` |
| Uninstall | `kubectl delete -f dadqn_v3/deploy/manifests/` |

### Updated experiment orchestrator

`dadqn_v3/diagnostics/cluster_2_results/scripts/run_v3_rps_X_on_manifest.sh` (new):
1. Preflight (existing 10 checks)
2. Reset cluster pods to 1 each
3. Update ConfigMap if needed (rare — only if changing decision-interval or other knobs)
4. `kubectl rollout restart deploy/dadqn-autoscaler` — fresh state
5. Wait until pod Ready
6. Start mesh monitor + Locust on cluster-1.4 (existing logic)
7. `sleep $((DURATION_S + 30))` — load test runs (no SSH-to-run-agent block needed)
8. Stop Locust + monitor (existing)
9. Pull mesh logs + Locust CSVs (existing)
10. Pull agent CSV + logs (`kubectl cp`, `kubectl logs`)
11. Plot

### Error handling

- `LOCUST_URL` set but unreachable → log a single warning, fall back to Prometheus, do NOT crash
- Prometheus unreachable → log error, sleep `decision_interval`, retry next tick
- `kubernetes-client` API call fails (transient) → retry once with 2s backoff; if still fails, log + skip this decision tick
- Pod crashes (model load error, OOM) → kubernetes auto-restarts (`restartPolicy: Always`)
- Each tick wrapped in try/except — Python equivalent of "no `set -e`"

### Testing

- **Build verification**: `docker build` succeeds, image size < 1.5 GB
- **Unit-level**: import check that `dadqn_v3.baselines.dadqn_serve` loads model + initialises agents without errors (uses existing model-load test paths)
- **Integration**: deploy manifest to running cluster, verify
  - Pod `Running` and `Ready=True` within 60 s
  - `kubectl logs` shows "Loading DA-DQN model" and at least one "decision:" entry within 90 s
  - Triggering Locust load (rps-600) causes `frontend` pod count to grow from 1 to ≥ 3 within 2 min
  - When load stops, pod counts shrink (after cooldown)

## Success Criteria

1. Image builds and pushes to `<dockerhub-user>/dadqn-autoscaler:v1`
2. `kubectl apply -f dadqn_v3/deploy/manifests/` deploys ServiceAccount, Role, RoleBinding, ConfigMap, Deployment without errors
3. Pod becomes Ready within 60 s
4. Within 90 s of pod start, agent logs first scaling decision
5. During a Locust load run (rps-600), agent scales `frontend` to ≥ 3 pods within 2 min
6. After load stops + cooldown, agent scales back to 1 pod
7. `kubectl logs` retrievable; `kubectl cp` extracts the CSV
8. Existing `run_rps800_4way_locust_on_worker.sh` style orchestrator still works after minor patch (new variant `..._on_manifest.sh`)
9. README explains experiment + production modes; another researcher can deploy from scratch using only the README + manifests + Docker image

## Out of scope

- Training pipeline (model already trained, weights baked into image)
- HPA replacement / Custom Metrics API integration
- Multi-namespace deployment (single namespace `default` only — researchers edit YAML for other ns)
- Multi-cluster / federation
- HTTP API for runtime control (start/stop/inspect)
- Prometheus exporter for the agent's own decisions (nice-to-have, not required)
- Helm chart (raw YAML is sufficient; can be added later)
- CI/CD for image builds (manual `docker build && docker push` for now)
- Auto-discovery of Locust (URL passed via ConfigMap)

## Open parameter

- `<dockerhub-user>` — Docker Hub username for image tag. To be provided by user before image build/push step in the implementation plan.

## Implementation order (input to writing-plans skill)

1. Refactor: write new `dadqn_v3/baselines/dadqn_serve.py` (kubernetes-client + always-on loop, Locust-optional)
2. Build: `dadqn_v3/deploy/Dockerfile`
3. Pull model weights from cluster-1.1 → laptop (so they're in the Docker build context)
4. Manifests: 4 YAML files in `dadqn_v3/deploy/manifests/`
5. README: deployment docs (experiment + production modes)
6. Local smoke test: build image, run container locally with mocked metrics endpoints to verify imports + model load
7. Push to Docker Hub
8. Deploy to cluster-1.1, verify success criteria 2-7
9. Update orchestrator: new `run_v3_rps_X_on_manifest.sh`
10. End-to-end test at rps-600: criteria 5-6
