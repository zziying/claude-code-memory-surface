#!/usr/bin/env python3
"""
Memory MCP Server — 最小参考实现。

提供 hook 所需的 semantic_search 接口：写入时自动切片 + embedding，
查询时余弦相似度排序返回。

这是一个起点，不是成品。记忆的分类方式、过期策略、额外字段
都应该按你自己的需求来改。
"""
import http.server, json, os, re, socketserver, sqlite3, urllib.request, uuid
from datetime import datetime
import numpy as np

TOKEN = os.environ.get("MCP_TOKEN", "changeme")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_PATH = os.environ.get("MEMORY_DB_PATH", "./memories.db")
PORT = int(os.environ.get("MEMORY_MCP_PORT", "3458"))

EMBED_MODEL = "gemini-embedding-001"
EMBED_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent?key={GEMINI_API_KEY}"

MAX_CHUNK_CHARS = 600
MIN_CHUNK_CHARS = 80


TOOLS = [
    {
        "name": "write_memory",
        "description": "Write a new memory. Auto-chunks content and generates embeddings for semantic search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "tags": {"type": "string", "default": ""}
            },
            "required": ["content"]
        }
    },
    {
        "name": "read_memory",
        "description": "Read memories, optionally filtered by tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tags": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 20}
            }
        }
    },
    {
        "name": "search_memory",
        "description": "Keyword search (SQL LIKE) in content and tags. For meaning-based recall, use semantic_search.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    {
        "name": "semantic_search",
        "description": "Search by meaning via embedding cosine similarity. Returns memory chunks ranked by relevance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "update_memory",
        "description": "Update a memory by ID. Re-chunks and re-embeds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "string", "default": ""}
            },
            "required": ["id", "content"]
        }
    },
    {
        "name": "delete_memory",
        "description": "Delete a memory by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"]
        }
    }
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at)")
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


def chunk_memory(content):
    content = content.strip()
    if not content:
        return []
    if re.search(r'^## ', content, re.MULTILINE):
        parts = re.split(r'(?=^## )', content, flags=re.MULTILINE)
    elif re.search(r'【[^】]+】', content):
        parts = re.split(r'(?=【[^】]+】)', content)
    else:
        parts = content.split('\n\n')
    parts = [p.strip() for p in parts if p.strip()]
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
    merged = []
    for c in final:
        if merged and len(c) < MIN_CHUNK_CHARS:
            merged[-1] = merged[-1] + '\n' + c
        else:
            merged.append(c)
    return list(enumerate(merged))


def store_chunks(conn, memory_id, content):
    conn.execute("DELETE FROM memory_chunks WHERE memory_id=?", (memory_id,))
    chunks = chunk_memory(content)
    for idx, text in chunks:
        cid = f"{memory_id}_{idx}"
        emb = calc_embedding(text)
        emb_blob = np.asarray(emb, dtype=np.float32).tobytes() if emb else None
        conn.execute(
            "INSERT INTO memory_chunks (chunk_id, memory_id, chunk_index, chunk_text, embedding) VALUES (?, ?, ?, ?, ?)",
            (cid, memory_id, idx, text, emb_blob))
    return len(chunks)


def handle_tool(name, args):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat() + "Z"

    try:
        if name == "write_memory":
            mid = str(uuid.uuid4())[:8]
            content = args["content"]
            tags = args.get("tags", "")
            conn.execute(
                "INSERT INTO memories (id, content, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (mid, content, tags, now, now))
            n_chunks = store_chunks(conn, mid, content)
            conn.commit()
            return f"Memory saved [id={mid}, tags={tags}, chunks={n_chunks}]"

        elif name == "read_memory":
            tags = args.get("tags", "")
            limit = args.get("limit", 20)
            if tags:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE tags LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{tags}%", limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?",
                    (limit,)).fetchall()
            if not rows:
                return "No memories found."
            return json.dumps([{
                "id": r["id"], "content": r["content"], "tags": r["tags"],
                "created_at": r["created_at"]
            } for r in rows], ensure_ascii=False)

        elif name == "search_memory":
            keyword = args["query"]
            rows = conn.execute(
                "SELECT * FROM memories WHERE id = ? OR content LIKE ? OR tags LIKE ? ORDER BY created_at DESC LIMIT 20",
                (keyword, f"%{keyword}%", f"%{keyword}%")).fetchall()
            if not rows:
                return f"No memories matching '{keyword}'."
            return json.dumps([{
                "id": r["id"], "content": r["content"], "tags": r["tags"],
                "created_at": r["created_at"]
            } for r in rows], ensure_ascii=False)

        elif name == "semantic_search":
            query = args["query"]
            limit = args.get("limit", 5)
            q_emb = calc_embedding(query)
            if not q_emb:
                return "Error: failed to embed query"
            q_vec = np.asarray(q_emb, dtype=np.float32)
            q_norm = float(np.linalg.norm(q_vec))
            if q_norm == 0:
                return "Error: zero-norm query embedding"
            rows = conn.execute("""
                SELECT c.chunk_id, c.memory_id, c.chunk_index, c.chunk_text, c.embedding, m.tags
                FROM memory_chunks c JOIN memories m ON c.memory_id = m.id
                WHERE c.embedding IS NOT NULL""").fetchall()
            if not rows:
                return "No embedded chunks found."
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
            return json.dumps([{
                "score": round(s, 4),
                "chunk_text": r["chunk_text"],
                "parent_memory_id": r["memory_id"],
                "chunk_index": r["chunk_index"],
                "tags": r["tags"]
            } for s, r in top], ensure_ascii=False)

        elif name == "update_memory":
            mid = args["id"]
            content = args["content"]
            tags = args.get("tags", "")
            conn.execute("UPDATE memories SET content=?, tags=?, updated_at=? WHERE id=?",
                         (content, tags, now, mid))
            n_chunks = store_chunks(conn, mid, content)
            conn.commit()
            return f"Memory [{mid}] updated, {n_chunks} chunks."

        elif name == "delete_memory":
            mid = args["id"]
            conn.execute("DELETE FROM memory_chunks WHERE memory_id=?", (mid,))
            conn.execute("DELETE FROM memories WHERE id=?", (mid,))
            conn.commit()
            return f"Memory [{mid}] deleted."

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
            res = {"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"memory-mcp","version":"1.0"}}
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
