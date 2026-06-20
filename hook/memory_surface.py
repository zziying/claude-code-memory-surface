#!/usr/bin/env python3
"""Memory surfacing hook v3 — three-tier intent gate + LLM reranker.

Upgraded from v1 (cosine threshold only) to v3:
  - Three-tier Intent Gate (SKIP / TRIGGER / LIGHT)
  - LLM-based reranker (any OpenAI-compatible API) replaces hard score threshold
  - Conversation context extraction from transcript for reranker
  - Event-aware dual search (optional)
  - Cosine gate for LIGHT mode (skip reranker when no promising candidates)

Pipeline:
  1. Intent Gate (~0ms)
  2. Search: event + general in parallel (~1s)
  3. Hard Filter: dedup via transcript, skip archived/deep/resolved (~0ms)
  4. [LIGHT only] Cosine gate — skip reranker if best score too low
  5. LLM Reranker (~2-4s)
  6. Inject: max 1 event + max 1 fragment (configurable)

Compatible with any memory MCP server that exposes a search tool
returning chunks with: chunk_text, parent_memory_id, chunk_index, score, category.
"""

import json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ── Config (override via env vars or .env file) ─────────────────────
MCP_URL = os.environ.get("MEMORY_MCP_URL", "")
SEARCH_TOOL_NAME = os.environ.get("SEARCH_TOOL_NAME", "semantic_search")

# LLM reranker (any OpenAI-compatible API: DeepSeek, OpenAI, local, etc.)
RERANKER_API = os.environ.get("RERANKER_API_URL", "https://api.deepseek.com/chat/completions")
RERANKER_KEY = os.environ.get("RERANKER_API_KEY", "")
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "deepseek-chat")

# Role names for reranker prompt (customize to your setup)
USER_NAME = os.environ.get("USER_NAME", "user")
AI_NAME = os.environ.get("AI_NAME", "assistant")

TIMEOUT_SEARCH = float(os.environ.get("TIMEOUT_SEARCH", "2.0"))
TIMEOUT_RERANK = float(os.environ.get("TIMEOUT_RERANK", "6.0"))
LIGHT_COS_GATE = float(os.environ.get("LIGHT_COS_GATE", "0.65"))
EVENT_BRIEF_MAX = int(os.environ.get("EVENT_BRIEF_MAX", "300"))

LOG_PATH = os.environ.get("HOOK_LOG_PATH",
                          os.path.expanduser("~/.claude/hooks/surface_debug.log"))
LOG_MAX_KB = 200

# ── Intent Gate keywords ─────────────────────────────────────────────
# "I'm asking about the past" signals. Customize for your language/style.
RECALL_KW = [
    "还记得", "上次", "那次", "之前", "以前", "记得", "记忆",
    "那个时候", "那一次", "说过", "聊过", "做过", "提过", "试过",
    "上周", "上个月", "前几天", "最近",
    "后来怎么", "继续上次",
    "复盘", "回顾", "进度", "坚持了", "连续",
    "remember", "last time", "earlier", "previously", "before",
]
# Compound words to exclude (e.g. "记忆库" contains "记忆" but is about
# the system itself, not recalling a specific memory)
RECALL_EXCLUDE = os.environ.get("RECALL_EXCLUDE", "记忆库,记忆浮现,记忆系统").split(",")

NOISE_WORDS = {
    "嗯", "好", "ok", "哦", "哈", "嗯嗯", "好的", "行", "可以",
    "收到", "明白", "了解", "对", "是的", "好吧", "行吧",
    "哈哈", "哈哈哈", "笑死", "nice", "cool", "yeah", "yes", "no",
    "谢谢", "thanks", "thx", "继续", "next", "lgtm",
}

CODE_SKIP_RE = [
    re.compile(r'```'),
    re.compile(r'^\s*(git |npm |pip |brew |cargo |docker |kubectl |ssh |curl |make |cd |ls |cat |rm )', re.M),
    re.compile(r'(Traceback|FAIL|error\[|Error:|Exception:|panic:|fatal:)', re.I),
    re.compile(r'(/Users/|/root/|/home/|/tmp/|/var/|/etc/)'),
    re.compile(r'^\s*(def |class |func |import |from |const |let |var |function )', re.M),
    re.compile(r'^\s*[\{\["]', re.M),
]

