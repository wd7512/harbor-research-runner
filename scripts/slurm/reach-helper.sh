start_reach_helper() {
    local dir="$1" port="$2"
    (cd "$dir" && exec python3 -m http.server "$port" --bind 0.0.0.0) >/dev/null 2>&1 &
    reach_helper_pid=$!
}

reach_helper_up() {
    python3 -c '
import sys, time, urllib.request
url = "http://127.0.0.1:%s/summary.txt" % sys.argv[1]
deadline = time.monotonic() + float(sys.argv[2])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
while True:
    try:
        opener.open(url, timeout=1).read(1)
        sys.exit(0)
    except Exception:
        if time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.1)
' "$1" "${2:-0}" >/dev/null 2>&1
}
