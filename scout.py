#!/usr/bin/env python3
"""
AI Video Scout - finds new AI films and music videos on YouTube each day.

Pipeline:
  1. Search YouTube for each query, limited to one full Pacific calendar day
     (by default the day that just ended, so a run at 12:15 AM covers
     12:00:00 AM - 11:59:59 PM of the previous day)
  2. Fetch details (duration, stats, aspect ratio) for every hit
  3. Format rules: drop live streams, videos under 1 minute, vertical / #shorts
  4. Your rules: blocked channels, excluded title keywords
  5. Engagement: minimum views and minimum like/view ratio
  6. LLM classifier: drop tutorials and AI news
  7. Write a clickable HTML review page to docs/ (served by GitHub Pages)

Thresholds live in config.json and can be overridden with environment
variables MIN_LIKE_RATIO_PCT and MIN_VIEWS (the GitHub workflow
sets these from repository variables or manual-run inputs).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
YT_API = "https://www.googleapis.com/youtube/v3"

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

ENV_OVERRIDES = {
    "min_like_ratio_pct": ("MIN_LIKE_RATIO_PCT", float),
    "min_views": ("MIN_VIEWS", int),
}


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key, (env, cast) in ENV_OVERRIDES.items():
        raw = os.environ.get(env, "").strip()
        if raw:
            try:
                cfg[key] = cast(raw)
            except ValueError:
                sys.exit(f"{env}={raw!r} is not a valid {cast.__name__}")
    return cfg


# --------------------------------------------------------------------------
# YouTube Data API
# --------------------------------------------------------------------------

def yt_get(endpoint: str, params: dict, api_key: str) -> dict:
    params = {**params, "key": api_key}
    for attempt in range(4):
        resp = requests.get(f"{YT_API}/{endpoint}", params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"YouTube {endpoint} error {resp.status_code}: {resp.text[:400]}")
    raise RuntimeError(f"YouTube {endpoint} kept returning server errors")


def search_videos(cfg: dict, api_key: str, start: dt.datetime, end: dt.datetime) -> tuple[dict[str, list[str]], int]:
    """Return {video_id: [queries that found it]} and quota units used."""
    hits: dict[str, list[str]] = {}
    units = 0
    for query in cfg["queries"]:
        token = None
        for _ in range(cfg["pages_per_query"]):
            params = {
                "part": "id",
                "q": query,
                "type": "video",
                "order": "date",
                "publishedAfter": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "publishedBefore": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "maxResults": 50,
            }
            if cfg.get("relevance_language"):
                params["relevanceLanguage"] = cfg["relevance_language"]
            if token:
                params["pageToken"] = token
            data = yt_get("search", params, api_key)
            units += 100
            for item in data.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid:
                    found_by = hits.setdefault(vid, [])
                    if query not in found_by:
                        found_by.append(query)
            token = data.get("nextPageToken")
            if not token:
                break
    return hits, units


def fetch_details(ids: list[str], api_key: str) -> tuple[list[dict], int]:
    items, units = [], 0
    for i in range(0, len(ids), 50):
        data = yt_get(
            "videos",
            {
                "part": "snippet,contentDetails,statistics,player",
                "id": ",".join(ids[i:i + 50]),
                # Asking for a max height makes the API return embedWidth/
                # embedHeight, which reveal the true aspect ratio.
                "maxHeight": 720,
            },
            api_key,
        )
        units += 1
        items.extend(data.get("items", []))
    return items, units


DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(iso: str) -> int:
    m = DURATION_RE.match(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize(item: dict, queries: list[str]) -> dict:
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    pl = item.get("player", {})
    thumbs = sn.get("thumbnails", {})
    thumb = next((thumbs[k]["url"] for k in ("high", "medium", "default") if k in thumbs), "")
    views = as_int(st.get("viewCount")) or 0
    likes = as_int(st.get("likeCount"))  # None when the creator hides likes
    ratio = round(likes / views * 100, 2) if (likes is not None and views) else None
    return {
        "id": item["id"],
        "url": f"https://www.youtube.com/watch?v={item['id']}",
        "title": sn.get("title", ""),
        "channel": sn.get("channelTitle", ""),
        "channel_id": sn.get("channelId", ""),
        "channel_url": f"https://www.youtube.com/channel/{sn.get('channelId', '')}",
        "published": sn.get("publishedAt", ""),
        "description": sn.get("description", ""),
        "thumb": thumb,
        "live": sn.get("liveBroadcastContent", "none"),
        "duration_s": parse_duration(item.get("contentDetails", {}).get("duration", "")),
        "embed_w": as_int(pl.get("embedWidth")),
        "embed_h": as_int(pl.get("embedHeight")),
        "views": views,
        "likes": likes,
        "comments": as_int(st.get("commentCount")),
        "like_ratio_pct": ratio,
        "queries": queries,
    }


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

SHORTS_TAG = re.compile(r"#shorts?\b", re.IGNORECASE)


def format_reason(v: dict, cfg: dict) -> str | None:
    if v["live"] in ("live", "upcoming"):
        return "Live or upcoming stream"
    if v["duration_s"] < cfg["min_duration_seconds"]:
        return f"Shorter than {cfg['min_duration_seconds']} seconds"
    w, h = v["embed_w"], v["embed_h"]
    if w and h and h > w:
        return "Vertical format"
    if SHORTS_TAG.search(v["title"]) or SHORTS_TAG.search(v["description"]):
        return "Tagged #shorts"
    return None


def _channel_in(v: dict, names: list[str]) -> bool:
    wanted = {n.strip().lower() for n in names if n.strip()}
    return v["channel"].lower() in wanted or v["channel_id"].lower() in wanted


def rule_reason(v: dict, cfg: dict) -> str | None:
    if _channel_in(v, cfg.get("blocked_channels", [])):
        return "Blocked channel"
    title = v["title"].lower()
    for kw in cfg.get("exclude_title_keywords", []):
        if kw.strip() and kw.lower() in title:
            return f'Title contains "{kw}"'
    return None


def engagement_reason(v: dict, cfg: dict) -> str | None:
    if v["views"] < cfg["min_views"]:
        return f"Under {cfg['min_views']} views"
    if v["likes"] is None:
        return "Likes hidden" if cfg.get("hidden_likes") == "exclude" else None
    if v["like_ratio_pct"] < cfg["min_like_ratio_pct"]:
        return f"Like/view {v['like_ratio_pct']:.1f}% (below {cfg['min_like_ratio_pct']:g}%)"
    return None


# --------------------------------------------------------------------------
# LLM classifier
# --------------------------------------------------------------------------

CATEGORIES = ["CREATIVE", "TUTORIAL", "NEWS", "OTHER"]

CLASSIFIER_PROMPT = """You screen new YouTube uploads for a weekly "Top 10 AI films and music videos" list.
Using only each video's title, channel and description, assign exactly one category:

