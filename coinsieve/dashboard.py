"""Private dashboard: http://127.0.0.1:<port> — browse tiers, try filter values, save them, run the pipeline.

Standard library only (token logos lazily use logos.py). Binds to 127.0.0.1. Write endpoints require the custom header
X-Coin-Sieve: 1, which a foreign web page cannot send without a CORS preflight we never approve.
"""
import json
import logging
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from coinsieve import charts, funnel, market, whatif
from coinsieve.config_edit import write_values
from coinsieve.store import Store

log = logging.getLogger(__name__)

HTML = Path(__file__).with_name("dashboard.html")
TIERS = ("new_launches", "emerging", "established")
ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
PRICE_BATCH = 50   # Jupiter Price v3 returns at most 50 ids per call
MAX_PRICE_IDS = 100
LOGO_RETRY_S = 3600  # a logo that failed to download/decode is retried after this
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
        self.price_cache, self.price_lock = {}, threading.Lock()  # address -> (fetched_at, {price, change_24h})
        self.logo_dir = self.root / cfg["storage"]["logo_dir"]
        self.icons, self.bad_icons = {}, {}  # address -> icon URL (from stored metrics); URL -> failed_at
        self.market_busy = threading.Lock()  # one market refresh at a time

    def logo(self, address):
        """Vetted, cached PNG for a token's logo, or None. The URL comes from stored metrics, never the request."""
        url = self.icons.get(address)
        if not url or time.time() - self.bad_icons.get(url, 0) < LOGO_RETRY_S:
            return None
        from coinsieve import logos  # lazy: Pillow + requests only when a logo is actually needed
        png = logos.logo_png(url, self.logo_dir)
        if png is None:
            self.bad_icons[url] = time.time()  # broken or rate-limited (IPFS 429): retry later, not every page load
        return png

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
                chart_rows = store.chart_rows(t["metrics"]["address"] for t in tokens)
                hours = cfg["charts"]["history_hours"][tier]
                self.icons.update({t["metrics"]["address"]: t["metrics"]["icon"] for t in tokens if t["metrics"].get("icon")})
                members = set(store.current_members(tier))
                events = [dict(e) for e in store.events_since(week_ago, tier)]
                first = store.tier_first_event_ts(tier) or 0
                events = [e for e in events if e["ts"] > first + cfg["publishing"]["bootstrap_hours"] * 3600]
                tiers[tier] = {
                    "label": LABELS[tier], "last_run": ts, "stats": store.latest_run_stats(tier),
                    "members": len(members),
                    "tokens": [{"metrics": t["metrics"], "reasons": t["reasons"],
                                "member": t["metrics"]["address"] in members,
                                # read from the SQLite cache only; never fetched per page load
                                "chart": charts.view(chart_rows.get(t["metrics"]["address"]), hours, cfg, time.time())}
                               for t in tokens],
                    "chart_hours": hours,
                    "events": events[-60:],
                    "fields": [{"path": p, "label": lab, "kind": k, "help": h} for p, lab, k, h in whatif.FIELDS[tier]],
                    "values": whatif.current_values(cfg, tier),
                    "presets": whatif.presets(cfg, tier),
                    "typical": whatif.typical(tier, tokens),
                }
            return {"now": time.time(), "mode": "public" if self.public else "admin", "tiers": tiers,
                    "reason_labels": funnel.REASON_LABELS, "run": self.runner.status(),
                    "warn_pct": cfg["publishing"]["concentration_warning_pct"],
                    "price_refresh_s": cfg["api"]["price_refresh_s"],
                    "disclaimer": cfg["publishing"]["disclaimer"]}
        finally:
            store.close()

    def market(self):
        """Cached market strip; starts a background refresh when the cache is older than refresh_minutes.
        Page loads never wait on the external APIs."""
        cfg, now = self.load_cfg(), time.time()
        store = Store(str(self.db_path))
        try:
            history = store.market_snapshots(now - cfg["market"]["keep_days"] * 86400)
        finally:
            store.close()
        latest = history[-1] if history else None
        if (not latest or now - latest["ts"] >= cfg["market"]["refresh_minutes"] * 60) and not self.market_busy.locked():
            threading.Thread(target=self.refresh_market, args=(cfg, latest), daemon=True).start()
        return {"market": market.view(latest, history, cfg, now)}

    def refresh_market(self, cfg, latest):
        if not self.market_busy.acquire(blocking=False):
            return
        try:
            data = market.fetch(cfg, latest["data"] if latest else None)
            store = Store(str(self.db_path))
            try:
                store.add_market_snapshot(data, data["ts"], cfg["market"]["keep_days"])
            finally:
                store.close()
        except Exception:
            log.exception("market refresh failed")
        finally:
            self.market_busy.release()

    def record_demand(self, ids, now):
        """Rows on screen get their price charts fetched first by the next run (no API call here)."""
        if not ids:
            return
        try:
            store = Store(str(self.db_path))
            try:
                store.add_chart_demand(ids, now)
            finally:
                store.close()
        except Exception as e:  # e.g. database locked during a run: demand is a hint, not essential
            log.debug("chart demand not recorded: %s", e)

    def prices(self, ids):
        """Live USD prices from Jupiter Price v3, cached per address for price_refresh_s. Read-only."""
        api = self.load_cfg()["api"]
        ids = list(dict.fromkeys(a for a in ids if ADDR.match(a)))[:MAX_PRICE_IDS]
        now = time.time()
        self.record_demand(ids, now)
        with self.price_lock:
            stale = [a for a in ids if now - self.price_cache.get(a, (0, None))[0] >= api["price_refresh_s"]]
        for i in range(0, len(stale), PRICE_BATCH):
            batch = stale[i:i + PRICE_BATCH]
            url = api["jupiter_price_url"] + "?" + urllib.parse.urlencode({"ids": ",".join(batch)})
            req = urllib.request.Request(url, headers={"User-Agent": "coin-sieve/1.0"})  # default urllib UA gets 403
            try:
                with urllib.request.urlopen(req, timeout=api["timeout_s"]) as r:
                    data = json.load(r)
            except Exception as e:  # keep serving cached values; the page shows "–" for unknowns
                log.warning("price fetch failed: %s", e)
                continue
            with self.price_lock:
                for a in batch:
                    p = data.get(a) or {}
                    self.price_cache[a] = (now, {"price": p.get("usdPrice"), "change_24h": p.get("priceChange24h")})
        with self.price_lock:
            return {"ts": now, "prices": {a: self.price_cache[a][1] for a in ids if a in self.price_cache}}

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

        def send(self, code, payload, ctype="application/json", cache="no-store"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                if self.path == "/":
                    return self.send(200, HTML.read_bytes(), "text/html; charset=utf-8")
                if self.path == "/api/state":
                    return self.send(200, app.state())
                if self.path == "/api/market":
                    return self.send(200, app.market())
                if self.path == "/api/run":
                    return self.send(200, app.runner.status())
                if self.path.startswith("/api/prices?"):
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    return self.send(200, app.prices(",".join(q.get("ids", [])).split(",")))
                if self.path.startswith("/api/logo/"):
                    addr = self.path[len("/api/logo/"):]
                    png = app.logo(addr) if ADDR.match(addr) else None
                    if png is None:
                        return self.send(404, {"error": "no logo"})
                    return self.send(200, png, "image/png", "public, max-age=86400")
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
