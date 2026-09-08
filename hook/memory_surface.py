#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Memory surfacing hook v4 — pointer-vs-full injection, structured reranker output,
code-side snapshot adjudication.

v4 changes over v3:
  - LIGHT mode injects one-line *pointers* (memory id + supporting sentence), not
    full text. Only TRIGGER (explicit recall) injects full memories.
  - Reranker returns structured fields per candidate: score / sent / snap / date.
    `sent` = index of the sentence that supports the judgement (becomes the pointer hook).
    `snap` + `date` = "is this a progress snapshot of the topic, and when" — the code,
    not the model, decides which snapshot is newest (models anchor on the first one seen).
  - Recency bias in the general search lane (only affects who enters the rerank pool).
  - Same-tier newest-first tie-break among score-2 candidates.
  - TRIGGER bypasses transcript dedup and retries the reranker once on timeout.
  - Acknowledgement stripping: "oh right, I remember now" is not a recall request.
  - Channel-shell / timestamp stripping for messages relayed through chat plugins.
  - Optional mute regex: skip LIGHT search when the message matches (e.g. intimate chat).
  - Conversation context: 300KB transcript tail, last 10 messages, text blocks only,
    system injections stripped.

Pipeline:
  0. Normalize message (strip channel shells, leading timestamps)
  1. Intent Gate: SKIP / TRIGGER / LIGHT   (~0ms)
  2. Dual search: event + general lanes in parallel, recency-biased general lane (~1s)
  3. Hard filter: archived / deep / transcript dedup (LIGHT only) / resolved / low importance
  4. [LIGHT] cosine gate — skip the reranker when nothing looks promising
  5. LLM reranker with conversation context → structured JSON → snapshot adjudication
  6. Inject: TRIGGER = full text (max 1 event + 1 fragment); LIGHT = pointer lines

Compatible with any memory MCP server exposing a search tool that returns chunks with:
  chunk_text, parent_memory_id, chunk_index, score, category
