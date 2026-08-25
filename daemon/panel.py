"""本地管理面板：127.0.0.1 上的极小 HTTP 服务 + 单页前端。

不用任何 Web 框架 —— 只有几个 JSON 端点，标准库的 http.server 足够，
省掉一整条依赖链。只监听回环地址，不对外暴露。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import audio
import config

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
HISTORY = ROOT / "logs" / "data" / "history.jsonl"


def _tail_history(n: int = 30) -> list:
    if not HISTORY.exists():
        return []
    lines = HISTORY.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass                                   # 别把请求日志刷进控制台

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, _read(WEB / "index.html"),
                                  "text/html; charset=utf-8")
            if self.path == "/api/state":
                models = sorted(p.name for p in (ROOT / "models").iterdir()
                                if p.is_dir() and not p.name.startswith("_"))
                return self._send(200, json.dumps({
                    "status": app.status,
                    "paused": app.paused,
                    "error": app.rec_error,
                    "config": app.cfg,
                    "models": models,
                    "devices": audio.list_devices(),
                    "stats": app.stats,
                    "sessions": [{"pid": s.get("claude_pid"), "entry": s.get("entry"),
                                  "cwd": s.get("cwd")} for s in app.gate.sessions()],
                    "hotwords": _read(ROOT / "hotwords.txt"),
                    "rules": _read(ROOT / "rules.txt"),
                    "history": _tail_history(),
                }, ensure_ascii=False))
            return self._send(404, "{}")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or "{}")
            if self.path == "/api/config":
                changed_model = body.get("model") != app.cfg.get("model")
                app.cfg.update(body)
                config.save(app.cfg)
                app.gate.mode = app.cfg["gate_mode"]
                app.recorder.device = app.cfg["audio"]["device"]
                if changed_model:
                    app.reload_model()
                return self._send(200, json.dumps({"ok": True}))
            if self.path == "/api/control":
                act = body.get("action")
                if act in ("pause", "resume"):
                    app.set_paused(act == "pause")
                elif act == "quit":
                    app.request_quit()
                return self._send(200, json.dumps({"ok": True, "paused": app.paused}))
            if self.path == "/api/text":
                (ROOT / "hotwords.txt").write_text(body.get("hotwords", ""), encoding="utf-8")
                (ROOT / "rules.txt").write_text(body.get("rules", ""), encoding="utf-8")
                app.fixer.reload()
                return self._send(200, json.dumps({"ok": True}))
            return self._send(404, "{}")

    return Handler


def serve(app, port: int) -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=srv.serve_forever, name="panel", daemon=True).start()
    return f"http://127.0.0.1:{port}/"
