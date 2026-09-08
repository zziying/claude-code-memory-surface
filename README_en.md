# claude-code-memory-surface v4

> Let memories find you, instead of waiting until you remember to look.

[中文](README.md) | English

## The problem

External memory systems ([Ombre Brain](https://github.com/P0luz/Ombre-Brain), custom MCP servers, etc.) can all **store** memories, but retrieval is passive — the model has to know what to search for.

**Context compaction doesn't just delete memory content; it deletes the meta-memory of "I once had this memory."** If you don't remember having an experience, you never think to search for it, and it sits in the store untouched.

This project uses Claude Code's `UserPromptSubmit` hook to run a semantic search + LLM rerank on **every message**, and pushes relevant memories into context proactively. No model-initiated search, no human saying "go check."

## v4 vs v3

v3 answered "when to search, and is what we found actually relevant" (three-tier intent gate + LLM reranker + conversation context). v4 answers "once it is relevant: how much to inject, which sentence, and which version is newest":

| Change | Problem | Fix |
|--------|---------|-----|
| **Pointer vs full text** | A full old memory dropped into casual chat derails the topic | LIGHT injects a one-line pointer (memory id + supporting sentence); only TRIGGER (explicit recall) injects full text. Weak signal gets a clue, strong intent gets content |
| **Reranker points at a sentence** | Pointer hooks used the first 50 chars of the chunk, often metadata or an unrelated line | Each candidate body is fed with numbered sentences; the reranker returns `sent`, the index of the sentence that supports its judgement, and the pointer quotes exactly that |
| **Snapshot adjudication in code** | For "where am I at" questions, the model anchors on the first snapshot it reads; prompt discipline doesn't fix it | The reranker only labels `snap` (is this a progress snapshot of the same activity) and `date`. **Code** picks the newest when several compete. Dates are validated against record date / dates in the body / "yesterday"-style words resolved from the record date — the model can't invent one |
| **Recency bias in the candidate pool** | The newest progress fragment ranked 10th on cosine and never entered the pool, buried under older chatter on the same topic | The general lane over-fetches 12, re-sorts by `cosine + w·e^(-days/τ)`, cuts back to 8. Only affects who enters the pool; gate and rerank untouched |
| **Acknowledgement stripping** | "oh right, I remember now" was read as a recall request and got TRIGGER privileges | Acknowledgement forms are stripped before the keyword scan; question forms ("remember?") survive |
| **TRIGGER privileges** | "Do you remember X?" — X was deduped away because it already appeared in the session | TRIGGER skips transcript dedup and retries the reranker once on timeout |
| **Context extraction** | In tool-heavy sessions an 80KB tail held one or two real turns | 300KB tail, last 10 messages, text blocks only, system injections / channel shells / timestamps stripped |
| **Mute regex** | Some conversation modes shouldn't get memory popups at all | `MUTE_RE` match silences LIGHT; TRIGGER still works |
| **Regression tests** | Every prompt tweak broke something else | `tests/` runner drives the real hook over a case file (silent / inject / allow_pointer) |

## Architecture

```
User sends a message
  ↓
0. Normalize: strip <channel> shells, leading timestamps
  ↓
1. Intent Gate: SKIP / TRIGGER / LIGHT  (keyword scan after acknowledgement stripping)
  ↓  [LIGHT] MUTE_RE match → silent
2. Dual search: event + general lanes (parallel; general lane recency-biased then cut)
  ↓
3. Hard filter: archived / deep / transcript dedup (skipped for TRIGGER) / resolved / low importance
  ↓
4. [LIGHT] Cosine gate: best < 0.72 → skip reranker
  ↓
5. LLM reranker (with last 10 messages) → per candidate {score, sent, snap, date}
   → snapshot adjudication (≥2 snaps: latest date forced to 2, others 0)
   → same-tier newest-first (among 2s, event and fragment each take the newest)
  ↓
6. Inject
   TRIGGER → full text: max 1 event (briefed) + 1 fragment
   LIGHT   → pointer: ⟦déjà vu⟧ 9/4 related memory 「supporting sentence…」 [id] — search it if relevant
```

## Design invariants

Learned the hard way between v1 and v4. Don't break these while tuning:

- **Auxiliary signals only order candidates; they never justify an injection on their own.** A keyword hit only sends a candidate into the pool; recency bias only orders the pool; eligibility is always the reranker's "would the reply be noticeably worse without this?"
- **The LIGHT batch gate reads raw cosine, not the boosted score.** Everything living between 0.70 and 0.72 is marginal stuff the reranker can't save either.
- **The model labels, the code decides.** Asking the model "which is newest" anchors it on the first one it read; asking it to label each candidate's date and snapshot-ness and sorting in code works. Counter-lesson: if the prompt says "the program will decide recency", the model scores everything 0 and waits — so the prompt still demands normal scores, and the code ignores them for the snapshot group.
- **A pointer is not a surfacing.** A LIGHT pointer nobody opened shouldn't extend that memory's life; if your backend tracks access, don't bump it on the pointer path.

## Transcript-based dedup

The hook reads the Claude Code session transcript to decide which memories are already in context; no separate state file. The transcript is the source of truth and stays in sync after message rewinds. In v4 the `[8hex]` id in each pointer line lands in the transcript and doubles as the same-session dedup anchor.

## Setup

### Prerequisites

- Claude Code installed
- A memory MCP server with semantic search ([Ombre Brain](https://github.com/P0luz/Ombre-Brain), your own, or the reference implementation on the `reference-server` branch)
- (Optional) an LLM API key for the reranker. Without it the hook falls back to v1 cosine-threshold mode; pointers and adjudication need the reranker.

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
            "command": "MEMORY_MCP_URL='your-memory-backend-url' RERANKER_API_KEY='your-llm-api-key' python3 ~/.claude/hooks/memory_surface.py"
          }
        ]
      }
    ]
  }
}
```

Environment variables can also live in a `.env` file or the system environment.

### 3. Test

```bash
echo '{"prompt":"do you remember what we talked about last time"}' \
  | MEMORY_MCP_URL='your-url' RERANKER_API_KEY='your-key' \
  python3 ~/.claude/hooks/memory_surface.py
