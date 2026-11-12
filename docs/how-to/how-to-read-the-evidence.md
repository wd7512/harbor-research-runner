# How to read the evidence

An evidence bundle is a plain directory produced from a run folder. Start with `manifest.json` to check the bundle files and `report.md` for the summary.

1. **Check the manifest and report.** `manifest.json` lists each file's byte length and SHA-256 digest. `report.md` summarizes completeness, pass rates, paired results, usage, labels, set-aside reasons, and halt/resume history.

2. **Inspect the source records.** The run metadata and event log preserve the job, pinned model and task sources, machine information, launch checks, Harbor passes, trial verdicts, halts, resumes, and finish status.

- `trials.jsonl` has one row per canary, main, and set-aside trial. It records task, arm, reward, phase times, validity, labels, token use, request counts, and other measured behaviour.
- `proxy-requests.jsonl.gz` merges each request's start and end ledger rows, ordered by request start. It is useful for inspecting status, timing, token counts, model served, and failovers.
- `resource-stats.jsonl.gz` contains the host resource samples.
- `run-events.jsonl` records launch checks and run lifecycle events.
- `run-metadata.json` is the immutable record of the job, model card, arms, task set, machine check, host, and limits.

JSONL files contain one UTF-8 JSON object per line. Gzip files are compressed JSONL.

3. **Compare the three pass-rate views.** The report presents these views together for every arm:

| View | Trials included | What it answers |
|---|---|---|
| `first_attempt` | The first planned number of attempts per task and arm. An invalid first attempt counts with its observed reward; a missing reward counts as a failure. | What happened on the initial attempts before reruns changed the sample? |
| `final` | Valid trials still in `harbor-job/`. | What pass rate do the final valid trials support? |
| `final_excluding_reruns` | Final trials only for task-arm slots with no set-aside trial. | What does the result look like where no rerun was needed? |

   Each rate is shown as passes/trials, percentage, and a Wilson 95% interval. The paired comparison reports the final-view mean reward difference by task and a bootstrap 95% interval across tasks.

4. **Read validity reasons and measurement status.** The first matching validity rule decides the verdict. A reason is present on an invalid trial; valid trials may instead have labels.

| Reason | Meaning |
|---|---|
| `never_finished` | Harbor has no completed result for the trial. |
| `cancelled` | Harbor recorded `CancelledError`. |
| `environment_failed` | The environment setup phase failed. |
| `harness_setup_failed` | Harness setup failed, and this was not a repeated setup failure after a set-aside. |
| `host_fault` | A resource sample during the agent or verifier window breached a host limit or reported Docker unhealthy. |
| `upstream_terminal_error` | The last recorded request ended with an upstream status that counts against the trial. |
| `upstream_errors` | Counted upstream errors exceeded the configured tolerance. |
| `upstream_slow` | Successful responses with completion tokens had an aggregate output rate below the configured floor. |
| `no_verdict` | There is no grader reward and the failure was not in the verifier phase. |

   The instrument identifies the source of an invalid verdict: Harbor, the host, or the proxy. A trial can also be unmeasured. The `unmeasured_reason` values are `request_not_recorded` (a request start has no matching end although the trial closed), `ledger_damaged` (a ledger line or sequence gap falls within the trial), and `no_open_row` (requests exist without a trial-open row). Unmeasured trials are not rerun and prevent a run from being complete.

5. **Interpret labels on valid trials.**

| Label | Meaning |
|---|---|
| `harness_could_not_start` | Harness setup failed again after the setup failure had already been set aside; it is now a valid failure. |
| `timed_out` | Harbor reported `AgentTimeoutError`. |
| `exit_nonzero` | An exception occurred in the agent phase other than `AgentTimeoutError`. |
| `verifier_failed` | The verifier phase failed; this is a valid failure with reward 0. |
| `no_requests` | The trial has no `request_start` row. |
| `possible_bypass` | Harbor reports tokens but the proxy has no request end rows. |
| `refused_after_close` | The proxy refused one or more requests because the trial was already closed. |

   The report's label summary shows timed out, nonzero exit, verifier failed, harness could not start, no requests, and possible bypass counts. Inspect `trials.jsonl` for any `refused_after_close` labels.
