#!/usr/bin/env python3
"""Daily news digest: Miniflux + public Facebook groups -> an LLM -> Resend.

Runs on a stock python:*-alpine image with a bind-mounted copy of this file.
Standard library only, on purpose: no pip at container start (a package fetch
over an MTU-1400 PPPoE link hangs rather than failing cleanly), no Dockerfile
(a `build:` stanza has no `image:` line, so it would silently drop out of the
CVE scan matrix while CI still reported green).

Two entry points:
  * a scheduler thread that fires at DIGEST_HOUR local time, emails via Resend,
    and marks the entries read;
  * an HTTP server whose POST /run renders a fresh digest in the browser and
    deliberately does NOT email and does NOT mark anything read.
"""

import html
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

# ── config ───────────────────────────────────────────────────────────────────


@dataclass
class Config:
    miniflux_url: str
    miniflux_key: str
    groq_key: str
    groq_base: str
    groq_model: str
    groq_chunk_size: int
    groq_chunk_delay: int
    groq_max_item_chars: int
    resend_key: str
    digest_from: str
    digest_to: str
    subject_prefix: str
    region: str
    language: str
    digest_hour: int
    digest_tz: str
    lookback_hours: int
    max_items: int
    drop_categories: set
    catchup_minutes: int
    mark_read: bool
    run_now_cooldown: int
    state_dir: str
    manual_file: str
    fb_file: str
    fb_status_file: str
    listen_port: int


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def load_config():
    state_dir = os.environ.get("STATE_DIR", "/data")
    cfg = Config(
        miniflux_url=os.environ.get("MINIFLUX_URL", "http://miniflux:8080").rstrip("/"),
        miniflux_key=os.environ.get("MINIFLUX_API_KEY", ""),
        groq_key=os.environ.get("GROQ_API_KEY", ""),
        groq_base=os.environ.get(
            "GROQ_BASE_URL", "https://api.groq.com/openai/v1"
        ).rstrip("/"),
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        groq_chunk_size=_int("GROQ_CHUNK_SIZE", 20),
        groq_chunk_delay=_int("GROQ_CHUNK_DELAY", 20),
        groq_max_item_chars=_int("GROQ_MAX_ITEM_CHARS", 300),
        resend_key=os.environ.get("RESEND_API_KEY", ""),
        digest_from=os.environ.get("DIGEST_FROM", ""),
        digest_to=os.environ.get("DIGEST_TO", ""),
        subject_prefix=os.environ.get("DIGEST_SUBJECT_PREFIX", "Daily Digest"),
        region=os.environ.get("DIGEST_REGION", "National"),
        language=os.environ.get("DIGEST_LANGUAGE", "English"),
        digest_hour=_int("DIGEST_HOUR", 8),
        digest_tz=os.environ.get("DIGEST_TZ", "UTC"),
        lookback_hours=_int("DIGEST_LOOKBACK_HOURS", 24),
        max_items=_int("DIGEST_MAX_ITEMS", 120),
        drop_categories={
            c.strip().upper()
            for c in os.environ.get(
                "DIGEST_DROP_CATEGORIES", "SKIP,SPORT,ENTERTAINMENT"
            ).split(",")
            if c.strip()
        },
        catchup_minutes=_int("DIGEST_CATCHUP_MINUTES", 180),
        mark_read=os.environ.get("DIGEST_MARK_READ", "true").lower() == "true",
        run_now_cooldown=_int("RUN_NOW_COOLDOWN", 120),
        state_dir=state_dir,
        manual_file=os.environ.get(
            "MANUAL_NOTES_FILE", os.path.join(state_dir, "manual.md")
        ),
        fb_file=os.path.join(state_dir, "fb_digest.json"),
        fb_status_file=os.path.join(state_dir, "fb_scrape_status.json"),
        listen_port=_int("LISTEN_PORT", 8080),
    )
    missing = [
        n
        for n, v in (
            ("MINIFLUX_API_KEY", cfg.miniflux_key),
            ("GROQ_API_KEY", cfg.groq_key),
        )
        if not v
    ]
    if missing:
        log(f"WARNING: unset {', '.join(missing)} -- runs will degrade")
    return cfg


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ── transport ────────────────────────────────────────────────────────────────

USER_AGENT = "news-digest/1.0 (+https://github.com/AndrewDemsDS/baseplate)"


