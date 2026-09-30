"""`ndc dashboard`: read-only local page with the queue, usage windows, recent runs and the handoff.

Stdlib only, bound to 127.0.0.1. It never spends tokens: usage comes from the cached usage file, not from `/usage`.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import handoff, store
from .usage import UsageUnavailable, parse, usage_path


def snapshot(cfg: dict) -> dict:
    db = store.connect()
    try:
        tasks = [{k: t[k] for k in ("id", "title", "kind", "complexity", "risk", "status", "failures", "tier", "model",
                                    "depends_on", "notes")} for t in store.list_tasks(db)]
        runs = [dict(r) for r in db.execute("SELECT task_id, complexity, tier, ok, deltas, at FROM runs ORDER BY id DESC LIMIT 15")]
        text = handoff.build(db)
    finally:
        db.close()
    try:
        p = usage_path(cfg)
        us = parse(p.read_text(), None, None)
        windows = [{"name": w.name, "used": w.used_pct, "resets": w.resets_at.isoformat() if w.resets_at else None}
                   for w in us.windows]
    except (UsageUnavailable, OSError, ValueError):
        windows = None  # unknown: the runner fails closed too
    counts = {}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {"counts": counts, "tasks": tasks, "runs": runs, "usage": windows, "handoff": text}


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>NDC dashboard</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--ok:#1a7f37;--bad:#cf222e;--run:#0969da;--wait:#9a6700}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--line:#30363d;--ok:#3fb950;--bad:#f85149;--run:#58a6ff;--wait:#d29922}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;max-width:1000px;margin-inline:auto}
h1{font-size:18px;margin:0 0 12px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:20px 0 6px}
table{width:100%;border-collapse:collapse}td,th{padding:4px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--run)}
.pill{padding:1px 8px;border-radius:9px;border:1px solid currentColor;font-size:12px}
.done{color:var(--ok)}.blocked{color:var(--bad)}.running{color:var(--run)}.pending{color:var(--wait)}
pre{white-space:pre-wrap;background:transparent;border:1px solid var(--line);padding:8px;border-radius:6px}
.row{display:flex;gap:16px;flex-wrap:wrap}.row>div{flex:1 1 220px}
</style>
<h1>NDC <small style="color:var(--mut)" id=t></small></h1>
<div class=row id=usage></div><h2>Queue</h2><div id=counts></div>
<table id=tasks></table><h2>Recent runs</h2><table id=runs></table><h2>Handoff</h2><pre id=h></pre>
<script>
const e=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const $=id=>document.getElementById(id);
async function tick(){try{const d=await(await fetch("/api/state")).json();
$("usage").innerHTML=d.usage?d.usage.map(w=>`<div><b>${e(w.name)}</b> ${w.used.toFixed(0)}% used<div class=bar><i style="width:${w.used}%"></i></div><small>resets ${e(w.resets||"?")}</small></div>`).join(""):"<div>usage unknown (the runner will not start tasks)</div>";
$("counts").innerHTML=Object.entries(d.counts).map(([k,v])=>`<span class="pill ${k}">${e(k)} ${v}</span> `).join("")||"empty";
$("tasks").innerHTML="<tr><th>#<th>title<th>kind<th>size<th>risk<th>tier<th>status</tr>"+d.tasks.map(t=>`<tr><td>${t.id}<td>${e(t.title)}${t.notes?`<br><small>${e(t.notes)}</small>`:""}<td>${e(t.kind)}<td>${t.complexity}<td>${t.risk}<td>${e(t.tier||"")}<td class=${t.status}>${t.status}${t.failures?` (${t.failures} fail)`:""}`).join("");
$("runs").innerHTML="<tr><th>task<th>size<th>tier<th>ok<th>usage delta<th>at</tr>"+d.runs.map(r=>`<tr><td>#${r.task_id}<td>${r.complexity}<td>${e(r.tier||"")}<td>${r.ok?"yes":"no"}<td>${e(r.deltas)}<td>${e(r.at)}`).join("");
$("h").textContent=d.handoff;$("t").textContent=new Date().toLocaleTimeString();}catch(x){$("t").textContent="offline"}}
tick();setInterval(tick,5000);
</script>"""


def serve(cfg: dict, port: int = 8765) -> None:
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state":
                body, kind = json.dumps(snapshot(cfg)).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, kind = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"NDC dashboard: http://127.0.0.1:{srv.server_address[1]}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
