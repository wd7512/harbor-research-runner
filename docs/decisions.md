# Decision log

Rulings agreed between the owner and the advisor on 11 November 2026. This file carries no issue numbers or private paths, so it can be copied into the new repo as `docs/decisions.md`. To change a ruling, edit its entry and add a dated line saying why.

## What a result means

**D1. Estimand.** A study measures the effect of the harness with the model service held nominal. Upstream variation is a confound. It is controlled first by design (the arms for one task launch next to each other in time) and second by re-running invalid trials.

**D2. Upstream-invalid.** A trial tolerates upstream errors that reached the harness, provided the harness continued and the errors number at most one, or 2% of the trial's requests if that is larger. A trial is upstream-invalid when it exceeds that, when its last request errored, or when its overall output rate falls below the declared floor. Every threshold is an absolute number declared in the job file before the run. There are no percentile rules. An error the proxy absorbed by failover is recorded and does not count.

**D3. Re-run unit.** An invalid trial is re-run as a single trial, whatever its outcome. The original is kept and labelled. Partner arms are not re-run.

**D4. Clean failures are final.** A failure with clean instruments is never re-run. Nothing is retried because of its exception type.

**D5. Halting.** A run halts only on instrument evidence: the provider is no longer nominal, the proxy cannot record, the host is below its declared limits, or nothing has happened for longer than the longest task could take. Harbor's exception labels never halt a run and never trigger a re-run; some of them are text matches on agent output.

**D6. Invalid trial.** A trial is invalid only when an instrument independent of the agent's own output recorded interference during it. There are three instruments: the proxy ledger (upstream), Harbor's per-phase record (the trial never reached or finished the agent phase), and the host resource log (host fault). Validity never reads the reward. A container killed for exceeding the task's own memory limit is a valid failure. A harness that crashes before its first request is a valid failure. A harness setup failure that repeats on re-run is a valid failure labelled "harness could not start". A trial that ends with no grader verdict for a reason outside the agent is "no verdict": it is re-run and counted as a tool defect.

**D7. Task-level problems.** A task's own external dependencies are not per-trial invalidity. An oracle check on each machine decides the task set before any spend. Results cover a subset declared before the run. The runner carries no task-repair machinery.

**D8. Outcome.** The outcome is the grader's reward on the task state at the deadline. A non-zero agent exit with reward 1.0 is a pass; the exit code is reported separately. The deadline is enforced by closing the trial at the proxy when Harbor's agent phase ends, plus an in-container stop of the harness process tree. There is no outcome rule for timeouts.

**D9. Verifier failures.** A verifier timeout or error on a healthy host stands as a failure. A host fault during verification invalidates the trial. The verifier time multiplier is set per machine from the oracle check and declared before the run.

## How a run behaves

**D10. Refuse, halt, invalidate, mark.** A check may refuse to start, halt the run, invalidate a trial, or mark a trial in the evidence bundle. No research-validity check withholds the evidence bundle. Measurement faults stop the run when they happen. `hrr run` exits 0 only when every slot has its full number of valid, measured trials. The one check that may refuse to write an evidence bundle is the secret scan.

**D11. Re-run mechanics.** Harbor runs as a subprocess. Before every resume, a set-aside step moves every unfinished or invalid trial folder out of Harbor's job folder; Harbor then re-runs those slots. Set-aside folders are kept for investigation. `run` judges each trial once and records the verdict; nothing else decides validity. A slot is a task and an arm; set-asides are capped per slot.

**D12. Storage.** The proxy keeps one append-only ledger: two rows per request (start and end) plus open, close and refused rows per trial, with gapless sequence numbers. Request and response bodies are kept per trial on the run host. There is no database in the first version; the evidence bundle is plain files.

**D13. One proxy per run**, with in-memory pacing across all arms. One job runs at a time per machine. File names are fixed in `architecture.md`.

**D14. Results explorer.** Harbor's viewer is used for per-trial drill-down on the run host. Our explorer is read-only: comparison across arms and evidence bundles, a per-trial request series, a completeness table and a pass-rate-over-time chart. Finished run folders are made read-only.

## Scope

**D15. Old repo.** The old repo freezes at 0.15.4. Its parked milestone is not finished. No paid run happens on old code. The first paid study is the new tool's acceptance run, after end-to-end tests, a free oracle sweep and a free-model pilot.

**D16. Task fixes.** The tool is benchmark-agnostic. Task fixes live in a separate pinned task repository. A fix changes plumbing only, never the instruction or what the tests accept, and is proven by the oracle scoring 1.0 and a do-nothing agent scoring 0.0. A task not fixed within about an hour is excluded by the declared rule. Results state the patched and excluded counts.

**D17. Harness seam.** OpenCode is the only harness shipped. `PinnedHarness` is an abstract base with four methods; version-era logic is plain functions. A harness is identified by its upstream git commit; local image digests are a per-machine check. Trial identity travels in the proxy URL path. A scripted fake harness exists as a test fixture. HPC support is a later milestone.

**D18. Tests.** Four tiers: unit, integration, end-to-end (including contract tests that pin Harbor's behaviour) and live. Behaviours guarded in the old repo that still exist stay guarded; old tests are carried as cases. Every past incident becomes a named test before its code is written. Field proof on real run data is no longer required per change.

**D19. Process.** Changelog fragments replace the hand-kept worklog and changelog. Milestones replace the roadmap file. The gate, hygiene checks and the worker-delegation tool live in a separate tools repository used as a pinned development dependency.

**D20. Structure.** Six steps in flow order and five shared helpers, with the import rule in `architecture.md`. The proxy records facts and writes one upstream status per request; judgement lives in `trial_validity`. There are no override environment variables in the first version. A model card pins the exact upstream model. Job configs, evidence bundles and the paper live in a private study repository.

**D21. Carried and dropped.** Carried: the withdrawn list, the inactivity watchdog, the canary, the launch gate on spare memory, the secret scan. Lifecycle-critical code (process-group stop, Docker cleanup, in-container stop script, image extraction) is ported with its tests, not rewritten. Dropped: wave retry and its selector, the reconcile gates and their declarations, the capture database, the evidence pull-request command, the request query command, lineage inference, the trajectory export command, the windowed collapse check, the sequential-question agent, and all legacy guards.

**D22. Host ownership.** One job per host is enforced by a kernel advisory lock, not by scanning run folders. `docker_host/host_lock.py` takes an exclusive, non-blocking `flock` on the fixed node-local path `/var/tmp/hrr-host.lock`. `hrr run`, `hrr run --resume`, `hrr check-machine` and `hrr clean-docker --apply` acquire it before writing any event or creating any run folder, and hold it until the process exits. Acquisition never waits: a lock held by another process is a refusal (launch check `single_run` false, exit 1), and a platform or filesystem that does not enforce the lock, detected by a second-descriptor probe, is also a refusal, never a no-op fallback. The lock file is never unlinked, truncated or replaced, and its descriptor is not inherited by children, so a killed runner releases it through the kernel. No environment variable moves the path (D20). Reading run events can name the holder in a refusal message but never decides liveness. A resume acquires the lock, reads the run metadata, repeats the launch checks, starts the services and only then writes `resume`; a refused resume appends nothing. `clean-docker --apply` inventories and plans while holding the lock; the dry run is a lock-free snapshot.