Optional fields used when present: created_at, tags, importance, archived, resolved, parent_id.
"""

import contextlib, io, json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta


def _env(name, default):
    return os.environ.get(name, default)


def _env_bool(name, default):
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# ── Config (override via env vars) ───────────────────────────────────
MCP_URL = _env("MEMORY_MCP_URL", "")
SEARCH_TOOL_NAME = _env("SEARCH_TOOL_NAME", "semantic_search")
READ_TOOL_NAME = _env("READ_TOOL_NAME", "")   # optional: fetch full event body for briefing

RERANKER_API = _env("RERANKER_API_URL", "https://api.deepseek.com/chat/completions")
RERANKER_KEY = _env("RERANKER_API_KEY", "")
RERANKER_MODEL = _env("RERANKER_MODEL", "deepseek-v4-flash")
RERANKER_DISABLE_THINKING = _env_bool("RERANKER_DISABLE_THINKING", True)  # reasoning models eat max_tokens otherwise

USER_NAME = _env("USER_NAME", "user")
AI_NAME = _env("AI_NAME", "assistant")

TIMEOUT_SEARCH = float(_env("TIMEOUT_SEARCH", "2.0"))
TIMEOUT_RERANK = float(_env("TIMEOUT_RERANK", "9.0"))
LIGHT_COS_GATE = float(_env("LIGHT_COS_GATE", "0.72"))
EVENT_BRIEF_MAX = int(_env("EVENT_BRIEF_MAX", "600"))

# Injection shape
LIGHT_POINTER_ONLY = _env_bool("LIGHT_POINTER_ONLY", True)  # False = v3 behaviour (full text in LIGHT too)
SENT_PTR_ENABLE = _env_bool("SENT_PTR_ENABLE", True)        # pointer hook = reranker-chosen sentence
SENT_HOOK_MAX = int(_env("SENT_HOOK_MAX", "60"))
SNAP_ADJUDICATE = _env_bool("SNAP_ADJUDICATE", True)        # code picks the newest progress snapshot

# Candidate-pool recency bias (general lane only; never touches gate or rerank)
GEN_FETCH = int(_env("GEN_FETCH", "12"))
RECENCY_W = float(_env("RECENCY_W", "0.03"))
RECENCY_TAU = float(_env("RECENCY_TAU", "14"))

# Context extraction
CONTEXT_TURNS = int(_env("CONTEXT_TURNS", "10"))
CONTEXT_TAIL_KB = int(_env("CONTEXT_TAIL_KB", "300"))

# Optional mute: LIGHT mode stays silent when the message matches this regex.
# Explicit recall (TRIGGER) still works. Empty = disabled.
MUTE_RE = re.compile(_env("MUTE_RE", "")) if _env("MUTE_RE", "") else None

LOG_PATH = os.path.expanduser(_env("HOOK_LOG_PATH", "~/.claude/hooks/surface_debug.log"))
LOG_MAX_KB = int(_env("LOG_MAX_KB", "1000"))
LOG_KEEP = int(_env("LOG_KEEP", "2"))

# ── Intent Gate vocabulary ───────────────────────────────────────────
RECALL_KW = [
    "还记得", "上次", "那次", "之前", "以前", "记得", "记忆",
    "那个时候", "那一次", "说过", "聊过", "做过", "提过", "试过",
    "上周", "上个月", "前几天", "最近",
    "后来怎么", "后来弄", "继续上次",
    "复盘", "回顾", "进度", "坚持了", "连续",
    "我有没有", "我是不是",
    "remember", "last time", "earlier", "previously", "before",
    "did i", "have i", "how did", "what happened",
]
RECALL_EXCLUDE = [w for w in _env("RECALL_EXCLUDE", "记忆库,记忆浮现,记忆系统,记忆hook,记忆召回").split(",") if w]

# Acknowledgement forms of "remember": "oh right, I remember now" is the user recalling,
# not asking the assistant to. Stripped before keyword scan; questions ("remember?") survive.
ACK_STRIP_RE = re.compile(r"(?:还?记得|记住|想起来?)(?:了|啦)+(?![吗么嘛？?])|\b(?:i|now i|oh i) remember(?: now)?\b(?!\?)", re.I)

NOISE_WORDS = {
    "嗯", "好", "ok", "哦", "哈", "嗯嗯", "好的", "行", "可以",
    "收到", "明白", "了解", "对", "是的", "好吧", "行吧",
    "哈哈", "哈哈哈", "笑死", "nice", "cool", "yeah", "yes", "no",
    "谢谢", "thanks", "thx", "继续", "next", "lgtm",
    "sure", "sounds", "good", "great", "okay", "yep", "yup", "alright", "fine", "haha", "lol", "got", "it",
}
MIN_LEN_TRIGGER = 6

CODE_SKIP_RE = [
    re.compile(r'```'),
    re.compile(r'^\s*(git |npm |pip |brew |cargo |docker |kubectl |ssh |scp |curl |wget |make |cd |ls |cat |rm |mv |cp |chmod |chown )', re.M),
    re.compile(r'(Traceback|FAIL|error\[|Error:|Exception:|panic:|fatal:)', re.I),
    re.compile(r'(/Users/|/root/|/home/|/tmp/|/var/|/etc/|/opt/)'),
    re.compile(r'^\s*(def |class |func |import |from |const |let |var |function |type |interface |struct |enum |pub |fn )', re.M),
    re.compile(r'^\s*[\{\["]', re.M),
]

SYSTEM_PREFIXES = [p for p in _env("SYSTEM_PREFIXES", "[auto-trigger],[system],[cron]").split(",") if p]
# Injected lines that arrive with the user role but are not the user speaking.
# They are excluded from context extraction only (the gate still sees the raw message).
CONTEXT_SKIP_PREFIXES = SYSTEM_PREFIXES + [p for p in _env("CONTEXT_SKIP_PREFIXES", "").split(",") if p]

CHUNK_ID_RE = re.compile(r'\[(\w{6,}_\d+)\]')
MEMORY_ID_RE = re.compile(r'\[([a-f0-9]{8})\]')
HISTORICAL_KW = ["之前怎么", "上次怎么", "后来怎么", "怎么解决的", "复盘", "回顾", "当时",
                 "how did", "what happened", "last time"]
RELATIVE_TIME_WORDS = ["下周", "明天", "后天", "下个月", "过几天", "马上",
                       "next week", "tomorrow", "next month", "in a few days"]

CHANNEL_RE = re.compile(r'<channel\b[^>]*>(.*?)</channel>', re.S)
LEADING_TS_RE = re.compile(r'^\[\d\d-\d\d \d\d:\d\d( #\d+)?\]\s*')


# ════════════════════════════════════════════════════════════════════
# Logging (size-rotated: .1 / .2)
# ════════════════════════════════════════════════════════════════════
def log(msg, data=None):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_KB * 1024:
            for i in range(LOG_KEEP, 0, -1):
                src = LOG_PATH if i == 1 else f"{LOG_PATH}.{i - 1}"
                if os.path.exists(src):
                    os.replace(src, f"{LOG_PATH}.{i}")
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            ts = datetime.now().strftime('%m-%d %H:%M:%S')
            f.write(f"[{ts}] {msg}\n")
            if data is not None:
                f.write(f"  {json.dumps(data, ensure_ascii=False)[:1500]}\n")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# Step 1: Intent Gate
# ════════════════════════════════════════════════════════════════════
def intent_gate(msg):
    """Returns ('SKIP'|'TRIGGER'|'LIGHT', reason)."""
    if not msg or not msg.strip():
        return "SKIP", "empty"
    s = msg.strip()
    if any(s.startswith(p) for p in SYSTEM_PREFIXES):
        return "SKIP", "system"
    lo = s.lower()
    tokens = re.findall(r"[a-z]+|[一-鿿]+", lo)
    if lo in NOISE_WORDS or (tokens and all(t in NOISE_WORDS for t in tokens)):
        return "SKIP", "noise"
    scan = ACK_STRIP_RE.sub("", lo)
    if len(s) < MIN_LEN_TRIGGER:
        for kw in RECALL_KW:
            if kw in scan:
                return "TRIGGER", f"recall:{kw}"
        return "SKIP", f"short({len(s)})"
    for kw in RECALL_KW:
        if kw in scan:
            if any(ex in scan for ex in RECALL_EXCLUDE if kw in ex):
                continue
            return "TRIGGER", f"recall:{kw}"
    for pat in CODE_SKIP_RE:
        if pat.search(msg):
            return "SKIP", "code"
    return "LIGHT", f"default(len={len(s)})"


# ════════════════════════════════════════════════════════════════════
# Step 2: Search
# ════════════════════════════════════════════════════════════════════
def _mcp_call(tool, args, timeout):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(MCP_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["result"]["content"][0]["text"]


def mcp_search(query, limit=8, category=None):
    if not MCP_URL:
        return []
    args = {"query": query, "limit": limit, "no_track": True}
    if category:
        args["category"] = category
    try:
        text = _mcp_call(SEARCH_TOOL_NAME, args, TIMEOUT_SEARCH)
        if text.startswith("Error") or text.startswith("No "):
            if text.startswith("Error"):
                log(f"search_srv_err({category or 'all'}): {text[:120]}")
            return []
        return json.loads(text)
    except Exception as e:
        log(f"search_err({category or 'all'}): {e}")
        return []


def fetch_full_memory(mid):
    """Optional: full body of a memory via READ_TOOL_NAME (for event briefs)."""
    if not READ_TOOL_NAME or not mid:
        return ""
    try:
        text = _mcp_call(READ_TOOL_NAME, {"memory_id": mid, "no_track": True}, TIMEOUT_SEARCH)
        try:
            obj = json.loads(text)
            return obj.get("content") or obj.get("text") or text
        except Exception:
            return text
    except Exception as e:
        log(f"read_err: {e}")
        return ""


def _recency_adj(c):
    """Pool-ordering bonus for recent memories. Never changes c['score'] itself."""
    try:
        days = (datetime.now() - datetime.strptime(str(c.get("created_at", ""))[:10], "%Y-%m-%d")).days
        return RECENCY_W * (2.718281828 ** (-max(days, 0) / RECENCY_TAU))
    except Exception:
        return 0.0


def dual_search(query, ev_limit=3, gen_limit=8):
    """Event + general lanes in parallel. General lane over-fetches GEN_FETCH,
    re-sorts by cosine + recency bonus, then cuts back to gen_limit — this only
    decides who enters the rerank pool; the gate still reads raw cosine."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ev = ex.submit(mcp_search, query, ev_limit, "event")
        f_gen = ex.submit(mcp_search, query, max(GEN_FETCH, gen_limit))
        events = f_ev.result()
        general = f_gen.result()
    general = sorted(general, key=lambda c: (c.get("score") or 0) + _recency_adj(c), reverse=True)[:gen_limit]
    seen, merged = set(), []
    for c in events + general:
        pid = c.get("parent_memory_id", "")
        if pid not in seen:
            merged.append(c)
            seen.add(pid)
    return merged, len(events), len(general)


