registry_login() {
    local file="$1" mode owner current_owner registry username token extra line rc=0 xtrace=0
    if [ ! -f "$file" ]; then
        printf 'refusing: registry credentials file is not a regular file\n' >&2
        return 1
    fi
    mode="$(stat -c %a "$file" 2>/dev/null || stat -f %Lp "$file" 2>/dev/null)" || mode=""
    if [ "$mode" != 600 ]; then
        printf 'refusing: registry credentials file must have mode 600\n' >&2
        return 1
    fi
    owner="$(stat -c %u "$file" 2>/dev/null || stat -f %u "$file" 2>/dev/null)" || owner=""
    current_owner="$(id -u)"
    if [ -z "$owner" ] || [ "$owner" != "$current_owner" ]; then
        printf 'refusing: registry credentials file must be owned by the current user\n' >&2
        return 1
    fi
    case "$-" in
        *x*) xtrace=1; set +x ;;
    esac
    while IFS= read -r line || [ -n "$line" ]; do
        registry=""
        username=""
        token=""
        extra=""
        IFS=$' \t' read -r registry username token extra <<<"$line"
        if [ -z "$registry" ] || [ -z "$username" ] || [ -z "$token" ] || [ -n "$extra" ]; then
            printf 'refusing: registry credentials lines must have exactly three fields\n' >&2
            rc=1
            break
        fi
        if ! printf '%s\n' "$token" | docker login "$registry" -u "$username" --password-stdin >/dev/null 2>&1; then
            printf 'registry login failed for %s\n' "$registry" >&2
            rc=1
            break
        fi
    done <"$file"
    if [ "$xtrace" = 1 ]; then set -x; fi
    return "$rc"
}

load_image_cache() {
    local dir="$1" archive count=0 rc=0
    for archive in "$dir"/*.tar; do
        [ -f "$archive" ] || continue
        if docker load -i "$archive"; then
            count=$((count + 1))
        else
            rc=1
        fi
    done
    printf 'image cache: loaded %s\n' "$count"
    return "$rc"
}

save_image_cache() {
    local dir="$1" image name target temporary
    mkdir -p "$dir" || return 0
    while IFS= read -r image; do
        [ "${image##*:}" != '<none>' ] || continue
        case "$image" in
            *:*) ;;
            *) continue ;;
        esac
        name="$(printf '%s' "$image" | sed 's/[^A-Za-z0-9._-]/_/g')"
        target="$dir/$name.tar"
        [ -e "$target" ] && continue
        temporary="$dir/.$name.tar.tmp"
        if docker save "$image" >"$temporary"; then
            mv -f "$temporary" "$target" || rm -f "$temporary"
        else
            rm -f "$temporary"
        fi
    done < <(docker image ls --format '{{.Repository}}:{{.Tag}}')
    return 0
}
