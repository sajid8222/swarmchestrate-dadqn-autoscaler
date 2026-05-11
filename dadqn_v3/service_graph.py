"""Service dependency graph. Identical to v2 — the graph doesn't change."""

from collections import deque
from dadqn_v3.config import SCALABLE_SERVICES, KAPPA_HOP, DECAY_RATE

SERVICE_CALLS = {
    "frontend": ["recommendationservice", "currencyservice",
                 "productcatalogservice", "cartservice", "adservice"],
    "checkoutservice": ["cartservice", "paymentservice",
                        "shippingservice", "emailservice", "currencyservice"],
    "recommendationservice": ["productcatalogservice"],
    "cartservice": [], "currencyservice": [], "productcatalogservice": [],
    "adservice": [], "emailservice": [], "paymentservice": [], "shippingservice": [],
}

SERVICE_CALLED_BY = {svc: [] for svc in SCALABLE_SERVICES}
for caller, callees in SERVICE_CALLS.items():
    for callee in callees:
        if callee in SERVICE_CALLED_BY:
            SERVICE_CALLED_BY[callee].append(caller)


def _build_adjacency():
    adj = {svc: set() for svc in SCALABLE_SERVICES}
    for caller, callees in SERVICE_CALLS.items():
        for callee in callees:
            if caller in adj and callee in adj:
                adj[caller].add(callee)
                adj[callee].add(caller)
    return adj

_ADJ = _build_adjacency()


def get_graph_distance(a, b):
    if a == b: return 0
    visited = {a}
    queue = deque([(a, 0)])
    while queue:
        node, dist = queue.popleft()
        for n in _ADJ.get(node, set()):
            if n == b: return dist + 1
            if n not in visited:
                visited.add(n)
                queue.append((n, dist + 1))
    return 999


GRAPH_DISTANCES = {s: {t: get_graph_distance(s, t) for t in SCALABLE_SERVICES}
                   for s in SCALABLE_SERVICES}


def get_kappa_hop_neighbors(svc, kappa=None):
    if kappa is None: kappa = KAPPA_HOP
    return sorted(s for s in SCALABLE_SERVICES
                  if s != svc and GRAPH_DISTANCES[svc][s] <= kappa)


def get_decay_weights(svc, kappa=None):
    if kappa is None: kappa = KAPPA_HOP
    neighbors = get_kappa_hop_neighbors(svc, kappa)
    return {s: DECAY_RATE ** GRAPH_DISTANCES[svc][s] for s in neighbors}


def get_observation_dim(svc):
    own = 6
    neighbor_feats = 3 * len(get_kappa_hop_neighbors(svc))
    frontend_lat = 0 if svc == "frontend" else 1
    time_frac = 1
    return own + neighbor_feats + frontend_lat + time_frac


KAPPA_NEIGHBOR_MAP = {svc: get_kappa_hop_neighbors(svc) for svc in SCALABLE_SERVICES}
DECAY_WEIGHTS = {svc: get_decay_weights(svc) for svc in SCALABLE_SERVICES}
OBSERVATION_DIMS = {svc: get_observation_dim(svc) for svc in SCALABLE_SERVICES}
