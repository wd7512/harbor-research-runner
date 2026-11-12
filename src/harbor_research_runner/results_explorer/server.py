from __future__ import annotations

import csv
import io
import json
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.results_explorer.loader import EvidenceStore


class _Server(ThreadingHTTPServer):
    def __init__(self, address, store: EvidenceStore):
        self.store = store
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        segments = [unquote(part) for part in parts.path.split("/") if part]
        try:
            if not segments:
                self._send(
                    200,
                    b"<!doctype html><html><body><h1>Results explorer</h1></body></html>",
                    "text/html; charset=utf-8",
                )
                return
            if segments == ["api", "evidence"]:
                self._json(200, self.server.store.evidence_bundles())
                return
            if segments == ["api", "compare"]:
                query = parse_qs(parts.query)
                runs = query.get("run", [])
                self._json(
                    200,
                    self.server.store.compare(
                        runs,
                        query["a"][0],
                        query["b"][0],
                        query.get("view", ["final"])[0],
                    ),
                )
                return
            if len(segments) >= 3 and segments[:2] == ["api", "evidence"]:
                run_id = segments[2]
                if segments == ["api", "evidence", run_id, "summary"]:
                    self._json(200, self.server.store.summary(run_id))
                    return
                if segments == ["api", "evidence", run_id, "trials"]:
                    query = parse_qs(parts.query)
                    raw_valid = query.get("valid", [None])[0]
                    valid = None if raw_valid is None else raw_valid.lower() == "true"
                    self._json(
                        200,
                        self.server.store.trials(
                            run_id,
                            query.get("arm", [None])[0],
                            query.get("task", [None])[0],
                            valid,
                        ),
                    )
                    return
                if segments == ["api", "evidence", run_id, "over-time"]:
                    self._json(200, self.server.store.over_time(run_id))
                    return
                if len(segments) == 6 and segments[3] == "trials" and segments[5] == "requests":
                    self._json(200, self.server.store.requests(run_id, segments[4]))
                    return
                if segments == ["api", "evidence", run_id, "trials.csv"]:
                    output = io.StringIO(newline="")
                    rows = self.server.store.trials(run_id)
                    columns = [item.name for item in fields(TrialRow)]
                    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(rows)
                    self._send(200, output.getvalue().encode("utf-8"), "text/csv; charset=utf-8")
                    return
        except (KeyError, IndexError, ValueError):
            self._json(404, {"error": "not found"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        self._json(405, {"error": "method not allowed"})

    def do_PUT(self) -> None:
        self._json(405, {"error": "method not allowed"})

    def do_PATCH(self) -> None:
        self._json(405, {"error": "method not allowed"})

    def do_DELETE(self) -> None:
        self._json(405, {"error": "method not allowed"})

    def do_HEAD(self) -> None:
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


def build_server(store: EvidenceStore, port: int = 8765) -> ThreadingHTTPServer:
    return _Server(("127.0.0.1", port), store)
