"""4-way comparison plot: KHPA / DAS / CustomDAS / DA-DQN @ rps-0/100/200/400/600.

Reads experiment outputs from results/{locust,mesh_metrics}/ and writes
results/plots/fig_rps_4way_singlenode.png.

CSV naming conventions:
  Locust:  onlineboutique_<workload>_<autoscaler>_all_<ts>_stats_history.csv
  Mesh:    onlineboutique_mesh_metrics_onlineboutique_<workload>_<autoscaler>_default_5s_<ts>.csv

Where autoscaler is one of: khpa, das, customdas, dadqn.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
LOCUST_DIR = RESULTS / "locust"
MESH_DIR = RESULTS / "mesh_metrics"
OUT_DIR = RESULTS / "plots"

WORKLOADS = ["rps-0", "rps-100", "rps-200", "rps-400", "rps-600", "rps-1000"]
TARGET_RPS = {"rps-0": 0, "rps-100": 100, "rps-200": 200,
              "rps-400": 400, "rps-600": 600, "rps-1000": 1000}

AUTOSCALERS = ["khpa", "das", "customdas", "dadqn"]
LABEL_NICE = {"khpa": "KHPA", "das": "DAS",
              "customdas": "CustomDAS", "dadqn": "DA-DQN"}
COLOR = {"khpa": "#d62728", "das": "#2ca02c",
         "customdas": "#9467bd", "dadqn": "#ff7f0e"}
LINESTYLE = {"khpa": "-", "das": "--", "customdas": "-.", "dadqn": "-"}

SERVICES = [
    "frontend", "currencyservice", "productcatalogservice", "cartservice",
    "adservice", "checkoutservice", "emailservice", "paymentservice",
    "shippingservice", "recommendationservice",
]
SLA_MS = 1200
NODE_UTIL_CSV = RESULTS / "node_metrics" / "node_util.csv"


def load_node_util():
    if not NODE_UTIL_CSV.exists():
        return None
    df = pd.read_csv(NODE_UTIL_CSV)
    df["ts_unix"] = pd.to_numeric(df["ts_unix"], errors="coerce")
    for col in ("cpu_pct", "mem_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["ts_unix"]).sort_values("ts_unix").reset_index(drop=True)


def attach_node_series(locust_df, node_df):
    """For each (step, ts_unix) in locust_df, look up nearest node sample
    within 30s. Returns DataFrame with step, cpu_pct, mem_pct."""
    if node_df is None or locust_df.empty:
        return pd.DataFrame(columns=["step", "cpu_pct", "mem_pct"])
    rows = []
    for _, lr in locust_df.iterrows():
        ts = lr["ts_unix"]
        if ts == 0:
            continue
        diffs = (node_df["ts_unix"] - ts).abs()
        if diffs.empty:
            continue
        idx = diffs.idxmin()
        if diffs[idx] <= 30:
            rows.append({"step": int(lr["step"]),
                         "cpu_pct": float(node_df.loc[idx, "cpu_pct"]),
                         "mem_pct": float(node_df.loc[idx, "mem_pct"])})
    return pd.DataFrame(rows)


def find_locust(workload, asc):
    pat = f"onlineboutique_{workload}_{asc}_all_*_stats_history.csv"
    fs = sorted(LOCUST_DIR.glob(pat))
    return fs[-1] if fs else None


def find_mesh(workload, asc):
    # Framework runs (khpa/das/customdas): server appends "_<workload>_<acname>_default_5s_<ts>".
    # DA-DQN run: file is just "<our_prefix>_default_5s_<ts>".
    # Wildcard between asc and _default_5s catches both.
    pat = f"onlineboutique_mesh_metrics_onlineboutique_{workload}_{asc}*_default_5s_*.csv"
    fs = sorted(MESH_DIR.glob(pat))
    return fs[-1] if fs else None


def build_timeline(asc):
    locust_rows, pods_rows = [], []
    boundaries, mids = [], []
    step = 0
    starts = []
    for wl in WORKLOADS:
        starts.append(step)
        f = find_locust(wl, asc)
        if f is None:
            print(f"  [warn] no Locust for {asc} @ {wl}")
            boundaries.append(step)
            continue
        df = pd.read_csv(f)
        df = df[df["Name"] == "Aggregated"]
        if df.empty:
            boundaries.append(step)
            continue
        df = df.copy()
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="s")
        df = df.set_index("Timestamp").resample("5s").last().dropna(subset=["95%"])
        avg = pd.to_numeric(df["Total Average Response Time"], errors="coerce").fillna(0.0)
        cnt = pd.to_numeric(df["Total Request Count"], errors="coerce").fillna(0.0)
        cum_time_ms = avg * cnt
        d_cnt = cnt.diff().fillna(cnt)
        d_time = cum_time_ms.diff().fillna(cum_time_ms)
        win_mean = (d_time / d_cnt).where(d_cnt > 0, other=float("nan"))
        roll = win_mean.rolling(window=6, min_periods=1).mean().fillna(0.0)
        df = df.assign(rolling_lat_ms=roll)
        for _, row in df.iterrows():
            try:
                lat = float(row["rolling_lat_ms"])
            except (TypeError, ValueError):
                lat = 0.0
            try:
                rps = float(row["Requests/s"])
            except (TypeError, ValueError):
                rps = 0.0
            try:
                users = int(row["User Count"])
            except (TypeError, ValueError):
                users = 0
            ts_unix = int(row.name.timestamp()) if hasattr(row.name, "timestamp") else 0
            locust_rows.append({"step": step, "p95_ms": lat, "rps": rps,
                                "users": users, "target_rps": TARGET_RPS[wl],
                                "workload": wl, "ts_unix": ts_unix})
            step += 1

        m = find_mesh(wl, asc)
        if m is not None:
            mdf = pd.read_csv(m)
            mdf = mdf[mdf["Scope"] == "deployment"]
            mdf = mdf[mdf["Name"].isin(SERVICES)]
            mdf["Timestamp"] = pd.to_datetime(mdf["Timestamp"])
            tot = mdf.groupby("Timestamp")["Pods"].sum().sort_index()
            n = len([r for r in locust_rows if r["workload"] == wl])
            vals = list(tot.values)
            if len(vals) > n:
                vals = vals[:n]
            elif len(vals) < n:
                vals += [vals[-1] if vals else 0] * (n - len(vals))
            ws = starts[-1]
            for i, v in enumerate(vals):
                pods_rows.append({"step": ws + i, "total_pods": int(v)})
        else:
            print(f"  [warn] no mesh CSV for {asc} @ {wl}")
        boundaries.append(step)
        mids.append((starts[-1] + step) / 2)
    return (pd.DataFrame(locust_rows), pd.DataFrame(pods_rows),
            boundaries[:-1], mids)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    series = {a: build_timeline(a) for a in AUTOSCALERS}
    node_df = load_node_util()
    node_series = {a: attach_node_series(series[a][0], node_df) for a in AUTOSCALERS}
    boundaries, mids = [], []
    for a in AUTOSCALERS:
        _, _, b, m = series[a]
        if b:
            boundaries, mids = b, m
            break

    fig, axes = plt.subplots(3, 2, figsize=(16, 13), sharex=False)
    fig.suptitle(
        "Single-node (t3.2xlarge, 8 vCPU/30 GB) — KHPA / DAS / CustomDAS / DA-DQN @ 0/100/200/400/600/1000 RPS, 10 min each",
        fontsize=12, fontweight="bold")

    # (1) p95 latency vs SLA
    ax = axes[0, 0]
    for a in AUTOSCALERS:
        loc, _, _, _ = series[a]
        if not loc.empty:
            ax.plot(loc["step"].values, loc["p95_ms"].values, LINESTYLE[a],
                    color=COLOR[a], lw=1.6, label=LABEL_NICE[a])
    ax.axhline(SLA_MS, color="black", linestyle=":", lw=1, label=f"SLA ({SLA_MS}ms)")
    ax.set_ylabel("p95 latency (ms)")
    ax.set_title("client-side latency vs SLA")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    # (2) cluster pod count
    ax = axes[0, 1]
    for a in AUTOSCALERS:
        _, pods, _, _ = series[a]
        if not pods.empty:
            ax.plot(pods["step"].values, pods["total_pods"].values, LINESTYLE[a],
                    color=COLOR[a], lw=1.6, label=LABEL_NICE[a])
    ax.set_ylabel("total pods")
    ax.set_title("cluster pod count")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    # (3) load — users over time
    ax = axes[1, 0]
    for a in AUTOSCALERS:
        loc, _, _, _ = series[a]
        if not loc.empty:
            ax.plot(loc["step"].values, loc["users"].values, LINESTYLE[a],
                    color=COLOR[a], lw=1.0, alpha=0.7, label=LABEL_NICE[a])
    ax.set_ylabel("Number of users")
    ax.set_title("load (Number of users)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    # (4) per-workload avg pods (bar chart)
    ax = axes[1, 1]
    xs = np.arange(len(WORKLOADS))
    w = 0.8 / len(AUTOSCALERS)
    for i, a in enumerate(AUTOSCALERS):
        _, pods, bnds, _ = series[a]
        wl_pods = []
        prev = 0
        all_b = bnds + [pods["step"].max() + 1 if not pods.empty else 1]
        for j, b in enumerate(all_b[:len(WORKLOADS)]):
            mask = (pods["step"] >= prev) & (pods["step"] < b)
            avg = pods.loc[mask, "total_pods"].mean()
            wl_pods.append(avg if not np.isnan(avg) else 0)
            prev = b
        wl_pods += [0] * (len(WORKLOADS) - len(wl_pods))
        offset = (i - (len(AUTOSCALERS) - 1) / 2) * w
        bars = ax.bar(xs + offset, wl_pods, w, color=COLOR[a],
                      label=LABEL_NICE[a])
        for bar, v in zip(bars, wl_pods):
            if v > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, v + 0.3,
                        f"{v:.0f}", ha="center", va="bottom",
                        fontsize=7, fontweight="bold")
    ax.set_xticks(xs)
    ax.set_xticklabels(
        [f"Load {i+1}\n{wl}\n~{TARGET_RPS[wl]} RPS"
         for i, wl in enumerate(WORKLOADS)], fontsize=8)
    ax.set_ylabel("avg pods")
    ax.set_title("per-workload avg pods")
    ax.legend(loc="upper left", fontsize=7)
    ax.grid(alpha=0.3, axis="y")

    # (5) node CPU %
    ax = axes[2, 0]
    for a in AUTOSCALERS:
        ns = node_series[a]
        if not ns.empty:
            ax.plot(ns["step"].values, ns["cpu_pct"].values, LINESTYLE[a],
                    color=COLOR[a], lw=1.6, label=LABEL_NICE[a])
    ax.set_ylabel("node CPU %")
    ax.set_ylim(0, 100)
    ax.set_title("node CPU utilization (8 vCPU)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    # (6) node MEM %
    ax = axes[2, 1]
    for a in AUTOSCALERS:
        ns = node_series[a]
        if not ns.empty:
            ax.plot(ns["step"].values, ns["mem_pct"].values, LINESTYLE[a],
                    color=COLOR[a], lw=1.6, label=LABEL_NICE[a])
    ax.set_ylabel("node MEM %")
    ax.set_ylim(0, 100)
    ax.set_title("node MEM utilization (30 GB)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)

    for sub in (axes[0, 0], axes[0, 1], axes[1, 0], axes[2, 0], axes[2, 1]):
        for b in boundaries:
            sub.axvline(b, color="grey", linestyle=":", alpha=0.5, lw=0.8)
        if mids:
            sub.set_xticks(mids)
            sub.set_xticklabels(
                [f"Load {i+1}\n{wl}\n~{TARGET_RPS[wl]} RPS"
                 for i, wl in enumerate(WORKLOADS)], fontsize=8)

    fig.tight_layout()
    out = OUT_DIR / "fig_rps_4way_singlenode.png"
    fig.savefig(out, dpi=160, bbox_inches="tight")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
