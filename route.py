"""Route: the room's only way to its cloud model. A localhost reverse proxy that strips
whatever key the agent sends, injects the real credential (which never enters the room),
streams the reply back, and can capture every exchange (request and streamed reply) for leak scans and traces.
"""
import http.client
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "x-api-key", "authorization"}


def keychain(service):
    return subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                          capture_output=True, text=True, timeout=15, check=True).stdout.strip()


def claude_auth():
    """The Claude Code OAuth token, from its credentials file or the macOS keychain."""
    path = os.path.expanduser("~/.claude/.credentials.json")
    raw = open(path).read() if os.path.exists(path) else keychain("Claude Code-credentials")
    return {"Authorization": "Bearer " + json.loads(raw)["claudeAiOauth"]["accessToken"]}


# name: (host, path prefixes the agent may use, headers carrying the real credential, anthropic-beta to add)
UPSTREAMS = {"anthropic": ("api.anthropic.com", ("/v1/",), claude_auth, "oauth-2025-04-20")}


def gunzip(b):
    return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(b)


HEX = re.compile(rb"(?:[0-9a-f]{2}){32,}")


def readable(raw, enc=""):
    """Undo compression so captures can be scanned: whole-body gzip, or Connect envelopes
    (1 flag byte, 4 length bytes; flag bit 1 = gzipped) as Cursor sends them. Long hex runs
    (Cursor's BidiAppend carries the conversation as hex-encoded protobuf) are decoded and
    appended, so what they hold is scanned too."""
    try:
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gunzip(raw)
        else:
            out, i = [], 0
            while i + 5 <= len(raw) and raw[i] in (0, 1, 2, 3):
                n = int.from_bytes(raw[i + 1:i + 5], "big")
                part = raw[i + 5:i + 5 + n]
                out.append(gunzip(part) if raw[i] & 1 else part)
                i += 5 + n
            raw = b"\n".join(out) if out and i == len(raw) else raw
    except (zlib.error, ValueError):
        pass
    return b"\n".join([raw] + [bytes.fromhex(h.decode()) for h in HEX.findall(raw)])


def serve(port, upstream="anthropic", capture=None, bind="127.0.0.1", token=None, peers=None):
    """`upstream`: a name in UPSTREAMS, or the http:// URL of a local model (no credential). With
    `token`, only a request carrying it (as its api key) from an address in `peers` is forwarded."""
    local = upstream.startswith("http://")
    host, prefixes, secret, beta = (upstream[7:].split("/")[0], ("/",), dict, None) if local else UPSTREAMS[upstream]
    conn_for = lambda: (http.client.HTTPConnection(host, timeout=600) if local else                # noqa: E731
                        http.client.HTTPSConnection(host, 443, timeout=600))
    cache, lock = {}, threading.Lock()

    def creds():
        with lock:
            if time.time() - cache.get("t", 0) > 120:
                cache.update(v=secret(), t=time.time())
            return cache["v"]

    creds()                                            # fail fast; the value is never printed
    allowed = lambda h: token is None or any(token in v for v in h.headers.values()) and (   # noqa: E731
        peers is None or ipaddress.ip_address(h.client_address[0]) in peers)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def body(self):
            if self.headers.get("transfer-encoding", "").lower() != "chunked":
                return self.rfile.read(int(self.headers.get("content-length") or 0))
            out = []
            while n := int(self.rfile.readline().split(b";")[0], 16):
                out.append(self.rfile.read(n)), self.rfile.readline()
            self.rfile.readline()
            return b"".join(out)

        def forward(self):
            body = self.body()
            if not self.path.startswith(prefixes) or not allowed(self):
                self.send_response(404), self.send_header("content-length", "0"), self.end_headers()
                return
            real = creds()
            drop = HOP | {"anthropic-beta"} | {k.lower() for k in real}
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in drop}
            betas = [b.strip() for b in self.headers.get("anthropic-beta", "").split(",") if b.strip()]
            if beta and beta not in betas:
                betas.append(beta)
            if betas:
                hdrs["anthropic-beta"] = ",".join(betas)
            hdrs.update({**real, "Host": host, "Content-Length": str(len(body))})
            got, status, enc = [], None, ""
            for attempt in range(3):                       # DNS on this host flakes; nothing was sent yet
                conn = conn_for()
                try:
                    conn.connect()
                    break
                except OSError:
                    conn.close()
                    if attempt == 2:
                        self.send_response(502), self.send_header("content-length", "0"), self.end_headers()
                        return
                    time.sleep(1)
            try:
                conn.request(self.command, self.path, body=body, headers=hdrs)
                r = conn.getresponse()
                status, enc = r.status, r.getheader("content-encoding", "")
                self.send_response(r.status, r.reason)
                for k, v in r.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                while chunk := r.read1(65536):
                    got.append(chunk)
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass                                       # the agent hung up mid-stream
            finally:
                conn.close()
                if capture:                                # one JSON line per exchange: what left, what came back
                    rec = {"t": time.time(), "path": self.path, "status": status,
                           "request": readable(body).decode(errors="replace"),
                           "response": readable(b"".join(got), enc).decode(errors="replace")}
                    with lock, open(capture, "a") as f:
                        f.write(json.dumps(rec) + "\n")

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = forward

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, addr):         # an agent hanging up is not an error
            if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
                super().handle_error(request, addr)

    srv = Server((bind, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