def http_json(method, url, headers=None, body=None, timeout=60, retries=2):
    """Returns (status, parsed_or_text, err). Never raises.

    Retries on 5xx / timeout / connection error with exponential backoff and
    honours Retry-After on 429. Every caller treats err as a warning rather
    than a failure, which is what keeps one dead dependency from killing a run.
    """
    data = json.dumps(body).encode() if body is not None else None
    # An explicit UA is load-bearing, not cosmetic: Cloudflare in front of
    # api.groq.com rejects the stock "Python-urllib/3.x" agent with a 403
    # error 1010 (browser_signature_banned). Any real token passes.
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})

    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw) if raw else None, None
                except ValueError:
                    return resp.status, raw, None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 429 and attempt < retries:
                wait = int(exc.headers.get("Retry-After") or (5 * (attempt + 1)))
                log(f"  429 from {urllib.parse.urlparse(url).netloc}, sleeping {wait}s")
                time.sleep(min(wait, 90))
                last_err = f"HTTP 429: {raw}"
                continue
            if 500 <= exc.code < 600 and attempt < retries:
                last_err = f"HTTP {exc.code}: {raw}"
                time.sleep(2**attempt)
                continue
            return exc.code, raw, f"HTTP {exc.code}: {raw}"
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(2**attempt)
                continue
    return 0, None, last_err or "unknown transport error"


# ── model ────────────────────────────────────────────────────────────────────


@dataclass
class Item:
    idx: int
    source: str
    title: str
    url: str
    text: str
    published_at: int
    entry_id: int = 0


@dataclass
class Line:
    idx: int
    category: str
    summary: str


@dataclass
class RunResult:
    html: str = ""
    text: str = ""
    warnings: list = field(default_factory=list)
    entry_ids: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


# ── clean ────────────────────────────────────────────────────────────────────


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(s):
    if not s:
        return ""
    p = _Stripper()
    try:
        p.feed(s)
        p.close()
        out = "".join(p.parts)
    except Exception:
        out = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", out).strip()