# ════════════════════════════════════════════════════════════════════
# Step 3: Hard Filter
# ════════════════════════════════════════════════════════════════════
def get_transcript_ids(path):
    if not path or not os.path.exists(path):
        return set(), set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return set(CHUNK_ID_RE.findall(content)), set(MEMORY_ID_RE.findall(content))
    except Exception:
        return set(), set()


def extract_entities(msg):
    ents = set(w.lower() for w in re.findall(r'[a-zA-Z]{3,}', msg))
    zh = re.sub(r'[^一-鿿]', '', msg)
    for n in (2, 3, 4):
        for i in range(len(zh) - n + 1):
            ents.add(zh[i:i + n])
    return ents


def hard_filter(chunks, msg, transcript_path, mode="LIGHT"):
    """TRIGGER skips transcript dedup: when the user explicitly asks, a memory that
    already appeared earlier in the session may surface again. In-batch dedup stays."""
    entities = extract_entities(msg)
    historical = any(kw in msg.lower() for kw in HISTORICAL_KW)
    pushed_cids, pushed_mids = get_transcript_ids(transcript_path)
    kept, flog, seen_parents = [], [], set()

    for c in chunks:
        pid = c.get("parent_memory_id", "")
        cid = f"{pid}_{c.get('chunk_index', 0)}"
        drop = None
        if c.get("archived"):
            drop = "archived"
        elif c.get("category") == "deep":
            drop = "deep"
        elif cid in pushed_cids and mode != "TRIGGER":
            drop = "dup_chunk"
        elif pid in pushed_mids and mode != "TRIGGER":
            drop = "dup_parent"
        elif c.get("parent_id") and c["parent_id"] in pushed_mids and mode != "TRIGGER":
            drop = "dup_source"
        elif pid in seen_parents:
            drop = "dup_sibling"
        elif c.get("parent_id") and c["parent_id"] in seen_parents:
            drop = "dup_source_sibling"
        elif c.get("resolved") and not historical:
            drop = "resolved"
        elif c.get("category") == "fragment":
            imp = c.get("importance", 5)
            if isinstance(imp, (int, float)) and imp < 3:
                hay = (c.get("chunk_text", "") + " " + str(c.get("tags", ""))).lower()
                if not any(e in hay for e in entities):
                    drop = f"low_imp({imp})"
        flog.append({"cid": cid, "cat": c.get("category", ""), "drop": drop})
        if not drop:
            kept.append(c)
            seen_parents.add(pid)
            if c.get("parent_id"):
                seen_parents.add(c["parent_id"])
    return kept, flog, pushed_mids


