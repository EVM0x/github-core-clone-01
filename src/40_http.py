
# --------------------------------------------------------------------------- HTTP surface

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
REQUEST_COUNTER = [0]


def read_body(handler):
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks = []
        while True:
            line = handler.rfile.readline(65536)
            if not line:
                break
            size = int(line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline(65536)
                break
            chunks.append(handler.rfile.read(size))
            handler.rfile.read(2)
        return b"".join(chunks)
    length = handler.headers.get("Content-Length")
    if not length:
        return b""
    try:
        return handler.rfile.read(int(length))
    except (TypeError, ValueError):
        return b""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "forge/1.0"
    sys_version = ""
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def do_HEAD(self):
        self.handle_any("HEAD")

    # -- helpers ---------------------------------------------------------

    def request_id(self):
        REQUEST_COUNTER[0] += 1
        return "%08X:%08X" % (os.getpid() & 0xFFFFFFFF, REQUEST_COUNTER[0])

    def authorized(self):
        header = self.headers.get("Authorization", "")
        if not header:
            return False
        parts = header.split(None, 1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if TOKEN == "":
            return True
        return token == TOKEN

    def base_url(self):
        host = self.headers.get("Host") or "127.0.0.1"
        return "http://" + host

    def send_json(self, status, payload, headers=None, extra=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-GitHub-Request-Id", self.request_id())
        self.send_header("X-OAuth-Scopes", "repo, read:org")
        self.send_header("X-Accepted-OAuth-Scopes", "")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_bytes(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-GitHub-Request-Id", self.request_id())
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_error_json(self, status, message, headers=None):
        self.send_json(status, {"message": message, "documentation_url": "https://docs.github.com/rest"},
                       headers)

    # -- routing ---------------------------------------------------------

    def handle_any(self, method):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = {}
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                query.setdefault(key, value)
            body_raw = read_body(self)
        except Exception:
            try:
                self.send_error_json(400, "Bad Request")
            except Exception:
                pass
            return

        try:
            self.route(method, path, query, body_raw)
        except HttpError as err:
            try:
                self.send_error_json(err.status, err.message, err.headers)
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_error_json(500, "Internal Server Error")
            except Exception:
                pass

    def route(self, method, path, query, body_raw):
        if path == "/healthz":
            self.send_json(200, {"ok": True})
            return

        if path.startswith("/api/v3"):
            if not self.authorized():
                self.send_error_json(401, "Requires authentication")
                return
            body = None
            if body_raw:
                try:
                    body = json.loads(body_raw)
                except ValueError:
                    self.send_error_json(400, "Problems parsing JSON")
                    return
            parts = [segment for segment in path[len("/api/v3") :].split("/") if segment]
            status, payload, headers = rest_dispatch(method, parts, query, body, self.base_url())
            self.send_json(status, payload, headers)
            return

        if path == "/api/graphql":
            if not self.authorized():
                self.send_error_json(401, "Requires authentication")
                return
            try:
                body = json.loads(body_raw) if body_raw else None
            except ValueError:
                self.send_error_json(400, "Problems parsing JSON")
                return
            status, payload = graphql_dispatch(body, self.base_url())
            self.send_json(status, payload)
            return

        if path == "/mcp" or path.startswith("/mcp/"):
            self.mcp(method, body_raw)
            return

        if path.endswith(".git") or ".git/" in path:
            self.git(method, path, query, body_raw)
            return

        self.send_error_json(404, "Not Found")