SYSTEM_PREFIXES = os.environ.get("SYSTEM_PREFIXES", "[auto-trigger],[system],[cron]").split(",")

CHUNK_ID_RE = re.compile(r'\[(\w{6,}_\d+)\]')
MEMORY_ID_RE = re.compile(r'\[([a-f0-9]{8})\]')

HISTORICAL_KW = ["之前怎么", "上次怎么", "后来怎么", "怎么解决的", "复盘", "回顾", "当时"]


# ════════════════════════════════════════════════════════════════════
# Logging
# ════════════════════════════════════════════════════════════════════
def log(msg, data=None):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_KB * 1024:
            with open(LOG_PATH, 'r') as f:
                lines = f.readlines()
            with open(LOG_PATH, 'w') as f:
                f.writelines(lines[len(lines) // 2:])
        with open(LOG_PATH, 'a') as f:
            ts = datetime.now().strftime('%m-%d %H:%M:%S')
            f.write(f"[{ts}] {msg}\n")
            if data is not None:
                f.write(f"  {json.dumps(data, ensure_ascii=False)[:800]}\n")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# Step 1: Three-tier Intent Gate
# ════════════════════════════════════════════════════════════════════
def intent_gate(msg):
    """Returns ('SKIP'|'TRIGGER'|'LIGHT', reason)."""
    if not msg or not msg.strip():
        return "SKIP", "empty"
    s = msg.strip()
    if any(s.startswith(p) for p in SYSTEM_PREFIXES):
        return "SKIP", "system"
    lo = s.lower()
    if lo in NOISE_WORDS:
        return "SKIP", "noise"
    if len(s) < 6:
        for kw in RECALL_KW:
            if kw in lo:
                return "TRIGGER", f"recall:{kw}"
        return "SKIP", f"short({len(s)})"
    for kw in RECALL_KW:
        if kw in lo:
            if any(ex in lo for ex in RECALL_EXCLUDE if kw in ex):
                continue
            return "TRIGGER", f"recall:{kw}"
    for pat in CODE_SKIP_RE:
        if pat.search(msg):
            return "SKIP", "code"
    return "LIGHT", f"default(len={len(s)})"


# ════════════════════════════════════════════════════════════════════
# Step 2: Search
# ════════════════════════════════════════════════════════════════════
def mcp_search(query, limit=8, category=None):
    if not MCP_URL:
        return []
    args = {"query": query, "limit": limit, "no_track": True}
    if category:
        args["category"] = category
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": SEARCH_TOOL_NAME, "arguments": args}
    }).encode()
    req = urllib.request.Request(MCP_URL, data=body,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEARCH) as resp:
            data = json.loads(resp.read())
        text = data["result"]["content"][0]["text"]
        if text.startswith("Error") or text.startswith("No "):
            return []
        return json.loads(text)
    except Exception as e:
        log(f"search_err({category or 'all'}): {e}")
        return []


