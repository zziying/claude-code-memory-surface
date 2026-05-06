# claude-code-memory-surface

> Per-message proactive memory surfacing for Claude Code — using `UserPromptSubmit` hook + transcript-based dedup.

English | [中文](README_zh.md)

## What this is

When you talk to Claude Code, the model only "remembers" what's in its current context window. Memory MCP servers (like [Ombre-Brain](https://github.com/P0luz/Ombre-Brain)) solve part of this — they store memories durably and let the model retrieve them via tool calls. But there's still a gap:

**The model has to know to call the retrieval tool.** If it doesn't actively think "I should search memory now," relevant context stays hidden.

This project closes that gap: a Claude Code `UserPromptSubmit` hook that, on every user message, runs a semantic search and pushes the most relevant chunks into the model's context **before** the model starts responding. No tool call required — the relevant memory is just *there* when the model wakes up to your message.

```
You type a message
  ↓
UserPromptSubmit hook fires (before Claude sees your message)
  ↓
Hook embeds your message → runs semantic search against memory db
  ↓
Top relevant chunks (filtered by score + dedup) injected into context
  ↓
Claude reads your message *and* the surfaced memories together
```

## The novel bit

Almost all memory MCP servers (Ombre-Brain, mem0, letta, etc.) make the **model** responsible for memory retrieval. This project makes **the platform** responsible.

| Mechanism | Where it lives | Triggered by |
|---|---|---|
| Tool-based retrieval (everyone) | MCP tool the model calls | Model decides |
| `SessionStart` hook (Ombre-Brain) | Claude Code hook | Session start, once |
| **`UserPromptSubmit` hook (this project)** | Claude Code hook | **Every user message** |

The other novel piece is **transcript-based dedup**. Instead of maintaining a separate state file (which gets out of sync when you rewind a message), the hook reads the Claude Code session transcript file directly and parses already-pushed chunk IDs from it. This means:

- **Rewind-tolerant**: if you rewind a message, the hook automatically forgets it pushed those chunks (because they're no longer in transcript) and is willing to push them again.
- **No state file**: nothing to clean up, nothing to corrupt.
- **Briefing-aware**: the hook also detects full-memory references (e.g. `[5b4e983f]` from a briefing tool) and skips chunks of memories that were already pushed in full.

## Status: this is 抛砖引玉 (a starting point, not a finished product)

**The hook's tunables — `KEYWORDS`, `SCORE_THRESHOLD`, `MIN_LEN_TRIGGER` — are optimized for one specific person's conversation style. Yours will be different.**

The same goes for the reference memory MCP server: its schema (deep / daily / diary / memo categories, valence/arousal tags, piecewise decay curve) is one specific take. Your relationship with Claude Code, your topics, your cadence — all different.

This repo is meant as a working example to fork and customize. The README explains the architecture clearly enough that you can ask your own Claude Code to help you adapt it. **Don't expect plug-and-play** — expect a starting point.

## Repo layout

```
claude-code-memory-surface/
├── hook/
│   └── memory_surface.py         ← the novel contribution: UserPromptSubmit hook
├── reference/
│   └── server.py                 ← reference memory MCP server (schema inspired by Ombre-Brain)
├── scripts/
│   ├── backfill_chunks.py        ← chunk + embed existing memories (one-time)
│   └── backfill_retry.py         ← retry NULL embeddings (rate-limit recovery)
├── systemd/
│   └── memory-mcp.service.example
├── .env.example
├── .gitignore
├── LICENSE                       ← MIT
└── README.md
```

## Setup

### Prerequisites

- A Linux server (1GB RAM is plenty) for the memory MCP server
- Python 3.10+ with `numpy`
- A free Gemini API key from <https://aistudio.google.com/apikey>
- Claude Code installed locally

### 1. Deploy the reference memory MCP server (skip if you have your own)

```bash
git clone https://github.com/<your-username>/claude-code-memory-surface.git
cd claude-code-memory-surface
cp .env.example .env
# Edit .env — set MCP_TOKEN (any random hex), GEMINI_API_KEY
python3 reference/server.py    # runs on port 3458
```

For a permanent setup, copy `systemd/memory-mcp.service.example` to `/etc/systemd/system/memory-mcp.service`, edit the paths, and `systemctl enable --now memory-mcp`.

If you want it accessible over HTTPS, put nginx in front of port 3458.

### 2. Add the MCP server to Claude Code

In `~/.claude.json` (or via `claude mcp add`):

```json
{
  "mcpServers": {
    "memory": {
      "type": "sse",
      "url": "http://localhost:3458/<your_MCP_TOKEN>/sse"
    }
  }
}
```

### 3. Install the hook

```bash
mkdir -p ~/.claude/hooks
cp hook/memory_surface.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/memory_surface.py
```

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMORY_MCP_URL=http://localhost:3458/<your_MCP_TOKEN> python3 ~/.claude/hooks/memory_surface.py"
          }
        ]
      }
    ]
  }
}
```

### 4. Test the hook standalone

```bash
echo '{"prompt":"<some query that should match memories you have>"}' \
  | MEMORY_MCP_URL=http://localhost:3458/<your_MCP_TOKEN> python3 ~/.claude/hooks/memory_surface.py
