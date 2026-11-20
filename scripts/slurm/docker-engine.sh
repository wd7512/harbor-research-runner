# shellcheck shell=bash disable=SC2154
# Sourced by the Slurm scripts in this folder: provides a Docker API inside one allocation.
# Expects $work (a job-local directory) and $log (a file for engine output) to be set.
# After start_docker_engine, DOCKER_HOST points at the engine and $engine_selected names it:
# existing, podman or rootless-docker. stop_docker_engine stops what was started.

engine_pid=""
engine_selected=none
docker_version="${HRR_DOCKER_VERSION:-29.9.3}"

docker_ready() {
    local _
    for _ in $(seq 1 60); do
        if docker info >/dev/null 2>&1; then return 0; fi
        if [ -n "$engine_pid" ] && ! kill -0 "$engine_pid" 2>/dev/null; then return 1; fi
        sleep 1
    done
    return 1
}

fetch_docker_release() {
    local arch
    arch="$(uname -m)"
    curl -fsSL --retry 2 --max-time 180 \
        "https://download.docker.com/linux/static/stable/$arch/docker-$docker_version.tgz" \
        -o "$work/docker.tgz" && tar -xzf "$work/docker.tgz" -C "$work" || return 1
    if [ "${1:-}" = rootless ]; then
        curl -fsSL --retry 2 --max-time 180 \
            "https://download.docker.com/linux/static/stable/$arch/docker-rootless-extras-$docker_version.tgz" \
            -o "$work/rootless.tgz" && tar -xzf "$work/rootless.tgz" -C "$work" || return 1
        export PATH="$work/docker-rootless-extras:$PATH"
    fi
    export PATH="$work/docker:$PATH"
}

start_existing_engine() {
    [ -n "${DOCKER_HOST:-}" ] || [ -S /var/run/docker.sock ] || return 1
    command -v docker >/dev/null 2>&1 || return 1
    docker info >/dev/null 2>&1
}

start_podman_engine() {
    command -v podman >/dev/null 2>&1 || return 1
    command -v docker >/dev/null 2>&1 || fetch_docker_release || return 1
    export DOCKER_HOST="unix://$work/run/podman.sock"
    podman system service --time=0 "$DOCKER_HOST" >>"$log" 2>&1 &
    engine_pid=$!
    docker_ready
}

start_rootless_docker_engine() {
    fetch_docker_release rootless || return 1
    mkdir -p "$work/run" "$work/data" "$work/exec"
    export XDG_RUNTIME_DIR="$work/run"
    export DOCKER_HOST="unix://$work/run/docker.sock"
    export DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=false
    export DOCKERD_ROOTLESS_ROOTLESSKIT_DISABLE_HOST_LOOPBACK=false
    export PATH="$PATH:/usr/sbin:/sbin"
    printf '{}\n' >"$work/daemon.json"
    dockerd-rootless.sh --exec-opt native.cgroupdriver=cgroupfs --iptables=false \
        --ip6tables=false --bridge=none --ip-forward=false --ip-masq=false \
        --host "$DOCKER_HOST" --data-root "$work/data" --exec-root "$work/exec" \
        --pidfile "$work/run/docker.pid" --config-file "$work/daemon.json" \
        --storage-driver vfs >>"$log" 2>&1 &
    engine_pid=$!
    docker_ready
}

stop_docker_engine() {
    if [ -n "$engine_pid" ]; then
        kill "$engine_pid" 2>/dev/null
        wait "$engine_pid" 2>/dev/null
        engine_pid=""
    fi
}

start_docker_engine() {
    case "${HRR_ENGINE:-auto}" in
        existing) start_existing_engine && engine_selected=existing ;;
        podman) start_podman_engine && engine_selected=podman ;;
        rootless-docker) start_rootless_docker_engine && engine_selected=rootless-docker ;;
        auto)
            if start_existing_engine; then
                engine_selected=existing
            elif start_podman_engine; then
                engine_selected=podman
            else
                stop_docker_engine
                unset DOCKER_HOST
                start_rootless_docker_engine && engine_selected=rootless-docker
            fi
            ;;
        *) printf 'unknown HRR_ENGINE %s\n' "$HRR_ENGINE" >>"$log" ;;
    esac
    [ "$engine_selected" != none ]
}
