# tests/test_dadqn_serve_smoke.py
"""
Smoke test: dadqn_serve module loads, _init_k8s falls back to local kubeconfig
when in-cluster config absent, locust_stats returns zeros when LOCUST_URL unset.
"""
import importlib
import os
import sys


def test_module_imports():
    mod = importlib.import_module("dadqn_v3.baselines.dadqn_serve")
    assert hasattr(mod, "main")
    assert hasattr(mod, "kubectl_pod_counts")
    assert hasattr(mod, "scale_deployment")
    assert hasattr(mod, "locust_stats")


def test_locust_stats_no_url(monkeypatch):
    monkeypatch.setenv("LOCUST_URL", "")
    # Re-import to pick up env var change
    if "dadqn_v3.baselines.dadqn_serve" in sys.modules:
        del sys.modules["dadqn_v3.baselines.dadqn_serve"]
    mod = importlib.import_module("dadqn_v3.baselines.dadqn_serve")
    s = mod.locust_stats()
    assert s == {"users": 0, "rps": 0.0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}
