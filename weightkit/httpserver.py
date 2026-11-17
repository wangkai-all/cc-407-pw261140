"""基于标准库的极简 HTTP 框架：JSON 路由 + CORS + 静态文件。

三个服务都是独立进程，共用这一个文件；不引入任何第三方依赖。
"""

import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Response:
    def __init__(self, status=200, body=b"", content_type="application/json; charset=utf-8",
                 headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.content_type = content_type
        self.headers = headers or {}


def json_response(obj, status=200):
    return Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


class App:
    def __init__(self, name, static_dir=None):
        self.name = name
        self.routes = []  # (method, regex, handler)
        self.static_dir = static_dir

    def route(self, method, pattern):
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def deco(fn):
            self.routes.append((method.upper(), regex, fn))
            return fn
        return deco

    def _dispatch(self, method, path, query, body, headers):
        for m, regex, fn in self.routes:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                result = fn(match.groupdict(), query, body, headers)
                if isinstance(result, Response):
                    return result
                if isinstance(result, tuple):
                    obj, status = result
                    return json_response(obj, status)
                return json_response(result)
        if method == "GET" and self.static_dir:
            return self._static(path)
        return json_response({"error": f"{method} {path} 不存在"}, 404)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        safe = os.path.normpath(path).lstrip("/")
        full = os.path.join(self.static_dir, safe)
        if not os.path.abspath(full).startswith(os.path.abspath(self.static_dir)):
            return json_response({"error": "forbidden"}, 403)
        if not os.path.isfile(full):
            return json_response({"error": f"{path} 不存在"}, 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".csv": "text/csv; charset=utf-8",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as fh:
            return Response(200, fh.read(), ctype)

    def serve(self, port):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # 静默；需要排查时取消注释

            def _reply(self, resp):
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", str(len(resp.body)))
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.body)

            def _handle(self, method):
                try:
                    parsed = urlparse(self.path)
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length) if length else b""
                    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                    resp = app._dispatch(method, parsed.path, query, body, self.headers)
                except Exception as exc:  # 框架级兜底
                    traceback.print_exc()
                    resp = json_response(
                        {"error": f"{type(exc).__name__}: {exc}"}, 500
                    )
                self._reply(resp)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_OPTIONS(self):
                self._reply(Response(204, b""))

        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        print(f"[{self.name}] listening on http://0.0.0.0:{port}", flush=True)
        server.serve_forever()


def parse_json_body(body):
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"请求体不是合法 JSON: {exc}")


def parse_text_body(body):
    return body.decode("utf-8-sig")  # 容忍 Excel 导出的 BOM
