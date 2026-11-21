"""Render the load-test charts in docs/evidence/charts from the committed evidence files.

Usage: uv run --no-project --with matplotlib==3.9.2 python loadtest/charts.py
Every number plotted comes from docs/evidence/loadtest-*/ (k6 summaries, nginx summaries,
per-second metrics exported from PostgreSQL, final state and SSE client reports).
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence"
OUT = EVIDENCE / "charts"
RUNS = {
    "Run 1 (3 replicas)": EVIDENCE / "loadtest-run1-20261121",
    "Run 2 (3 replicas)": EVIDENCE / "loadtest-run2-20261121",
    "6 replicas": EVIDENCE / "loadtest-run3-6replicas-20261121",
}
COLORS = {"ok": "#2a8b7b", "conflict": "#9c661a", "busy": "#7a8699", "error": "#b3261e"}
plt.rcParams.update(
    {"figure.dpi": 110, "font.size": 10, "axes.spines.top": False, "axes.spines.right": False}
)


def k6(run: Path) -> dict:
    return json.loads((run / "a" / "k6-summary.json").read_text())["metrics"]


def seats(run: Path) -> dict[str, int]:
    text = (run / "a" / "final-state.txt").read_text()
    found = dict(re.findall(r"^\s*(available|held|sold)\s*\|\s*(\d+)", text, re.M))
    return {key: int(value) for key, value in found.items()}


def chart_outcomes() -> None:
    """Scenario A: what happened to the requests, per run."""
    labels, remaining, conflict, busy, error = [], [], [], [], []
    for name, run in RUNS.items():
        metrics = k6(run)
        total = metrics["http_reqs"]["count"]
        busy_count = metrics.get("server_busy", {}).get("count", 0)
        conflicts = metrics.get("business_conflicts", {}).get("count", 0) - busy_count
        # Use the same counter for all runs. The residual is not an HTTP status tally.
        errors = metrics.get("unexpected_errors", {}).get("passes", 0)
        residual = total - busy_count - conflicts - errors
        if min(busy_count, conflicts, errors, residual) < 0:
            raise ValueError(f"{name}: inconsistent k6 request totals")
        labels.append(name)
        busy.append(busy_count)
        conflict.append(conflicts)
        error.append(errors)
        remaining.append(residual)
    fig, ax = plt.subplots(figsize=(8, 3.6))
    left = [0] * len(labels)
    for values, key, text in (
        (remaining, "ok", "Remaining k6 requests"),
        (conflict, "conflict", "Business 409"),
        (busy, "busy", "Shed: 503 server-busy"),
        (error, "error", "k6 unexpected_errors"),
    ):
        ax.barh(labels, values, left=left, color=COLORS[key], label=text)
        left = [a + b for a, b in zip(left, values, strict=True)]
    ax.invert_yaxis()
    ax.set_xlabel("Requests per run (k6 total, 60 s of load plus ramp-down)")
    ax.set_title("Scenario A: request outcomes (3,000 VUs, 10,000 seats)")
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2, frameon=False)
    fig.subplots_adjust(left=0.2, right=0.97, top=0.88, bottom=0.36)
    fig.savefig(OUT / "a-request-outcomes.png")
    plt.close(fig)


def chart_seats() -> None:
    """Scenario A: seat state after the run (no seat sold twice in any run)."""
    fig, ax = plt.subplots(figsize=(8, 3.2))
    names = list(RUNS)
    states = [seats(run) for run in RUNS.values()]
    left = [0] * len(names)
    for key, color, text in (
        ("sold", "#204ea8", "Sold"),
        ("held", "#9c661a", "Held"),
        ("available", "#c9d1dc", "Available"),
    ):
        values = [state.get(key, 0) for state in states]
        ax.barh(names, values, left=left, color=color, label=text)
        left = [a + b for a, b in zip(left, values, strict=True)]
    ax.invert_yaxis()
    ax.set_xlim(0, 10_000)
    ax.set_xlabel("Seats (of 10,000) after the run and a 90 s drain")
    ax.set_title("Scenario A: seat inventory after the run (0 oversold in every run)")
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.3), ncol=3, frameon=False)
    fig.subplots_adjust(left=0.2, right=0.97, top=0.86, bottom=0.36)
    fig.savefig(OUT / "a-seat-inventory.png")
    plt.close(fig)


def per_second(run: Path) -> list[dict]:
    rows = []
    with (run / "a" / "metrics_1s.csv").open() as source:
        for row in csv.DictReader(source):
            # Seconds under load only (health checks alone stay far below 100 requests/s).
            if (
                row["source"] == "edge"
                and row["scope"] == "all"
                and int(row["req_total"] or 0) > 100
            ):
                rows.append(row)
    return rows


def chart_timeline() -> None:
    """Scenario A: per-second edge metrics stored by the monitoring pipeline."""
    fig, axes = plt.subplots(3, 1, figsize=(8, 7), sharex=True)
    for (name, run), color in zip(list(RUNS.items())[1:], ("#204ea8", "#2a8b7b"), strict=True):
        rows = per_second(run)
        if not rows:
            continue
        start = datetime.fromisoformat(rows[0]["ts"])
        xs = [(datetime.fromisoformat(r["ts"]) - start).total_seconds() for r in rows]
        req = [int(r["req_total"]) for r in rows]
        err = [100 * int(r["err_5xx"]) / count for r, count in zip(rows, req, strict=True)]
        p95 = [float(r["p95_ms"]) / 1000 if r["p95_ms"] else None for r in rows]
        axes[0].plot(xs, req, color=color, label=name)
        axes[1].plot(xs, err, color=color, label=name)
        axes[2].plot(xs, p95, color=color, label=name)
    axes[0].set_ylabel("Requests/s")
    axes[1].set_ylabel("5xx share (%)\n(nearly all 503 shed)")
    axes[2].set_ylabel("p95 latency (s)")
    axes[2].set_xlabel("Seconds since the first nginx second with >100 requests")
    axes[0].set_title("Scenario A per second: 3 vs 6 API replicas")
    axes[0].legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / "a-per-second.png")
    plt.close(fig)


def chart_sse() -> None:
    """Scenario B: run 1 vs run 2 for 2,000 SSE subscribers."""
    reports = {}
    for name in ("Run 1 (3 replicas)", "Run 2 (3 replicas)"):
        text = (RUNS[name] / "b" / "sse-client.txt").read_text()
        reports[name] = json.loads(next(line for line in text.splitlines() if line.startswith("{")))
    fig, (left, right) = plt.subplots(1, 2, figsize=(9, 3.4))
    names = list(reports)
    matched = [reports[n]["matching_clients"] for n in names]
    left.bar(names, [2000] * 2, color="#e4ebf3", label="Target 2,000")
    left.bar(names, matched, color="#2a8b7b", label="Matched the database")
    for i, value in enumerate(matched):
        left.text(i, value + 30, f"{value:,}", ha="center")
    left.set_ylim(0, 2300)
    left.set_title("Clients whose final seat map matched PostgreSQL")
    left.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=2)
    width = 0.35
    palette = ("#9c661a", "#204ea8")
    for offset, name, color in zip((-width / 2, width / 2), names, palette, strict=True):
        report = reports[name]
        values = [report["snapshot_ms"]["p50"] / 1000, report["delta_apply_ms"]["p50"] / 1000]
        right.bar([x + offset for x in (0, 1)], values, width, color=color, label=name)
    right.set_xticks([0, 1], ["Snapshot p50", "Delta apply p50"])
    right.set_ylabel("Seconds (log scale)")
    right.set_yscale("log")
    right.set_title("Latency (lower is better)")
    right.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / "b-sse.png")
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    chart_outcomes()
    chart_seats()
    chart_timeline()
    chart_sse()
    print("charts written to", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
