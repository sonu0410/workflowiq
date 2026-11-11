"""
WorkflowIQ engine
-----------------
- generate_post(): writes a platform-specific caption + hashtags + an image
  prompt for a topic, using an LLM (Groq).
- suggest_idea(): looks at past post topics and proposes ONE fresh content
  idea so the user doesn't have to think of one.
- image_url(): turns an image prompt into a real generated image using
  Pollinations (free, no API key) — the actual photo, not just a description.

Everything falls back to simple templates if no Groq key is set, so the app
still works for demos.
"""

import os
import json
import random
import urllib.parse
import requests

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

# How each platform should sound (used in the prompt).
PLATFORM_STYLE = {
    "Instagram": "visual and friendly, 2-3 short lines, a few emojis, 5-8 hashtags",
    "LinkedIn": "professional and insightful, 3-4 sentences, no emojis, 2-3 hashtags",
    "Twitter": "punchy and short (under 280 characters), 1-2 hashtags",
    "Facebook": "warm and conversational, 2-3 sentences, 2-3 hashtags",
}


# ---------- image generation (free, no key) ----------

def image_url(prompt, seed=None):
    """Build a Pollinations image URL. Loading this URL returns a real
    generated image — works directly in an <img> tag, no key needed."""
    if not prompt:
        prompt = "a clean, bright social media product photo"
    if seed is None:
        seed = random.randint(1, 999999)
    q = urllib.parse.quote(prompt)
    return (f"https://image.pollinations.ai/prompt/{q}"
            f"?width=768&height=768&nologo=true&seed={seed}")


# ---------- post generation ----------

def _extract_json(text):
    """Pull the JSON object out of a model reply, even if it's wrapped in
    ```json fences or has stray text around it."""
    if not text:
        return "{}"
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start:end + 1]
    return t


def _fallback_post(topic, platform):
    first = topic.split()[0].strip("#,.").capitalize() if topic else "New"
    return {
        "caption": f"Check out {topic}! We think you'll love it. Tell us what you think below.",
        "hashtags": f"#{first} #new #offer",
        "image_prompt": f"A clean, bright, professional product photo of {topic}, soft studio lighting",
    }


def generate_post(topic, platform, tone="friendly", brand="", avoid=None):
    if platform not in PLATFORM_STYLE:
        platform = "Instagram"
    if not GROQ_API_KEY:
        return _fallback_post(topic, platform)

    style = PLATFORM_STYLE[platform]
    brand_line = f"The brand is: {brand}.\n" if brand else ""
    avoid_line = ""
    if avoid:
        recent = "\n".join(f'- "{c[:120]}"' for c in list(avoid)[-6:])
        avoid_line = ("Do NOT repeat any of these captions we've already used — "
                      "write something clearly different:\n" + recent + "\n")
    prompt = (
        f"You are a social media manager. Write ONE {platform} post about: {topic}.\n"
        f"{brand_line}{avoid_line}"
        f"Tone: {tone}. Style specific to {platform}: {style}.\n"
        "The caption and hashtags must be written FOR THIS PLATFORM specifically "
        "(do not reuse the same wording across platforms).\n"
        "Return ONLY valid JSON with keys:\n"
        '  "caption": the post text (no hashtags inside it),\n'
        '  "hashtags": a single string of relevant hashtags separated by spaces,\n'
        '  "image_prompt": a detailed text-to-image prompt for an eye-catching '
        "photo/graphic to pair with this post (describe subject, setting, lighting, style)."
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.8,
                  # Reasoning models (e.g. gpt-oss) spend tokens "thinking" before
                  # answering, so give a generous budget and keep reasoning light —
                  # otherwise the JSON answer comes back empty/truncated.
                  "max_tokens": 1800,
                  "reasoning_effort": "low",
                  "response_format": {"type": "json_object"}},
            timeout=40,
        )
        r.raise_for_status()
        content = (r.json()["choices"][0]["message"].get("content") or "").strip()
        out = json.loads(_extract_json(content))
        hashtags = out.get("hashtags", "")
        if isinstance(hashtags, list):
            hashtags = " ".join(hashtags)
        return {
            "caption": str(out.get("caption", "")).strip(),
            "hashtags": str(hashtags).strip(),
            "image_prompt": str(out.get("image_prompt", "")).strip()
                            or f"A professional photo of {topic}",
        }
    except Exception as e:
        print("Groq error:", e)
        return _fallback_post(topic, platform)


# ---------- daily idea suggestions ----------

_IDEA_FALLBACK = [
    "Behind-the-scenes of how our product is made",
    "A happy customer story or testimonial",
    "A quick tip your audience can use today",
    "A limited-time weekend offer",
    "Myth vs fact about your product category",
    "Meet the team / a day in the life",
    "A before-and-after using your product",
    "Answer the question customers ask most",
]


def suggest_idea(past_topics):
    """Propose ONE fresh post idea, different from what's been done before."""
    past = [t for t in (past_topics or []) if t]
    if not GROQ_API_KEY:
        pool = [i for i in _IDEA_FALLBACK if i not in past] or _IDEA_FALLBACK
        return random.choice(pool)

    recent = "\n".join(f"- {t}" for t in past[-20:]) or "- (nothing yet)"
    prompt = (
        "You help a small business plan social media content.\n"
        "Here are topics they have already posted about recently:\n"
        f"{recent}\n\n"
        "Suggest ONE fresh, specific post idea for today that is DIFFERENT from the "
        "ones above and would engage their audience. Reply with just the idea as a "
        "short phrase (max ~12 words), no quotes, no explanation."
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.9, "max_tokens": 512,
                  "reasoning_effort": "low"},
            timeout=40,
        )
        r.raise_for_status()
        idea = (r.json()["choices"][0]["message"].get("content") or "").strip()
        idea = idea.strip('"').strip().splitlines()[-1].strip() if idea else ""
        return idea or random.choice(_IDEA_FALLBACK)
    except Exception as e:
        print("Groq error (suggest):", e)
        return random.choice(_IDEA_FALLBACK)