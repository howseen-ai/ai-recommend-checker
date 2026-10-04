#!/usr/bin/env python3
"""ai-recommend-checker: who does ChatGPT recommend instead of you?

Asks real buyer questions to an OpenAI model with live web search, counts which
brands it names (and in what order), lists the pages it cites, then renders a
shareable PNG card.

Standard library only. Playwright is optional (only to turn the card HTML into a PNG).

  python3 checker.py run  config.json --backend openai      # needs OPENAI_API_KEY
  python3 checker.py run  config.json --backend dataforseo  # needs DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD
  python3 checker.py card runs/<folder>                     # results.json + card.html + card.png
"""
import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "ai-recommend-checker/1.0"


# ---------------------------------------------------------------- backends

def _post(url, payload, headers, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from {url}: {e.read().decode()[:500]}")


def ask_openai(question, model, country):
    """OpenAI Responses API + the web_search tool.
    Docs: https://developers.openai.com/api/docs/guides/tools-web-search
    Answer text = message.content[].text, citations = its url_citation annotations."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY is not set. export OPENAI_API_KEY=sk-... and run again.")
    tool = {"type": "web_search"}
    if country:
        tool["user_location"] = {"type": "approximate", "country": country}
    raw = _post("https://api.openai.com/v1/responses",
                {"model": model, "tools": [tool], "input": question},
                {"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": UA})
    text, cites, searched = "", [], False
    for item in raw.get("output", []):
        if item.get("type") == "web_search_call":
            searched = True
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text += c.get("text", "")
                    for a in c.get("annotations", []) or []:
                        if a.get("type") == "url_citation":
                            cites.append({"url": a.get("url"), "title": a.get("title"),
                                          "start": a.get("start_index"), "end": a.get("end_index")})
    return {"text": text, "citations": cites, "web_search": searched, "cost_usd": None, "raw": raw}


def ask_dataforseo(question, model, country):
    """DataForSEO AI Optimization, ChatGPT LLM Responses (live).
    Docs: https://docs.dataforseo.com/v3/ai_optimization/chat_gpt/llm_responses/live/
    Non-reasoning models (gpt-4o-mini, gpt-4o, gpt-4.1...) only search the web when
    force_web_search is true. Reasoning models (gpt-5*) reject that flag and often skip search."""
    login, pwd = os.environ.get("DATAFORSEO_LOGIN"), os.environ.get("DATAFORSEO_PASSWORD")
    if not (login and pwd):
        sys.exit("Set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD first.")
    task = {"user_prompt": question[:500], "model_name": model, "web_search": True}
    if not model.startswith(("gpt-5", "o")):
        task["force_web_search"] = True
    if country:
        task["web_search_country_iso_code"] = country
    auth = base64.b64encode(f"{login}:{pwd}".encode()).decode()
    raw = _post("https://api.dataforseo.com/v3/ai_optimization/chat_gpt/llm_responses/live", [task],
                {"Content-Type": "application/json", "Authorization": f"Basic {auth}", "User-Agent": UA})
    t = (raw.get("tasks") or [{}])[0]
    if t.get("status_code") != 20000:
        sys.exit(f"DataForSEO error {t.get('status_code')}: {t.get('status_message')}")
    res = (t.get("result") or [{}])[0]
    text, cites = "", []
    for item in res.get("items") or []:
        if item.get("type") != "message":
            continue
        for s in item.get("sections") or []:
            offset = len(text)
            text += s.get("text") or ""
            for a in s.get("annotations") or []:
                cites.append({"url": a.get("url"), "title": a.get("title"),
                              "start": (a.get("start_index") or 0) + offset,
                              "end": (a.get("end_index") or 0) + offset, "span": a.get("text")})
    return {"text": text, "citations": cites, "web_search": bool(res.get("web_search")),
            "cost_usd": t.get("cost"), "raw": raw}


BACKENDS = {"openai": (ask_openai, "gpt-5.5"), "dataforseo": (ask_dataforseo, "gpt-4o-mini")}


# ---------------------------------------------------------------- helpers

def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def clean_url(u):
    """Drop tracking params (ChatGPT adds utm_source=openai) and fragments."""
    p = urlparse(u or "")
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    return urlunparse((p.scheme, p.netloc, p.path, "", urlencode(q), ""))


def domain_of(u):
    d = urlparse(u or "").netloc.lower()
    return d[4:] if d.startswith("www.") else d


def prose_only(text, citations):
    """Answer text without the citation links, so a retailer page title such as
    'Tatcha Water Cream - Sephora' does not count as the model naming Tatcha."""
    out = text
    for c in citations:
        span = c.get("span")
        if span:
            out = out.replace(span, " ")
    out = re.sub(r"\[([^\]]*)\]\((https?://[^)]+)\)", " ", out)  # leftover markdown links
    out = re.sub(r"https?://\S+", " ", out)
    return out


def brand_patterns(b):
    names = [b["name"]] + list(b.get("aliases", []))
    if "&" in b["name"]:
        names.append(b["name"].replace("&", "and"))
    return [re.compile(r"(?<![\w])" + re.escape(n).replace(r"\ ", r"[\s\-]?") + r"(?![\w])", re.I)
            for n in names if n]


def first_pos(text, b):
    hits = [m.start() for p in brand_patterns(b) for m in [p.search(text)] if m]
    return min(hits) if hits else None


# ---------------------------------------------------------------- commands

def cmd_run(a):
    cfg = json.load(open(a.config))
    ask, default_model = BACKENDS[a.backend]
    model = a.model or default_model
    date = dt.date.today().isoformat()
    out = a.out or os.path.join(HERE, "runs", f"{slugify(cfg['brand']['name'])}-{date}")
    os.makedirs(os.path.join(out, "raw"), exist_ok=True)
    answers, total = [], 0.0
    for i, q in enumerate(cfg["questions"], 1):
        print(f"[{i}/{len(cfg['questions'])}] {q}")
        r = ask(q, model, cfg.get("country", "US"))
        json.dump(r.pop("raw"), open(os.path.join(out, "raw", f"q{i:02d}.json"), "w"), indent=1, ensure_ascii=False)
        total += r["cost_usd"] or 0
        print(f"    web_search={r['web_search']}  citations={len(r['citations'])}  cost=${r['cost_usd']}")
        answers.append({"n": i, "question": q, **r})
    meta = {"brand": cfg["brand"], "category": cfg.get("category", ""), "competitors": cfg.get("competitors", []),
            "backend": a.backend, "model": model, "country": cfg.get("country", "US"),
            "date": date, "cost_usd": round(total, 6) if total else None}
    json.dump({"meta": meta, "answers": answers}, open(os.path.join(out, "answers.json"), "w"), indent=1, ensure_ascii=False)
    print(f"\nSaved {len(answers)} answers to {out}/answers.json (total cost: ${total:.4f})")
    print("Next: optionally add other brands to brands_found.json (see SKILL.md), then:")
    print(f"  python3 checker.py card {out}")


def cmd_card(a):
    d = json.load(open(os.path.join(a.dir, "answers.json")))
    meta, answers = d["meta"], d["answers"]
    you = dict(meta["brand"], you=True)
    brands = [you] + [dict(c) for c in meta["competitors"]]
    extra_path = os.path.join(a.dir, "brands_found.json")
    if os.path.exists(extra_path):  # other brands spotted in the answers (by you or by Claude)
        known = {b["name"].lower() for b in brands}
        for b in json.load(open(extra_path)).get("brands", []):
            if b["name"].lower() not in known:
                brands.append(b)
                known.add(b["name"].lower())

    n = len(answers)
    stats = {b["name"]: {"answers": 0, "ranks": [], "first": 0} for b in brands}
    pages = defaultdict(lambda: {"answers": set(), "hits": 0, "title": ""})
    per_q = []
    for ans in answers:
        prose = prose_only(ans["text"], ans["citations"])
        # a brand the buyer typed in the question ("cheaper alternative to Tatcha") is not a recommendation
        asked = {b["name"] for b in brands if first_pos(ans["question"], b) is not None}
        found = sorted((first_pos(prose, b), b["name"]) for b in brands
                       if b["name"] not in asked and first_pos(prose, b) is not None)
        order = [name for _, name in found]
        for rank, name in enumerate(order, 1):
            stats[name]["answers"] += 1
            stats[name]["ranks"].append(rank)
            if rank == 1:
                stats[name]["first"] += 1
        for c in ans["citations"]:
            u = clean_url(c["url"])
            pages[u]["answers"].add(ans["n"])
            pages[u]["hits"] += 1
            pages[u]["title"] = pages[u]["title"] or (c.get("title") or "").split("\n")[0].strip()
        per_q.append({"n": ans["n"], "question": ans["question"], "brands_in_order": order,
                      "excluded_because_in_question": sorted(asked),
                      "web_search": ans["web_search"], "citations": len(ans["citations"])})

    ranking = []
    for b in brands:
        s = stats[b["name"]]
        if s["answers"] or b.get("you") or b in brands[1:1 + len(meta["competitors"])]:
            ranking.append({"name": b["name"], "domain": b.get("domain", ""), "you": bool(b.get("you")),
                            "answers": s["answers"], "rate": round(s["answers"] / n, 3) if n else 0,
                            "named_first": s["first"],
                            "avg_position": round(sum(s["ranks"]) / len(s["ranks"]), 2) if s["ranks"] else None})
    ranking.sort(key=lambda r: (-r["answers"], r["avg_position"] or 99, r["name"]))
    top_pages = sorted(({"url": u, "domain": domain_of(u), "title": p["title"], "answers": len(p["answers"]),
                         "citations": p["hits"]} for u, p in pages.items()),
                       key=lambda p: (-p["answers"], -p["citations"], p["url"]))
    own = meta["brand"].get("domain", "").lower()
    own_cited = len({ans["n"] for ans in answers for c in ans["citations"]
                     if own and domain_of(c["url"]).endswith(own)})
    results = {"meta": meta, "questions": n, "searched": sum(1 for a in answers if a["web_search"]),
               "ranking": ranking, "top_pages": top_pages[:10], "own_site_cited_in": own_cited,
               "per_question": per_q}
    json.dump(results, open(os.path.join(a.dir, "results.json"), "w"), indent=1, ensure_ascii=False)

    page = render_html(results, a.footer)
    hp = os.path.join(a.dir, "card.html")
    open(hp, "w").write(page)
    png = os.path.join(a.dir, "card.png")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page(viewport={"width": 1200, "height": 900}, device_scale_factor=2)
            pg.goto("file://" + os.path.abspath(hp))
            pg.wait_for_load_state("networkidle")
            pg.locator("#card").screenshot(path=png)
            br.close()
        print(f"Card: {png}")
    except ImportError:
        print(f"Card HTML: {hp}\n(pip install playwright && playwright install chromium to get the PNG)")
    you_row = next(r for r in ranking if r["you"])
    print(f"{meta['brand']['name']}: named in {you_row['answers']}/{n} answers. Top: "
          + ", ".join(f"{r['name']} {r['answers']}/{n}" for r in ranking[:5]))


# ---------------------------------------------------------------- card

def render_html(r, footer):
    m, n = r["meta"], r["questions"]
    esc = html.escape
    you = next(x for x in r["ranking"] if x["you"])
    rows = r["ranking"][:8]
    if you not in rows:
        rows = rows[:7] + [you]
    top = max([x["answers"] for x in rows] + [1])

    def logo(domain, name):
        if domain:
            return (f'<img class="logo" src="https://www.google.com/s2/favicons?domain={esc(domain)}&sz=128" '
                    f'onerror="this.replaceWith(Object.assign(document.createElement(\'div\'),'
                    f'{{className:\'logo ph\',textContent:\'{esc(name[:1])}\'}}))">')
        return f'<div class="logo ph">{esc(name[:1].upper())}</div>'

    bars = ""
    for i, x in enumerate(rows, 1):
        w = max(2, round(100 * x["answers"] / top)) if x["answers"] else 0
        pos = f'avg. position {x["avg_position"]}' if x["avg_position"] else "never named"
        bars += f"""<div class="row{' you' if x['you'] else ''}">
  <div class="rank">{i if x in r['ranking'][:8] else '#' + str(r['ranking'].index(x) + 1)}</div>{logo(x['domain'], x['name'])}
  <div class="mid"><div class="name">{esc(x['name'])}{' <span class="tag">YOU</span>' if x['you'] else ''}</div>
    <div class="track"><div class="fill" style="width:{w}%"></div></div><div class="sub">{pos}</div></div>
  <div class="num">{x['answers']}<span>/{n}</span></div></div>"""

    plist = ""
    for i, p in enumerate(r["top_pages"][:5], 1):
        title = p["title"] or p["url"]
        title = title if len(title) <= 70 else title[:67].rstrip() + "..."
        plist += f"""<div class="page">{logo(p['domain'], p['domain'])}
  <div class="mid"><div class="ptitle">{esc(title)}</div><div class="sub">{esc(p['domain'])} · in {p['answers']} answer{'s' if p['answers'] != 1 else ''}</div></div>
  <div class="pnum">{p['citations']}<span> cite{'s' if p['citations'] != 1 else ''}</span></div></div>"""

    d0 = dt.date.fromisoformat(m["date"])
    date = f"{d0:%b} {d0.day}, {d0.year}"
    engine = f"OpenAI {m['model']} + web search, via API"
    rate = round(100 * you["answers"] / n) if n else 0
    headline = (f"answers name {m['brand']['name']} ({rate}%)" if you["answers"]
                else f"answers name {m['brand']['name']}")
    comp = " · ".join(f"{esc(x['name'])} <b>{x['answers']}/{n}</b>" for x in r["ranking"]
                      if x["name"] in {c["name"] for c in m["competitors"]})
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#050507;font-family:Inter,system-ui,sans-serif;color:#F4F4F6;width:1200px}}
#card{{width:1200px;padding:64px 64px 48px;background:
 radial-gradient(900px 500px at 85% -10%,rgba(124,92,255,.28),transparent 60%),
 radial-gradient(700px 500px at -10% 110%,rgba(255,92,138,.16),transparent 60%),#0A0A0F}}
.kicker{{font-size:22px;letter-spacing:.14em;text-transform:uppercase;color:#9C9CB0;font-weight:600}}
h1{{font-size:64px;line-height:1.05;font-weight:800;letter-spacing:-.02em;margin:14px 0 26px}}
h1 em{{font-style:normal;background:linear-gradient(90deg,#B9A6FF,#FF8FB1);-webkit-background-clip:text;color:transparent}}
.hero{{display:flex;gap:28px;align-items:stretch;margin-bottom:40px}}
.stat{{flex:0 0 330px;border-radius:24px;padding:28px 30px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09)}}
.big{{font-size:96px;font-weight:800;line-height:1;letter-spacing:-.03em}}
.big span{{font-size:44px;color:#9C9CB0}}
.statl{{font-size:24px;color:#C9C9D6;margin-top:10px;line-height:1.3}}
.ctx{{flex:1;border-radius:24px;padding:28px 30px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09);font-size:25px;line-height:1.45;color:#C9C9D6}}
.ctx b{{color:#fff}}
h2{{font-size:24px;letter-spacing:.12em;text-transform:uppercase;color:#9C9CB0;font-weight:700;margin:0 0 18px}}
.row{{display:flex;align-items:center;gap:20px;padding:16px 22px;border-radius:18px;margin-bottom:10px;background:rgba(255,255,255,.03)}}
.row.you{{background:rgba(255,143,177,.10);border:1px solid rgba(255,143,177,.45)}}
.rank{{width:44px;font-size:26px;font-weight:700;color:#7C7C90;text-align:center}}
.logo{{width:52px;height:52px;border-radius:14px;background:#fff;padding:7px;object-fit:contain;flex:0 0 52px}}
.logo.ph{{display:flex;align-items:center;justify-content:center;font-weight:800;font-size:26px;color:#0A0A0F;background:#D9D3FF}}
.mid{{flex:1;min-width:0}}
.name{{font-size:30px;font-weight:700}}
.tag{{font-size:16px;font-weight:800;letter-spacing:.1em;background:#FF8FB1;color:#0A0A0F;border-radius:8px;padding:3px 8px;vertical-align:middle;margin-left:8px}}
.track{{height:12px;border-radius:6px;background:rgba(255,255,255,.07);margin:10px 0 6px;overflow:hidden}}
.fill{{height:100%;border-radius:6px;background:linear-gradient(90deg,#7C5CFF,#B9A6FF)}}
.you .fill{{background:linear-gradient(90deg,#FF5C8A,#FF8FB1)}}
.sub{{font-size:19px;color:#8E8EA3}}
.num{{font-size:44px;font-weight:800;min-width:110px;text-align:right}}
.num span,.pnum span{{font-size:22px;color:#8E8EA3;font-weight:600}}
.pages{{margin-top:36px}}
.page{{display:flex;align-items:center;gap:20px;padding:14px 22px;border-radius:18px;margin-bottom:10px;background:rgba(255,255,255,.03)}}
.ptitle{{font-size:24px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.pnum{{font-size:32px;font-weight:800;text-align:right;min-width:170px}}
.foot{{display:flex;justify-content:space-between;margin-top:30px;font-size:19px;color:#7C7C90}}
</style></head><body><div id="card">
<div class="kicker">{esc(m['category'])} · {n} real buyer questions · {date}</div>
<h1>Who ChatGPT recommends<br>instead of <em>{esc(m['brand']['name'])}</em></h1>
<div class="hero"><div class="stat"><div class="big">{you['answers']}<span>/{n}</span></div>
<div class="statl">{esc(headline)}</div></div>
<div class="ctx">Your competitors: {comp}<br>{esc(m['brand']['name'])}'s own site was cited in <b>{r['own_site_cited_in']} of {n}</b> answers.<br>
Model: {esc(engine)}. Web search ran on <b>{r['searched']} of {n}</b> questions.</div></div>
<h2>Brands named, by number of answers</h2>{bars}
<div class="pages"><h2>Pages it cited most</h2>{plist}</div>
<div class="foot"><div>{esc(footer)}</div><div>One run = one snapshot, not a trend</div></div>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="ask the questions and save the raw answers")
    r.add_argument("config")
    r.add_argument("--backend", choices=BACKENDS, default="openai")
    r.add_argument("--model", help="openai default gpt-5.5, dataforseo default gpt-4o-mini")
    r.add_argument("--out")
    c = sub.add_parser("card", help="count brands and pages, render the PNG card")
    c.add_argument("dir")
    c.add_argument("--footer", default="ai-recommend-checker · free and open source")
    a = ap.parse_args()
    cmd_run(a) if a.cmd == "run" else cmd_card(a)


if __name__ == "__main__":
    main()
