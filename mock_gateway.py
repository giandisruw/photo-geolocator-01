#!/usr/bin/env python3
"""Mock model gateway for testing main.py's model path locally.

Serves the Anthropic Messages endpoint (/v1/messages) and the OpenAI
Chat Completions endpoint (/v1/chat/completions) on loopback, returning
the TRUE coordinates of the photo it is shown (looked up by the image's
sha256 against a practice trial's photos.json / answers.json). This lets
main.py's model path be exercised end to end without a real provider key.

Usage:
  python3 mock_gateway.py <trial> [port]
    trial: 0, 1 or 2 (the practice trial whose answers it returns)
    port:  default 18200
Then run main.py with:
  ANTHROPIC_BASE_URL=http://127.0.0.1:<port> ANTHROPIC_API_KEY=test \
  OPENAI_BASE_URL=http://127.0.0.1:<port> OPENAI_API_KEY=test
"""
import base64
import hashlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRIAL = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18200
KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "photo-geolocator-01-kit")
ENV_DIR = os.path.join(KIT, "practice", "trial-" + TRIAL, "input", "env")
REF_DIR = os.path.join(KIT, "practice", "trial-" + TRIAL, "reference")

photos = json.load(open(os.path.join(ENV_DIR, "photos.json")))["photos"]
answers = json.load(open(os.path.join(REF_DIR, "answers.json")))["photos"]
by_sha = {}
for p, a in zip(photos, answers):
    by_sha[p["sha256"]] = (p["id"], a["lat_e6"] / 1e6, a["lon_e6"] / 1e6)


def lookup(jpeg_bytes):
    sha = hashlib.sha256(jpeg_bytes).hexdigest()
    return by_sha.get(sha)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send(400, {"error": {"type": "invalid_json",
                                       "message": "not JSON"}})
            return
        # Find the image in the request (Anthropic or OpenAI shape).
        jpeg = None
        messages = data.get("messages", [])
        for msg in messages:
            content = msg.get("content", [])
            if isinstance(content, str):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        jpeg = base64.b64decode(src["data"])
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    if url.startswith("data:image/jpeg;base64,"):
                        jpeg = base64.b64decode(url.split(",", 1)[1])
        if jpeg is None:
            self._send(400, {"error": {"type": "invalid_json",
                                       "message": "no image in request"}})
            return
        found = lookup(jpeg)
        if found is None:
            self._send(400, {"error": {"type": "invalid_json",
                                       "message": "unknown image"}})
            return
        pid, lat, lon = found
        text = (
            "The signs and driving side put this in a built-up area; "
            "the architecture and vegetation narrow it down. "
            "{\"lat\": %.6f, \"lon\": %.6f}" % (lat, lon)
        )
        if self.path == "/v1/messages":
            self._send(200, {
                "id": "msg_mock_%s" % pid,
                "type": "message",
                "role": "assistant",
                "model": data.get("model", ""),
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 60},
            })
        elif self.path == "/v1/chat/completions":
            self._send(200, {
                "id": "chatcmpl_mock_%s" % pid,
                "object": "chat.completion",
                "model": data.get("model", ""),
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 60,
                          "total_tokens": 1060},
            })
        else:
            self._send(404, {"error": {"type": "not_found",
                                       "message": "no route " + self.path}})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write("mock gateway: trial-%s on 127.0.0.1:%d\n" % (TRIAL, PORT))
    sys.stderr.flush()
    server.serve_forever()