# ════════════════════════════════════════════════════════════════════
# Step 4: Conversation context
# ════════════════════════════════════════════════════════════════════
def _clean_user_text(text):
    text = re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.S)
    blocks = CHANNEL_RE.findall(text)
    if blocks and not CHANNEL_RE.sub('', text).strip():
        text = "\n".join(b.strip() for b in blocks)
    text = LEADING_TS_RE.sub('', text.strip())
    return text.strip()


def get_recent_context(transcript_path, n_msgs=CONTEXT_TURNS):
    """Last N messages from the transcript tail, text blocks only (no tool_use),
    labelled with USER_NAME/AI_NAME. Tool-heavy sessions push real turns far back,
    hence the large tail."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            read_from = max(0, size - CONTEXT_TAIL_KB * 1024)
            f.seek(read_from)
            if read_from > 0:
                f.readline()
            tail = f.read().decode('utf-8', errors='replace')
        turns = []
        for line in reversed(tail.splitlines()):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            content = obj.get("message", {}).get("content", "")
            if t == "user":
                if isinstance(content, list):
                    content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
                if not isinstance(content, str):
                    continue
                clean = _clean_user_text(content)
                if not clean or any(clean.startswith(p) for p in CONTEXT_SKIP_PREFIXES):
                    continue
                turns.append((USER_NAME, clean[:150]))
            elif t == "assistant":
                if isinstance(content, list):
                    text = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                elif isinstance(content, str):
                    text = content.strip()
                else:
                    text = ""
                if text:
                    turns.append((AI_NAME, text[:150]))
            if len(turns) >= n_msgs:
                break
        if len(turns) < 2:
            return ""
        turns.reverse()
        return "\n".join(f"{role}: {text}" for role, text in turns)
    except Exception as e:
        log(f"context_err: {e}")
        return ""


# ════════════════════════════════════════════════════════════════════
# Step 5: LLM Reranker (structured output) + snapshot adjudication
# ════════════════════════════════════════════════════════════════════
RERANK_RULES = """Candidate types:
- event_overview: an aggregated overview of many memories, high information density. Prefer it when the user needs history / patterns / the arc of something.
- fragment: one specific memory moment. Prefer it when the user needs exact details or a particular occasion.

