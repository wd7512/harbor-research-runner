#!/bin/bash
#SBATCH --job-name=hrr-probe
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --time=00:45:00
#SBATCH --output=hrr-probe-%j.out

# Answers whether a Slurm compute node can run hrr, in one allocation.
# Submit from a login node, adding the site's partition and account options:
#   sbatch --partition=PARTITION scripts/slurm/probe-host.sbatch
# Optional environment:
#   HRR_PROBE_DIR       persistent directory for the receipt (default: the submit directory)
#   HRR_GATEWAY_URL     model gateway base URL to test for reachability (no key is sent)
#   HRR_ENGINE          auto (default), existing, podman or rootless-docker
#   HRR_STORAGE_DRIVER  auto (default), overlay2 first then vfs, or a specific driver
#   HRR_DOCKER_VERSION  static Docker release for rootless-docker (default 29.9.3)
#   HRR_COMPOSE_VERSION Compose CLI plugin release (default 2.39.4)
#   HRR_REF             hrr git ref to install and run check-machine with (default: skip)
# Every check runs even when an earlier one fails; results go to summary.txt.

set -u
umask 077
probe_root="${HRR_PROBE_DIR:-${SLURM_SUBMIT_DIR:-$PWD}}"
job_id="${SLURM_JOB_ID:-local-$$}"
receipt="$probe_root/hrr-probe-$job_id"
work="/tmp/hrr-probe-$job_id"
script_dir="${HRR_SLURM_DIR:-}"
if [ -z "$script_dir" ] && [ -n "${SLURM_JOB_ID:-}" ]; then
    script_dir="$(dirname "$(scontrol show job "$SLURM_JOB_ID" | sed -n 's/^ *Command=//p')")"
fi
script_dir="${script_dir:-$(dirname "$0")}"
mkdir -p "$receipt" "$work/run" "$work/data" "$work/exec" "$work/client" "$work/tmp" "$work/bin"
summary="$receipt/summary.txt"
: >"$summary"
export TMPDIR="$work/tmp"
export DOCKER_CONFIG="$work/client"
export PATH="$work/bin:$PATH:/usr/sbin:/sbin"

record() {
    printf '%s=%s\n' "$1" "$2" >>"$summary"
}

check() {
    local name="$1"
    shift
    "$@" >"$receipt/$name.log" 2>&1
    local rc=$?
    record "$name" "$rc"
    return "$rc"
}

helper_pid=""
cleanup() {
    if [ -n "$helper_pid" ]; then kill "$helper_pid" 2>/dev/null; fi
    if command -v docker >/dev/null 2>&1 && [ -n "${DOCKER_HOST:-}" ]; then
        docker ps -aq --filter label=hrr-probe=1 | xargs -r docker rm -f >/dev/null 2>&1
    fi
    stop_docker_engine
    chmod -R u+w "$work" 2>/dev/null
    rm -rf "$work"
}
trap cleanup EXIT

# 1. Host facts
{
    hostname
    uname -a
    nproc
    free -m
    printf 'cgroup_fs=%s\n' "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)"
    printf 'cgroup_self=%s\n' "$(cat /proc/self/cgroup 2>/dev/null)"
    printf 'max_user_namespaces=%s\n' "$(cat /proc/sys/user/max_user_namespaces 2>/dev/null)"
    printf 'subuid=%s\n' "$(grep -c "^$(id -un):" /etc/subuid 2>/dev/null)"
    printf 'subgid=%s\n' "$(grep -c "^$(id -un):" /etc/subgid 2>/dev/null)"
    for tool in newuidmap newgidmap slirp4netns fuse-overlayfs docker podman podman-hpc apptainer singularity uv curl git python3; do
        printf 'tool %s=%s\n' "$tool" "$(command -v "$tool" || echo missing)"
    done
    env | grep -E '^SLURM_(JOB_ID|JOB_PARTITION|CPUS_ON_NODE|MEM_PER_NODE|JOB_END_TIME)=' | sort
    squeue -h -j "$job_id" -o 'end=%e limit=%l' 2>/dev/null
} >"$receipt/host.log" 2>&1
record arch "$(uname -m)"
record cpus "$(nproc)"
record memory_mb "$(free -m | awk '/^Mem:/ {print $2}')"
record cgroup_fs "$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo unknown)"

