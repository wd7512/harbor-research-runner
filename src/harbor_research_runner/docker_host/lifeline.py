from __future__ import annotations

import os
import threading


def watch_lifeline(fd: int, stopped: threading.Event) -> None:
    try:
        while os.read(fd, 1):
            pass
    except OSError:
        pass
    finally:
        os.close(fd)
        stopped.set()


def start_lifeline_watch(fd: int | None, stopped: threading.Event) -> None:
    if fd is not None:
        threading.Thread(target=watch_lifeline, args=(fd, stopped), daemon=True).start()
