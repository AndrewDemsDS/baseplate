#!/usr/bin/env python3
"""Scrape public Facebook groups, logged out, into a JSON file for the digest.

Runs headless Chromium via Playwright. Never logs in and holds no credentials:
the worst case is that a run returns nothing.

Posts are read out of the embedded `<script type="application/json" data-sjs>`
payloads rather than the DOM. The DOM yields exactly one usable post, truncated
at "See more", with a relative timestamp and a permalink carrying a 300-char
tracking blob. The JSON yields 3-5 posts with full text, epoch timestamps and
clean permalinks, keyed on GraphQL field names that survive CSS churn.

That 3-5 is a hard ceiling: scrolling adds nothing logged out. Hence several
runs a day plus post_id dedupe against a rolling window, rather than one run.

Output (atomic, via os.replace):
  $STATE_DIR/fb_digest.json  {"posts": [...], "generated_at": ...}
  $STATE_DIR/fb_scrape_status.json  {"last_success", "consecutive_failures", ...}

A run that extracts zero posts across every group does NOT overwrite
fb_digest.json. Silent zero-extraction is the likely failure mode when Meta
renames a field, so the last good data is kept and the status file carries the
failure count for the digest to render as a staleness banner.
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timezone

# ── config ───────────────────────────────────────────────────────────────────


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"fb_scrape: {name} is required")
    return v


STATE_DIR = env("STATE_DIR", "/data")
GROUPS = [g.strip() for g in env("FB_GROUPS", "").split(",") if g.strip()]
RUNS_PER_DAY = int(env("FB_RUNS_PER_DAY", "4"))
JITTER_MAX = int(env("FB_JITTER_SECONDS", "3600"))
KEEP_HOURS = int(env("FB_KEEP_HOURS", "72"))
NAV_TIMEOUT = int(env("FB_NAV_TIMEOUT", "45")) * 1000
SETTLE_MS = int(env("FB_SETTLE_MS", "4000"))
LOCALE = env("FB_LOCALE", "en-GB")
RETRY_BACKOFF = [30, 300, 1800]

# Optional Playwright storage_state. Mount it read-only; it holds session
# cookies, so treat it exactly like a password even though it is not one.
# Produced by, on a desktop:
#   playwright open --save-storage=fb_state.json https://www.facebook.com/
# Without it the scraper runs logged out, which only ever sees PUBLIC groups
# (and most groups are private). With it, scrolling also works, so a fetch is
# not stuck at the 3-5 post ceiling.
STATE_FILE = env("FB_STATE_FILE", "/secrets/fb_state.json")
SCROLLS = int(env("FB_SCROLLS", "4"))

DIGEST_JSON = os.path.join(STATE_DIR, "fb_digest.json")
STATUS_JSON = os.path.join(STATE_DIR, "fb_scrape_status.json")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


def log(msg):
    print(
        f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}",
        flush=True,
    )


# ── json extraction ──────────────────────────────────────────────────────────


def walk(node, hit):
    """Depth-first walk of a decoded JSON blob, calling hit() on every dict."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            hit(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def harvest(blobs, group):
    """Pull post-shaped objects out of the decoded data-sjs payloads.

    A post is any object carrying a post_id plus message text. Author,
    timestamp and permalink are all optional: pinned and highlight entries
    routinely omit creation_time, and dropping those would lose real posts.
    """
    found = {}

    def hit(d):
        pid = d.get("post_id")
        if not pid or not isinstance(pid, str):
            return
        msg = d.get("message")
        text = msg.get("text") if isinstance(msg, dict) else None
        if not text or not isinstance(text, str) or not text.strip():
            return

        url = d.get("wwwURL") or d.get("url")
        if not (isinstance(url, str) and ("/posts/" in url or "/permalink/" in url)):
            url = f"https://www.facebook.com/groups/{group}/posts/{pid}/"

        author = None
        actors = d.get("actors")
        if isinstance(actors, list) and actors and isinstance(actors[0], dict):
            author = actors[0].get("name")
        if not author:
            owner = d.get("owning_profile")
            if isinstance(owner, dict):
                author = owner.get("name")

        ts = d.get("creation_time")
        ts = int(ts) if isinstance(ts, (int, float)) else None

        prev = found.get(pid)
        # Keep the richest variant: the same post_id appears in several blobs,
        # some without a timestamp or author.
        if prev and len(prev["text"]) >= len(text) and prev["created_at"]:
            return
        found[pid] = {
            "post_id": pid,
            "group": group,
            "author": author,
            "text": text.strip(),
            "url": url,
            "created_at": ts,
        }

    for blob in blobs:
        try:
            walk(json.loads(blob), hit)
        except (ValueError, RecursionError):
            continue
    return list(found.values())


# ── scraping ─────────────────────────────────────────────────────────────────

BLOCKED = {"image", "media", "font", "imageset"}