def dual_search(query, ev_limit=3, gen_limit=8):
    """Event + general search in parallel, dedup by parent_id."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ev = ex.submit(mcp_search, query, ev_limit, "event")
        f_gen = ex.submit(mcp_search, query, gen_limit)
        events = f_ev.result()
        general = f_gen.result()
    seen = set()
    merged = []
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
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return set(CHUNK_ID_RE.findall(content)), set(MEMORY_ID_RE.findall(content))
    except Exception:
        return set(), set()


def extract_entities(msg):
    ents = set()
    for w in re.findall(r'[a-zA-Z]{3,}', msg):
        ents.add(w.lower())
    zh = re.sub(r'[^一-鿿]', '', msg)
    for n in (2, 3, 4):
        for i in range(len(zh) - n + 1):
            ents.add(zh[i:i + n])
    return ents


def hard_filter(chunks, msg, transcript_path):
    entities = extract_entities(msg)
    historical = any(kw in msg for kw in HISTORICAL_KW)
    pushed_cids, pushed_mids = get_transcript_ids(transcript_path)
    kept, flog = [], []
    seen_parents = set()

    for c in chunks:
        pid = c.get("parent_memory_id", "")
        cidx = c.get("chunk_index", 0)
        cid = f"{pid}_{cidx}"
        drop = None

        if c.get("archived"):
            drop = "archived"
        elif c.get("category") == "deep":
            drop = "deep"
        elif cid in pushed_cids:
            drop = "dup_chunk"
        elif pid in pushed_mids:
            drop = "dup_parent"
        elif c.get("parent_id") and c["parent_id"] in pushed_mids:
            drop = "dup_source"
        elif pid in seen_parents:
            drop = "dup_sibling"
        elif c.get("resolved") and not historical:
            drop = "resolved"
        elif c.get("category") == "fragment":
            imp = c.get("importance", 5)
            if isinstance(imp, (int, float)) and imp < 3:
                hay = c.get("chunk_text", "") + " " + c.get("tags", "")
                if not any(e in hay.lower() for e in entities):
                    drop = f"low_imp({imp})"

        flog.append({"cid": cid, "cat": c.get("category", ""), "drop": drop})
        if not drop:
            kept.append(c)
            seen_parents.add(pid)
            if c.get("parent_id"):
                seen_parents.add(c["parent_id"])

    return kept, flog


# ════════════════════════════════════════════════════════════════════
# Step 4: Context extraction + LLM Reranker
# ════════════════════════════════════════════════════════════════════
def get_recent_context(transcript_path, n_turns=3):
    """Extract last N user+assistant turns from transcript JSONL."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            read_from = max(0, size - 80_000)
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
            if t == "user":
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, str):
                    clean = re.sub(r'<system-reminder>.*?</system-reminder>', '', content, flags=re.S).strip()
                    if clean:
                        turns.append((USER_NAME, clean[:150]))
            elif t == "assistant":
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    texts = [b.get("text", "") for b in content if b.get("type") == "text"]
                    text = " ".join(texts).strip()
                elif isinstance(content, str):
                    text = content.strip()
                else:
                    text = ""
                if text:
                    turns.append((AI_NAME, text[:150]))
            if len(turns) >= n_turns * 2:
                break
        if len(turns) < 2:
            return ""
        turns.reverse()
        return "\n".join(f"{role}: {text}" for role, text in turns)
    except Exception as e:
        log(f"context_err: {e}")
        return ""


RERANK_PROMPT_CTX = """You are a personal memory retrieval quality judge.

Candidate types:
- event_overview: aggregated overview of multiple memories, high information density. Prefer when user needs history/patterns/progress.
- fragment: a specific memory moment. Prefer when user needs exact details or a specific moment.

Selection rules:
- When event provides sufficient context, prefer event over fragment
- Only select fragment when it adds details not covered by event
- When event and fragment overlap, pick event only
- For status/progress questions, prefer recent memories
- Pay attention to conversation tone: teasing/joking/sarcasm should not be taken literally. Use the recent conversation context to judge.

Recent conversation (for context/tone):
{context}

Current message:
{msg}

Candidate memories:
{candidates}

Scoring:
2 = directly useful — the answer would be noticeably worse without this memory
1 = tangentially related but not necessary
0 = irrelevant / too generic / keyword coincidence with wrong context

Be strict. Topical overlap alone is not a 2. Keywords that match by coincidence but in a different context should score 0.

Return a JSON array only:
[{{"id":1,"score":0,"reason":"..."}}]"""

RERANK_PROMPT_NO_CTX = """You are a personal memory retrieval quality judge.

Candidate types:
- event_overview: aggregated overview of multiple memories, high information density.
- fragment: a specific memory moment.

Selection rules:
- When event provides sufficient context, prefer event over fragment
- Only select fragment when it adds details not covered by event
- For status/progress questions, prefer recent memories

Current message:
{msg}

Candidate memories:
{candidates}

Scoring:
2 = directly useful — the answer would be noticeably worse without this memory
1 = tangentially related but not necessary
0 = irrelevant / too generic

Be strict. Topical overlap alone is not a 2.

Return a JSON array only:
[{{"id":1,"score":0,"reason":"..."}}]"""