CREATIVE - an original creative work made with AI: short film, music video, trailer, animation, spec ad, visual poem, experimental or art film.
TUTORIAL - teaches or demonstrates how to do something: how-to, tutorial, guide, workflow, prompt tips, tool walkthrough or review, course, "I tested X", comparisons of tools.
NEWS - AI news, updates, announcements, release coverage, weekly roundups, commentary, opinion, reactions, podcasts about AI.
OTHER - anything else: compilations of other people's work, product promos, livestream replays, or videos that merely mention AI.

Notes:
- Creators often credit their tools ("made with Runway, Suno, Kling"). Tool credits alone do NOT make a video a tutorial.
- A creative work with a short behind-the-scenes note in the description is still CREATIVE.
- Titles may be in any language.
Give a reason of under 15 words for each. Classify every video you are given."""

CLASSIFY_TOOL = {
    "name": "record_classifications",
    "description": "Record one classification for every video provided.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "category", "reason"],
                },
            }
        },
        "required": ["classifications"],
    },
}


def classify(videos: list[dict], cfg: dict) -> dict[str, dict]:
    """Return {video_id: {"category", "reason"}}. Failed batches are skipped
    (those videos stay on the list as UNCLASSIFIED rather than being lost)."""
    if not videos:
        return {}
    import anthropic

    client = anthropic.Anthropic(max_retries=4)
    size = cfg.get("classifier_batch_size", 20)
    results: dict[str, dict] = {}
    for i in range(0, len(videos), size):
        batch = videos[i:i + size]
        payload = [
            {"id": v["id"], "title": v["title"], "channel": v["channel"],
             "description": v["description"][:800]}
            for v in batch
        ]
        try:
            msg = client.messages.create(
                model=cfg["classifier_model"],
                max_tokens=4096,
                system=CLASSIFIER_PROMPT,
                tools=[CLASSIFY_TOOL],
                tool_choice={"type": "tool", "name": "record_classifications"},
                messages=[{"role": "user",
                           "content": "Classify these videos:\n" + json.dumps(payload, ensure_ascii=False, indent=1)}],
            )
        except Exception as exc:  # keep going; unclassified videos stay visible
            print(f"warning: classifier batch {i // size + 1} failed: {exc}", file=sys.stderr)
            continue
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use":
                for c in block.input.get("classifications", []):
                    if c.get("id") and c.get("category") in CATEGORIES:
                        results[c["id"]] = {"category": c["category"], "reason": c.get("reason", "")}
    return results


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

PAGE_FIELDS = ["id", "url", "title", "channel", "channel_url", "published", "thumb",
               "duration_s", "views", "likes", "comments", "like_ratio_pct", "queries",
               "category", "category_reason", "stage", "reason"]


def slim(v: dict, with_description: bool) -> dict:
    out = {k: v.get(k) for k in PAGE_FIELDS if k in v}
    if with_description:
        out["description"] = v["description"][:600]
    return out


def embed_json(data: dict) -> str:
    # Safe to place inside <script type="application/json">
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def write_outputs(out_dir: Path, date_str: str, payload: dict) -> Path:
    archive_dir, data_dir = out_dir / "archive", out_dir / "data"
    archive_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").touch()

    (data_dir / f"{date_str}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    dates = sorted((p.stem for p in data_dir.glob("*.json")), reverse=True)[:120]

    template = (ROOT / "template.html").read_text(encoding="utf-8")
    pages = [
        (out_dir / "index.html", {"archive_prefix": "archive/", "latest": "index.html"}),
        (archive_dir / f"{date_str}.html", {"archive_prefix": "", "latest": "../index.html"}),
    ]
    if dates and date_str != dates[0]:
        pages = pages[1:]  # backfilling an older day: refresh its archive page, keep index on the newest day
    for path, nav in pages:
        html = template.replace("__SCOUT_DATA__", embed_json({**payload, "archive": dates, "nav": nav}))
        path.write_text(html, encoding="utf-8")
    return out_dir / "index.html"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def day_window(day: dt.date, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    """UTC start/end of a local calendar day (handles 23/25-hour DST days)."""
    start = dt.datetime.combine(day, dt.time(0), tz)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tz)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def run(cfg: dict, out_dir: Path, use_classifier: bool,
        day: dt.date | None = None, force: bool = False) -> dict | None:
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    if not yt_key:
        sys.exit("YOUTUBE_API_KEY is not set")
    if use_classifier and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (or pass --no-classify)")

    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    now = dt.datetime.now(dt.timezone.utc)
    # Default: the most recent calendar day that has fully ended.
    day = day or (now.astimezone(tz).date() - dt.timedelta(days=1))
    date_str = day.isoformat()
    if not force and (out_dir / "data" / f"{date_str}.json").exists():
        print(f"{date_str} already done; skipping (use --force to redo it).")
        return None
    start, end = day_window(day, tz)

    hits, units = search_videos(cfg, yt_key, start, end)
    items, detail_units = fetch_details(list(hits), yt_key)
    units += detail_units
    videos = [normalize(it, hits.get(it["id"], [])) for it in items]

    kept, removed, pending = [], [], []

    def drop(v: dict, stage: str, reason: str) -> None:
        v["stage"], v["reason"] = stage, reason
        removed.append(v)

    for v in videos:
        if (r := format_reason(v, cfg)):
            drop(v, "Format", r)
            continue
        if (r := rule_reason(v, cfg)):
            drop(v, "Your rules", r)
            continue
        v["trusted"] = _channel_in(v, cfg.get("always_include_channels", []))
        if not v["trusted"] and (r := engagement_reason(v, cfg)):
            drop(v, "Engagement", r)
            continue
        pending.append(v)

    to_classify = [v for v in pending if not v["trusted"]]
    labels = classify(to_classify, cfg) if use_classifier else {}
    excluded = set(cfg.get("exclude_categories", []))

    for v in pending:
        if v["trusted"]:
            v["category"], v["category_reason"] = "TRUSTED", "Always-include channel"
        elif v["id"] in labels:
            v["category"] = labels[v["id"]]["category"]
            v["category_reason"] = labels[v["id"]]["reason"]
        else:
            v["category"] = "UNCLASSIFIED"
            v["category_reason"] = "Classifier skipped" if not use_classifier else "Classifier did not return a label"
        if v["category"] in excluded:
            drop(v, "Classifier", f"{v['category'].title()}: {v['category_reason']}")
            continue
        kept.append(v)

    kept.sort(key=lambda v: (v["like_ratio_pct"] or 0), reverse=True)

    payload = {
        "date": date_str,
        "date_label": day.strftime("%A, %B %-d, %Y"),
        "generated_at": now.isoformat(),
        "window_label": f"12:00 AM – 11:59 PM {start.astimezone(tz).strftime('%Z')} on {day.strftime('%a, %b %-d')}",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "settings": {
            "queries": cfg["queries"],
            "min_like_ratio_pct": cfg["min_like_ratio_pct"],
            "min_views": cfg["min_views"],
            "min_duration_seconds": cfg["min_duration_seconds"],
            "exclude_categories": sorted(excluded),
            "classifier_model": cfg["classifier_model"] if use_classifier else None,
        },
        "stats": {"found": len(videos), "kept": len(kept), "removed": len(removed),
                  "classified": len(labels), "youtube_quota_units": units},
        "kept": [slim(v, True) for v in kept],
        "removed": [slim(v, False) for v in removed],
    }
    index = write_outputs(out_dir, date_str, payload)
    print(f"{date_str}: found {len(videos)}, kept {len(kept)}, removed {len(removed)} "
          f"(YouTube quota ~{units} units, {len(to_classify)} sent to classifier) -> {index}")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Find new AI films and music videos on YouTube.")
    ap.add_argument("--config", type=Path, default=ROOT / "config.json")
    ap.add_argument("--out", type=Path, default=ROOT / "docs")
    ap.add_argument("--no-classify", action="store_true", help="Skip the LLM step (for testing)")
    ap.add_argument("--date", type=dt.date.fromisoformat,
                    help="Day to scan, YYYY-MM-DD (default: yesterday in the configured timezone)")
    ap.add_argument("--force", action="store_true", help="Redo a day that already has results")
    args = ap.parse_args()
    run(load_config(args.config), args.out, use_classifier=not args.no_classify,
        day=args.date, force=args.force)


if __name__ == "__main__":
    main()
