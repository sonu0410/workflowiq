"""
WorkflowIQ backend (FastAPI)
----------------------------
- /api/suggest : look at past post topics, propose a fresh idea for today.
- /api/generate: AI writes a caption + hashtags + image for each chosen
  platform; each goes into the approval queue with status "Pending".
- /api/regenerate: rewrite a draft you didn't like (new text + new image).
- /api/action  : approve / reject / publish a post. Nothing is marked
  published until a human approves it.
- /api/export.csv: download all posts as CSV (opens in Excel).
- A Telegram alert fires when new posts are waiting for approval (if set up).

Honest note: this tool drafts posts (text + a real generated image) and
manages an approval workflow. It does NOT auto-post to Instagram/Facebook/etc.
— that needs each platform's official API (Meta/LinkedIn), a separate
integration. Here an approved post is marked ready and you copy it to publish.
"""

import os
import io
import csv
import json
import uuid
import datetime
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import engine

HERE = os.path.dirname(__file__)
POSTS = os.path.join(HERE, "posts.jsonl")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

VALID = {"approve": "Approved", "reject": "Rejected", "publish": "Published",
         "unapprove": "Pending"}

app = FastAPI(title="WorkflowIQ")


def read_posts():
    items = []
    if os.path.exists(POSTS):
        for line in open(POSTS, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    return items


def write_posts(items):
    with open(POSTS, "w", encoding="utf-8") as f:
        for i in items:
            f.write(json.dumps(i, ensure_ascii=False) + "\n")


def _norm(text):
    return " ".join((text or "").lower().split())


def generate_unique(topic, platform, tone, brand, existing):
    """Generate a post and make sure its caption isn't an exact repeat of one
    we've already stored for this platform. Retries a few times if it is."""
    seen = {_norm(c) for c in existing}
    gen = None
    for _ in range(3):
        gen = engine.generate_post(topic, platform, tone, brand, avoid=existing)
        if _norm(gen["caption"]) not in seen:
            return gen
    return gen  # give up after retries, return the last one


def notify(n, topic):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    text = f"\U0001F4DD {n} new post(s) waiting for your approval\nTopic: “{topic}”"
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)


@app.get("/api/suggest")
async def suggest():
    topics = [p.get("topic", "") for p in read_posts()]
    return JSONResponse({"idea": engine.suggest_idea(topics)})


@app.post("/api/generate")
async def generate(request: Request):
    body = await request.json()
    topic = (body.get("topic") or "").strip()
    tone = (body.get("tone") or "friendly").strip()
    brand = (body.get("brand") or "").strip()
    platforms = body.get("platforms") or ["Instagram"]
    if not topic:
        return JSONResponse({"error": "Enter a topic or product."}, status_code=400)

    items = read_posts()
    created = []
    for p in platforms[:4]:
        prior = [i["caption"] for i in items if i.get("platform") == p]
        gen = generate_unique(topic, p, tone, brand, prior)
        post = {
            "id": uuid.uuid4().hex[:8],
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "topic": topic, "platform": p, "tone": tone, "brand": brand,
            "caption": gen["caption"], "hashtags": gen["hashtags"],
            "image_prompt": gen["image_prompt"],
            "image_url": engine.image_url(gen["image_prompt"]),
            "status": "Pending",
        }
        items.append(post)
        created.append(post)
    write_posts(items)
    if created:
        notify(len(created), topic)
    return JSONResponse({"ok": True, "created": created})


@app.post("/api/regenerate")
async def regenerate(request: Request):
    """Rewrite a draft you didn't like: new caption/hashtags + a new image."""
    body = await request.json()
    pid = body.get("id")
    items = read_posts()
    for i in items:
        if i["id"] == pid:
            plat = i.get("platform", "Instagram")
            prior = [x["caption"] for x in items if x.get("platform") == plat]
            gen = generate_unique(i.get("topic", ""), plat,
                                  i.get("tone", "friendly"), i.get("brand", ""), prior)
            i["caption"] = gen["caption"]
            i["hashtags"] = gen["hashtags"]
            i["image_prompt"] = gen["image_prompt"]
            i["image_url"] = engine.image_url(gen["image_prompt"])
            i["status"] = "Pending"
            write_posts(items)
            return JSONResponse({"ok": True, "post": i})
    return JSONResponse({"error": "Post not found."}, status_code=404)


@app.post("/api/action")
async def action(request: Request):
    body = await request.json()
    pid = body.get("id")
    act = body.get("action")
    if act not in VALID:
        return JSONResponse({"error": "Unknown action."}, status_code=400)
    items = read_posts()
    found = False
    for i in items:
        if i["id"] == pid:
            i["status"] = VALID[act]
            if "caption" in body and body["caption"] is not None:
                i["caption"] = str(body["caption"])
            if "hashtags" in body and body["hashtags"] is not None:
                i["hashtags"] = str(body["hashtags"])
            found = True
            break
    if not found:
        return JSONResponse({"error": "Post not found."}, status_code=404)
    write_posts(items)
    return JSONResponse({"ok": True})


@app.get("/api/posts")
async def posts():
    return JSONResponse({"posts": list(reversed(read_posts()))})


@app.get("/api/stats")
async def stats():
    items = read_posts()
    by_status = {"Pending": 0, "Approved": 0, "Published": 0, "Rejected": 0}
    by_platform = {}
    for i in items:
        s = i.get("status", "Pending")
        by_status[s] = by_status.get(s, 0) + 1
        p = i.get("platform", "Instagram")
        by_platform[p] = by_platform.get(p, 0) + 1
    return JSONResponse({
        "total": len(items), "by_status": by_status, "by_platform": by_platform,
        "ai": bool(engine.GROQ_API_KEY),
    })


@app.get("/api/export.csv")
async def export_csv():
    items = read_posts()
    buf = io.StringIO()
    cols = ["time", "topic", "platform", "tone", "status", "caption", "hashtags", "image_prompt"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for i in items:
        w.writerow(i)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=workflowiq_posts.csv"})


@app.get("/health")
async def health():
    return {"ok": True, "posts": len(read_posts()), "ai": bool(engine.GROQ_API_KEY)}


app.mount("/", StaticFiles(directory=os.path.join(HERE, "public"), html=True), name="static")
