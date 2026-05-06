#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: 主动浮现相关记忆chunks。

Dedup based on transcript_path (source of truth that survives rewind).
Compatible with any memory MCP server that exposes a `semantic_search` tool
returning chunks with: chunk_text, parent_memory_id, chunk_index, score, category.

Configure MCP_URL via env var (or hardcode below for one-off setup).
All other tunables — KEYWORDS, SCORE_THRESHOLD, etc. — should be customized
to your conversation style. See README.
"""
import json, os, re, sys, urllib.request

# === Configuration (override via env vars or edit directly) ===
MCP_URL = os.environ.get("MEMORY_MCP_URL", "")  # e.g. https://your.host/memory-mcp/<token>
TIMEOUT_S = float(os.environ.get("HOOK_TIMEOUT_S", "1.5"))
SCORE_THRESHOLD = float(os.environ.get("HOOK_SCORE_THRESHOLD", "0.7"))   # < this → weak relevance, skip
MAX_CHUNKS = int(os.environ.get("HOOK_MAX_CHUNKS", "2"))
MIN_LEN_TRIGGER = int(os.environ.get("HOOK_MIN_LEN_TRIGGER", "6"))       # short noise skip threshold

# Customize KEYWORDS to your conversation style — these are "I'm querying history" signals.
# Examples below are Chinese-leaning; adjust for your language/style.
KEYWORDS = ["还记得", "上次", "那次", "之前", "以前", "咱们家", "我们家",
            "记得", "记忆", "老规矩", "那个时候", "那一次",
            "remember", "last time", "earlier"]
CHUNK_ID_PATTERN = re.compile(r'\[(\w{6,}_\d+)\]')      # hook output: [5b4e983f_0]
MEMORY_ID_PATTERN = re.compile(r'\[([a-f0-9]{8})\]')    # briefing output: [5b4e983f]


def should_trigger(msg):
    if not msg:
        return False
    # 含关键词 → 无视长度直接trigger（"还记得"3字也算）
    if any(kw in msg for kw in KEYWORDS):
        return True
    # 否则要够长才trigger（避免"嗯""好""哈哈"这类noise）
    return len(msg) >= MIN_LEN_TRIGGER


def get_pushed_from_transcript(transcript_path):
    """Returns (pushed_chunk_ids, pushed_memory_ids).
    chunk_ids: hook之前push过的具体chunks
    memory_ids: briefing或别处push过的整memory（包含full content）
    transcript是source of truth — rewind后会自动sync。"""
    if not transcript_path or not os.path.exists(transcript_path):
        return set(), set()
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            content = f.read()
        return (set(CHUNK_ID_PATTERN.findall(content)),
                set(MEMORY_ID_PATTERN.findall(content)))
    except Exception:
        return set(), set()


def semantic_search(query):
    if not MCP_URL:
        return []
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "semantic_search", "arguments": {"query": query, "limit": 5}}
    }).encode()
    req = urllib.request.Request(MCP_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        return json.loads(data["result"]["content"][0]["text"])
    except Exception:
        return []


def main():
    try:
        event = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    msg = event.get("prompt") or event.get("user_message") or ""
    transcript_path = event.get("transcript_path", "")
    if not should_trigger(msg):
        sys.exit(0)

    chunks = semantic_search(msg)
    if not chunks:
        sys.exit(0)

    # quality gate: top 1 score要够高（< 0.7的弱相关全部skip）
    if chunks[0].get("score", 0) < SCORE_THRESHOLD:
        sys.exit(0)

    pushed_chunks, pushed_memories = get_pushed_from_transcript(transcript_path)
    selected = []
    for c in chunks:
        cid = f"{c['parent_memory_id']}_{c['chunk_index']}"
        if cid in pushed_chunks:
            continue
        # 整memory被briefing/别处push过 → 它所有chunks都skip
        if c['parent_memory_id'] in pushed_memories:
            continue
        if c.get("score", 0) < SCORE_THRESHOLD:
            continue
        selected.append(c)
        if len(selected) >= MAX_CHUNKS:
            break

    if not selected:
        sys.exit(0)

    print("[memory-surface] auto-surfaced relevant chunks:")
    for c in selected:
        print(f"\n--- [{c['parent_memory_id']}_{c['chunk_index']}] "
              f"({c['category']}, score={c['score']:.2f}) ---")
        print(c["chunk_text"])


if __name__ == "__main__":
    main()