def truncate(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _norm_url(u):
    try:
        p = urllib.parse.urlsplit(u or "")
        return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"
    except ValueError:
        return u or ""


def _trigrams(s):
    s = re.sub(r"[^\w\s]", "", (s or "").lower())
    return {s[i : i + 3] for i in range(max(0, len(s) - 2))}


def dedupe(items):
    """Drop exact URL repeats, then near-duplicate titles (syndicated stories)."""
    out, seen_urls, kept_grams = [], set(), []
    for it in items:
        key = _norm_url(it.url)
        if key and key in seen_urls:
            continue
        grams = _trigrams(it.title)
        if grams and any(
            len(grams & g) / max(1, min(len(grams), len(g))) > 0.75 for g in kept_grams
        ):
            continue
        seen_urls.add(key)
        kept_grams.append(grams)
        out.append(it)
    return out


# ── sources ──────────────────────────────────────────────────────────────────


def fetch_miniflux(cfg, since_ts, warnings):
    if not cfg.miniflux_key:
        warnings.append("Miniflux: no API key configured")
        return []
    url = (
        f"{cfg.miniflux_url}/v1/entries?after={since_ts}&limit={cfg.max_items}"
        "&order=published_at&direction=desc"
    )
    status, body, err = http_json("GET", url, {"X-Auth-Token": cfg.miniflux_key})
    if err or not isinstance(body, dict):
        warnings.append(f"Miniflux unreachable: {err or 'bad response'}")
        return []
    items = []
    for e in body.get("entries", []):
        feed = (e.get("feed") or {}).get("title") or "unknown"
        items.append(
            Item(
                idx=0,
                source=feed,
                title=strip_html(e.get("title") or "(untitled)"),
                url=e.get("url") or "",
                text=truncate(
                    strip_html(e.get("content") or ""), cfg.groq_max_item_chars
                ),
                published_at=_epoch(e.get("published_at")),
                entry_id=e.get("id") or 0,
            )
        )
    log(f"  miniflux: {len(items)} entries (of {body.get('total', '?')} total)")
    return items


def _epoch(s):
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def fetch_fb_json(cfg, since_ts, warnings):
    """Read whatever fb-scraper last managed to write.

    Deliberately decoupled: the scraper owns its own schedule and freshness,
    so a blocked scrape degrades to a staleness note rather than a failed run.
    """
    try:
        with open(cfg.fb_file) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        warnings.append(f"Facebook groups: unreadable cache ({exc})")
        return []

    st = {}
    try:
        with open(cfg.fb_status_file) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        pass
    fails = st.get("consecutive_failures", 0)
    if fails >= 2:
        age = _ago(st.get("last_success"))
        warnings.append(
            f"Facebook groups: {fails} scrapes in a row returned nothing "
            f"(last good {age}); showing cached posts"
        )

    items = []
    for p in data.get("posts", []):
        ts = p.get("created_at") or p.get("first_seen") or 0
        if ts and ts < since_ts:
            continue
        author = p.get("author")
        items.append(
            Item(
                idx=0,
                source=f"FB/{p.get('group', 'group')}"
                + (f" · {author}" if author else ""),
                title=truncate(p.get("text") or "", 120),
                url=p.get("url") or "",
                text=truncate(p.get("text") or "", cfg.groq_max_item_chars),
                published_at=ts,
            )
        )
    if items:
        log(f"  facebook: {len(items)} post(s) in window")
    return items


def fetch_manual(cfg, since_ts, warnings):
    """Anything pasted into manual.md during a bookmarked noticeboard check."""
    try:
        with open(cfg.manual_file) as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return []
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if not body:
            continue
        m = re.search(r"(https?://\S+)", body)
        items.append(
            Item(
                idx=0,
                source="Noticeboard (manual)",
                title=truncate(body, 120),
                url=m.group(1) if m else "",
                text=truncate(body, cfg.groq_max_item_chars),
                published_at=int(time.time()),
            )
        )
    if items:
        log(f"  manual: {len(items)} note(s)")
    return items


SOURCES = [fetch_miniflux, fetch_fb_json, fetch_manual]


def _ago(ts):
    if not ts:
        return "never"
    d = int(time.time()) - int(ts)
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


# ── llm ──────────────────────────────────────────────────────────────────────


def system_map(cfg):
    return (
        f"You are a news desk editor producing a daily {cfg.region} digest. You "
        f"always write in {cfg.language}, whatever language the source is in. You "
        "never invent facts and you never output URLs."
    )


SYSTEM_REDUCE = "You are a news desk editor. You respond with JSON only."


def groq_chat(cfg, messages, json_mode=False, max_tokens=1200, temperature=0.2):
    if not cfg.groq_key:
        return None, "GROQ_API_KEY not set", {}
    body = {
        "model": cfg.groq_model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    status, resp, err = http_json(
        "POST",
        f"{cfg.groq_base}/chat/completions",
        {"Authorization": f"Bearer {cfg.groq_key}"},
        body,
        timeout=120,
    )
    if err or not isinstance(resp, dict):
        return None, err or "bad Groq response", {}
    try:
        return resp["choices"][0]["message"]["content"], None, resp.get("usage", {})
    except (KeyError, IndexError, TypeError):
        return None, f"unexpected Groq payload: {str(resp)[:200]}", {}


def summarize_chunk(cfg, items, warnings, usage_acc):
    """MAP: one compact classified line per item.

    Chunked because the free tier caps at 12,000 tokens per minute while a
    single-shot batch of a day's news is ~21,000. The context window is
    a red herring here; TPM is the binding constraint.
    """
    listing = "\n".join(
        f"[{it.idx}] {it.source} | {it.title} | {it.text}" for it in items
    )
    prompt = (
        "Below are news items, possibly in several languages. For EACH item output "
        "exactly one line, no preamble, no markdown:\n"
        f"<index> | <CATEGORY> | <one-sentence {cfg.language} summary, max 25 words>\n"
        "CATEGORY is one of: NATIONAL, LOCAL, WORLD, BUSINESS, SPORT, ENTERTAINMENT, SKIP.\n"
        f"Use NATIONAL for {cfg.region} news, LOCAL for a specific municipality, "
        "neighbourhood or community, and for community-group posts.\n"
        "Use SKIP for advertorials, horoscopes, celebrity gossip, listicles and reposts.\n"
        f"Translate everything into {cfg.language}. Do not editorialise.\n\n" + listing
    )

    out, err, usage = groq_chat(
        cfg,
        [
            {"role": "system", "content": system_map(cfg)},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1200,
    )
    _acc(usage_acc, usage)
    if err:
        warnings.append(f"Summary chunk failed ({err}); those items are shown raw")
        return []

    valid = {it.idx for it in items}
    lines = []
    for raw in (out or "").splitlines():
        parts = [p.strip() for p in raw.split("|", 2)]
        if len(parts) != 3:
            continue
        m = re.search(r"\d+", parts[0])
        if not m:
            continue
        idx = int(m.group())
        if idx not in valid:
            continue  # hallucinated index: drop rather than render a bad link
        lines.append(Line(idx=idx, category=parts[1].upper()[:20], summary=parts[2]))
    return lines


def build_digest(cfg, lines, warnings, usage_acc):
    """REDUCE: rank, merge duplicates, and section the classified lines.

    The model emits indices, never URLs. Python owns the index -> Item.url
    table and builds every anchor itself, so link hallucination is impossible
    by construction.
    """
    listing = "\n".join(f"{ln.idx} | {ln.category} | {ln.summary}" for ln in lines)
    prompt = (
        "Here are one-line English summaries with categories and indices.\n"
        f"{listing}\n\n"
        "Produce a daily digest as JSON with this exact shape:\n"
        '{"sections":[{"name":"...","bullets":[{"text":"...","indices":[1,7]}]}]}\n'
        "Sections, in this order and only if non-empty: "
        f'"{cfg.region}" (max 8 bullets, the NATIONAL items), '
        '"Local / community" (include EVERY LOCAL item, do not cut), '
        '"Business" (max 3), "World" (max 3).\n'
        "Rank by importance. Merge bullets covering the same story and list all their "
        "indices. Never output a URL. Output JSON only."
    )

    out, err, usage = groq_chat(
        cfg,
        [
            {"role": "system", "content": SYSTEM_REDUCE},
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
        max_tokens=2000,
        temperature=0.3,
    )
    _acc(usage_acc, usage)
    if err:
        warnings.append(f"Digest assembly failed ({err}); showing unranked summaries")
        return None
    try:
        data = json.loads(out)
        sections = data["sections"]
        assert isinstance(sections, list)
        return sections
    except (ValueError, KeyError, TypeError, AssertionError) as exc:
        warnings.append(
            f"Digest assembly returned unusable JSON ({exc}); "
            "showing unranked summaries"
        )
        return None


def _acc(acc, usage):
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        acc[k] = acc.get(k, 0) + int((usage or {}).get(k, 0) or 0)


def sections_from_lines(cfg, lines):
    """Fallback when REDUCE fails: fixed-order grouping, no ranking or merging."""
    order = ["NATIONAL", "LOCAL", "BUSINESS", "WORLD"]
    names = {
        "NATIONAL": cfg.region,
        "LOCAL": "Local / community",
        "BUSINESS": "Business",
        "WORLD": "World",
    }
    out = []
    for cat in order:
        bullets = [
            {"text": ln.summary, "indices": [ln.idx]}
            for ln in lines
            if ln.category == cat
        ]
        if bullets:
            out.append({"name": names[cat], "bullets": bullets})
    leftovers = [ln for ln in lines if ln.category not in order]
    if leftovers:
        out.append(
            {
                "name": "Other",
                "bullets": [
                    {"text": ln.summary, "indices": [ln.idx]} for ln in leftovers
                ],
            }
        )
    return out


def sections_raw(items):
    """Last resort when MAP fails entirely: titles grouped by feed, untranslated."""
    by_source = {}
    for it in items:
        by_source.setdefault(it.source, []).append(it)
    return [
        {
            "name": src,
            "bullets": [{"text": it.title, "indices": [it.idx]} for it in group],
        }
        for src, group in sorted(by_source.items())
    ]


# ── render ───────────────────────────────────────────────────────────────────

E = html.escape
CSS_BODY = (
    "margin:0;padding:24px 12px;background:#f6f7f9;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
    "Arial,sans-serif;color:#1b1f24;"
)
CSS_CARD = (
    "max-width:640px;margin:0 auto;background:#fff;border-radius:10px;"
    "padding:28px 26px;border:1px solid #e3e6ea;"
)


def _links(indices, by_idx):
    out = []
    for i in indices or []:
        it = by_idx.get(i)
        if not it:
            continue
        label = E(it.source)
        out.append(
            f'<a href="{E(it.url)}" style="color:#3059c4;text-decoration:none;">'
            f"[{label}]</a>"
            if it.url
            else f"[{label}]"
        )
    return " ".join(out)


def render_digest_html(sections, by_idx, warnings, meta):
    p = [
        f'<div style="{CSS_BODY}"><div style="{CSS_CARD}">',
        f'<h1 style="margin:0 0 4px;font-size:20px;">{E(meta.get("title", "Digest"))}</h1>',
        f'<div style="color:#697280;font-size:12px;margin-bottom:20px;">'
        f"{E(meta.get('subtitle', ''))}</div>",
    ]

    if warnings:
        p.append(
            '<div style="border:1px solid #e0b4b4;background:#fdf3f3;'
            'border-radius:6px;padding:12px 14px;margin-bottom:20px;">'
            '<strong style="color:#a33;font-size:13px;">Warnings</strong>'
            '<ul style="margin:8px 0 0;padding-left:18px;color:#7a3b3b;font-size:13px;">'
        )
        p += [f"<li>{E(w)}</li>" for w in warnings]
        p.append("</ul></div>")

    if not sections:
        p.append('<p style="color:#697280;">Nothing new in the window.</p>')

    for sec in sections:
        p.append(
            f'<h2 style="font-size:15px;margin:22px 0 10px;padding-bottom:6px;'
            f'border-bottom:1px solid #eceef1;">{E(str(sec.get("name", "")))}</h2>'
        )
        p.append(
            '<ul style="margin:0;padding-left:18px;line-height:1.55;font-size:14px;">'
        )
        for b in sec.get("bullets", []):
            links = _links(b.get("indices"), by_idx)
            p.append(
                f'<li style="margin-bottom:9px;">{E(str(b.get("text", "")))}'
                f'<span style="font-size:12px;color:#8a93a0;"> {links}</span></li>'
            )
        p.append("</ul>")

    srcs = meta.get("source_counts") or {}
    if srcs:
        p.append(
            '<h2 style="font-size:15px;margin:24px 0 8px;padding-bottom:6px;'
            'border-bottom:1px solid #eceef1;">Sources</h2>'
            '<div style="font-size:12px;color:#8a93a0;line-height:1.7;">'
        )
        p.append(" &middot; ".join(f"{E(k)} {v}" for k, v in sorted(srcs.items())))
        p.append("</div>")
    p.append(
        f'<div style="margin-top:24px;font-size:11px;color:#a2a9b4;">'
        f"{E(meta.get('footer', ''))}</div></div></div>"
    )
    return "".join(p)


def render_digest_text(sections, by_idx, warnings, meta):
    out = [meta.get("title", "Digest"), meta.get("subtitle", ""), ""]
    if warnings:
        out += ["WARNINGS:"] + [f"  - {w}" for w in warnings] + [""]
    for sec in sections:
        out += [str(sec.get("name", "")).upper(), "-" * len(str(sec.get("name", "")))]
        for b in sec.get("bullets", []):
            out.append(f"* {b.get('text', '')}")
            for i in b.get("indices") or []:
                it = by_idx.get(i)
                if it and it.url:
                    out.append(f"    {it.source}: {it.url}")
        out.append("")
    return "\n".join(out)


# ── deliver ──────────────────────────────────────────────────────────────────


def send_resend(cfg, subject, html_body, text_body, idem):
    if not cfg.resend_key:
        return "RESEND_API_KEY not set"
    body = {
        "from": cfg.digest_from,
        "to": [cfg.digest_to],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    for attempt in range(2):
        status, resp, err = http_json(
            "POST",
            "https://api.resend.com/emails",
            {
                "Authorization": f"Bearer {cfg.resend_key}",
                # 24h dedup window: a restart loop at 08:00 must not mail six copies.
                "Idempotency-Key": idem,
            },
            body,
            timeout=45,
            retries=0,
        )
        if not err:
            log(
                f"  resend: sent id={(resp or {}).get('id', '?') if isinstance(resp, dict) else '?'}"
            )
            return None
        log(f"  resend attempt {attempt + 1} failed: {err}")
        if attempt == 0:
            time.sleep(30)
    return err


# ── miniflux write ───────────────────────────────────────────────────────────


def mark_read(cfg, entry_ids, warnings):
    if not entry_ids:
        return
    status, _, err = http_json(
        "PUT",
        f"{cfg.miniflux_url}/v1/entries",
        {"X-Auth-Token": cfg.miniflux_key},
        {"entry_ids": entry_ids, "status": "read"},
    )
    if err:
        warnings.append(f"Could not mark {len(entry_ids)} entries read: {err}")
    else:
        log(f"  marked {len(entry_ids)} entries read")


# ── orchestration ────────────────────────────────────────────────────────────

RUN_LOCK = threading.Lock()
STATE = {}


def state_path(cfg):
    return os.path.join(cfg.state_dir, "state.json")


def load_state(cfg):
    try:
        with open(state_path(cfg)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(cfg, st):
    tmp = state_path(cfg) + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(st, fh, indent=1)
        os.replace(tmp, state_path(cfg))
    except OSError as exc:
        log(f"  could not persist state: {exc}")


def run(cfg, *, mark_read_entries, deliver, trigger):
    started = time.time()
    warnings = []
    usage = {}
    since = int(started) - cfg.lookback_hours * 3600
    log(
        f"run start (trigger={trigger}, deliver={deliver}, mark_read={mark_read_entries})"
    )

    items = []
    for src in SOURCES:
        try:
            items.extend(src(cfg, since, warnings))
        except Exception as exc:
            warnings.append(
                f"Source {src.__name__} crashed: {type(exc).__name__}: {exc}"
            )

    items = dedupe(items)[: cfg.max_items]
    for n, it in enumerate(items, 1):
        it.idx = n
    by_idx = {it.idx: it for it in items}
    source_counts = {}
    for it in items:
        source_counts[it.source] = source_counts.get(it.source, 0) + 1

    # MAP
    lines = []
    if items and cfg.groq_key:
        chunks = [
            items[i : i + cfg.groq_chunk_size]
            for i in range(0, len(items), cfg.groq_chunk_size)
        ]
        log(f"  summarising {len(items)} items in {len(chunks)} chunk(s)")
        for n, chunk in enumerate(chunks):
            if n:
                time.sleep(cfg.groq_chunk_delay)  # the TPM governor
            lines.extend(summarize_chunk(cfg, chunk, warnings, usage))
    elif items:
        warnings.append("No Groq key: showing raw headlines, untranslated")

    kept = [ln for ln in lines if ln.category not in cfg.drop_categories]
    dropped = len(lines) - len(kept)

    # REDUCE
    if kept:
        sections = build_digest(cfg, kept, warnings, usage) or sections_from_lines(
            cfg, kept
        )
    elif items:
        sections = sections_raw(items)  # raw mode
    else:
        sections = []

    tz = ZoneInfo(cfg.digest_tz)
    now_local = datetime.now(tz)
    meta = {
        "title": f"{cfg.subject_prefix}, {now_local:%a %d %b}",
        "subtitle": (
            f"{len(items)} items from the last {cfg.lookback_hours}h"
            + (f", {dropped} filtered out" if dropped else "")
        ),
        "source_counts": source_counts,
        "footer": (
            f"Generated {now_local:%Y-%m-%d %H:%M %Z} · trigger: {trigger} "
            f"· {usage.get('total_tokens', 0)} tokens · "
            f"{time.time() - started:.1f}s"
        ),
    }

    res = RunResult(
        html=render_digest_html(sections, by_idx, warnings, meta),
        text=render_digest_text(sections, by_idx, warnings, meta),
        warnings=warnings,
        entry_ids=[it.entry_id for it in items if it.entry_id],
        meta={
            "items": len(items),
            "dropped": dropped,
            "sections": len(sections),
            "usage": usage,
            "source_counts": source_counts,
            "duration": round(time.time() - started, 1),
            "trigger": trigger,
        },
    )

    if deliver:
        subject = f"{cfg.subject_prefix}, {now_local:%a %d %b}"
        idem = f"digest-{now_local:%Y-%m-%d}"
        err = send_resend(cfg, subject, res.html, res.text, idem)
        res.meta["delivery"] = ("FAILED: " + err) if err else "sent"
        if err:
            warnings.append(f"Email delivery failed: {err}")
    # Always cache, so /last works whether or not delivery happened.
    try:
        with open(os.path.join(cfg.state_dir, "last_digest.html"), "w") as fh:
            fh.write(res.html)
    except OSError:
        pass

    if mark_read_entries:
        mark_read(cfg, res.entry_ids, warnings)

    log(
        f"run done: {len(items)} items, {len(sections)} sections, "
        f"{usage.get('total_tokens', 0)} tokens, {res.meta['duration']}s"
    )
    return res


def run_guarded(cfg, *, mark_read_entries, deliver, trigger):
    if not RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("a run is already in progress")
    try:
        res = run(
            cfg, mark_read_entries=mark_read_entries, deliver=deliver, trigger=trigger
        )
        st = load_state(cfg)
        tz = ZoneInfo(cfg.digest_tz)
        st["last_run"] = int(time.time())
        st["last_meta"] = res.meta
        st["last_warnings"] = res.warnings
        if deliver:
            st["last_scheduled_date"] = datetime.now(tz).strftime("%Y-%m-%d")
            st["last_delivery"] = res.meta.get("delivery", "unknown")
        tok = st.get("tokens_by_day", {})
        day = datetime.now(tz).strftime("%Y-%m-%d")
        tok[day] = tok.get(day, 0) + res.meta.get("usage", {}).get("total_tokens", 0)
        st["tokens_by_day"] = {k: v for k, v in sorted(tok.items())[-7:]}
        save_state(cfg, st)
        return res
    finally:
        RUN_LOCK.release()


# ── scheduler ────────────────────────────────────────────────────────────────


def seconds_until_next(hour, tz):
    now = datetime.now(ZoneInfo(tz))
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def next_run_str(cfg):
    tz = ZoneInfo(cfg.digest_tz)
    now = datetime.now(tz)
    target = now.replace(hour=cfg.digest_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.isoformat(timespec="seconds")


def catchup_if_needed(cfg):
    """An in-memory scheduler loses 08:00 if the container restarts at 07:58.

    Fire late only inside a bounded window: a digest arriving at 23:00 covering
    the wrong day is worse than no digest.
    """
    tz = ZoneInfo(cfg.digest_tz)
    now = datetime.now(tz)
    st = load_state(cfg)
    if st.get("last_scheduled_date") == now.strftime("%Y-%m-%d"):
        return
    target = now.replace(hour=cfg.digest_hour, minute=0, second=0, microsecond=0)
    if target <= now <= target + timedelta(minutes=cfg.catchup_minutes):
        log(f"catch-up: today's {cfg.digest_hour:02d}:00 run was missed, firing now")
        try:
            run_guarded(
                cfg, mark_read_entries=cfg.mark_read, deliver=True, trigger="catchup"
            )
        except Exception as exc:
            log(f"catch-up failed: {type(exc).__name__}: {exc}")


def scheduler_loop(cfg):
    while True:
        # Re-derive the target from local wall-clock on every tick rather than
        # computing it once, so the EET/EEST switch is handled by the tz
        # database instead of open-coded arithmetic.
        remaining = seconds_until_next(cfg.digest_hour, cfg.digest_tz)
        while remaining > 0:
            time.sleep(min(remaining, 300))
            prev, remaining = (
                remaining,
                seconds_until_next(cfg.digest_hour, cfg.digest_tz),
            )
            if remaining > prev:  # we crossed the target
                break
        try:
            run_guarded(
                cfg, mark_read_entries=cfg.mark_read, deliver=True, trigger="scheduled"
            )
        except Exception as exc:
            log(f"scheduled run failed: {type(exc).__name__}: {exc}")
        time.sleep(60)


# ── web ──────────────────────────────────────────────────────────────────────

STATUS_CSS = (
    "margin:0;padding:24px 12px;background:#f6f7f9;font-family:"
    "ui-monospace,SFMono-Regular,Menlo,monospace;color:#1b1f24;font-size:13px;"
)


def render_status_html(cfg):
    st = load_state(cfg)
    meta = st.get("last_meta") or {}
    tz = ZoneInfo(cfg.digest_tz)
    day = datetime.now(tz).strftime("%Y-%m-%d")
    rows = [
        ("Next scheduled run", next_run_str(cfg)),
        (
            "Last run",
            _ago(st.get("last_run")) + (f" ({meta.get('trigger')})" if meta else ""),
        ),
        ("Last delivery", st.get("last_delivery", "never")),
        ("Items last run", str(meta.get("items", "-"))),
        ("Filtered out", str(meta.get("dropped", "-"))),
        (
            "Tokens today",
            f"{st.get('tokens_by_day', {}).get(day, 0)} / 100000 free-tier daily",
        ),
        ("Model", cfg.groq_model),
        ("Recipient", cfg.digest_to),
    ]
    p = [
        f'<div style="{STATUS_CSS}"><div style="{CSS_CARD}">',
        f'<h1 style="font-size:17px;margin:0 0 16px;">{E(cfg.subject_prefix)}</h1>',
        '<table style="border-collapse:collapse;width:100%;">',
    ]
    for k, v in rows:
        p.append(
            f'<tr><td style="padding:5px 0;color:#697280;width:44%;">{E(k)}</td>'
            f'<td style="padding:5px 0;">{E(str(v))}</td></tr>'
        )
    p.append("</table>")

    counts = meta.get("source_counts") or {}
    if counts:
        p.append(
            '<h2 style="font-size:13px;margin:20px 0 6px;color:#697280;">'
            'Per-source contribution</h2><table style="width:100%;">'
        )
        for k, v in sorted(counts.items()):
            p.append(
                f'<tr><td style="padding:3px 0;">{E(k)}</td>'
                f'<td style="padding:3px 0;text-align:right;">{v}</td></tr>'
            )
        p.append("</table>")

    warns = st.get("last_warnings") or []
    if warns:
        p.append(
            '<div style="border:1px solid #e0b4b4;background:#fdf3f3;border-radius:6px;'
            'padding:10px 12px;margin-top:18px;color:#7a3b3b;">'
            '<strong>Warnings from last run</strong><ul style="margin:6px 0 0;'
            'padding-left:18px;">'
        )
        p += [f"<li>{E(w)}</li>" for w in warns]
        p.append("</ul></div>")

    p.append(
        '<form method="post" action="/run" style="margin-top:22px;">'
        '<button type="submit" style="background:#1b1f24;color:#fff;border:0;'
        "border-radius:6px;padding:10px 18px;font-size:13px;cursor:pointer;"
        'font-family:inherit;">Run now</button>'
        '<span style="color:#8a93a0;margin-left:12px;">renders in the browser; '
        "does not email and does not mark anything read</span></form>"
        '<div style="margin-top:14px;"><a href="/last" style="color:#3059c4;">'
        "view last digest (cached, no API call)</a></div>"
        "</div></div>"
    )
    return "".join(p)


def make_handler(cfg):
    last_manual = {"at": 0.0}

    class Handler(BaseHTTPRequestHandler):
        server_version = "news-digest"

        def log_message(self, fmt, *args):
            log(f"  http {self.address_string()} {fmt % args}")

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/healthz":
                return self._send(200, "ok", "text/plain; charset=utf-8")
            if path == "/":
                return self._send(200, render_status_html(cfg))
            if path == "/last":
                try:
                    with open(os.path.join(cfg.state_dir, "last_digest.html")) as fh:
                        return self._send(200, fh.read())
                except OSError:
                    return self._send(404, "<p>No digest has been generated yet.</p>")
            return self._send(404, "<p>Not found.</p>")

        def do_POST(self):
            # POST specifically so browser prefetch, link scanners and health
            # probes cannot spend API quota.
            if urllib.parse.urlparse(self.path).path != "/run":
                return self._send(404, "<p>Not found.</p>")
            wait = cfg.run_now_cooldown - (time.time() - last_manual["at"])
            if wait > 0:
                return self._send(
                    429,
                    f"<p>Cooling down, try again in {int(wait)}s. "
                    '<a href="/">back</a></p>',
                )
            try:
                res = run_guarded(
                    cfg, mark_read_entries=False, deliver=False, trigger="manual"
                )
            except RuntimeError as exc:
                return self._send(409, f'<p>{E(str(exc))}. <a href="/">back</a></p>')
            except Exception as exc:
                return self._send(
                    500,
                    f"<p>Run failed: {E(f'{type(exc).__name__}: {exc}')}"
                    '</p><p><a href="/">back</a></p>',
                )
            last_manual["at"] = time.time()
            return self._send(
                200,
                res.html + '<div style="text-align:center;padding:16px;">'
                '<a href="/" style="color:#3059c4;">back to status</a></div>',
            )

    return Handler


def main():
    cfg = load_config()
    os.makedirs(cfg.state_dir, exist_ok=True)

    if "--once" in sys.argv:
        deliver = "--send" in sys.argv
        res = run_guarded(cfg, mark_read_entries=False, deliver=deliver, trigger="cli")
        print(res.text)
        return

    log(
        f"digest hour {cfg.digest_hour:02d}:00 {cfg.digest_tz}; "
        f"next scheduled run: {next_run_str(cfg)}"
    )
    catchup_if_needed(cfg)
    threading.Thread(target=scheduler_loop, args=(cfg,), daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", cfg.listen_port), make_handler(cfg))
    log(f"listening on :{cfg.listen_port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
