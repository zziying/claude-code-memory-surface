#!/usr/bin/env python3
"""Backfill chunks + embeddings for existing memories."""
import sqlite3, json, os, time, urllib.request, re
import numpy as np

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    # fallback: read from .env in repo root
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("GEMINI_API_KEY="):
                    GEMINI_API_KEY = line.split("=", 1)[1].strip()
                    break

EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
EMBED_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent?key={GEMINI_API_KEY}"
DB_PATH = os.environ.get("MEMORY_DB_PATH", "./memories.db")
MAX_CHUNK_CHARS = 600
MIN_CHUNK_CHARS = 80


def calc_embedding(text):
    if not text.strip():
        return None
    try:
        body = json.dumps({"model": f"models/{EMBED_MODEL}", "content": {"parts": [{"text": text[:8000]}]}}).encode()
        req = urllib.request.Request(EMBED_API_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("embedding", {}).get("values")
    except Exception as e:
        print(f"  ERR: {e}", flush=True)
        return None


def chunk_memory(content):
    content = content.strip()
    if not content: return []
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
            final.append(c); continue
        lines = c.split('\n'); buf = ''
        for line in lines:
            if len(buf) + len(line) + 1 > MAX_CHUNK_CHARS and buf:
                final.append(buf.strip()); buf = line
            else:
                buf = (buf + '\n' + line) if buf else line
        if buf.strip(): final.append(buf.strip())
    merged = []
    for c in final:
        if merged and len(c) < MIN_CHUNK_CHARS:
            merged[-1] = merged[-1] + '\n' + c
        else:
            merged.append(c)
    return list(enumerate(merged))


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.id, m.category, m.content, LENGTH(m.content) as clen
        FROM memories m
        WHERE NOT EXISTS (SELECT 1 FROM memory_chunks c WHERE c.memory_id = m.id)
        ORDER BY m.category, m.created_at
    """).fetchall()
    print(f"Memories needing backfill: {len(rows)}", flush=True)
    if not rows:
        print("Nothing to do."); return
    total_chunks = 0; total_embedded = 0; failed = 0
    for i, m in enumerate(rows):
        chunks = chunk_memory(m["content"])
        print(f"[{i+1}/{len(rows)}] {m['id']} ({m['category']}, {m['clen']} chars) -> {len(chunks)} chunks", flush=True)
        for idx, text in chunks:
            cid = f"{m['id']}_{idx}"
            emb = calc_embedding(text)
            if emb:
                emb_blob = np.asarray(emb, dtype=np.float32).tobytes(); total_embedded += 1
            else:
                emb_blob = None; failed += 1
            conn.execute("INSERT INTO memory_chunks (chunk_id, memory_id, chunk_index, chunk_text, embedding) VALUES (?, ?, ?, ?, ?)",
                (cid, m["id"], idx, text, emb_blob))
            total_chunks += 1
            time.sleep(0.05)
        conn.commit()
    print(f"\nDone: {total_chunks} chunks total, {total_embedded} embedded, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
