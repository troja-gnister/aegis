import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A request carrying this header is answered exactly as the test describes, on any
# path: like web's media delivery (an empty response with X-Accel-Redirect and chosen
# headers, or a 304), or like a hostile answer on a location that must ignore it.
RESPONSE_HEADER = "X-Test-Upstream-Response"
# Django's SecurityMiddleware and XFrameOptionsMiddleware set these on every response;
# the gateway hides web's copies and adds its own.
DJANGO_SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"))
# A redirect too long to describe in a request header (8 KiB client header lines): an
# original eleven directories deep, each named with 240 non-UTF-8 bytes (0xE9), requested
# through the gateway's originals location.
LONG_PATH = "/media/v1/originals/long"
LONG_REDIRECT = (
    "/__aegis_roots/gateway-test/" + "/".join(["%E9" * 240] * 11) + "/long.bin"
)


class EchoHandler(BaseHTTPRequestHandler):
    def respond(self) -> None:
        if self.path == LONG_PATH or RESPONSE_HEADER in self.headers:
            self.accel()
            return
        body = json.dumps(
            {
                "method": self.command,
                "host": self.headers.get("Host"),
                "path": self.path,
                "request_id": self.headers.get("X-Request-ID"),
                "forwarded_for": self.headers.get("X-Forwarded-For"),
                "forwarded_proto": self.headers.get("X-Forwarded-Proto"),
                "proxy_attestation": self.headers.get("X-Aegis-Proxy-Attestation"),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # Django's SecurityMiddleware also emits a referrer policy; the gateway must own it.
        self.send_header("Referrer-Policy", "unsafe-url")
        for name, value in DJANGO_SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def accel(self) -> None:
        if self.path == LONG_PATH:
            status = 200
            headers = [
                ["X-Accel-Redirect", LONG_REDIRECT],
                ["Content-Type", "application/octet-stream"],
                ["Cache-Control", "private, no-store"],
            ]
        else:
            encoded = self.headers.get(RESPONSE_HEADER, "")
            specification = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
            status = int(specification["status"])
            headers = specification["headers"]
        self.send_response(status)
        for name, value in (*DJANGO_SECURITY_HEADERS, *headers):
            self.send_header(name, value)
        if status != 304:
            self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = respond
    do_POST = respond
    # Only described (accel) answers are requested with HEAD; they have no body.
    do_HEAD = respond

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), EchoHandler).serve_forever()
