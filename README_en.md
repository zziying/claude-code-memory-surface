# claude-code-memory-surface v3

> Let memories find you, instead of waiting for you to look for them.

[中文](README.md) | English

## The problem

External memory systems ([Ombre Brain](https://github.com/P0luz/Ombre-Brain), custom MCP servers, etc.) can **store** memories, but retrieval is passive — the model must know what to search for.

**The real problem: context compression (compact) doesn't just delete memory content — it deletes the meta-memory of knowing that memory exists.** If you don't know you had an experience, you won't think to search for it, and the memory sits in the database untriggered forever.

This project uses Claude Code's `UserPromptSubmit` hook to automatically run semantic search + LLM reranking on **every message**, pushing relevant memories into context. No manual search needed, no human prompt of "go look that up."

## v3 vs v1

v1 used cosine similarity + hand-tuned thresholds. High noise, low hit rate.

v3 adds four core improvements:

| Improvement | Problem | Solution |
|-------------|---------|----------|
| **Three-tier Intent Gate** | "ok" / "yeah" shouldn't trigger search | SKIP / TRIGGER / LIGHT routing saves API calls |
| **LLM Reranker** | High cosine ≠ actually relevant | LLM judges "would the answer be worse without this memory?" |
| **Conversation context** | "that hurts" might be teasing, not real pain | Extract last 3 turns from transcript, feed to reranker for tone awareness |
| **Event-first search** | Too many fragments, low info density | Support event type (aggregated overviews), prioritize over fragments |

## Architecture

```
User sends message
  ↓
UserPromptSubmit hook fires
  ↓
1. Intent Gate: SKIP / TRIGGER / LIGHT
  ↓
2. Dual search: event + general (parallel)
  ↓
3. Hard filter: dedup(transcript) + archived + deep + resolved
  ↓
4. [LIGHT] Cosine gate: best score < 0.65 → skip reranker
  ↓
5. LLM Reranker: score with conversation context (0/1/2)
  ↓
6. Inject: max 1 event + 1 fragment → context
```

## Transcript-based dedup

This is the core design choice. The hook reads Claude Code's session transcript directly to determine what's already in context — no separate state file. The transcript is ground truth: present means pushed, absent means not. Message rewinds automatically sync. No state file to maintain, clean up, or get stale.

## Setup

### Prerequisites

- Claude Code installed locally
- A memory MCP server with semantic search ([Ombre Brain](https://github.com/P0luz/Ombre-Brain), custom, or the included reference implementation)
- (Optional) LLM API key for reranker (without it, falls back to v1 cosine threshold mode)

### 1. Install the hook

```bash
git clone https://github.com/zziying/claude-code-memory-surface.git
cp claude-code-memory-surface/hook/memory_surface.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/memory_surface.py
```

### 2. Configure Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMORY_MCP_URL='your-url' RERANKER_API_KEY='your-key' python3 ~/.claude/hooks/memory_surface.py"
          }
        ]
      }
    ]
  }
}
```

### 3. Test

```bash
echo '{"prompt":"do you remember what we talked about last time"}' \
  | MEMORY_MCP_URL='your-url' RERANKER_API_KEY='your-key' \
  python3 ~/.claude/hooks/memory_surface.py
```

## Configuration

All parameters via environment variables. See `.env.example` for the full list.

### Core

| Parameter | Purpose | Default |
|-----------|---------|---------|
| `MEMORY_MCP_URL` | Memory MCP server URL | (required) |
| `RERANKER_API_KEY` | LLM reranker API key | (empty = cosine fallback) |
| `RERANKER_API_URL` | OpenAI-compatible API endpoint | `https://api.deepseek.com/chat/completions` |
| `RERANKER_MODEL` | Reranker model | `deepseek-chat` |
| `USER_NAME` / `AI_NAME` | Role names in reranker prompt | `user` / `assistant` |

### Tuning