def scrape_group(context, group, authed):
    page = context.new_page()
    # Images and fonts are ~90% of the bytes and none of the data.
    page.route(
        "**/*",
        lambda r: r.abort() if r.request.resource_type in BLOCKED else r.continue_(),
    )

    # Only the first few posts are baked into the inline <script> payloads.
    # Everything further down the feed arrives as GraphQL XHR responses, so
    # scrolling alone grows the blob count without yielding posts. Capture the
    # response bodies too. Bodies are read lazily here and the handler swallows
    # everything: a failed read must never take the run down.
    xhr = []

    def on_response(resp):
        try:
            if "/api/graphql" in resp.url:
                xhr.append(resp.text())
        except Exception:
            pass

    page.on("response", on_response)
    # A hard ceiling on every operation, not just goto: logged in, the feed
    # keeps issuing requests, and one wedged group must not stall the whole run.
    page.set_default_timeout(NAV_TIMEOUT)
    try:
        url = f"https://www.facebook.com/groups/{group}"
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        status = resp.status if resp else 0
        page.wait_for_timeout(SETTLE_MS)

        final = page.url
        if "/login" in final or "login.php" in final:
            if authed:
                # Distinguish "never had a session" from "session went stale",
                # because the fix is different and only one needs a human.
                raise RuntimeError(
                    "session expired or invalid: re-run the one-time "
                    "`playwright open --save-storage=...` login and replace "
                    f"{STATE_FILE}"
                )
            raise RuntimeError(f"login wall, group is private (redirected to {final})")

        blobs = []
        # Logged out, scrolling adds literally nothing (3-5 posts is the hard
        # ceiling). Logged in it does, so harvest after each scroll and merge:
        # the JSON payloads for earlier posts get dropped from the DOM as the
        # virtualised feed advances.
        for n in range(SCROLLS + 1 if authed else 1):
            if n:
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1500)
            blobs.extend(
                b
                for b in page.eval_on_selector_all(
                    'script[type="application/json"]',
                    "els => els.map(e => e.textContent)",
                )
                if b
            )

        # GraphQL responses are newline-delimited JSON, not one object.
        for body in xhr:
            blobs.extend(ln for ln in (body or "").splitlines() if ln.startswith("{"))

        posts = harvest(blobs, group)
        log(
            f"  {group}: http={status} blobs={len(blobs)} "
            f"xhr={len(xhr)} posts={len(posts)}"
        )
        return posts
    finally:
        page.close()


def scrape_all(groups):
    from playwright.sync_api import sync_playwright

    posts = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        try:
            authed = os.path.exists(STATE_FILE)
            log(
                f"  session: {'authenticated' if authed else 'logged out'}"
                + ("" if authed else f" (no {STATE_FILE}); private groups will fail")
            )
            context = browser.new_context(
                user_agent=UA,
                locale=LOCALE,
                viewport={"width": 1366, "height": 900},
                extra_http_headers={"Accept-Language": f"{LOCALE},en;q=0.8"},
                storage_state=STATE_FILE if authed else None,
            )
            for g in groups:
                try:
                    posts.extend(scrape_group(context, g, authed))
                except Exception as exc:  # one bad group != bad run
                    log(f"  {g}: FAILED {type(exc).__name__}: {exc}")
        finally:
            browser.close()
    return posts


# ── state ────────────────────────────────────────────────────────────────────


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def merge(existing, fresh, now):
    """Union old and new on post_id, then drop anything past the keep window.

    Only 3-5 posts are visible per fetch, so a busy group scrolls past between
    runs. The rolling window is what turns several thin fetches into a usable
    day's worth.
    """
    by_id = {p["post_id"]: p for p in existing}
    added = 0
    for p in fresh:
        if p["post_id"] not in by_id:
            added += 1
            p = dict(p, first_seen=now)
        else:
            p = dict(p, first_seen=by_id[p["post_id"]].get("first_seen", now))
        by_id[p["post_id"]] = p

    cutoff = now - KEEP_HOURS * 3600
    kept = [
        p
        for p in by_id.values()
        if max(p.get("created_at") or 0, p.get("first_seen") or 0) >= cutoff
    ]
    kept.sort(
        key=lambda p: p.get("created_at") or p.get("first_seen") or 0, reverse=True
    )
    return kept, added


def run_once():
    now = int(time.time())
    status = read_json(STATUS_JSON, {})
    if not GROUPS:
        log("FB_GROUPS is empty; nothing to scrape")
        status.update(last_attempt=now, error="FB_GROUPS empty")
        write_json(STATUS_JSON, status)
        return 0

    log(f"scraping {len(GROUPS)} group(s): {', '.join(GROUPS)}")
    posts = []
    err = None
    for attempt, backoff in enumerate([0] + RETRY_BACKOFF):
        if backoff:
            log(f"retry {attempt}/{len(RETRY_BACKOFF)} in {backoff}s")
            time.sleep(backoff)
        try:
            posts = scrape_all(GROUPS)
            err = None
            if posts:
                break
            err = "zero posts extracted"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log(f"  run failed: {err}")

    status["last_attempt"] = now
    if posts:
        current = read_json(DIGEST_JSON, {}).get("posts", [])
        kept, added = merge(current, posts, now)
        write_json(DIGEST_JSON, {"generated_at": now, "posts": kept})
        status.update(
            last_success=now,
            consecutive_failures=0,
            error=None,
            last_post_count=len(kept),
            last_new=added,
        )
        log(f"wrote {len(kept)} post(s) ({added} new) to {DIGEST_JSON}")
    else:
        # Do NOT overwrite fb_digest.json. Stale data beats no data, and the
        # digest renders a staleness banner off consecutive_failures.
        status["consecutive_failures"] = status.get("consecutive_failures", 0) + 1
        status["error"] = err or "unknown"
        log(
            f"NO POSTS ({status['error']}); keeping last good file, "
            f"consecutive_failures={status['consecutive_failures']}"
        )
    write_json(STATUS_JSON, status)
    return len(posts)


def main():
    if "--once" in sys.argv:
        run_once()
        return
    interval = max(3600, 86400 // max(1, RUNS_PER_DAY))
    while True:
        run_once()
        # Jitter so we are not hitting Facebook on a round number every day.
        sleep_for = interval + random.randint(0, JITTER_MAX)
        log(f"next run in {sleep_for // 60} min")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