def llm_rerank(msg, candidates, context=""):
    if not candidates or not RERANKER_KEY:
        return []
    lines = []
    for i, c in enumerate(candidates):
        date = c.get("created_at", "")[:10]
        cat = c.get("category", "")
        ctype = "event_overview" if cat == "event" else "fragment"
        tags = c.get("tags", "")
        text = c.get("chunk_text", "")[:300]
        hdr = f"[{i + 1}] (type={ctype}, {date}"
        if tags:
            hdr += f", {tags}"
        hdr += f") {text}"
        lines.append(hdr)

    if context:
        prompt = RERANK_PROMPT_CTX.format(context=context, msg=msg[:200], candidates="\n".join(lines))
    else:
        prompt = RERANK_PROMPT_NO_CTX.format(msg=msg[:200], candidates="\n".join(lines))

    body = json.dumps({
        "model": RERANKER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 800,
    }).encode()
    req = urllib.request.Request(RERANKER_API, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RERANKER_KEY}"
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_RERANK) as resp:
            data = json.loads(resp.read())
        raw = data["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```\w*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)
        return json.loads(raw)
    except Exception as e:
        log(f"rerank_err: {e}")
        return []


# ════════════════════════════════════════════════════════════════════
# Step 5: Format & Inject
# ════════════════════════════════════════════════════════════════════
def days_ago(date_str):
    try:
        created = datetime.strptime(date_str[:10], "%Y-%m-%d")
        d = (datetime.now() - created).days
        if d == 0:
            return "today"
        elif d == 1:
            return "1 day ago"
        else:
            return f"{d} days ago"
    except Exception:
        return ""


def brief_event(text):
    lines = text.split('\n')
    body = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith('## ') or s.startswith('**Status**') or s.startswith('**Linked**'):
            continue
        if s.startswith('**Key') or s.startswith('**关键节点**'):
            break
        body.append(s)
    out = ' '.join(body)
    if len(out) > EVENT_BRIEF_MAX:
        out = out[:EVENT_BRIEF_MAX] + '...'
    return out


def format_output(inject):
    print("【auto-surfaced memories】")
    for c in inject:
        date = c.get("created_at", "")[:10]
        cat = c.get("category", "")
        pid = c.get("parent_memory_id", "")
        ci = c.get("chunk_index", "")
        ago = days_ago(date) if date else ""
        tag = f"[{pid}_{ci}]"
        if date:
            tag += f" {date}"
        if ago:
            tag += f" ({ago})"
        if cat == "event":
            print(f"\n[event overview] {tag}")
            print(brief_event(c.get("chunk_text", "")))
        else:
            imp = c.get("importance", "")
            if imp:
                tag += f" imp={imp}"
            print(f"\n[memory fragment] {tag}")
            print(c["chunk_text"])


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
    log("=== invoke ===", {"msg": msg[:120]})

    # Step 1: Intent Gate
    mode, reason = intent_gate(msg)
    log(f"gate: {mode} ({reason})")
    if mode == "SKIP":
        sys.exit(0)

    # Step 2: Search
    if mode == "TRIGGER":
        merged, n_ev, n_gen = dual_search(msg, ev_limit=3, gen_limit=8)
    else:
        merged, n_ev, n_gen = dual_search(msg, ev_limit=2, gen_limit=5)
    log(f"search: {n_ev}ev+{n_gen}gen -> {len(merged)} merged")
    if not merged:
        sys.exit(0)

    # Step 3: Hard Filter
    candidates, flog = hard_filter(merged, msg, tpath)
    log(f"filter: {len(candidates)}/{len(merged)} kept", flog)
    if not candidates:
        sys.exit(0)

    # Step 4: Cosine gate (LIGHT mode only — skip expensive reranker if nothing looks promising)
    if mode == "LIGHT":
        best_cos = max((c.get("score", 0) for c in candidates), default=0)
        if best_cos < LIGHT_COS_GATE:
            log(f"light_gate: best={best_cos:.3f} < {LIGHT_COS_GATE}, skip")
            sys.exit(0)

    # Step 5: LLM Reranker
    if RERANKER_KEY:
        context = get_recent_context(tpath)
        scores = llm_rerank(msg, candidates, context)
        log("rerank", scores)
        inject_event, inject_frag = None, None
        for r in scores:
            idx = r.get("id", 0) - 1
            if 0 <= idx < len(candidates) and r.get("score") == 2:
                c = candidates[idx]
                if c.get("category") == "event" and not inject_event:
                    inject_event = c
                elif c.get("category") != "event" and not inject_frag:
                    inject_frag = c
        inject = [x for x in [inject_event, inject_frag] if x]
    else:
        # Fallback: no reranker key → use cosine score threshold (v1 behavior)
        inject = [c for c in candidates if c.get("score", 0) >= 0.7][:2]

    elapsed = time.time() - t0
    log(f"done: inject={len(inject)} time={elapsed:.1f}s")

    if not inject:
        sys.exit(0)

    format_output(inject)


if __name__ == "__main__":
    main()
