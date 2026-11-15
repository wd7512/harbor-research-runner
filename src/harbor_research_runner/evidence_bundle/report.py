import json
from datetime import UTC, datetime
from itertools import combinations

from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.statistics.completeness import completeness
from harbor_research_runner.statistics.paired import paired_difference
from harbor_research_runner.statistics.rates import arm_rates, trials_in_view
from harbor_research_runner.statistics.usage import label_counts, usage_medians

LABELS = (
    "timed_out",
    "exit_nonzero",
    "verifier_failed",
    "harness_could_not_start",
    "no_requests",
    "possible_bypass",
    "oom_killed",
)
VIEWS = (
    ("first_attempt", "First attempt"),
    ("final", "Final"),
    ("final_excluding_reruns", "Final excluding reruns"),
)
USAGE_LABELS = {
    "requests": "Requests",
    "prompt_tokens": "Prompt tokens",
    "completion_tokens": "Completion tokens",
    "prompt_chars": "Prompt characters",
    "completion_chars": "Completion characters",
    "turns": "Turns",
    "cost_usd_estimate": "Estimated cost (USD)",
}


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _task_source(metadata: dict) -> str:
    source = metadata.get("job", {}).get("tasks")
    if isinstance(source, dict) and source:
        return json.dumps(source, sort_keys=True, separators=(",", ":"))
    return metadata.get("task_set", {}).get("ref", "unknown")


def _number(value: float | int | None, precision: int = 2) -> str:
    return "—" if value is None else f"{value:.{precision}f}"


def _rate_cell(value) -> str:
    return f"{value.passes}/{value.trials}; {value.rate * 100:.1f}% [{value.low * 100:.1f}%, {value.high * 100:.1f}%]"


def render_report(
    rows: list[TrialRow],
    metadata: dict,
    events: list[dict],
    planned_slots: list[tuple[str, str]],
    repetitions: int,
    *,
    machine_check: MachineCheck,
) -> str:
    arms = [arm["label"] for arm in metadata["arms"]]
    task_names = sorted({task for task, _arm in planned_slots})
    excluded_tasks = machine_check.excluded_tasks()
    host = metadata.get("host", {})
    host_profile = host.get("host_profile", {})
    machine = (
        ", ".join(
            value
            for value in (
                host.get("fingerprint") or machine_check.host_fingerprint,
                host.get("arch"),
                f"memory_cap={host_profile['memory_cap']}"
                if host_profile.get("memory_cap")
                else None,
                f"rootless={str(host_profile['rootless']).lower()}"
                if "rootless" in host_profile
                else None,
            )
            if value
        )
        or "unknown"
    )
    report = [
        "# Run report",
        "",
        "## Run",
        "",
        f"- Run id: {metadata['run_id']}",
        f"- Tool version: {metadata['tool_version']}",
        f"- Model: {metadata['model_card']['id']}",
        f"- Task source: {_task_source(metadata)}",
        f"- Tasks: {len(task_names)} run; {len(excluded_tasks)} excluded by the machine check ({', '.join(excluded_tasks) or 'none'})",
        f"- Machine: {machine}",
        f"- Date: {_timestamp(metadata['created_t'])}",
        "",
        "## Completeness",
        "",
        "| Measure | Count |",
        "|---|---:|",
    ]
    complete = completeness(rows, planned_slots, repetitions)
    report.extend(
        [
            f"| Slots planned | {complete.slots_planned} |",
            f"| Slots complete | {complete.slots_complete} |",
            f"| Slots incomplete | {complete.slots_incomplete} |",
            f"| Valid trials | {complete.trials_valid} |",
            f"| Unmeasured trials | {complete.trials_unmeasured} |",
            f"| Tokens spent on set-aside trials | {complete.set_aside_tokens} |",
            "",
            "Set aside by reason:",
            "",
            "| Reason | Trials |",
            "|---|---:|",
        ]
    )
    if complete.set_aside_by_reason:
        report.extend(
            f"| {reason} | {count} |"
            for reason, count in sorted(complete.set_aside_by_reason.items())
        )
    else:
        report.append("| None | 0 |")
    report.extend(["", "Halt and resume history:", ""])
    history = [event for event in events if event.get("type") in ("halt", "resume")]
    if history:
        for event in history:
            detail = (
                f" — {event.get('reason')}: {event.get('detail')}"
                if event.get("type") == "halt"
                else f" — {event.get('tool_version')}"
            )
            report.append(f"- {_timestamp(event['t'])}: {event['type']}{detail}")
    else:
        report.append("- None")

    report.extend(["", "## Pass rate by arm", ""])
    report.append("| Arm | " + " | ".join(label for _view, label in VIEWS) + " |")
    report.append("|---|" + "---:|" * len(VIEWS))
    rates_by_view = {view: arm_rates(rows, view) for view, _label in VIEWS}
    for arm in arms:
        report.append(
            "| "
            + arm
            + " | "
            + " | ".join(
                _rate_cell(rates_by_view[view][arm]) if arm in rates_by_view[view] else "—"
                for view, _label in VIEWS
            )
            + " |"
        )

    report.extend(
        [
            "",
            "## Paired comparison",
            "",
            "Final view; difference is first arm minus second arm.",
            "",
        ]
    )
    report.append("| Arms | Paired tasks | Mean difference | 95% interval |")
    report.append("|---|---:|---:|---:|")
    for arm_a, arm_b in combinations(arms, 2):
        paired = paired_difference(rows, "final", arm_a, arm_b)
        interval = (
            "—"
            if paired.low is None or paired.high is None
            else f"[{paired.low:.3f}, {paired.high:.3f}]"
        )
        report.append(
            f"| {arm_a} − {arm_b} | {paired.tasks} | {_number(paired.mean_difference, 3)} | {interval} |"
        )
    if len(arms) < 2:
        report.append("| None | 0 | — | — |")

    report.extend(["", "## Usage by arm", "", "Final view; medians are per valid trial.", ""])
    report.append("| Arm | " + " | ".join(USAGE_LABELS.values()) + " |")
    report.append("|---|" + "---:|" * len(USAGE_LABELS))
    final_rows = trials_in_view(rows, "final")
    for arm in arms:
        medians = usage_medians(final_rows, arm)
        report.append(
            f"| {arm} | "
            + " | ".join(
                _number(medians[field], 6 if field == "cost_usd_estimate" else 2)
                for field in USAGE_LABELS
            )
            + " |"
        )

    report.extend(["", "## Outcome labels by arm", ""])
    report.append("| Arm | " + " | ".join(LABELS) + " |")
    report.append("|---|" + "---:|" * len(LABELS))
    for arm in arms:
        counts = label_counts(rows, arm)
        report.append(
            f"| {arm} | " + " | ".join(str(counts.get(label, 0)) for label in LABELS) + " |"
        )

    report.extend(["", "## Set-aside trials", "", "| Reason | Trials |", "|---|---:|"])
    if complete.set_aside_by_reason:
        report.extend(
            f"| {reason} | {count} |"
            for reason, count in sorted(complete.set_aside_by_reason.items())
        )
    else:
        report.append("| None | 0 |")
    report.extend(["", f"Tokens spent: {complete.set_aside_tokens}", ""])
    return "\n".join(report)
