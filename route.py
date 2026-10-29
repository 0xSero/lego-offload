"""Route: the room's only way to its cloud model. A localhost reverse proxy that strips
whatever key the agent sends, injects the real credential (which never enters the room),
streams the reply back, and can capture every exchange (request and streamed reply) for leak scans and traces.
"""
import http.client
import json
import os
import subprocess
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "x-api-key", "authorization"}


def claude_token():
    """The Claude Code OAuth token, from its credentials file or the macOS keychain."""
    path = os.path.expanduser("~/.claude/.credentials.json")
    raw = open(path).read() if os.path.exists(path) else subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True, timeout=15, check=True).stdout
    return json.loads(raw)["claudeAiOauth"]["accessToken"]


def zai_key():
    """providers.zai.apiKey from omp's models.yml (stdlib scan, no yaml dependency)."""
    inside = False
    for line in open(os.path.expanduser("~/.omp/agent/models.yml")):
        if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
            inside = line.strip() == "zai:"
        elif inside and line.strip().startswith("apiKey:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    raise RuntimeError("providers.zai.apiKey not found")


UPSTREAMS = {"anthropic": ("api.anthropic.com", ("/v1/",), claude_token, "oauth-2025-04-20"),
             "zai": ("api.z.ai", ("/api/coding/paas/v4/",), zai_key, None)}


def serve(port, upstream="anthropic", capture=None):
    host, prefixes, secret, beta = UPSTREAMS[upstream]
    cache, lock = {}, threading.Lock()
    secret_now = lambda: cache.get("v") if time.time() - cache.get("t", 0) < 120 else None

    def token():
        with lock:
            if not secret_now():
                cache.update(v=secret(), t=time.time())
            return cache["v"]

    token()                                            # fail fast; the value is never printed

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def forward(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            if not self.path.startswith(prefixes):
                self.send_response(404), self.send_header("content-length", "0"), self.end_headers()
                return
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"anthropic-beta"}}
            betas = [b.strip() for b in self.headers.get("anthropic-beta", "").split(",") if b.strip()]
            if beta and beta not in betas:
                betas.append(beta)
            if betas:
                hdrs["anthropic-beta"] = ",".join(betas)
            hdrs.update({"Authorization": "Bearer " + token(), "Host": host, "Content-Length": str(len(body))})
            conn, got, status, enc = http.client.HTTPSConnection(host, 443, timeout=600), [], None, ""
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
            finally:
                conn.close()
                if capture:                                # one JSON line per exchange: what left, what came back
                    raw = b"".join(got)
                    if enc == "gzip":
                        raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
                    rec = {"t": time.time(), "path": self.path, "status": status,
                           "request": body.decode(errors="replace"), "response": raw.decode(errors="replace")}
                    with lock, open(capture, "a") as f:
                        f.write(json.dumps(rec) + "\n")

        do_GET = do_POST = forward

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
