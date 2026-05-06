#!/usr/bin/env python3
"""
Memory MCP Server (reference implementation for claude-code-memory-surface)

Provides chunk-level embedding + semantic_search tool that the hook script
consumes. Schema design (valence/arousal + 衰减 + chunk-level embed) is
inspired by Ombre-Brain (https://github.com/P0luz/Ombre-Brain).

Configurable via environment variables — see .env.example.
"""
import http.server, json, math, os, re, socketserver, sqlite3, urllib.request, uuid
from datetime import datetime, timedelta
import numpy as np

TOKEN = os.environ.get("MCP_TOKEN", "changeme")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_PATH = os.environ.get("MEMORY_DB_PATH", "./memories.db")
PORT = int(os.environ.get("MEMORY_MCP_PORT", "3458"))

# Decay config
DECAY_RATE = 0.95
SHORT_TERM_DAYS = 3

# Embedding config
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 3072
EMBED_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent?key={GEMINI_API_KEY}"

# Chunking config
MAX_CHUNK_CHARS = 600
MIN_CHUNK_CHARS = 80


TOOLS = [
    {
        "name": "write_memory",
        "description": "Write a new memory. Categories: deep (long-term identity/rules/preferences), daily (recent events, auto-expire), diary (daily journal entry with emotions). Emotional tags: valence (-1 to 1, negative=unpleasant, positive=pleasant), arousal (0 to 1, low=calm, high=intense). Auto chunks + embeds for semantic search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "category": {"type": "string", "enum": ["deep", "daily", "diary", "memo"]},
                "tags": {"type": "string", "default": ""},
                "source": {"type": "string", "default": "chat"},
                "valence": {"type": "number", "default": 0.0},
                "arousal": {"type": "number", "default": 0.5}
            },
            "required": ["content", "category"]
        }
    },
    {
        "name": "read_memory",
        "description": "Read memories. Filter by category and/or tags. Returns by relevance (decay weight).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["deep", "daily", "diary", "memo", "all"], "default": "all"},
                "tags": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 20},
                "no_track": {"type": "boolean", "default": False},
                "sort_by": {"type": "string", "enum": ["weight", "recent", "emotion"], "default": "weight"}
            }
        }
    },
    {
        "name": "search_memory",
        "description": "Keyword search (SQL LIKE) by literal substring in content/tags/id. Use for exact-word recall. For meaning-based recall, use semantic_search.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    {
        "name": "semantic_search",
        "description": "Semantic search by MEANING (not keyword). Embeds query, returns chunks of memories ranked by cosine similarity. Use when you remember the meaning but not exact words (e.g. '我们家狗' finds Joffy memory). Returns chunks (sub-sections of memories) with parent_memory_id; use read_memory(id) to get full memory if needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "category": {"type": "string", "enum": ["deep", "daily", "diary", "memo", "all"], "default": "all"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "update_memory",
        "description": "Update an existing memory by ID. Re-chunks + re-embeds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "string", "default": ""},
                "valence": {"type": "number"},
                "arousal": {"type": "number"}
            },
            "required": ["id", "content"]
        }
    },
    {
        "name": "delete_memory",
        "description": "Delete a memory by ID (and its chunks).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"]
        }
    },
    {
        "name": "stats",
        "description": "Get memory statistics.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "cleanup",
        "description": "Clean up old daily memories. Default 30 days.",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Delete daily memories older than this many days (default 30)", "default": 30}}
        }
    },
    {
        "name": "decay_update",
        "description": "Recalculate decay weights for all memories (v3 piecewise: short-term recency, long-term emotion).",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "briefing",
        "description": "Get a compact briefing for a new window.",
        "inputSchema": {"type": "object", "properties": {}}
    }
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, category TEXT NOT NULL,
            tags TEXT DEFAULT '', source TEXT DEFAULT 'chat',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            valence REAL DEFAULT 0.0, arousal REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0, last_accessed TEXT DEFAULT '',
            decay_weight REAL DEFAULT 1.0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON memories(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decay ON memories(decay_weight)")
    # v3: chunks table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_chunks (
            chunk_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding BLOB
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_memory ON memory_chunks(memory_id)")
    conn.commit()
    conn.close()


def calc_embedding(text):
    """Call Gemini, return list[float] or None on failure."""
    if not GEMINI_API_KEY or not text.strip():
        return None
    try:
        body = json.dumps({
            "model": f"models/{EMBED_MODEL}",
            "content": {"parts": [{"text": text[:8000]}]}
        }).encode()
        req = urllib.request.Request(EMBED_API_URL, data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        return data.get("embedding", {}).get("values")
    except Exception as e:
        print(f"[embed error] {e}", flush=True)
        return None


def cosine_similarity(a, b):
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def chunk_memory(content):
    """Split content into chunks. Returns list of (index, text)."""
    content = content.strip()
    if not content:
        return []
    # Strategy 1: split by ## headers
    if re.search(r'^## ', content, re.MULTILINE):
        parts = re.split(r'(?=^## )', content, flags=re.MULTILINE)
    # Strategy 2: split by 【...】 Chinese section headers
    elif re.search(r'【[^】]+】', content):
        parts = re.split(r'(?=【[^】]+】)', content)
    # Strategy 3: split by double newline (paragraphs)
    else:
        parts = content.split('\n\n')
    parts = [p.strip() for p in parts if p.strip()]
    # Split too-long chunks by lines
    final = []
    for c in parts:
        if len(c) <= MAX_CHUNK_CHARS:
            final.append(c)
            continue
        lines = c.split('\n')
        buf = ''
        for line in lines:
            if len(buf) + len(line) + 1 > MAX_CHUNK_CHARS and buf:
                final.append(buf.strip())
                buf = line
            else:
                buf = (buf + '\n' + line) if buf else line
        if buf.strip():
            final.append(buf.strip())
    # Merge too-short chunks into previous
    merged = []
    for c in final:
        if merged and len(c) < MIN_CHUNK_CHARS:
            merged[-1] = merged[-1] + '\n' + c
        else:
            merged.append(c)
    return list(enumerate(merged))


def store_chunks(conn, memory_id, content):
    """Delete existing chunks, re-chunk, embed, store. Returns count."""
    conn.execute("DELETE FROM memory_chunks WHERE memory_id=?", (memory_id,))
    chunks = chunk_memory(content)
    for idx, text in chunks:
        cid = f"{memory_id}_{idx}"
        emb = calc_embedding(text)
        emb_blob = np.asarray(emb, dtype=np.float32).tobytes() if emb else None
        conn.execute(
            "INSERT INTO memory_chunks (chunk_id, memory_id, chunk_index, chunk_text, embedding) VALUES (?, ?, ?, ?, ?)",
            (cid, memory_id, idx, text, emb_blob)
        )
    return len(chunks)


def calc_decay(created_at, last_accessed, arousal, access_count, category):
    """v3 piecewise: short-term recency-weighted, long-term emotion-weighted."""
    if category == "deep":
        return 1.0
    now = datetime.utcnow()
    try:
        created = datetime.fromisoformat(created_at.replace("Z", ""))
    except:
        created = now
    days_since = max(0.0, (now - created).total_seconds() / 86400)
    time_factor = DECAY_RATE ** days_since
    emotion_factor = max(0.0, min(1.0, arousal))
    access_bonus = min(access_count * 0.05, 0.3)
    if days_since <= SHORT_TERM_DAYS:
        weight = 0.7 * time_factor + 0.3 * emotion_factor + access_bonus
    else:
        weight = 0.3 * time_factor + 0.7 * emotion_factor + access_bonus
    weight = min(1.0, weight)
    if category == "diary":
        weight = max(0.3, weight)
    return max(0.01, weight)


def handle_tool(name, args):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat() + "Z"

    try:
        if name == "write_memory":
            mid = str(uuid.uuid4())[:8]
            content = args["content"]
            category = args["category"]
            tags = args.get("tags", "")
            source = args.get("source", "chat")
            valence = max(-1.0, min(1.0, float(args.get("valence", 0.0))))
            arousal = max(0.0, min(1.0, float(args.get("arousal", 0.5))))
            conn.execute(
                "INSERT INTO memories (id, content, category, tags, source, created_at, updated_at, valence, arousal, access_count, last_accessed, decay_weight) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1.0)",
                (mid, content, category, tags, source, now, now, valence, arousal, now)
            )
            n_chunks = store_chunks(conn, mid, content)
            conn.commit()
            return f"Memory saved [id={mid}, category={category}, tags={tags}, valence={valence:+.1f}, arousal={arousal:.1f}, chunks={n_chunks}]"

        elif name == "read_memory":
            category = args.get("category", "all")
            tags = args.get("tags", "")
            limit = args.get("limit", 20)
            sort_by = args.get("sort_by", "weight")
            query = "SELECT * FROM memories"
            params = []
            conditions = []
            if category and category != "all":
                conditions.append("category = ?")
                params.append(category)
            if tags:
                conditions.append("tags LIKE ?")
                params.append(f"%{tags}%")
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            if sort_by == "weight":
                query += " ORDER BY decay_weight DESC, created_at DESC"
            elif sort_by == "emotion":
                query += " ORDER BY arousal DESC, created_at DESC"
            else:
                query += " ORDER BY created_at DESC"
            query += " LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            if not rows:
                return "No memories found."
            if not args.get("no_track", False):
                for r in rows:
                    conn.execute("UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?", (now, r["id"]))
                conn.commit()
            return json.dumps([{
                "id": r["id"], "category": r["category"], "content": r["content"], "tags": r["tags"],
                "source": r["source"], "created_at": r["created_at"], "updated_at": r["updated_at"],
                "valence": r["valence"], "arousal": r["arousal"],
                "access_count": r["access_count"] + 1, "decay_weight": round(r["decay_weight"], 3)
            } for r in rows], ensure_ascii=False)

        elif name == "search_memory":
            keyword = args["query"]
            rows = conn.execute(
                "SELECT * FROM memories WHERE id = ? OR id LIKE ? OR content LIKE ? OR tags LIKE ? ORDER BY decay_weight DESC LIMIT 20",
                (keyword, f"{keyword}%", f"%{keyword}%", f"%{keyword}%")
            ).fetchall()
            if not rows:
                return f"No memories matching '{keyword}'."
            for r in rows:
                conn.execute("UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?", (now, r["id"]))
            conn.commit()
            return json.dumps([{
                "id": r["id"], "category": r["category"], "content": r["content"], "tags": r["tags"],
                "source": r["source"], "created_at": r["created_at"], "updated_at": r["updated_at"],
                "valence": r["valence"], "arousal": r["arousal"],
                "access_count": r["access_count"] + 1, "decay_weight": round(r["decay_weight"], 3)
            } for r in rows], ensure_ascii=False)

        elif name == "semantic_search":
            query = args["query"]
            limit = args.get("limit", 5)
            cat = args.get("category", "all")
            q_emb = calc_embedding(query)
            if not q_emb:
                return "Error: failed to embed query (Gemini API issue?)"
            q_vec = np.asarray(q_emb, dtype=np.float32)
            q_norm = float(np.linalg.norm(q_vec))
            if q_norm == 0:
                return "Error: zero-norm query embedding"
            # fetch all chunks with embeddings, optionally filter by parent category
            sql = """SELECT c.chunk_id, c.memory_id, c.chunk_index, c.chunk_text, c.embedding,
                            m.category, m.tags, m.created_at, m.valence, m.arousal
                     FROM memory_chunks c JOIN memories m ON c.memory_id = m.id
                     WHERE c.embedding IS NOT NULL"""
            params = []
            if cat != "all":
                sql += " AND m.category = ?"
                params.append(cat)
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                return "No embedded chunks found (run backfill?)."
            # cosine similarity
            scored = []
            for r in rows:
                emb = np.frombuffer(r["embedding"], dtype=np.float32)
                e_norm = float(np.linalg.norm(emb))
                if e_norm == 0:
                    continue
                score = float(np.dot(q_vec, emb) / (q_norm * e_norm))
                scored.append((score, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            top = scored[:limit]
            # update access tracking on parent memories
            for _, r in top:
                conn.execute("UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?", (now, r["memory_id"]))
            conn.commit()
            return json.dumps([{
                "score": round(s, 4),
                "chunk_text": r["chunk_text"],
                "parent_memory_id": r["memory_id"],
                "chunk_index": r["chunk_index"],
                "category": r["category"], "tags": r["tags"],
                "created_at": r["created_at"],
                "valence": r["valence"], "arousal": r["arousal"]
            } for s, r in top], ensure_ascii=False)

        elif name == "update_memory":
            mid = args["id"]
            content = args["content"]
            tags = args.get("tags", "")
            updates = ["content=?", "updated_at=?"]
            params = [content, now]
            if tags:
                updates.append("tags=?")
                params.append(tags)
            if "valence" in args:
                updates.append("valence=?")
                params.append(max(-1.0, min(1.0, float(args["valence"]))))
            if "arousal" in args:
                updates.append("arousal=?")
                params.append(max(0.0, min(1.0, float(args["arousal"]))))
            params.append(mid)
            conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id=?", params)
            n_chunks = store_chunks(conn, mid, content)
            conn.commit()
            return f"Memory [{mid}] updated, re-chunked into {n_chunks} chunks."

        elif name == "delete_memory":
            mid = args["id"]
            conn.execute("DELETE FROM memory_chunks WHERE memory_id=?", (mid,))
            conn.execute("DELETE FROM memories WHERE id=?", (mid,))
            conn.commit()
            return f"Memory [{mid}] deleted (and its chunks)."

        elif name == "stats":
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            total_chars = conn.execute("SELECT COALESCE(SUM(LENGTH(content)), 0) FROM memories").fetchone()[0]
            cats = conn.execute("SELECT category, COUNT(*) as cnt FROM memories GROUP BY category").fetchall()
            avg_valence = conn.execute("SELECT COALESCE(AVG(valence), 0) FROM memories").fetchone()[0]
            avg_arousal = conn.execute("SELECT COALESCE(AVG(arousal), 0) FROM memories").fetchone()[0]
            low_weight = conn.execute("SELECT COUNT(*) FROM memories WHERE decay_weight < 0.3").fetchone()[0]
            n_chunks = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
            n_embedded = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE embedding IS NOT NULL").fetchone()[0]
            cat_str = ", ".join([f"{r['category']}: {r['cnt']}" for r in cats]) if cats else "empty"
            return (f"Total memories: {total}\nTotal chars: {total_chars} (~{total_chars//2} tokens)\n"
                    f"By category: {cat_str}\n"
                    f"Emotional avg: valence={avg_valence:+.2f}, arousal={avg_arousal:.2f}\n"
                    f"Fading memories (weight<0.3): {low_weight}\n"
                    f"Chunks: {n_chunks} total, {n_embedded} embedded ({100*n_embedded/n_chunks if n_chunks else 0:.0f}%)")

        elif name == "cleanup":
            days = args.get("days", 30)
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
            cursor = conn.execute("SELECT id, content FROM memories WHERE category='daily' AND created_at < ?", (cutoff,))
            old = cursor.fetchall()
            if not old:
                return f"No daily memories older than {days} days to clean up."
            for r in old:
                conn.execute("DELETE FROM memory_chunks WHERE memory_id=?", (r["id"],))
            conn.execute("DELETE FROM memories WHERE category='daily' AND created_at < ?", (cutoff,))
            conn.commit()
            deleted_list = "\n".join([f"  - [{r['id']}] {r['content'][:60]}..." for r in old])
            return f"Cleaned up {len(old)} old daily memories (and their chunks):\n{deleted_list}"

        elif name == "decay_update":
            rows = conn.execute("SELECT id, created_at, last_accessed, arousal, access_count, category FROM memories").fetchall()
            updated = 0
            for r in rows:
                new_weight = calc_decay(r["created_at"], r["last_accessed"], r["arousal"], r["access_count"], r["category"])
                old_weight = conn.execute("SELECT decay_weight FROM memories WHERE id=?", (r["id"],)).fetchone()[0]
                if abs(new_weight - old_weight) > 0.01:
                    conn.execute("UPDATE memories SET decay_weight=? WHERE id=?", (new_weight, r["id"]))
                    updated += 1
            conn.commit()
            return f"Decay updated for {updated}/{len(rows)} memories (v3 piecewise)."

        elif name == "briefing":
            sections = []
            deep = conn.execute("""SELECT * FROM memories WHERE category='deep'
                ORDER BY CASE WHEN tags LIKE '%pin%' THEN 0 ELSE 1 END, decay_weight DESC""").fetchall()
            sections.append(f"=== DEEP ({len(deep)} items) ===")
            for r in deep:
                sections.append(f"[{r['id']}] (v={r['valence']:+.1f} a={r['arousal']:.1f}) {r['content']}")
            cutoff = (datetime.utcnow() - timedelta(days=3)).isoformat() + "Z"
            daily = conn.execute("SELECT * FROM memories WHERE category='daily' AND created_at > ? ORDER BY decay_weight DESC", (cutoff,)).fetchall()
            sections.append(f"\n=== DAILY ({len(daily)} items, last 3 days) ===")
            for r in daily:
                sections.append(f"[{r['id']}] {r['tags']} | {r['content'][:150]}{'...' if len(r['content'])>150 else ''}")
            memos = conn.execute("SELECT * FROM memories WHERE category='memo' ORDER BY created_at DESC LIMIT 4").fetchall()
            sections.append(f"\n=== MEMOS FROM USER ({len(memos)} items) ===")
            for r in memos:
                sections.append(f"[{r['id']}] {r['created_at'][:10]} | {r['content']}")
            if not memos:
                sections.append("(no memos)")
            milestone_diary = conn.execute("SELECT * FROM memories WHERE category='diary' AND tags LIKE '%milestone%' ORDER BY created_at ASC").fetchall()
            recent_diary = conn.execute("SELECT * FROM memories WHERE category='diary' AND (tags IS NULL OR tags NOT LIKE '%milestone%') ORDER BY created_at DESC LIMIT 2").fetchall()
            sections.append(f"\n=== MILESTONE DIARIES ({len(milestone_diary)} items, full) ===")
            for r in milestone_diary:
                sections.append(f"[{r['id']}] 🔋{r['decay_weight']*100:.0f}% {r['created_at'][:10]} | {r['content']}")
            if recent_diary:
                sections.append(f"\n=== RECENT DIARIES ({len(recent_diary)} items, truncated) ===")
                for r in recent_diary:
                    sections.append(f"[{r['id']}] 🔋{r['decay_weight']*100:.0f}% {r['created_at'][:10]} | {r['content'][:200]}{'...' if len(r['content'])>200 else ''}")
            return "\n".join(sections)
        return "Unknown tool: " + name
    finally:
        conn.close()


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if not self.path.startswith("/" + TOKEN):
            self.send_response(401); self.end_headers(); return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except:
            self.send_response(400); self.end_headers(); return
        method = body.get("method")
        rid = body.get("id")
        if method == "initialize":
            res = {"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"memory-mcp","version":"3.0"}}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            name = body["params"]["name"]
            args = body["params"].get("arguments", {})
            try: text = handle_tool(name, args)
            except Exception as e: text = f"Error: {e}"
            res = {"content": [{"type": "text", "text": text}]}
        elif method == "ping":
            res = {}
        elif method == "notifications/initialized":
            self.send_response(204); self.end_headers(); return
        else:
            self.send_response(404); self.end_headers(); return
        resp = json.dumps({"jsonrpc":"2.0","id":rid,"result":res})
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(resp)))
        self.end_headers()
        self.wfile.write(resp.encode())
    def log_message(self, *a): pass


class TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    init_db()
    print(f"Memory MCP running on :{PORT} (token: {TOKEN[:8]}..., embed: {'ON' if GEMINI_API_KEY else 'OFF'})")
    TS(("", PORT), H).serve_forever()
