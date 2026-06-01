"""Service dependency graph for Online Boutique. CASCADE redesign — callers as neighbors.

PATCH (V4 per-pod deployment): graph is built from a hardcoded full-10 service list,
not SCALABLE_SERVICES. Each agent pod patches SCALABLE_SERVICES to [MY_SERVICE] for
MAS-agent-instantiation, but the graph must remain over the full call graph so
caller-neighbour counts (-> obs dims) match the v4 trained models.
"""

from collections import deque
from dadqn_v3.config import SCALABLE_SERVICES, KAPPA_HOP, DECAY_RATE  # noqa: F401

_FULL_SERVICES = [
    "frontend", "currencyservice", "productcatalogservice", "cartservice",
    "adservice", "checkoutservice", "emailservice", "paymentservice",
    "shippingservice", "recommendationservice",
]

SERVICE_CALLS = {
    "frontend": ["adservice", "recommendationservice", "checkoutservice",
                 "currencyservice", "cartservice", "productcatalogservice",
                 "shippingservice"],
    "checkoutservice": ["cartservice", "currencyservice", "productcatalogservice",
                        "shippingservice", "paymentservice", "emailservice"],
    "recommendationservice": ["productcatalogservice"],
    "cartservice": [], "currencyservice": [], "productcatalogservice": [],
    "adservice": [], "emailservice": [], "paymentservice": [], "shippingservice": [],
}

SERVICE_CALLED_BY = {svc: [] for svc in _FULL_SERVICES}
for _caller, _callees in SERVICE_CALLS.items():
    for _callee in _callees:
        if _callee in SERVICE_CALLED_BY and _caller in _FULL_SERVICES:
            SERVICE_CALLED_BY[_callee].append(_caller)


def _build_adjacency():
    adj = {svc: set() for svc in _FULL_SERVICES}
    for caller, callees in SERVICE_CALLS.items():
        for callee in callees:
            if caller in adj and callee in adj:
                adj[caller].add(callee); adj[callee].add(caller)
    return adj

_ADJ = _build_adjacency()


def get_graph_distance(a, b):
    if a == b: return 0
    visited = {a}; queue = deque([(a, 0)])
    while queue:
        node, dist = queue.popleft()
        for n in _ADJ.get(node, set()):
            if n == b: return dist + 1
            if n not in visited:
                visited.add(n); queue.append((n, dist + 1))
    return 999


GRAPH_DISTANCES = {s: {t: get_graph_distance(s, t) for t in _FULL_SERVICES}
                   for s in _FULL_SERVICES}


def get_caller_neighbors(svc):
    return sorted(SERVICE_CALLED_BY.get(svc, []))


def get_decay_weights(svc, kappa=None):
    return {c: 1.0 for c in get_caller_neighbors(svc)}


def get_kappa_hop_neighbors(svc, kappa=None):
    return get_caller_neighbors(svc)


def get_observation_dim(svc):
    own = 6
    neighbor_feats = 3 * len(get_caller_neighbors(svc))
    frontend_lat = 0 if svc == "frontend" else 1
    time_frac = 1
    return own + neighbor_feats + frontend_lat + time_frac


KAPPA_NEIGHBOR_MAP = {svc: get_caller_neighbors(svc) for svc in _FULL_SERVICES}
DECAY_WEIGHTS = {svc: get_decay_weights(svc) for svc in _FULL_SERVICES}
OBSERVATION_DIMS = {svc: get_observation_dim(svc) for svc in _FULL_SERVICES}