| Parameter | Purpose | Default |
|-----------|---------|---------|
| `LIGHT_COS_GATE` | Cosine threshold to skip reranker in LIGHT mode | `0.65` |
| `RECALL_EXCLUDE` | Compound words excluded from recall keyword matching | `记忆库,记忆浮现,记忆系统` |

## Using different memory backends

The hook requires your MCP server to expose a search tool (default name: `semantic_search`):

**Request:**
```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {"query": "...", "limit": 5, "no_track": true}
  }
}
```

**Response:**
```json
{
  "result": {
    "content": [{"type": "text", "text": "[{\"chunk_text\": \"...\", \"parent_memory_id\": \"...\", \"chunk_index\": 0, \"score\": 0.74, \"category\": \"fragment\"}]"}]
  }
}
```

Set `SEARCH_TOOL_NAME` if your tool has a different name.

## Using different LLMs as reranker

Any OpenAI-compatible API works:

- **DeepSeek** (recommended, cheap): default config
- **OpenAI**: `RERANKER_API_URL=https://api.openai.com/v1/chat/completions` + `RERANKER_MODEL=gpt-4o-mini`
- **Local** (Ollama etc.): `RERANKER_API_URL=http://localhost:11434/v1/chat/completions`

Without `RERANKER_API_KEY`, the hook falls back to v1 behavior (cosine ≥ 0.7 → push).

## Intent Gate

Three tiers to avoid calling the LLM on every message:

- **SKIP**: empty, noise words ("ok", "yeah", "haha"), code blocks, system messages → no search
- **TRIGGER**: recall keywords ("remember", "last time", "before", etc.) → full search + rerank
- **LIGHT**: everything else → light search, only call reranker if cosine gate passes

The LIGHT cosine gate (default 0.65) balances "don't waste reranker calls" vs "don't miss relevant memories." Lower it if reranker cost isn't a concern.

## Debug

Logs go to `~/.claude/hooks/surface_debug.log` — Intent Gate results, search counts, filter decisions, reranker scores. Check here first when surfacing quality drops.

## Real-world metrics

Data from daily usage debug logs (Chinese conversation, ~500 fragments in memory):

**Intent Gate routing:**

| Tier | % | Notes |
|------|---|-------|
| SKIP | ~12% | Noise, code, system messages — zero latency |
| LIGHT | ~79% | Normal messages, light search |
| TRIGGER | ~8% | Recall keywords detected, full search |

**Reranker filtering:**

Score distribution across all candidates evaluated by the reranker:

| Score | % | Meaning |
|-------|---|---------|
| 0 (irrelevant) | ~82% | Filtered out |
| 1 (tangential) | ~9% | Filtered out |
| 2 (directly useful) | ~9% | Injected into context |

The reranker filters out ~90% of candidates. Injected memories are generally contextually relevant.

**Latency:**

| Path | % of messages | Latency |
|------|---------------|---------|
| SKIP (Intent Gate) | ~12% | ~0ms |
| Cosine gate (search only) | ~18% | ~1-2s |
| Full pipeline (with reranker) | ~62% | P50 3.6s / P90 5.1s |

Latency is dominated by the LLM reranker API call. Search alone takes 1-2s. Without a reranker, response is sub-second (v1 behavior).

**Injection frequency:**

Average 0.24 memories per message — most messages inject nothing. Memories only surface when genuinely relevant.

## Known limits

- **Reranker adds 2-4s latency** per TRIGGER/LIGHT message. Remove `RERANKER_API_KEY` to fall back to v1 if unacceptable.
- **Event support depends on backend**: if your memory backend has no event type, event search returns empty — hook works fine, just without event-first prioritization.

## Similar projects

- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) — Full memory MCP server with hold/grow/breath/dream
- [claude-mem](https://github.com/thedotmack/claude-mem) — UserPromptSubmit + ChromaDB
- [ClawMem](https://github.com/yoloshii/ClawMem) — BM25 + vector + reranking + intent classification
- [claude-hooks](https://github.com/mann1x/claude-hooks) — UserPromptSubmit + Qdrant + attention decay

## License

MIT
