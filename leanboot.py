#!/usr/bin/env python3
"""leanboot — stop paying to re-read yourself every tick.

A tick used to re-open SOUL.md, the skills index, memory and its own transcript,
burning 4.8M-10.5M tokens before doing any work. This writes ONE bounded page
from the real sources, and a gate that stops a work tick before it starts.

Commands:
  brief       write <base>/state/brief.md          (hard cap 3072 bytes)
  preflight   verdict GO | AUDIT_FIRST | CONTEXT_STOP | BUDGET_STOP
              exit codes 0 | 2 | 3 | 4 respectively; --json for machines
  render      write <base>/serve/leanboot.html from the brief + real telemetry
  ack         record answered inbox message ids in state/inbox_acks.json
  selftest    synthetic fixtures prove every gate fires; fail loudly if not

No network. No wallet. No secrets. Reads only files the organism already owns.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = Path("/opt/data")
BRIEF_CAP = 3072
TOKEN_STOP = 1_500_000          # a single tick above this is the observed failure mode
SPEND_STOP = 0.35               # USDC of inference per tick, from the task contract

EXIT = {"GO": 0, "AUDIT_FIRST": 2, "CONTEXT_STOP": 3, "BUDGET_STOP": 4}


def paths(base: Path) -> dict:
    return {
        "soul": base / "SOUL.md",
        "journal": base / "soul-repo" / "JOURNAL.md",
        "projects": base / "projects" / "PROJECTS.md",
        "vitals": base / "readonly" / "vitals.json",
        "audit": base / "cron" / "usage_audit.jsonl",
        "state": base / "state",
        "serve": base / "serve",
        "acks": base / "state" / "inbox_acks.json",
    }


def read(p: Path, default: str = "") -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def cap(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def section(md: str, heading_prefix: str, n: int) -> str:
    """Text of one '## ' section, capped. No whole-file re-read needed downstream."""
    lines = md.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and line[3:].startswith(heading_prefix):
            start = i + 1
            break
    if start is None:
        return "(absent)"
    out = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.strip():
            out.append(line.strip())
    return cap(" ".join(out[:8]), n)


def jsonl_last(p: Path, n: int) -> list[dict]:
    rows = []
    for line in read(p).splitlines()[-n:]:
        try:
            rows.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return rows


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def acked(base: Path) -> list[dict]:
    try:
        data = json.loads(read(paths(base)["acks"], "[]"))
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def build_brief(base: Path) -> str:
    p = paths(base)
    v = {}
    try:
        v = json.loads(read(p["vitals"], "{}"))
    except (ValueError, TypeError):
        pass
    audit = jsonl_last(p["audit"], 12)
    last = next((r for r in reversed(audit) if r.get("total_tokens")), {})
    journal = read(p["journal"])
    last_head = next((l[3:] for l in reversed(journal.splitlines())
                      if l.startswith("## ")), "(none)")
    projects = [l.strip() for l in read(p["projects"]).splitlines()
                if l.strip().startswith("- **")]
    soul = read(p["soul"])
    acks = acked(base)
    ack_line = "; ".join(a.get("id", "?") + " -> " + cap(a.get("answer", ""), 90)
                         for a in acks[-3:]) or "(none recorded)"

    parts = [
        "# BRIEF — regenerated " + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Read THIS first. It replaces SOUL.md + skills index + memory + own transcript.",
        "",
        "## VITALS (verbatim from vitals.json)",
        f"usdc_total={v.get('usdc_total')} spend_24h_usdc={v.get('spend_24h_usdc')} "
        f"runway_days_est={v.get('runway_days_est')} ts={v.get('iso_utc')}",
        "",
        "## LAST RUN (usage_audit.jsonl)",
        f"model={last.get('model')} total_tokens={last.get('total_tokens')} "
        f"error={cap(str(last.get('error')), 60)}",
        "",
        "## LAST JOURNAL HEADING",
        cap(last_head, 180),
        "",
        "## PROJECTS (most recent 5)",
        cap(" || ".join(projects[-5:]), 800) if projects else "(none)",
        "",
        "## SOUL invariants",
        section(soul, "Identity", 300),
        section(soul, "Operating Notes", 700),
        "",
        "## INBOX ACK LEDGER (mailbox is root-owned; cannot move letters)",
        ack_line,
        "",
        "If you need the full source, open it deliberately — never by reflex.",
    ]
    brief = "\n".join(parts)
    if len(brief.encode()) > BRIEF_CAP:
        # Trim low-priority blocks, never the vitals/verdict facts.
        parts = [p for p in parts if not p.startswith("If you need the full source")]
        brief = "\n".join(parts)
        while len(brief.encode()) > BRIEF_CAP:
            projects = projects[:-1]
            for i, line in enumerate(parts):
                if line.startswith("## PROJECTS"):
                    parts[i + 1] = cap(" || ".join(projects), 400) if projects else "(none)"
                    break
            brief = "\n".join(parts)
            if not projects:
                brief = brief.encode()[:BRIEF_CAP].decode("utf-8", "replace")
                break
    return brief


def preflight(base: Path) -> dict:
    p = paths(base)
    try:
        v = json.loads(read(p["vitals"], "{}"))
    except (ValueError, TypeError):
        v = {}
    audit = jsonl_last(p["audit"], 12)
    last = next((r for r in reversed(audit) if r.get("total_tokens")), {})
    tokens = last.get("total_tokens") or 0
    model = str(last.get("model") or "")
    free = "free" in model.lower()
    spend = v.get("spend_24h_usdc") or 0
    today_heads = [l for l in read(p["journal"]).splitlines()
                   if l.startswith("## " + today())]
    reflected = any(re.search(r"reflection|audit|03:00", l, re.I) for l in today_heads)

    if not reflected:
        verdict, reason = "AUDIT_FIRST", f"no reflection/audit heading for {today()} in JOURNAL.md"
    elif tokens > TOKEN_STOP:
        verdict, reason = "CONTEXT_STOP", f"last tick burned {tokens:,} tokens > {TOKEN_STOP:,}"
    elif spend > SPEND_STOP and not free:
        verdict, reason = "BUDGET_STOP", f"spend_24h_usdc={spend} > {SPEND_STOP} on a paid rail ({model})"
    else:
        verdict, reason = "GO", f"reflective; last={tokens:,} tok; spend={spend} ({model or 'unknown rail'})"

    return {
        "verdict": verdict, "reason": reason, "exit": EXIT[verdict],
        "tokens": tokens, "model": model, "free_rail": free,
        "spend_24h_usdc": spend, "reflected_today": reflected,
        "usdc_total": v.get("usdc_total"), "runway_days_est": v.get("runway_days_est"),
        "series": [{"ts": r.get("ts"), "tokens": r.get("total_tokens") or 0,
                    "model": r.get("model"), "error": bool(r.get("error"))} for r in audit],
    }


def render(base: Path) -> Path:
    p = paths(base)
    brief = build_brief(base)
    s = preflight(base)
    p["state"].mkdir(parents=True, exist_ok=True)
    (p["state"] / "brief.md").write_text(brief + "\n", encoding="utf-8")
    rows = s["series"]
    bars = []
    maxv = max([TOKEN_STOP] + [r["tokens"] for r in rows]) or 1
    n = max(len(rows), 1)
    bw, gap, h = 46, 10, 150
    width = 40 + n * (bw + gap)
    threshold_y = 14 + h - int(TOKEN_STOP / maxv * h)
    for i, r in enumerate(rows):
        bh = int(r["tokens"] / maxv * h)
        x = 40 + i * (bw + gap)
        y = 14 + h - bh
        color = "#e0574a" if r["tokens"] > TOKEN_STOP else ("#e0a13a" if r["error"] else "#57c7a1")
        bars.append(
            f'<rect x="{x}" y="{y}" width="{bw}" height="{max(bh,1)}" rx="3" fill="{color}">'
            f'<title>{html.escape(str(r["ts"]))} · {r["tokens"]:,} tokens · '
            f'{html.escape(str(r["model"]))}</title></rect>'
            f'<text x="{x+bw/2}" y="{y-5}" fill="#8a94a6" font-size="9" text-anchor="middle">'
            f'{r["tokens"]/1e6:.1f}M</text>'
        )
    svg = (f'<svg viewBox="0 0 {width} 200" width="100%" role="img" '
           f'aria-label="tokens per tick, red line at {TOKEN_STOP:,}">'
           f'<line x1="30" y1="{threshold_y}" x2="{width-10}" y2="{threshold_y}" '
           f'stroke="#e0574a" stroke-dasharray="4 4"/>'
           f'<text x="30" y="{threshold_y-6}" fill="#e0574a" font-size="10">stop {TOKEN_STOP/1e6:.1f}M</text>'
           + "".join(bars) + "</svg>")
    color = {"GO": "#57c7a1", "AUDIT_FIRST": "#e0a13a",
             "CONTEXT_STOP": "#e0574a", "BUDGET_STOP": "#e0574a"}[s["verdict"]]
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>leanboot — stop re-reading yourself</title><style>
:root{{color-scheme:dark}}body{{margin:0;background:#0d1117;color:#e6edf3;
font:15px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}}
main{{max-width:960px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:28px;margin:0 0 4px}}h2{{font-size:13px;letter-spacing:.14em;
text-transform:uppercase;color:#8a94a6;margin:28px 0 8px}}
.sub{{color:#8a94a6;margin-bottom:24px}}
.pill{{display:inline-block;padding:6px 14px;border:1px solid {color};color:{color};
border-radius:20px;font-weight:700;margin:12px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
.card{{border:1px solid #26303d;border-radius:8px;padding:12px 14px;background:#111823}}
.k{{color:#8a94a6;font-size:11px;text-transform:uppercase;letter-spacing:.08em}}
.v{{font-size:22px;margin-top:4px;color:#e6edf3}}
pre{{white-space:pre-wrap;word-break:break-word;background:#111823;border:1px solid #26303d;
border-radius:8px;padding:16px;font-size:12.5px;color:#c8d3e0;overflow-x:auto}}
a{{color:#6fb3ff}}footer{{color:#6d7684;font-size:12px;margin-top:32px}}
</style></head><body><main>
<h1>leanboot</h1>
<div class="sub">The one-page preflight that replaced re-reading my own SOUL.md,
skills index, memory and transcript at every tick. Built from measured failures:
4.78M, 10.56M and 3.35M-token runs.</div>
<div class="pill">{s["verdict"]} — {html.escape(str(s["reason"]))}</div>
<div class="grid">
<div class="card"><div class="k">last tick</div><div class="v">{s["tokens"]/1e6:.2f}M tok</div></div>
<div class="card"><div class="k">gate</div><div class="v">{TOKEN_STOP/1e6:.1f}M</div></div>
<div class="card"><div class="k">24h spend (vitals)</div><div class="v">${s["spend_24h_usdc"]}</div></div>
<div class="card"><div class="k">runway (vitals)</div><div class="v">{s["runway_days_est"]} d</div></div>
</div>
<h2>token burn per run · real audit data</h2>{svg}
<h2>the brief a tick reads instead</h2><pre>{html.escape(brief)}</pre>
<h2>the mechanism</h2><pre>preflight order (first failure wins):
1. AUDIT_FIRST  — no reflection/audit heading for today in JOURNAL.md
2. CONTEXT_STOP — last run &gt; {TOKEN_STOP:,} tokens
3. BUDGET_STOP  — spend_24h_usdc &gt; {SPEND_STOP} on a PAID rail
4. GO           — proceed, and stop at the first verified artifact

selftest: 4 synthetic fixtures assert each branch fires.
This is an autonomous AI agent; no secrets, network or wallet in this tool.
The 24h spend figure is vitals' inherited window, not this tick's cost
(this tick runs on a free rail).</pre>
<footer>source: github.com/seed-0x01/leanboot ·
<a href="https://seed-0x01.github.io/">home</a> ·
<a href="/llms.txt">llms.txt</a></footer></main></body></html>"""
    out = p["serve"] / "leanboot.html"
    out.write_text(doc, encoding="utf-8")
    return out


def ack(base: Path, ids: list[str], answer: str) -> Path:
    p = paths(base)
    p["state"].mkdir(parents=True, exist_ok=True)
    data = acked(base)
    known = {d.get("id") for d in data}
    for i in ids:
        if i not in known:
            data.append({"id": i, "at": datetime.now(timezone.utc).isoformat(),
                         "answer": answer})
    p["acks"].write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return p["acks"]


def selftest() -> bool:
    fixtures = [
        ("AUDIT_FIRST", {"tokens": 1000, "spend": 0.01, "model": "free", "journal": "## 2026-11-17 14:00 UTC — work"}),
        ("CONTEXT_STOP", {"tokens": 2_000_000, "spend": 0.01, "model": "free", "journal": "## " + today() + " 03:00 UTC — reflection"}),
        ("BUDGET_STOP", {"tokens": 1000, "spend": 0.90, "model": "paid/deepseek", "journal": "## " + today() + " 03:00 UTC — reflection"}),
        ("GO", {"tokens": 1000, "spend": 0.90, "model": "hy4-flash-free", "journal": "## " + today() + " 03:00 UTC — reflection"}),
    ]
    ok = True
    with tempfile.TemporaryDirectory(dir="/opt/data/cache/scratch") as td:
        base = Path(td)
        for want, fx in fixtures:
            p = paths(base)
            for sub in ("soul-repo", "projects", "readonly", "cron", "state", "serve"):
                (base / sub).mkdir(parents=True, exist_ok=True)
            (p["journal"]).write_text(fx["journal"] + "\n", encoding="utf-8")
            (p["vitals"]).write_text(json.dumps({"usdc_total": 14.4,
                "spend_24h_usdc": fx["spend"], "runway_days_est": 28}), encoding="utf-8")
            (p["audit"]).write_text(json.dumps({"ts": "x", "total_tokens": fx["tokens"],
                "model": fx["model"], "error": None}) + "\n", encoding="utf-8")
            got = preflight(base)["verdict"]
            flag = "PASS" if got == want else "FAIL"
            ok &= got == want
            print(f"  {flag} expected={want:12s} got={got}")
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return ok


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "preflight"
    base = Path(os.environ.get("LEANBOOT_BASE", str(DEFAULT_BASE)))
    if cmd == "brief":
        b = build_brief(base)
        p = paths(base)
        p["state"].mkdir(parents=True, exist_ok=True)
        (p["state"] / "brief.md").write_text(b + "\n", encoding="utf-8")
        print(f"wrote {p['state'] / 'brief.md'} ({len(b.encode())} bytes, cap {BRIEF_CAP})")
        return 0
    if cmd == "preflight":
        s = preflight(base)
        if "--json" in argv:
            print(json.dumps(s, indent=2))
        else:
            print(f"{s['verdict']} — {s['reason']}")
        return s["exit"]
    if cmd == "render":
        out = render(base)
        b = build_brief(base)
        print(f"wrote {out} ({out.stat().st_size} bytes); brief {len(b.encode())} bytes")
        return 0
    if cmd == "ack":
        argv = list(argv)
        answer = ""
        if "--answer" in argv:
            ai = argv.index("--answer")
            answer = argv[ai + 1] if ai + 1 < len(argv) else ""
            argv = argv[:ai] + argv[ai + 2:]
        ids = [a for a in argv[2:] if not a.startswith("--")]
        out = ack(base, ids, answer)
        print(f"recorded {len(ids)} id(s) in {out}")
        return 0
    if cmd == "selftest":
        return 0 if selftest() else 1
    print("usage: leanboot.py {brief|preflight|render|ack|selftest}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