Selection rules:
- When an event provides enough context, prefer the event over fragments.
- Only pick a fragment when it adds details the event does not cover.
- When an event and a fragment overlap, pick the event only.
- Progress / status messages ("where am I at" on a show, a course, a project, a health issue — including the user announcing they are about to start or continue an ongoing activity): among fragments that are progress snapshots of the same activity, the one with the latest date is the answer and scores 2 even if it is short; older snapshots are stale and score 0. Example: user says "going to keep watching the show", candidates are "reached episode 12" (Aug 17) and "episode 15, one left" (Aug 19) → Aug 19 scores 2, Aug 17 scores 0. Forbidden reasoning: "[older snapshot] already gives enough context, [newer] is newer but not a direct answer" — the latest snapshot of the same topic IS the direct answer. Mark all such snapshots snap=true with their dates (the program re-checks recency from `date`). This applies only to fragment-vs-fragment competition; an event_overview is the arc, not a snapshot.
- Read the conversation tone: words used while teasing, joking or being dramatic do not mean their literal sense. Use the recent context to judge.
- Keyword coincidence in a different context scores 0. Be especially wary of cross-language / abbreviation collisions ("fire" as slang ≠ FIRE the financial movement; Apple the company ≠ apple the fruit).
- Topical adjacency is not necessity. "Same scene" or "same kind of activity" is not content relevance. The only test is: would the reply be noticeably worse without this memory? Forbidden reasoning: "this fragment happened during the same kind of session, so it is direct context for the current message". This rule bans "admit anything that can be connected"; it does not ban any domain: when the user's question is genuinely about a topic, their own history on that topic (their plan, their setup, what they went through) is the necessary grounding and scores 2."""

RERANK_OUTPUT = """Each candidate body is split into numbered sentences ⟨n⟩. Return a JSON array only, one item per candidate:
[{{"id":1,"score":0,"sent":1,"snap":false,"date":"","reason":"..."}}]
- score: 2 = directly useful (reply would be noticeably worse without it); 1 = tangential, not necessary; 0 = irrelevant / generic / keyword coincidence
- sent: the ⟨n⟩ of the single sentence that best supports your judgement (give the most relevant one even when scoring 0)
- snap: true only if the current message is a progress/status message (including announcing start/continue of an ongoing activity) AND this candidate is a progress snapshot of that same activity; otherwise false. Fragments only; events are always false
- date: the event date of this record as YYYY-MM-DD; if the body states no date, copy the date from the candidate header. Never guess
Be strict. Topical overlap alone is not a 2."""

RERANK_PROMPT_CTX = """You are a personal memory retrieval quality judge for an assistant ({ai}) and its user ({user}).

""" + RERANK_RULES + """

Recent conversation (for context / tone):
{context}

Current message:
{msg}

Candidate memories:
{candidates}

""" + RERANK_OUTPUT

RERANK_PROMPT_NO_CTX = """You are a personal memory retrieval quality judge for an assistant ({ai}) and its user ({user}).

""" + RERANK_RULES + """

Current message:
{msg}

Candidate memories:
{candidates}

""" + RERANK_OUTPUT

UNIT_RE = re.compile(r"[^\n。！？；.!?;]+(?:[。！？；.!?;]+|\n|$)")


def split_units(text, limit=300):
    """Sentence / line units. A body with no sentence punctuation is one unit."""
    return [u.strip() for u in UNIT_RE.findall((text or "")[:limit]) if u.strip()]


