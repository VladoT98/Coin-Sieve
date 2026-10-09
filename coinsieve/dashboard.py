"""Website + private dashboard: http://127.0.0.1:<port> — the coverage list ("Tokens"), token profiles,
Methodology; admin mode adds coverage-criteria what-if, saving and a refresh button.

Reads only from SQLite and the logo cache: background jobs (`sieve.py run|profiles|holders|charts`) do all
fetching. Exception until Stage E: the market strip is still refreshed by a server thread.
Standard library only. Binds to 127.0.0.1. Write endpoints require the custom header X-Coin-Sieve: 1, which a
foreign web page cannot send without a CORS preflight we never approve.
"""
import json
import logging
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from coinsieve import charts, coverage, facts, funnel, history, market, site_text, whatif
from coinsieve.config_edit import write_values
from coinsieve.digest import blocked_reason
from coinsieve.sanitize import clean_text
from coinsieve.store import Store

log = logging.getLogger(__name__)

HTML = Path(__file__).with_name("dashboard.html")
TIERS = ("coverage",)   # the site shows the coverage list only (other tiers stay runnable from the CLI)
ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


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
        self.logo_dir = self.root / cfg["storage"]["logo_dir"]
        self.icons = {}  # address -> icon URL (from stored profiles/metrics, never from the request)
        self.market_busy = threading.Lock()  # one market refresh at a time

    def logo(self, address):
        """Cached PNG for a token's logo, or None. Read from the logo cache only (`sieve.py profiles` fills it);
        the URL comes from stored data, never the request."""
        url = self.icons.get(address)
        if not url:
            return None
        from coinsieve import logos  # lazy: Pillow only when a logo is served
        path = logos._cache_path(url, self.logo_dir)
        return path.read_bytes() if path.exists() else None

    def load_cfg(self):
        with open(self.config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def tier_tokens(self, store, tier):
        ts = store.latest_run_ts(tier)
        return ts, (store.latest_since(tier, ts) if ts else [])

    def state(self):
        """Everything the page shows, from the database only."""
        cfg, now = self.load_cfg(), time.time()
        chain, pub, texts = cfg["chain"], cfg["publishing"], site_text.load()
        store = Store(str(self.db_path))
        try:
            ts, tokens = self.tier_tokens(store, coverage.TIER)
            members = set(store.current_members(coverage.TIER))
            if self.public:  # visitors see the covered list only
                tokens = [t for t in tokens if t["metrics"]["address"] in members]
            addrs = [t["metrics"]["address"] for t in tokens]
            profs, hold = store.profiles(chain, addrs), store.holder_reports(chain, addrs)
            chart_rows, hours = store.chart_rows(addrs), cfg["charts"]["history_hours"][coverage.TIER]
            rows = []
            for t in tokens:
                m, a = t["metrics"], t["metrics"]["address"]
                prof = profs.get(a) or {}
                p = prof.get("data") or {}
                icon = p.get("icon") or m.get("icon")
                if icon:
                    self.icons[a] = icon
                chks = coverage.checks(m, cfg)
                rows.append({
                    "metrics": {**m, **display_names(m, pub, texts)}, "ts": t["ts"], "member": a in members,
                    "status": coverage.status(chks), "checks": chks, "reasons": [] if self.public else t["reasons"],
                    "links": p.get("links"), "supply": p.get("supply"), "profile_at": prof.get("updated_at"),
                    "holders": holders_view(hold.get(a)), "unlock": None, "logo": bool(icon),
                    # read from the SQLite cache only; never fetched per page load
                    "chart": charts.view(chart_rows.get(a), hours, cfg, now),
                })
            out = {"now": now, "mode": "public" if self.public else "admin", "chain": chain, "texts": texts,
                   "last_run": ts, "stale_after_s": cfg["coverage"]["stale_after_minutes"] * 60,
                   "members": len(members), "tokens": rows, "chart_hours": hours,
                   "criteria": whatif.current_values(cfg, coverage.TIER),
                   "exclude_tags": cfg["jupiter"]["exclude_tags"], "sources": self.sources(store, ts),
                   "columns": {"max": cfg["site"]["columns_max"], "default": cfg["site"]["default_columns"]},
                   "unlocks_public": cfg["unlocks"]["public"],
                   "facts": cfg["facts"],
                   "run": None if self.public else self.runner.status()}
            if not self.public:
                out["fields"] = [{"path": p, "label": lab, "kind": k, "help": h}
                                 for p, lab, k, h in whatif.FIELDS[coverage.TIER]]
                out["reason_labels"] = funnel.REASON_LABELS
            return out
        finally:
            store.close()

    def token_detail(self, address):
        """Token page extras (chart history, holder history, points to check, fetch times), or None.
        Public mode: covered tokens only. From the database only."""
        cfg, now = self.load_cfg(), time.time()
        chain = cfg["chain"]
        store = Store(str(self.db_path))
        try:
            ts, tokens = self.tier_tokens(store, coverage.TIER)
            t = next((x for x in tokens if x["metrics"]["address"] == address), None)
            if t is None or (self.public and address not in store.current_members(coverage.TIER)):
                return None
            m = t["metrics"]
            prof = store.profiles(chain, [address]).get(address) or {}
            hold = store.holder_reports(chain, [address]).get(address)
            hv, hist = holders_view(hold), store.holder_history(address)
            series = history.view(store.price_history(chain, address), cfg, now)
            return {
                "address": address, "history": series, "holder_history": hist,
                "points": facts.points(m, hv, (prof.get("data") or {}).get("supply"), m.get("links_found"),
                                       m.get("profile_checked"), hist, cfg),
                "fetched": {"market": t["ts"], "links": prof.get("updated_at"), "holders": hold and hold["ts"],
                            "chart": max((s["fetched_at"] or 0 for s in series.values()), default=None) or None},
            }
        finally:
            store.close()

    @staticmethod
    def sources(store, coverage_ts):
        """Last successful fetch per data source (Methodology page)."""
        prof, hold = store.max_ts("token_profiles", "updated_at"), store.max_ts("holder_reports", "ts")
        return {"jupiter": coverage_ts, "dexscreener": prof, "coingecko": prof, "homepage": prof, "rugcheck": hold,
                "solana_rpc": hold, "geckoterminal": store.max_ts("chart_series", "fetched_at"),
                "market": store.max_ts("market_snapshots", "ts")}

    def market_view(self):
        """(market strip from the cache, latest snapshot or None). No API calls."""
        cfg, now = self.load_cfg(), time.time()
        store = Store(str(self.db_path))
        try:
            history = store.market_snapshots(now - cfg["market"]["keep_days"] * 86400)
        finally:
            store.close()
        latest = history[-1] if history else None
        return {"market": market.view(latest, history, cfg, now)}, latest

    def market(self):
        """Cached market strip; starts a background refresh when the cache is older than refresh_minutes.
        Page loads never wait on the external APIs."""
        cfg, now = self.load_cfg(), time.time()
        view, latest = self.market_view()
        if (not latest or now - latest["ts"] >= cfg["market"]["refresh_minutes"] * 60) and not self.market_busy.locked():
            threading.Thread(target=self.refresh_market, args=(cfg, latest), daemon=True).start()
        return view

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

    def evaluate(self, body):
        if self.public:
            raise PermissionError("read-only in public mode")
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


def display_names(m, pub, texts):
    """Token names are untrusted: cleaned (no URLs, handles, control characters), length-capped, and hidden when
    they contain a banned or blocked word."""
    hidden = blocked_reason(m, pub) is not None
    return {"symbol": "?" if hidden else clean_text(m.get("symbol"), pub["max_symbol_len"]) or "?",
            "name": texts["table"]["name_hidden"] if hidden else clean_text(m.get("name"), pub["max_name_len"])}


def holders_view(row):
    if not row:
        return None
    d = row["data"]
    return {"ts": row["ts"], "top10_excl_pct": d.get("top10_excl_pct"), "top10_all_pct": d.get("top10_all_pct"),
            "excluded_pct": d.get("excluded_pct"), "complete": d.get("complete"), "holders": d.get("holders", [])[:20],
            "authorities": d.get("authorities")}


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
                    return self.send(200, None if app.public else app.runner.status())
                if self.path.startswith("/api/token/"):
                    addr = self.path[len("/api/token/"):]
                    d = app.token_detail(addr) if ADDR.match(addr) else None
                    return self.send(200, d) if d else self.send(404, {"error": "unknown token"})
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


def export(config_path, out_dir):
    """Static copy of the public site for GitHub Pages: index.html (static mode) + api/state.json,
    api/market.json + logos/<address>.png from the logo cache. Same data the public server would send."""
    import shutil
    from coinsieve import logos
    app, out = App(config_path, public=True), Path(out_dir)
    (out / "api" / "token").mkdir(parents=True, exist_ok=True)
    (out / "logos").mkdir(exist_ok=True)
    state = app.state()
    for t in state["tokens"]:
        url, addr = app.icons.get(t["metrics"]["address"]), t["metrics"]["address"]
        cached = logos._cache_path(url, app.logo_dir) if url else None
        t["logo"] = bool(cached and cached.exists() and ADDR.match(addr))
        if t["logo"]:
            shutil.copyfile(cached, out / "logos" / f"{addr}.png")
        detail = app.token_detail(addr) if ADDR.match(addr) else None
        if detail:  # token page data (chart, holder history, points to check)
            (out / "api" / "token" / f"{addr}.json").write_text(json.dumps(detail), encoding="utf-8")
    (out / "api" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (out / "api" / "market.json").write_text(json.dumps(app.market_view()[0]), encoding="utf-8")
    html = HTML.read_text(encoding="utf-8").replace("<script>\n\"use strict\";",
                                                    "<script>window.CS_STATIC = true;</script>\n<script>\n\"use strict\";", 1)
    if "window.CS_STATIC" not in html:
        raise RuntimeError("could not mark the page as static")
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")  # serve files as-is (no Jekyll processing)
    return len(state["tokens"])


def refresh_market(config_path):
    """Fetch the market strip once and store it (scheduled job; the local server does this in a thread)."""
    app = App(config_path, public=True)
    _, latest = app.market_view()
    app.refresh_market(app.load_cfg(), latest)


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