```

You should see `[memory-surface] auto-surfaced relevant chunks:` followed by chunks. If you see nothing, try a longer query or one containing a keyword from the `KEYWORDS` list.

### 5. (Optional) Backfill embeddings for existing memories

If you already have memories in the database without embeddings:

```bash
MEMORY_DB_PATH=./memories.db GEMINI_API_KEY=<your_key> python3 scripts/backfill_chunks.py
```

If you hit Gemini rate limits mid-run, use the retry script:

```bash
MEMORY_DB_PATH=./memories.db GEMINI_API_KEY=<your_key> python3 scripts/backfill_retry.py
```

## How to customize for your style

These are the things you'll probably want to change.

### Hook script (`hook/memory_surface.py`)

| Tunable | What it controls | How to tune |
|---|---|---|
| `KEYWORDS` | Words that indicate "I'm querying memory" — bypass length check and trigger | Add words you naturally use when referencing past stuff. The defaults are Chinese + a few English. |
| `SCORE_THRESHOLD` | Minimum cosine similarity to push a chunk | Higher = stricter (less noise, more misses). 0.7 worked well for me; 0.65 if your queries are vague, 0.75 if you want very tight matching. |
| `MIN_LEN_TRIGGER` | Below this length, skip unless keyword match | 6 chars catches "嗯", "ok", "haha"; raise to 15 for English-heavy use. |
| `MAX_CHUNKS` | Max chunks to push per message | 2 is conservative; 3-4 if your memory is heavily chunked. |

### Memory schema (`reference/server.py`)

| Decision | Default | Alternatives |
|---|---|---|
| Categories | `deep` / `daily` / `diary` / `memo` | Maybe just `notes` / `facts`. Or topic-based: `work` / `personal` / `tech`. |
| Decay curve | piecewise: short-term recency-weighted, long-term emotion-weighted | Linear / exponential / no decay. |
| Chunking strategy | `## headers` → `【】 sections` → paragraphs (max 600 chars) | Tune for the markdown style you actually use. |
| Embedding model | `gemini-embedding-001` (3072-dim, free tier) | Any model with an OpenAI-compatible embeddings endpoint. |

The point is: **don't keep my decisions if they don't fit you.** Fork, change, redeploy.

## Compatibility — using your own memory backend

The hook only requires that your MCP server expose a `semantic_search` tool with this signature:

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

**Response (the `text` field is a JSON-encoded list of chunk objects):**
```json
{
  "result": {
    "content": [{"type": "text", "text": "[{\"chunk_text\": \"...\", \"parent_memory_id\": \"...\", \"chunk_index\": 0, \"score\": 0.74, \"category\": \"...\"}]"}]
  }
}
```

If you have a memory MCP server that already does chunk-level embedding (e.g. you can adapt Ombre-Brain), wrap its retrieval in a tool with this signature and the hook will work.

## Architecture details

### Why per-message hook beats tool-based retrieval

When retrieval is a tool the model calls, the model has to *decide* to call it — and that decision is unreliable. Models default to working with what's in context. If the user mentions "Joffy" and the model hasn't seen "Joffy" before, it will often respond ambiguously rather than search.

A hook bypasses the decision entirely. By the time the model sees your message, the relevant chunks are already in its context. The model just reads them like any other context.

### Why transcript-based dedup beats state files

Naive dedup keeps a `pushed_chunks.json` file. Two failure modes:

1. **Rewind**: user rewinds a message that triggered a push. The state file still says "this chunk was pushed" but it's no longer in context. Hook will skip pushing again on the next relevant message → user sees no recall.
2. **TTL-based reset**: state file is wiped after N hours of inactivity. But Claude Code sessions can last 3-4 days, so wiping mid-session re-pushes things → noise.

Reading the transcript file directly fixes both. The transcript is the actual source of truth for what's in the model's context. Anything in there is "pushed"; anything not in there is "not pushed" — including stuff that was rewound out.

The hook uses two regex patterns:
- `\[(\w{6,}_\d+)\]` matches chunks pushed by the hook itself (format `[memory-id_chunk-index]`)
- `\[([a-f0-9]{8})\]` matches full memory IDs from briefing-style tools (format `[memory-id]`)

Anything matching the second pattern is treated as "the entire memory is already in context" — all chunks of that memory are skipped.

### Quality gates

Embedding-based search returns "similar" chunks, not necessarily "relevant" ones. Common phrases like "how are you doing recently" pull up anything with a "recent state" theme regardless of subject. To filter:

- **Score threshold (0.7)**: weak matches (typically the noisy false positives) live in the 0.6-0.7 band. Cutting at 0.7 cleanly separates them from real matches (0.7+).
- **Length gating**: `"嗯"` and `"ok"` get filtered before any API call (saves rate limit + latency).
- **Keyword whitelist**: explicit history-querying words ("还记得", "上次", "remember") bypass length gating.

## Known limits

- **Gemini free-tier rate limits** are strict for `gemini-embedding-001` — observed ~75 RPM stable. Heavy bursts (writing many large memories at once) will hit 429. The `backfill_retry.py` script handles this with adaptive backoff.
- **No multimodal memory yet**: images, voice, etc. aren't embedded.
- **No cross-memory linking**: the chunks table doesn't track explicit relations between memories. Could be added in a future iteration.
- **Embedding model migration is destructive**: changing `EMBED_MODEL` requires re-embedding everything (the chunks store raw float32 BLOBs, not model-tagged vectors).

## Prior art / inspiration

- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) by P0luz — the schema design (valence/arousal labels, piecewise decay, chunk-level embedding) is directly inspired by this project. Ombre-Brain implements `SessionStart`-hook-based surfacing and is a great Memory MCP server in its own right. The novel direction in `claude-code-memory-surface` is the per-message `UserPromptSubmit` hook + transcript-based dedup, not the memory schema itself.
- [mem0](https://github.com/mem0ai/mem0) — provider-agnostic memory layer. Different abstraction (more like a structured-fact extractor with vector backend).
- [letta (memgpt)](https://github.com/letta-ai/letta) — full agent framework with tiered memory. Different scope.

## License

MIT.
