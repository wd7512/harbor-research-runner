# shellcheck shell=bash disable=SC2154
# Sourced by the Slurm scripts in this folder: provides a Docker API and Compose plugin inside one allocation.
# Rootless Docker exec inherits the daemon's umask, so runc needs a wrapper that restores 022.
# Expects $work (a job-local directory) and $log (a file for engine output) to be set.
# After start_docker_engine, DOCKER_HOST points at the engine and $engine_selected names it:
# existing, podman or rootless-docker. stop_docker_engine stops what was started.

engine_pid=""
engine_selected=none
engine_storage_driver=""
docker_version="${HRR_DOCKER_VERSION:-29.9.3}"
compose_version="${HRR_COMPOSE_VERSION:-2.39.4}"

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

install_runc_umask_wrapper() {
    mkdir -p "$work/runc-umask" || return 1
    {
        printf '%s\n' '#!/bin/sh' 'umask 022'
        printf 'exec "%s" "$@"\n' "$work/docker/runc"
    } >"$work/runc-umask/runc" || return 1
    chmod 755 "$work/runc-umask/runc" || return 1
    export PATH="$work/runc-umask:$PATH"
}

ensure_compose_plugin() {
    if docker compose version >/dev/null 2>&1; then return 0; fi
    export DOCKER_CONFIG="$work/docker-config"
    mkdir -p "$DOCKER_CONFIG/cli-plugins" || return 1
    curl -fsSL --retry 2 --max-time 180 \
        "https://github.com/docker/compose/releases/download/v$compose_version/docker-compose-linux-$(uname -m)" \
        -o "$DOCKER_CONFIG/cli-plugins/docker-compose" || return 1
    chmod 700 "$DOCKER_CONFIG/cli-plugins/docker-compose" || return 1
    docker compose version
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

start_rootless_docker_with_driver() {
    local storage_driver="$1"
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
        --storage-driver "$storage_driver" >>"$log" 2>&1 &
    engine_pid=$!
    docker_ready
}

start_rootless_docker_engine() {
    local storage_driver="${HRR_STORAGE_DRIVER:-auto}"
    fetch_docker_release rootless || return 1
    install_runc_umask_wrapper || return 1
    if [ "$storage_driver" = auto ]; then
        if start_rootless_docker_with_driver overlay2; then return 0; fi
        stop_docker_engine
        rm -rf "$work/data" "$work/exec"
        printf 'storage driver unavailable: falling back from overlay2 to vfs\n' >>"$log"
        start_rootless_docker_with_driver vfs
        return $?
    fi
    start_rootless_docker_with_driver "$storage_driver"
}

stop_docker_engine() {
    if [ -n "$engine_pid" ]; then
        kill "$engine_pid" 2>/dev/null || true
        wait "$engine_pid" 2>/dev/null || true
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
    [ "$engine_selected" != none ] || return 1
    ensure_compose_plugin || return 1
    engine_storage_driver="$(docker info --format '{{.Driver}}')" || return 1
    export engine_storage_driver
}
