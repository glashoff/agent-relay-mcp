"""Relay events (JSON lines over a Unix socket, with reconnect) and the
minimal MCP client, against small fake sockets."""

import asyncio
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler

from agent_relay_voice import relay


def test_notices():
    wanted = ["run_ended", "request_added"]
    assert relay.notice_for({"type": "run_ended", "project": "relay", "phase": "finished"}, wanted) == \
        "[Ereignis: Der Auftrag in Projekt relay ist fertig.]"
    assert "fehlgeschlagen" in relay.notice_for(
        {"type": "run_ended", "project": "x", "phase": "failed"}, wanted)
    n = relay.notice_for({"type": "request_added", "project": "web", "number": 7,
                          "full_access": False}, wanted)
    assert n == "[Ereignis: Eine Nachricht an Projekt web wartet am Laptop auf Freigabe, Nummer 7.]"
    assert "vollem Zugriff" in relay.notice_for(
        {"type": "request_added", "project": "web", "number": 7, "full_access": True}, wanted)
    assert relay.notice_for({"type": "run_started", "project": "x"}, wanted) is None
    assert relay.notice_for({"type": "grant_added"}, wanted + ["grant_added"]) is None
    assert relay.spoken_notice("[Ereignis: Der Auftrag ist fertig.]") == "Der Auftrag ist fertig."


def test_project_names():
    text = ("Projects (every project is locked unless approved at the laptop):\n"
            "- agent-relay-mcp (dev container, running; locked)\n"
            "- familienkalender (dev container; locked)\n")
    assert relay.project_names(text) == ["agent-relay-mcp", "familienkalender"]


def test_events_reconnect_when_the_socket_appears_later(tmp_path):
    path = tmp_path / "events.sock"

    async def run():
        received, connected = [], []
        task = asyncio.create_task(relay.read_events(path, received.append, connected.append,
                                                     max_delay=0.2))
        await asyncio.sleep(0.3)               # socket missing: retried, nothing breaks
        assert connected and connected[-1] is False

        clients = []

        async def serve(reader, writer):
            clients.append(writer)
            writer.write(b'{"type": "run_ended", "project": "a", "phase": "finished"}\n')
            writer.write(b"garbage\n")
            writer.write(b'{"type": "request_added", "project": "b", "number": 1}\n')
            await writer.drain()

        server = await asyncio.start_unix_server(serve, path=str(path))
        for _ in range(50):
            if len(received) >= 2:
                break
            await asyncio.sleep(0.05)
        assert [e["type"] for e in received] == ["run_ended", "request_added"]
        # the relay restarts: connection drops, a new socket comes up
        for w in clients:
            w.close()
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)       # Python 3.13 removes it on close
        await asyncio.sleep(0.3)
        server = await asyncio.start_unix_server(serve, path=str(path))
        for _ in range(60):
            if len(received) >= 4:
                break
            await asyncio.sleep(0.05)
        assert len(received) == 4
        task.cancel()
        server.close()

    asyncio.run(run())


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def test_call_tool_over_unix_socket(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.headers["Host"], self.headers.get("Mcp-Session-Id"), body.get("method")))
            if body.get("method") == "initialize":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "sess-1")
                data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {}}).encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif "id" not in body:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                result = {"jsonrpc": "2.0", "id": body["id"], "result": {
                    "content": [{"type": "text", "text": "- p1 (dev container; locked)"}]}}
                data = f"event: message\ndata: {json.dumps(result)}\n\n".encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    path = tmp_path / "mcp.sock"
    server = _UnixHTTPServer(str(path), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        text = relay.call_tool(path, "list_projects")
    finally:
        server.shutdown()
    assert relay.project_names(text) == ["p1"]
    assert [s[2] for s in seen] == ["initialize", "notifications/initialized", "tools/call"]
    assert all(s[0] == "localhost:8765" for s in seen)
    assert seen[1][1] == "sess-1" and seen[2][1] == "sess-1"