def sent_hook(c):
    """Pointer hook: the reranker's supporting sentence, with a leading 【title】/# title kept
    as topic anchor; falls back to the first 50 chars when sent is missing/out of range."""
    text = c.get("chunk_text", "") or ""
    units = split_units(text)
    s = c.get("_sent")
    if SENT_PTR_ENABLE and isinstance(s, int) and 1 <= s <= len(units):
        u = re.sub(r"^[-•·*]\s*", "", units[s - 1])
        u = re.sub(r"\s+", " ", u).strip().rstrip("。！？；.!?;")
        m = re.match(r"(【[^】]{1,30}】|#{1,3} [^\n]{1,30}\n)", text.lstrip())
        if m:
            title = m.group().strip().lstrip("#").strip()
            body = re.sub(r"^(【[^】]*】|#{1,3} [^\n]{1,30})\s*[-•·*]?\s*", "", u)
            if not u.startswith(m.group().strip()):
                u = (title if title.startswith("【") else f"《{title}》") + " " + body
        if u:
            return u[:SENT_HOOK_MAX]
    return re.sub(r"\s+", " ", text).strip()[:50]


DATE_FULL_RE = re.compile(r"(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?")
DATE_MD_RE = re.compile(r"(?<![\d/])(\d{1,2})(?:月|/)(\d{1,2})日?(?![\d/])")
RELATIVE_DAYS = [("大前天", 3), ("前天", 2), ("昨天", 1), ("昨日", 1), ("昨晚", 1),
                 ("day before yesterday", 2), ("yesterday", 1), ("last night", 1)]


