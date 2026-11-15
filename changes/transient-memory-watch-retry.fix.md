The memory watcher retries transient Docker listing and stats failures. It exits after 30 consecutive failed samples so a persistent Docker outage still halts the run.