```

## Configuration

All parameters are environment variables; see `.env.example`.

### Core

| Variable | Purpose | Default |
|----------|---------|---------|
| `MEMORY_MCP_URL` | Memory MCP server URL | (required) |
| `SEARCH_TOOL_NAME` | Search tool name | `semantic_search` |
| `READ_TOOL_NAME` | (optional) tool returning one memory's full body, used for event briefs | empty = use chunk_text |
| `RERANKER_API_KEY` | LLM reranker API key | (empty = cosine-threshold fallback) |
| `RERANKER_API_URL` | OpenAI-compatible endpoint | `https://api.deepseek.com/chat/completions` |
| `RERANKER_MODEL` | Reranker model | `deepseek-v4-flash` |
| `RERANKER_DISABLE_THINKING` | Send `thinking: disabled` (reasoning models otherwise burn max_tokens) | `1` |
| `USER_NAME` / `AI_NAME` | Role names in the prompt and context labels | `user` / `assistant` |

### Injection shape (v4)

| Variable | Purpose | Default |
|----------|---------|---------|
| `LIGHT_POINTER_ONLY` | LIGHT injects pointers only; `0` restores v3 (full text in both modes) | `1` |
| `SENT_PTR_ENABLE` | Pointer hook = the reranker's supporting sentence (else first 50 chars) | `1` |
| `SENT_HOOK_MAX` | Max hook length | `60` |
| `SNAP_ADJUDICATE` | Code-side newest-snapshot adjudication | `1` |

### Tuning

| Variable | Purpose | Default |
|----------|---------|---------|
| `LIGHT_COS_GATE` | Cosine threshold below which LIGHT skips the reranker | `0.72` |
| `GEN_FETCH` / `RECENCY_W` / `RECENCY_TAU` | General-lane over-fetch / today's full bonus / e-folding days | `12` / `0.03` / `14` |
| `CONTEXT_TURNS` / `CONTEXT_TAIL_KB` | Messages fed to the reranker / transcript tail size | `10` / `300` |
| `TIMEOUT_RERANK` | Reranker timeout (TRIGGER retries once) | `9.0` |
| `MUTE_RE` | Regex that silences LIGHT when matched | empty |
| `RECALL_EXCLUDE` | Compound words excluded from keyword matching (comma-separated) | see `.env.example` |
| `RECALL_KW` (in code) | Keywords that trigger a full search | mixed zh/en |

## Using different memory backends

The only hard requirement is a search tool (default name `semantic_search`) over JSON-RPC:

**Request:**
```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {"query": "...", "limit": 8, "no_track": true, "category": "event"}
  }
}
```
`category` is only sent on the event lane; a backend without events returns an empty array and the hook carries on.

**Response:**
```json
{
  "result": {
    "content": [{"type": "text", "text": "[{\"chunk_text\": \"...\", \"parent_memory_id\": \"...\", \"chunk_index\": 0, \"score\": 0.74, \"category\": \"fragment\", \"created_at\": \"2026-09-04T10:00:00\"}]"}]
  }
}
```
`created_at` is optional but strongly recommended: recency bias, snapshot adjudication, newest-first tie-break and the date in pointers all depend on it. Other optional fields: `tags`, `importance`, `archived`, `resolved`, `parent_id`.

If `READ_TOOL_NAME` is set, the hook calls it with `{"memory_id": ..., "no_track": true}` to brief events from their full body; a JSON `content` field or plain text both work.

### Ombre Brain

Ombre Brain's `breath` tool returns a slightly different shape; map it to the format above with a small adapter or proxy.

## Using different LLMs as reranker

Any OpenAI-compatible API:

- **DeepSeek** (recommended, cheap): the defaults. The v4 series are reasoning models; keep `RERANKER_DISABLE_THINKING=1`
- **OpenAI**: `RERANKER_API_URL=https://api.openai.com/v1/chat/completions` + `RERANKER_MODEL=gpt-4o-mini` + `RERANKER_DISABLE_THINKING=0`
- **Local** (Ollama etc.): `RERANKER_API_URL=http://localhost:11434/v1/chat/completions` + `RERANKER_MODEL=your-model` + `RERANKER_DISABLE_THINKING=0`

The reranker must reliably emit a JSON array; on parse failure the hook fails soft and injects nothing.

## Intent Gate

- **SKIP**: empty, noise words ("ok", "haha"), code blocks, system messages, under 6 chars with no recall keyword → no search
- **TRIGGER**: recall keywords ("remember", "last time", "before", 之前, 上次, ...) → full search + rerank, skips the cosine gate and transcript dedup, injects full text
- **LIGHT**: everything else → light search, reranker only if the cosine gate passes, pointers only

TRIGGER is not forced recall: it removes the cosine gate, but the reranker gate stays, and with no score-2 candidate it stays silent.

Some keywords ("recently", "progress") are common in ordinary sentences and grant TRIGGER privileges by accident. If your logs show many such false triggers, consider demoting keywords to "widen the gate only" and letting the reranker decide full-vs-pointer — that's on the roadmap, not done yet.

## Regression tests

```bash
MEMORY_MCP_URL=... RERANKER_API_KEY=... python3 tests/test_memory_surface.py
python3 tests/test_memory_surface.py --only noise-ack -v
```

The runner drives the real hook as a subprocess (real backend, real reranker, 1-6s per case) and duplicates no logic. `tests/cases.example.json` is a template; positive cases must be edited to match what's in your store. Three expectations: `silent` (nothing at all), `inject` (something, with optional `must_contain` / `must_contain_any` / `must_not_contain`), and `inject` + `allow_pointer:true` (a pointer passes, full text fails).

Case discipline is in the runner docstring: no hard-coded memory ids in positive cases, real wording, keywords anchored to context not to one answer, operational questions should stay silent. Feed new false positives/negatives from the debug log back into the case file.

## Debug

Logs go to `~/.claude/hooks/surface_debug.log` (rotated at 1MB into .1/.2): gate decision, search hits, filter reasons, full reranker output, adjudication actions, final injection.

When quality drops: runs of `0ev+0gen` plus `search_srv_err` mean the backend's embedding is down; scattered `rerank_err` is the reranker's occasional bad JSON, clusters of it point at the key or the timeout; `snap_adjudicate` lines show when the code overrode the model's pick.

## Real-world metrics (v3 era, June 2026)

From daily-use debug logs (Chinese conversation, ~500 fragments). v4 routing proportions are about the same; injection shape changed (LIGHT → pointers) so injection counts are no longer comparable:

| Tier | % |
|------|---|
| SKIP | ~12% |
| LIGHT | ~79% |
| TRIGGER | ~8% |

Reranker scores: 0 ~82%, 1 ~9%, 2 ~9%. Full pipeline P50 3.6s / P90 5.1s, dominated by the reranker call.

## Known limits

- **Reranker adds 2-4s latency**. Remove `RERANKER_API_KEY` to fall back to v1 (no pointers, no adjudication then).
- **Event support depends on the backend**: without an event type the event lane returns empty; the hook works, just without event-first.
- **Snapshot adjudication depends on `created_at`**: without it, date validation can only accept dates written in the body and adjudication rarely fires.
- **The reranker prompt is English**; it works fine for other languages. Edit `RERANK_RULES` / `RERANK_OUTPUT` to change it.
- **Python 3.9 note**: the macOS system Python 3.9 misreports `Non-UTF-8 code` when running a script with very long CJK lines directly; the coding declaration on line 2 works around it — keep it.

## Roadmap

- A `redundant` assessment: things already mentioned in the recent conversation score 0 (semantic dedup on top of the mechanical transcript-id dedup)
- Event reality status (planned / not_occurred / occurred) so "said they would" and "did" separate in adjudication
- Full-vs-pointer decided by the reranker semantically, with keywords only widening the gate
- Emotional hedge: when a negative memory is injected, add a one-line pointer to a positive memory on the same topic (needs a valence field in the backend)

## Similar projects

- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) — Full memory MCP server with hold/grow/breath/dream
- [claude-mem](https://github.com/thedotmack/claude-mem) — UserPromptSubmit + ChromaDB
- [ClawMem](https://github.com/yoloshii/ClawMem) — BM25 + vector + reranking + intent classification
- [claude-hooks](https://github.com/mann1x/claude-hooks) — UserPromptSubmit + Qdrant + attention decay
- [eggshell-memory](https://github.com/dankefox/eggshell-memory) — verbatim evidence windows + structured judgement + code-side timeline adjudication; v4's `sent` / `snap` design borrows from it

## License

MIT