def date_options(c):
    """Dates the model is allowed to claim for a candidate: created_at, dates written in
    the body, and relative words resolved against created_at. Anything else is rejected."""
    opts, anchor = set(), None
    try:
        anchor = datetime.strptime(str(c.get("created_at", ""))[:10], "%Y-%m-%d")
        opts.add(anchor.strftime("%Y-%m-%d"))
    except Exception:
        pass
    text = (c.get("chunk_text", "") or "")
    for y, m, d in DATE_FULL_RE.findall(text):
        try:
            opts.add(datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    if anchor:
        for m, d in DATE_MD_RE.findall(text):
            try:
                opts.add(datetime(anchor.year, int(m), int(d)).strftime("%Y-%m-%d"))
            except ValueError:
                pass
        low = text.lower()
        for word, n in RELATIVE_DAYS:
            if word in low:
                opts.add((anchor - timedelta(days=n)).strftime("%Y-%m-%d"))
    return opts


def ground_date(c, d):
    d = str(d or "")[:10]
    return d if d and d in date_options(c) else str(c.get("created_at", ""))[:10]


def snap_adjudicate(scores, candidates):
    """Two or more fragments flagged snap=true → the one with the latest grounded date is
    forced to 2, the rest to 0, regardless of the model's own scores (models anchor on the
    first snapshot they read, or score everything 0 and wait for the program). Date tie or a
    single snapshot → no intervention. Returns (scores, note)."""
    snaps = []
    for r in scores:
        idx = r.get("id", 0) - 1
        if not (0 <= idx < len(candidates)):
            continue
        c = candidates[idx]
        if c.get("category") == "event" or r.get("snap") is not True:
            continue
        snaps.append((ground_date(c, r.get("date")), idx, r))
    if len(snaps) < 2:
        return scores, None
    snaps.sort(key=lambda x: x[0], reverse=True)
    top_date = snaps[0][0]
    if sum(1 for s in snaps if s[0] == top_date) > 1:
        return scores, {"skip": "date_tie", "date": top_date, "n": len(snaps)}
    note = {"latest": None, "stale": [], "changed": 0}
    for d, idx, r in snaps:
        cid = f"{candidates[idx].get('parent_memory_id', '')}_{candidates[idx].get('chunk_index', '')}"
        new = 2 if (d, idx) == (snaps[0][0], snaps[0][1]) else 0
        old = r.get("score")
        (note.__setitem__("latest", f"{cid}@{d}(was {old})") if new == 2 else note["stale"].append(f"{cid}@{d}(was {old})"))
        if old != new:
            r["score"] = new
            r["reason"] = f"[snap→{'latest' if new == 2 else 'stale'} was {old}] " + str(r.get("reason", ""))
            note["changed"] += 1
    return scores, note


def llm_rerank(msg, candidates, context="", retries=0):
    if not candidates or not RERANKER_KEY:
        return []
    lines = []
    for i, c in enumerate(candidates):
        date = str(c.get("created_at", ""))[:10]
        ctype = "event_overview" if c.get("category") == "event" else "fragment"
        tags = c.get("tags", "")
        if SENT_PTR_ENABLE:
            text = "".join(f"⟨{j + 1}⟩{u}" for j, u in enumerate(split_units(c.get("chunk_text", ""))))
        else:
            text = (c.get("chunk_text", "") or "")[:300]
        hdr = f"[{i + 1}] (type={ctype}, {date}" + (f", {tags}" if tags else "") + f") {text}"
        lines.append(hdr)
    tmpl = RERANK_PROMPT_CTX if context else RERANK_PROMPT_NO_CTX
    prompt = tmpl.format(ai=AI_NAME, user=USER_NAME, context=context, msg=msg[:200],
                         candidates="\n".join(lines))
    payload = {"model": RERANKER_MODEL, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.0, "max_tokens": 1200}
    if RERANKER_DISABLE_THINKING:
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(RERANKER_API, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {RERANKER_KEY}"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_RERANK) as resp:
                data = json.loads(resp.read())
            raw = data["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = re.sub(r'^```\w*\n?', '', raw)
                raw = re.sub(r'\n?```$', '', raw)
            return json.loads(raw)
        except Exception as e:
            if attempt < retries:
                log(f"rerank_err: {e} (retrying)")
                continue
            log(f"rerank_err: {e}")
            return []


# ════════════════════════════════════════════════════════════════════
# Step 6: Format & Inject
# ════════════════════════════════════════════════════════════════════
def days_ago(date_str):
    try:
        d = (datetime.now() - datetime.strptime(date_str[:10], "%Y-%m-%d")).days
        return "today" if d == 0 else ("1 day ago" if d == 1 else f"{d} days ago")
    except Exception:
        return ""


def brief_event(text):
    """Title + body, with metadata lines dropped; a trailing 'current status' section is
    kept whole. Adjust the markers to your event format."""
    lines = text.split('\n')
    title, body, status = "", [], []
    in_status = False
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith('## ') or s.startswith('# '):
            if not title:
                title = s.lstrip('# ').strip()
                continue
            in_status = ('status' in s.lower()) or ('当前状态' in s)
            continue
        if s.startswith('**Status**') or s.startswith('**Linked**') or s.startswith('**Scope**'):
            continue
        (status if in_status else body).append(s)
    body_text = ' '.join(body)
    status_text = ' '.join(status)[:250]
    budget = max(150, EVENT_BRIEF_MAX - len(status_text))
    if len(body_text) > budget:
        body_text = body_text[:budget] + '...'
    out = (f"《{title}》 " if title else "") + body_text
    if status_text:
        out += f"\n▸ current status: {status_text}"
    return out


def format_full(inject):
    all_text = " ".join(c.get("chunk_text", "") for c in inject)
    print("【auto-surfaced memories】")
    if any(w in all_text.lower() for w in RELATIVE_TIME_WORDS):
        print("(note: relative times like 'next week' are relative to the record date, not today)")
    for c in inject:
        date = str(c.get("created_at", ""))[:10]
        pid = c.get("parent_memory_id", "")
        tag = f"[{pid}_{c.get('chunk_index', '')}]" + (f" {date}" if date else "")
        ago = days_ago(date) if date else ""
        if ago:
            tag += f" ({ago})"
        if c.get("category") == "event":
            full = fetch_full_memory(pid) or c.get("chunk_text", "")
            print(f"\n[event overview] {tag}")
            print("(aggregated from several memories, not verbatim)")
            print(brief_event(full))
        else:
            imp = c.get("importance", "")
            if imp:
                tag += f" imp={imp}"
            print(f"\n[memory fragment] {tag}")
            print(c["chunk_text"])


def format_pointers(inject):
    """LIGHT rendering: one line per hit, no body. Weak signal gets a clue; only explicit
    recall gets content. The [8hex] id lands in the transcript and doubles as same-session
    dedup for later LIGHT passes."""
    for c in inject:
        date = str(c.get("created_at", ""))[:10]
        md = f"{int(date[5:7])}/{int(date[8:10])} " if len(date) >= 10 else ""
        label = "related event" if c.get("category") == "event" else "related memory"
        print(f"⟦déjà vu⟧ {md}{label} 「{sent_hook(c)}…」 [{c.get('parent_memory_id', '')}] — search it if relevant, ignore otherwise")


# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════
def main():
    t0 = time.time()
    try:
        event = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)
    msg = event.get("prompt") or event.get("user_message") or ""
    tpath = event.get("transcript_path", "")

    # Step 0: normalize. A message that is entirely <channel> blocks (chat-plugin relay)
    # is unwrapped — the shell is ~60 chars of noise that drags cosine down and fools the
    # short/noise gates. Mixed content is left alone.
    blocks = CHANNEL_RE.findall(msg)
    if blocks and not CHANNEL_RE.sub('', msg).strip():
        msg = "\n".join(b.strip() for b in blocks)
    msg = LEADING_TS_RE.sub('', msg)
    log("=== invoke ===", {"msg": msg[:120]})

    # Step 1
    mode, reason = intent_gate(msg)
    log(f"gate: {mode} ({reason})")
    if mode == "SKIP":
        sys.exit(0)
    if mode == "LIGHT" and MUTE_RE and MUTE_RE.search(msg):
        log("mute: LIGHT muted by MUTE_RE")
        sys.exit(0)

    # Step 2
    if mode == "TRIGGER":
        merged, n_ev, n_gen = dual_search(msg, ev_limit=3, gen_limit=8)
    else:
        merged, n_ev, n_gen = dual_search(msg, ev_limit=2, gen_limit=8)
    log(f"search: {n_ev}ev+{n_gen}gen -> {len(merged)} merged",
        [{"id": f"{c.get('parent_memory_id', '')}_{c.get('chunk_index', '')}", "s": c.get("score"),
          "cat": c.get("category")} for c in merged[:8]])
    if not merged:
        sys.exit(0)

    # Step 3
    candidates, flog, pushed_mids = hard_filter(merged, msg, tpath, mode)
    log(f"filter: {len(candidates)}/{len(merged)} kept", flog)
    if not candidates:
        sys.exit(0)

    # Step 4 (LIGHT only)
    if mode == "LIGHT":
        best_cos = max((c.get("score", 0) for c in candidates), default=0)
        if best_cos < LIGHT_COS_GATE:
            log(f"light_gate: best={best_cos:.3f} < {LIGHT_COS_GATE}, skip rerank")
            sys.exit(0)
        log(f"light_gate: best={best_cos:.3f} >= {LIGHT_COS_GATE}, proceed")

    # Step 5
    if RERANKER_KEY:
        context = get_recent_context(tpath)
        if context:
            log(f"context: {len(context)} chars", context[:500])
        scores = llm_rerank(msg, candidates, context, retries=1 if mode == "TRIGGER" else 0)
        log("rerank", scores)
        if SNAP_ADJUDICATE:
            scores, note = snap_adjudicate(scores, candidates)
            if note:
                log("snap_adjudicate", note)
        for r in scores:
            idx = r.get("id", 0) - 1
            if 0 <= idx < len(candidates):
                candidates[idx]["_sent"] = r.get("sent")
        # Same-tier newest-first: among score-2 hits, event and fragment each take the most
        # recently created one. Tie-break only — no global time weighting.
        ev2, fr2 = [], []
        for r in scores:
            idx = r.get("id", 0) - 1
            if 0 <= idx < len(candidates) and r.get("score") == 2:
                (ev2 if candidates[idx].get("category") == "event" else fr2).append(candidates[idx])
        inject_event = max(ev2, key=lambda c: str(c.get("created_at") or ""), default=None)
        inject_frag = max(fr2, key=lambda c: str(c.get("created_at") or ""), default=None)
        inject = [x for x in [inject_event, inject_frag] if x]
    else:
        # No reranker key → v1 behaviour: cosine threshold only
        inject = [c for c in candidates if c.get("score", 0) >= 0.7][:2]

    log(f"done: inject={len(inject)} time={time.time() - t0:.1f}s",
        [f"{c.get('parent_memory_id', '')}_{c.get('chunk_index', '')} ({c.get('category', '')})" for c in inject])
    if not inject:
        sys.exit(0)

    # Step 6
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if mode == "TRIGGER" or not LIGHT_POINTER_ONLY:
            format_full(inject)
        else:
            format_pointers(inject)
    sys.stdout.write(buf.getvalue())


if __name__ == "__main__":
    main()
