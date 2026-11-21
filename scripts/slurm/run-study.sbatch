#!/bin/bash
#SBATCH --job-name=hrr-study
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --exclusive
#SBATCH --time=24:00:00
#SBATCH --output=hrr-study-%j.out

# Runs, or resumes, one hrr study inside one Slurm allocation. hrr stops launching
# trials HRR_STOP_MARGIN_MIN minutes before the allocation ends and exits 2 when the
# run is resumable; submit this script again to continue. Run the probe first
# (probe-host.sbatch) and read docs/how-to/how-to-run-on-a-slurm-cluster.md.
# Required environment:
#   HRR_STUDY_DIR   study folder on persistent storage; hrr runs from here
#   HRR_REF         hrr release tag to install, e.g. v0.1.1
#   HRR_ENV_FILE    file (mode 600) of KEY=value lines naming the model card's key_env
# Optional environment:
#   HRR_JOB              job file relative to HRR_STUDY_DIR (default study.yaml)
#   HRR_RUNS_DIR         runs folder relative to HRR_STUDY_DIR (default runs)
#   HRR_EVIDENCE_DIR     evidence folder relative to HRR_STUDY_DIR (default evidence)
#   HRR_RUNS_LOCAL       1 keeps the runs folder on node-local /tmp during the job and
#                        copies it back at the end, for storage containers cannot mount
#   HRR_STOP_MARGIN_MIN  minutes before the allocation ends to stop launching (default 120)
#   HRR_ENGINE           auto (default), existing, podman or rootless-docker
#   HRR_DOCKER_VERSION   static Docker release for rootless-docker (default 29.9.3)
#   HRR_COMPOSE_VERSION  Compose CLI plugin release (default 2.39.4)
#   HRR_REGISTRY_AUTH    registry credentials file (mode 600), when registry login is needed
#   HRR_IMAGE_CACHE      persistent folder for Docker image tar archives

set -eu
umask 077
study="${HRR_STUDY_DIR:?set HRR_STUDY_DIR}"
ref="${HRR_REF:?set HRR_REF to an hrr release tag}"
env_file="${HRR_ENV_FILE:?set HRR_ENV_FILE}"
job_file="${HRR_JOB:-study.yaml}"
runs_rel="${HRR_RUNS_DIR:-runs}"
evidence_rel="${HRR_EVIDENCE_DIR:-evidence}"
margin_min="${HRR_STOP_MARGIN_MIN:-120}"
job_id="${SLURM_JOB_ID:?run this under Slurm}"
script_dir="${HRR_SLURM_DIR:-$(dirname "$(scontrol show job "$job_id" | sed -n 's/^ *Command=//p')")}"
work="/tmp/hrr-study-$job_id"
log_dir="$study/slurm-logs/$job_id"
log="$log_dir/engine.log"
state="$study/.hrr-run-id"
mkdir -p "$work/tmp" "$work/bin" "$log_dir"
export TMPDIR="$work/tmp"
export PATH="$work/bin:$PATH"
export DOCKER_CONFIG="$work/docker-config"

if [ "$(stat -c %a "$env_file")" != 600 ]; then
    printf 'refusing: %s must have mode 600\n' "$env_file" >&2
    exit 1
fi

end_epoch="${SLURM_JOB_END_TIME:-}"
if [ -z "$end_epoch" ]; then
    end_epoch="$(date -d "$(squeue -h -j "$job_id" -o %e)" +%s)"
fi
stop_at="$(date -d "@$((end_epoch - margin_min * 60))" --iso-8601=seconds)"
if [ "$((end_epoch - margin_min * 60))" -le "$(date +%s)" ]; then
    printf 'refusing: the allocation ends within HRR_STOP_MARGIN_MIN (%s min)\n' "$margin_min" >&2
    exit 1
fi

runs_dir="$study/$runs_rel"
if [ "${HRR_RUNS_LOCAL:-0}" = 1 ]; then
    runs_dir="/tmp/hrr-runs/$(basename "$study")"
    mkdir -p "$runs_dir" "$study/$runs_rel"
    cp -a "$study/$runs_rel/." "$runs_dir/"
fi

# shellcheck source=scripts/slurm/docker-engine.sh
. "$script_dir/docker-engine.sh"
# shellcheck source=scripts/slurm/registry.sh
. "$script_dir/registry.sh"
cleanup() {
    rc=$?
    trap - EXIT
    if [ -n "${HRR_IMAGE_CACHE:-}" ]; then save_image_cache "$HRR_IMAGE_CACHE"; fi
    stop_docker_engine
    if [ "${HRR_RUNS_LOCAL:-0}" = 1 ]; then
        cp -a "$runs_dir/." "$study/$runs_rel/"
    fi
    chmod -R u+w "$work" 2>/dev/null
    rm -rf "$work"
    printf 'job=%s exit=%s engine=%s stop_at=%s\n' "$job_id" "$rc" "$engine_selected" "$stop_at" \
        >>"$study/slurm-logs/jobs.txt"
    exit "$rc"
}
trap cleanup EXIT
if ! start_docker_engine; then
    printf 'no Docker API or Compose plugin on this node; see %s\n' "$log" >&2
    exit 1
fi
if [ -n "${HRR_REGISTRY_AUTH:-}" ]; then registry_login "$HRR_REGISTRY_AUTH"; fi
if [ -n "${HRR_IMAGE_CACHE:-}" ]; then load_image_cache "$HRR_IMAGE_CACHE"; fi

export UV_TOOL_DIR="$work/uv-tools" UV_TOOL_BIN_DIR="$work/bin"
uv tool install --quiet "harbor-research-runner @ git+https://github.com/wd7512/harbor-research-runner@$ref"
hrr --version

set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

cd "$study"
set +e
if [ -s "$state" ]; then
    run_id="$(cat "$state")"
    hrr run --resume "$run_id" --runs-dir "$runs_dir" --evidence-dir "$evidence_rel" \
        --stop-launching-at "$stop_at"
    rc=$?
else
    before="$(ls -1 "$runs_dir" 2>/dev/null || true)"
    hrr run "$job_file" --runs-dir "$runs_dir" --evidence-dir "$evidence_rel" \
        --stop-launching-at "$stop_at"
    rc=$?
    run_id="$(comm -13 <(printf '%s\n' "$before" | sort) <(ls -1 "$runs_dir" 2>/dev/null | sort) | tail -1)"
    if [ "$rc" = 2 ] && [ -n "$run_id" ]; then printf '%s\n' "$run_id" >"$state"; fi
fi
set -e

case "$rc" in
    0) printf 'run %s complete\n' "$run_id"; rm -f "$state" ;;
    2) printf 'run %s is resumable: submit this script again to continue\n' "$run_id" ;;
    3) printf 'run %s finished incomplete; see its evidence\n' "$run_id"; rm -f "$state" ;;
    *) printf 'hrr exited %s for run %s\n' "$rc" "${run_id:-none}" ;;
esac
exit "$rc"
