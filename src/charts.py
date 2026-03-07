"""
Chart generation for weekly summary emails.
Uses matplotlib to produce inline PNG images (no files written to disk).
"""

from io import BytesIO
from typing import Optional

from config import KEY_LIFTS  # fallback when tracked_lifts not passed


def _get_plt():
    """Import matplotlib lazily (not needed on every run)."""
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend, safe for servers
    import matplotlib.pyplot as plt
    return plt


def generate_1rm_chart(lift_history: list[dict],
                        tracked_lifts: list[dict] = None) -> Optional[BytesIO]:
    """
    Generate a line chart of estimated 1RM over time for key lifts.
    Shows MAIN + AUXILIARY lifts. Falls back to KEY_LIFTS if tracked_lifts not provided.
    Returns a BytesIO PNG, or None if there's not enough data.
    """
    if tracked_lifts:
        lifts_to_chart = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                          if tl.get("lift_type", "MAIN") in ("MAIN", "AUXILIARY")]
    else:
        lifts_to_chart = KEY_LIFTS

    lift_data: dict[str, list[tuple[str, float]]] = {}

    for row in lift_history:
        ex_name = row.get("Exercise", "")
        est = row.get("Est 1RM", "")
        date_str = row.get("Date", "")
        if not est or not date_str:
            continue
        for _domain, lift in lifts_to_chart:
            if lift.lower() in ex_name.lower():
                try:
                    lift_data.setdefault(lift, []).append((date_str, float(est)))
                except (ValueError, TypeError):
                    pass

    series = {k: v for k, v in lift_data.items() if len(v) >= 2}
    if not series:
        return None

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(7, 3.5))

    colors = ["#2563eb", "#dc2626", "#16a34a", "#d97706"]
    for (lift, points), color in zip(series.items(), colors):
        dates = [p[0] for p in points]
        values = [p[1] for p in points]
        ax.plot(dates, values, marker="o", markersize=4, label=lift,
                color=color, linewidth=1.8)

        # Only show a few x-axis labels to avoid clutter
    step = max(1, len(next(iter(series.values()))) // 6)
    sample_dates = next(iter(series.values()))[::step]
    ax.set_xticks([p[0] for p in sample_dates])
    ax.set_xticklabels([p[0][:10] for p in sample_dates], rotation=30, ha="right", fontsize=8)

    ax.set_ylabel("Est. 1RM (kg)", fontsize=9)
    ax.set_title("Estimated 1RM Trajectory", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_volume_chart(recent_weeks: list[dict], current_week: Optional[dict] = None) -> Optional[BytesIO]:
    """
    Generate a bar chart of completed sessions (exercise count) per week.
    Returns a BytesIO PNG, or None if there's not enough data.
    """
    weeks = list(recent_weeks)
    if current_week:
        weeks = weeks + [current_week]

    if len(weeks) < 2:
        return None

    labels = []
    counts = []
    for w in weeks:
        wn = w.get("week_num", "?")
        labels.append(f"Wk {wn}")
        done = sum(
            1 for day in w.get("days", [])
            for ex in day.get("exercises", [])
            if ex.get("done") is True
        )
        counts.append(done)

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(7, 3))

    bars = ax.bar(labels, counts, color="#2563eb", alpha=0.75, width=0.55)
    for bar, count in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1,
                str(count), ha="center", va="bottom", fontsize=8)

    ax.set_ylabel("Exercises completed", fontsize=9)
    ax.set_title("Weekly Training Volume (completed sets)", fontsize=11, fontweight="bold")
    ax.set_ylim(0, max(counts) * 1.25 + 1)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_bodyweight_chart(health_log: list[dict]) -> Optional[BytesIO]:
    """
    Generate a scatter + rolling average chart of bodyweight over time.
    Returns a BytesIO PNG, or None if there's not enough data.
    """
    points = []
    for entry in health_log:
        d = entry.get("Date", "")
        bw = entry.get("Bodyweight (kg)", "")
        if d and bw:
            try:
                points.append((d, float(str(bw).replace(",", "."))))
            except (ValueError, TypeError):
                pass

    if len(points) < 3:
        return None

    points.sort(key=lambda x: x[0])
    dates = [p[0] for p in points]
    weights = [p[1] for p in points]

    # 7-day rolling average (simple)
    window = 7
    rolling = []
    for i in range(len(weights)):
        window_vals = weights[max(0, i - window + 1):i + 1]
        rolling.append(sum(window_vals) / len(window_vals))

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(7, 3))

    ax.scatter(dates, weights, color="#6b7280", s=18, alpha=0.5, label="Daily")
    ax.plot(dates, rolling, color="#2563eb", linewidth=2, label="7-day avg")

    step = max(1, len(dates) // 6)
    ax.set_xticks(dates[::step])
    ax.set_xticklabels([d[:10] for d in dates[::step]], rotation=30, ha="right", fontsize=8)

    ax.set_ylabel("kg", fontsize=9)
    ax.set_title("Bodyweight Trend", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf
