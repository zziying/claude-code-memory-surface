# claude-code-memory-surface

> Let memories find you, instead of waiting for you to look for them.

[中文](README.md) | English

## The problem

Memory MCP servers for Claude Code all share one assumption: **the model has to decide when to search memory.** If it doesn't think to search, relevant context stays buried.

Several projects ([claude-mem](https://github.com/thedotmack/claude-mem), [ClawMem](https://github.com/yoloshii/ClawMem), etc.) solve this with a `UserPromptSubmit` hook — running a semantic search on every user message and injecting relevant memories into context before the model responds.

This project takes the same approach, but makes a different choice for deduplication: **it reads the Claude Code session transcript directly** to determine what's already been pushed, instead of maintaining a separate state file.

```
You send a message
  ↓
UserPromptSubmit hook fires (before Claude sees your message)
  ↓
Hook embeds your message → semantic search against memory db
  ↓
Top relevant chunks (filtered by score + dedup) injected into context
  ↓
Claude reads your message and the surfaced memories together
```

## Transcript-based dedup

This is the main difference from similar projects.

Maintaining a `pushed_chunks.json` state file for dedup has two failure modes:

1. **Rewind**: user rewinds a message that triggered a push. The state file still says "pushed" but the chunk is no longer in context. Next relevant message → hook skips it → user sees no recall.
2. **TTL reset**: state file is wiped after inactivity. But Claude Code sessions can last days — wiping mid-session causes re-pushes.

This project reads the transcript file directly. The transcript is the ground truth of what's in context: present means pushed, absent means not pushed — including things that were rewound out. No state file to maintain, clean up, or get out of sync.

The hook uses two regex patterns:
- `\[(\w{6,}_\d+)\]` — matches chunks pushed by the hook itself (`[memory-id_chunk-index]`)
- `\[([a-f0-9]{8})\]` — matches full memory references from other tools (e.g. a briefing tool). If your memory system doesn't output this format, this rule simply won't match anything.

## Status: a starting point, not a finished product

Everyone's memory system is different. The hook's trigger keywords, score threshold, and length gate are tuned for one person's conversation style. The included memory MCP server is a minimal reference implementation — just basic CRUD + semantic search, enough to verify the hook works. You'll likely swap it for your own backend.

Fork it, change it, make it yours.

## Repo layout

```
claude-code-memory-surface/
├── hook/
│   └── memory_surface.py         ← the core: UserPromptSubmit hook script
├── reference/
│   └── server.py                 ← minimal memory MCP server (reference impl)
├── scripts/
│   ├── backfill_chunks.py        ← chunk + embed existing memories (one-time)
│   └── backfill_retry.py         ← retry NULL embeddings (rate-limit recovery)
├── systemd/
│   └── memory-mcp.service.example
├── .env.example
├── .gitignore
└── LICENSE                       ← MIT
```

## Setup

### Prerequisites

- A memory MCP server that supports `semantic_search` (your own, or an existing solution like Ombre-Brain)
- Claude Code installed locally

> No memory backend yet? The repo includes a minimal reference implementation (`reference/server.py`) — needs Python 3.10+ / numpy / Gemini API key. See `.env.example` for config.

### 1. Install the hook

```bash
git clone https://github.com/Qizhan7/claude-code-memory-surface.git
cp claude-code-memory-surface/hook/memory_surface.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/memory_surface.py
```

### 2. Configure the hook

Add to `~/.claude/settings.json` (replace the URL with your own memory backend):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMORY_MCP_URL=<your_memory_backend_url> python3 ~/.claude/hooks/memory_surface.py"
          }
        ]
      }
    ]
  }
}
```

### 3. Test

```bash
echo '{"prompt":"some query that should match memories you have"}' \
  | MEMORY_MCP_URL=<your_memory_backend_url> python3 ~/.claude/hooks/memory_surface.py
```

You should see `[memory-surface] auto-surfaced relevant chunks:` followed by matched chunks.

### 5. Backfill embeddings for existing memories (optional)

```bash
MEMORY_DB_PATH=./memories.db GEMINI_API_KEY=<your_key> python3 scripts/backfill_chunks.py
```

Hit Gemini rate limits? Use the retry script:

```bash
MEMORY_DB_PATH=./memories.db GEMINI_API_KEY=<your_key> python3 scripts/backfill_retry.py
```

## Tuning

| Parameter | What it controls | How to tune |
|---|---|---|
| `KEYWORDS` | Trigger words — messages containing these bypass length check | Add words you naturally use when referencing past events. Defaults are Chinese-leaning. |
| `SCORE_THRESHOLD` | Minimum cosine similarity to push a chunk | Higher = stricter. Default 0.7; lower to 0.65 for vague queries, raise to 0.75 for tight matching. |
| `MIN_LEN_TRIGGER` | Messages shorter than this are skipped (unless keyword match) | Default 6 chars; raise to 15 for English-heavy use. |
| `MAX_CHUNKS` | Max chunks to push per message | Default 2. Raise to 3-4 if your memory is heavily chunked. |

## Using your own memory backend

The hook only requires your MCP server to expose a `semantic_search` tool:

**Request:**
```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {"query": "...", "limit": 5}
  }
}
```

**Response (`text` is a JSON-encoded list of chunk objects):**
```json
{
  "result": {
    "content": [{"type": "text", "text": "[{\"chunk_text\": \"...\", \"parent_memory_id\": \"...\", \"chunk_index\": 0, \"score\": 0.74}]"}]
  }
}
```

Any memory backend that can wrap its retrieval in this interface will work with the hook. The included `reference/server.py` implements this interface and can serve as an adaptation reference.

## Known limits

- **Gemini free-tier rate limits** are strict for `gemini-embedding-001` — observed ~75 RPM stable. `backfill_retry.py` handles this with adaptive backoff.
- **Changing embedding model requires full re-embed**: chunks store raw float32 BLOBs without model tags.

## Similar projects

- [claude-mem](https://github.com/thedotmack/claude-mem) — UserPromptSubmit + ChromaDB, closest in approach
- [ClawMem](https://github.com/yoloshii/ClawMem) — Most feature-rich: BM25 + vector + reranking + intent classification
- [claude-hooks](https://github.com/mann1x/claude-hooks) — UserPromptSubmit + Qdrant + attention decay
- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) — SessionStart hook + full memory MCP server

## License

MIT
