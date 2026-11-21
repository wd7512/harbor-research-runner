start_reach_helper() {
    local dir="$1" port="$2"
    (cd "$dir" && exec python3 -m http.server "$port" --bind 0.0.0.0) >/dev/null 2>&1 &
    reach_helper_pid=$!
}

reach_helper_up() {
    python3 -c 'import sys, urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/summary.txt" % sys.argv[1], timeout=5).read(1)' "$1" >/dev/null 2>&1
}
