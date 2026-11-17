"""A local stand-in for the inference service that can stall (test-harness-stall.sh).

Usage::

    fake_inference.py --port P --log FILE [--stall-headers N] [--stall-midstream N]
                      [--ready-at SECONDS] [--min-wait SECONDS] [--stream-gap SECONDS]
                      [--stream-chunks N]

Answers every ``POST`` with a short final answer in the OpenAI chat format
(streamed if the request asks for it), and never asks for a tool call. The
options make it misbehave the way the real service was seen to:

- ``--stall-headers N``: the first N requests get no response at all;
- ``--stall-midstream N``: the next N get headers and one chunk, then nothing;
- ``--ready-at S``: no request is answered before S seconds after the first
  one arrived; a request waiting at that moment is answered then;
- ``--min-wait S``: every request is answered only once it has waited S
  seconds, so giving up earlier and asking again never helps;
- ``--stream-gap``, ``--stream-chunks``: a slow stream that stays alive.

A stalled request is held until the client closes the connection. ``FILE``
gets one JSON line per event, with the seconds since the first request:
``start``, ``headers``, ``done``, ``stalled`` and ``client-abort``.

Listens on 127.0.0.1 only. Test tool: never imported, never run by the
workflow.
"""

import argparse
import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ANSWER = ("### Summary\n\n", "Fake ", "review ", "answer.\n")


class State:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.lock = threading.Lock()
        self.count = 0
        self.first: float | None = None

    def arrival(self) -> tuple[int, float, float]:
        """The request's number, its arrival time and the first request's."""
        now = time.monotonic()
        with self.lock:
            self.count += 1
            if self.first is None:
                self.first = now
            return self.count, now, self.first

    def log(self, **event: object) -> None:
        with self.lock, open(self.options.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")


def client_gone(connection: socket.socket) -> bool:
    readable, _, _ = select.select([connection], [], [], 0)
    if not readable:
        return False
    try:
        return connection.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: State

    def log_message(self, format: str, *args: Any) -> None:  # the base class's parameter name
        pass

    def _json(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._json({"object": "list", "data": []})

    def do_POST(self) -> None:
        state, options = self.state, self.state.options
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            request = json.loads(raw)
        except ValueError:
            request = {}
        if not isinstance(request, dict):
            request = {}
        number, arrived, first = state.arrival()

        def seconds() -> float:
            return round(time.monotonic() - first, 1)

        def hold(until: float | None) -> bool:
            """Waits until ``until`` (forever if None); True if the client left first."""
            while until is None or time.monotonic() < until:
                if client_gone(self.connection):
                    state.log(i=number, ev="client-abort", t=seconds(), waited=round(time.monotonic() - arrived, 1))
                    self.close_connection = True
                    return True
                time.sleep(0.1)
            return False

        state.log(i=number, ev="start", t=seconds())
        if number <= options.stall_headers:
            state.log(i=number, ev="stalled", t=seconds(), where="headers")
            hold(None)
            return
        if hold(max(first + options.ready_at, arrived + options.min_wait)):
            return
        state.log(i=number, ev="headers", t=seconds(), waited=round(time.monotonic() - arrived, 1))
        words = [*ANSWER, *(["more. "] * max(0, options.stream_chunks - len(ANSWER)))]
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        model = request.get("model")
        if not request.get("stream"):
            message = {"role": "assistant", "content": "".join(words)}
            self._json(
                {
                    "id": "fake",
                    "object": "chat.completion",
                    "created": 1,
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": usage,
                }
            )
            state.log(i=number, ev="done", t=seconds())
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        base = {"id": "fake", "object": "chat.completion.chunk", "created": 1, "model": model}

        def event(payload: object) -> None:
            text = payload if isinstance(payload, str) else json.dumps(payload)
            data = f"data: {text}\n\n".encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        def delta(content: dict[str, str], finish: str | None = None) -> dict[str, object]:
            return {**base, "choices": [{"index": 0, "delta": content, "finish_reason": finish}]}

        try:
            event(delta({"role": "assistant", "content": ""}))
            if number <= options.stall_headers + options.stall_midstream:
                state.log(i=number, ev="stalled", t=seconds(), where="stream")
                hold(None)
                return
            for word in words:
                time.sleep(options.stream_gap)
                event(delta({"content": word}))
            event(delta({}, "stop"))
            event({**base, "choices": [], "usage": usage})
            event("[DONE]")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            state.log(i=number, ev="done", t=seconds())
        except OSError:
            state.log(i=number, ev="client-abort", t=seconds(), waited=round(time.monotonic() - arrived, 1))
            self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--stall-headers", type=int, default=0)
    parser.add_argument("--stall-midstream", type=int, default=0)
    parser.add_argument("--ready-at", type=float, default=0)
    parser.add_argument("--min-wait", type=float, default=0)
    parser.add_argument("--stream-gap", type=float, default=0.05)
    parser.add_argument("--stream-chunks", type=int, default=len(ANSWER))
    Handler.state = State(parser.parse_args())
    server = ThreadingHTTPServer(("127.0.0.1", Handler.state.options.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
