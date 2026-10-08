"""Private dashboard: http://127.0.0.1:<port> — browse tiers, try filter values, save them, run the pipeline.

Standard library only. Binds to 127.0.0.1. Write endpoints require the custom header
X-Coin-Sieve: 1, which a foreign web page cannot send without a CORS preflight we never approve.
"""
import json
import logging
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from coinsieve import funnel, whatif
from coinsieve.config_edit import write_values
from coinsieve.store import Store

log = logging.getLogger(__name__)

HTML = Path(__file__).with_name("dashboard.html")
TIERS = ("new_launches", "emerging", "established")
LABELS = {"new_launches": "New Launches", "emerging": "Emerging", "established": "Established"}


class Runner:
    """One pipeline run at a time, as a subprocess; output kept for the UI."""

    def __init__(self, root, log_path):
        self.root, self.log_path, self.proc, self.started = root, log_path, None, None
        self.lock = threading.Lock()

    def start(self, tiers):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return False
            out = open(self.log_path, "w", encoding="utf-8")
            self.proc = subprocess.Popen([sys.executable, "sieve.py", "run", "--tiers", ",".join(tiers)],
                                         cwd=self.root, stdout=out, stderr=subprocess.STDOUT)
            self.started = time.time()
            return True

    def status(self):
        running = bool(self.proc and self.proc.poll() is None)
        tail = ""
        if self.log_path.exists():
            tail = "".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-8:])
        return {"running": running, "started": self.started,
                "exit_code": None if running or not self.proc else self.proc.returncode, "tail": tail}


class App:
    def __init__(self, config_path, public=False):
        self.public = public  # public mode: read-only (no saving filters, no pipeline runs)
        self.config_path = Path(config_path).resolve()
        self.root = self.config_path.parent
        cfg = self.load_cfg()
        self.db_path = self.root / cfg["storage"]["db_path"]
        self.runner = Runner(self.root, self.root / cfg["storage"]["log_dir"] / "dashboard-run.log")

    def load_cfg(self):
        with open(self.config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def tier_tokens(self, store, tier):
        ts = store.latest_run_ts(tier)
        return ts, (store.latest_since(tier, ts) if ts else [])

    def state(self):
        cfg = self.load_cfg()
        store = Store(str(self.db_path))
        try:
            week_ago = time.time() - 7 * 86400
            tiers = {}
            for tier in TIERS:
                ts, tokens = self.tier_tokens(store, tier)
                members = set(store.current_members(tier))
                events = [dict(e) for e in store.events_since(week_ago, tier)]
                first = store.tier_first_event_ts(tier) or 0
                events = [e for e in events if e["ts"] > first + cfg["publishing"]["bootstrap_hours"] * 3600]
                tiers[tier] = {
                    "label": LABELS[tier], "last_run": ts, "stats": store.latest_run_stats(tier),
                    "members": len(members),
                    "tokens": [{"metrics": t["metrics"], "reasons": t["reasons"],
                                "member": t["metrics"]["address"] in members} for t in tokens],
                    "events": events[-60:],
                    "fields": [{"path": p, "label": lab, "kind": k, "help": h} for p, lab, k, h in whatif.FIELDS[tier]],
                    "values": whatif.current_values(cfg, tier),
                    "presets": whatif.presets(cfg, tier),
                    "typical": whatif.typical(tier, tokens),
                }
            return {"now": time.time(), "mode": "public" if self.public else "admin", "tiers": tiers,
                    "reason_labels": funnel.REASON_LABELS, "run": self.runner.status(),
                    "warn_pct": cfg["publishing"]["concentration_warning_pct"],
                    "disclaimer": cfg["publishing"]["disclaimer"]}
        finally:
            store.close()

    def evaluate(self, body):
        tier = body.get("tier")
        if tier not in TIERS:
            raise ValueError("unknown tier")
        cfg = self.load_cfg()
        candidate = whatif.with_values(cfg, whatif.validate(tier, body.get("values") or {}))
        store = Store(str(self.db_path))
        try:
            _, tokens = self.tier_tokens(store, tier)
        finally:
            store.close()
        saved, edited = whatif.evaluate_all(tier, tokens, cfg), whatif.evaluate_all(tier, tokens, candidate)
        checks = {t["metrics"]["address"]: whatif.checklist(tier, t, candidate) for t in tokens}
        return {"results": {a: {"saved": saved[a][0], "status": s, "reasons": r, "checks": checks[a]}
                            for a, (s, r) in edited.items()},
                "impact": whatif.impact(tier, checks.values())}

    def save(self, body):
        if self.public:
            raise PermissionError("read-only in public mode")
        tier = body.get("tier")
        if tier not in TIERS:
            raise ValueError("unknown tier")
        values = whatif.validate(tier, body.get("values") or {})
        write_values(str(self.config_path), values)
        log.info("dashboard saved %d %s filter values", len(values), tier)
        return {"ok": True, "values": whatif.current_values(self.load_cfg(), tier)}

    def run(self, body):
        if self.public:
            raise PermissionError("read-only in public mode")
        tiers = [t for t in (body.get("tiers") or TIERS) if t in TIERS]
        return {"started": self.runner.start(tiers), "run": self.runner.status()}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet console
            log.debug(fmt, *args)

        def send(self, code, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                if self.path == "/":
                    return self.send(200, HTML.read_bytes(), "text/html; charset=utf-8")
                if self.path == "/api/state":
                    return self.send(200, app.state())
                if self.path == "/api/run":
                    return self.send(200, app.runner.status())
                self.send(404, {"error": "not found"})
            except Exception as e:
                log.exception("GET %s failed", self.path)
                self.send(500, {"error": str(e)})

        def do_POST(self):
            if self.headers.get("X-Coin-Sieve") != "1":
                return self.send(403, {"error": "missing dashboard header"})
            routes = {"/api/evaluate": app.evaluate, "/api/config": app.save, "/api/run": app.run}
            if self.path not in routes:
                return self.send(404, {"error": "not found"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                self.send(200, routes[self.path](body))
            except PermissionError as e:
                self.send(403, {"error": str(e)})
            except (ValueError, KeyError) as e:
                self.send(400, {"error": str(e)})
            except Exception as e:
                log.exception("POST %s failed", self.path)
                self.send(500, {"error": str(e)})

    return Handler


def serve(config_path, port, open_browser=True, public=False):
    app = App(config_path, public)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{port}/"
    print(f"Coin Sieve dashboard ({'public' if public else 'admin'} mode): {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