# 2. Network reachability from the compute node
http_code() {
    curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 10 --max-time 20 "$1" 2>>"$receipt/network.log" || true
}
record net_docker_hub "$(http_code https://registry-1.docker.io/v2/)"
record net_ghcr "$(http_code https://ghcr.io/v2/)"
record net_github "$(http_code https://github.com)"
record net_pypi "$(http_code https://pypi.org/simple/)"
if [ -n "${HRR_GATEWAY_URL:-}" ]; then
    record net_gateway "$(http_code "$HRR_GATEWAY_URL")"
fi

# 3. A Docker API: an existing socket, Podman's Docker-compatible service, or rootless Docker
log="$receipt/engine.log"
# shellcheck source=scripts/slurm/docker-engine.sh
. "$script_dir/docker-engine.sh"
start_docker_engine
selected="$engine_selected"
record engine "$selected"
record storage_driver "$engine_storage_driver"
if [ "$selected" = none ]; then
    record verdict "no-docker-api"
    cat "$summary"
    exit 1
fi
check compose docker compose version

docker info --format '{{json .}}' >"$receipt/docker-info.json" 2>"$receipt/docker-info.err"
docker network ls >"$receipt/docker-networks.log" 2>&1
record memory_limit "$(docker info --format '{{.MemoryLimit}}' 2>/dev/null)"
record cgroup_version "$(docker info --format '{{.CgroupVersion}}' 2>/dev/null)"
record server_version "$(docker info --format '{{.ServerVersion}}' 2>/dev/null)"
record bridge_network "$(docker network ls --format '{{.Name}}' 2>/dev/null | grep -cx bridge)"

# 4. Containers: pull, run, network modes, memory cap, bind mounts
image=busybox:1.36
run_probe() {
    docker run --rm --label hrr-probe=1 "$@"
}
check pull docker pull "$image"
check run_host_network run_probe --network host "$image" true
check run_default_network run_probe "$image" true
check memory_cap_applied run_probe --network host --memory 64m "$image" \
    sh -c 'cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes'
check memory_cap_enforced run_probe --network host --memory 64m "$image" \
    sh -c 'head -c 200m /dev/zero | tail >/dev/null'
if [ "$(sed -n 's/^memory_cap_enforced=//p' "$summary")" = 137 ]; then
    record memory_cap "kernel"
else
    record memory_cap "none (the hrr memory watchdog must enforce task limits)"
fi
mkdir -p "$work/mount" "$receipt/mount"
check bind_mount_job_local run_probe --network host -v "$work/mount:/m" "$image" \
    sh -c 'echo ok >/m/f && cat /m/f'
check bind_mount_persistent run_probe --network host -v "$receipt/mount:/m" "$image" \
    sh -c 'echo ok >/m/f && cat /m/f'

# 5. Can a container reach a service on the job host (the hrr proxy's path)?
port=$((20000 + RANDOM % 20000))
python3 -m http.server "$port" --bind 0.0.0.0 --directory "$receipt" >/dev/null 2>&1 &
helper_pid=$!
sleep 2
for address in 127.0.0.1 10.0.2.2; do
    check "reach_host_$address" run_probe --network host \
        "$image" wget -q -O /dev/null -T 5 "http://$address:$port/summary.txt"
done
check reach_host_gateway run_probe --add-host host.docker.internal:host-gateway \
    "$image" wget -q -O /dev/null -T 5 "http://host.docker.internal:$port/summary.txt"

# 6. Optional: install hrr and run its machine check against this engine
if [ -n "${HRR_REF:-}" ]; then
    if command -v uv >/dev/null 2>&1; then
        export UV_TOOL_DIR="$work/uv-tools" UV_TOOL_BIN_DIR="$work/bin"
        check hrr_install uv tool install \
            "harbor-research-runner @ git+https://github.com/wd7512/harbor-research-runner@$HRR_REF"
        check hrr_version hrr --version
    else
        record hrr_install "uv-missing"
    fi
fi

record verdict "probed"
cat "$summary"
