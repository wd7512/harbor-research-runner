# Test fixtures

Every file in this folder has one line here saying where it came from. No file over 200 KB.

| File | Source |
|---|---|
| `proc-vmstat-linux.txt` | Whole output of `docker run --rm alpine cat /proc/vmstat` from Docker Desktop's VM, Linux kernel `7.0.12-linuxkit`, captured 13 Nov 2026; `pswpout` is 0. |
| `vm_stat-macos.txt` | Whole output of `vm_stat` captured on the Mac on 13 Nov 2026. |
| `opencode_eras/legacy-v1.0.json` | Ported from OLD `tests/fixtures/opencode_eras/legacy-v1.0.json`; the proxy URL is trial-scoped and the `x-trial-id` header is removed. |
| `opencode_eras/legacy-v1.0.cmd` | Ported from OLD `tests/fixtures/opencode_eras/legacy-v1.0.cmd`. |
| `opencode_eras/modern.json` | Ported from OLD `tests/fixtures/opencode_eras/modern.json`; the proxy URL is trial-scoped and the `x-trial-id` header is removed. |
| `opencode_eras/modern.cmd` | Ported from OLD `tests/fixtures/opencode_eras/modern.cmd`. |
| `opencode_eras/modern-1.19.3.json` | Ported from OLD `tests/fixtures/opencode_eras/modern-1.19.3.json`; the proxy URL is trial-scoped and the `x-trial-id` header is removed. |
| `opencode_eras/modern-1.19.3.cmd` | Ported from OLD `tests/fixtures/opencode_eras/modern-1.19.3.cmd`. |
| `opencode_eras/2x.json` | Ported from OLD `tests/fixtures/opencode_eras/2x.json`; the proxy URL is trial-scoped and the `x-trial-id` header is removed. |
| `opencode_eras/2x.cmd` | Ported from OLD `tests/fixtures/opencode_eras/2x.cmd`. |
| `opencode_eras/opencode-1.19.5-global-help.txt` | Captured 13 Nov 2026 from the hrr-built arm64 image of upstream commit `810c0eba1a7348036f8bdf61e6632d0c36c6876c` (v1.19.5), running `/usr/local/bin/opencode --help` with stderr merged. |
| `opencode_eras/opencode-1.19.5-run-help.txt` | Captured 13 Nov 2026 from the hrr-built arm64 image of upstream commit `810c0eba1a7348036f8bdf61e6632d0c36c6876c` (v1.19.5), running `/usr/local/bin/opencode run --help` with stderr merged. |
| `opencode_eras/opencode-2.2.7-global-help.txt` | Captured 13 Nov 2026 from the hrr-built arm64 image of upstream commit `fedc6f208aa2bd54c1019227214568083f6380ad` (v2.2.7), running `/usr/local/bin/opencode --help` with stderr merged. |
| `opencode_eras/opencode-2.2.7-run-help.txt` | Captured 13 Nov 2026 from the hrr-built arm64 image of upstream commit `fedc6f208aa2bd54c1019227214568083f6380ad` (v2.2.7), running `/usr/local/bin/opencode run --help` with stderr merged. |
| `opencode_build/opencode-1.19.5-listing.txt` | `tar -tzf` archive entries for the public upstream commit `810c0eba1a7348036f8bdf61e6632d0c36c6876c` (v1.19.5) of `https://github.com/anomalyco/opencode`. |
| `harbor_job/README.md` | Provenance and redaction notes for approved Harbor 0.23.0 samples from the two source runs listed in the file. |
| `harbor_job/config.json` | Trimmed Harbor job config from the approved 12 Nov 2026 sample; product agent settings substituted as documented in `harbor_job/README.md`. |
| `harbor_job/result.json` | Trimmed Harbor job result from the approved 12 Nov 2026 sample. |
| `harbor_job/lock.json` | Trimmed Harbor job lock with the four selected task locks from the approved 12 Nov 2026 sample. |
| `harbor_job/pass-bn-fit/config.json` | Trimmed Harbor trial config from the pass sample; agent settings use the product shape documented in `harbor_job/README.md`. |
| `harbor_job/pass-bn-fit/result.json` | Trimmed Harbor trial result from the pass sample. |
| `harbor_job/pass-bn-fit/lock.json` | Trimmed Harbor trial lock from the pass sample. |
| `harbor_job/pass-bn-fit/agent/trajectory.json` | Structure-trimmed trajectory from the redacted pass sample; free text is replaced and counted entries are retained. |
| `harbor_job/fail-break-filter/config.json` | Trimmed Harbor trial config from the failed-trial sample; agent settings use the product shape documented in `harbor_job/README.md`. |
| `harbor_job/fail-break-filter/result.json` | Trimmed Harbor trial result from the failed-trial sample. |
| `harbor_job/fail-break-filter/lock.json` | Trimmed Harbor trial lock from the failed-trial sample. |
| `harbor_job/timeout-git-multibranch-2027run/config.json` | Trimmed Harbor trial config from the timeout sample; agent settings use the product shape documented in `harbor_job/README.md`. |
| `harbor_job/timeout-git-multibranch-2027run/result.json` | Trimmed Harbor trial result from the timeout sample. |
| `harbor_job/timeout-git-multibranch-2027run/lock.json` | Trimmed Harbor trial lock from the timeout sample. |
| `harbor_job/nonzeroexit-pytorch-2027run/config.json` | Trimmed Harbor trial config from the nonzero-exit sample; agent settings use the product shape documented in `harbor_job/README.md`. |
| `harbor_job/nonzeroexit-pytorch-2027run/result.json` | Trimmed Harbor trial result from the nonzero-exit sample. |
| `harbor_job/nonzeroexit-pytorch-2027run/lock.json` | Trimmed Harbor trial lock from the nonzero-exit sample. |
| `harbor_job/task-tomls/bn-fit-modify.task.toml` | Trimmed timeout fields from the approved Harbor dataset-cache sample for `terminal-bench/bn-fit-modify`. |
| `harbor_job/task-tomls/break-filter-js-from-html.task.toml` | Trimmed timeout fields from the approved Harbor dataset-cache sample for `terminal-bench/break-filter-js-from-html`. |
| `harbor_job/task-tomls/git-multibranch.task.toml` | Trimmed timeout fields from the approved Harbor dataset-cache sample for `terminal-bench/git-multibranch`. |
| `harbor_job/task-tomls/pytorch-model-cli.task.toml` | Trimmed timeout fields from the approved Harbor dataset-cache sample for `terminal-bench/pytorch-model-cli`. |
| `oom/docker-oom-events.jsonl` | Two real Docker Desktop OOM events from Harbor 0.23.0 oracle trials on 13 Nov 2026; `config_files` and host paths redacted. |
| `oom/agent-result.json` | Real Harbor 0.23.0 agent-phase OOM trial result from 13 Nov 2026; host paths redacted. |
| `oom/verifier-result.json` | Real Harbor 0.23.0 verifier-phase OOM trial result from 13 Nov 2026; host paths redacted. |
| `docker_info/docker-desktop-macos.json` | Profile-relevant fields from `docker info --format '{{json .}}'` on Docker Desktop 29.8.0, aarch64, 13 Nov 2026; ID redacted and name generic. |
| `docker_info/rootless.json` | Hand-written Docker info variant for a rootless daemon with no memory or swap limits and cgroup v2. |
| `docker_info/no-cgroup.json` | Hand-written Docker info variant with no reported cgroup version. |

- `run_folders/`: hand-made compact run-folder samples for `evidence_bundle.trials`, covering a clean two-task/two-arm run and a set-aside, unmeasured, unfinished, and canary trial.
- `run_folders/{clean,retries}/machine-check.json` and `expected-report.md`: hand-made five-task machine-check records and the resulting evidence report goldens; each includes three tasks and excludes two.
