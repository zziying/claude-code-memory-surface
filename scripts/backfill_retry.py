#!/usr/bin/env python3
"""Retry embed for chunks with NULL embedding. Adaptive rate."""
import sqlite3, json, os, time, urllib.request
import numpy as np

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
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


def embed_once(text):
    """Returns (values_or_None, error_str_or_None)"""
    try:
        body = json.dumps({"model": "models/gemini-embedding-001", "content": {"parts": [{"text": text[:8000]}]}}).encode()
        req = urllib.request.Request(EMBED_API_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("embedding", {}).get("values"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)[:80]


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT chunk_id, chunk_text FROM memory_chunks WHERE embedding IS NULL ORDER BY chunk_id").fetchall()
    n = len(rows)
    print(f"Chunks needing embed: {n}", flush=True)
    if n == 0:
        return
    sleep_s = 1.5
    consecutive_success = 0
    consecutive_429 = 0
    success = 0
    fail = 0
    for i, r in enumerate(rows):
        emb, err = embed_once(r["chunk_text"])
        if emb:
            blob = np.asarray(emb, dtype=np.float32).tobytes()
            conn.execute("UPDATE memory_chunks SET embedding=? WHERE chunk_id=?", (blob, r["chunk_id"]))
            conn.commit()
            success += 1
            consecutive_success += 1
            consecutive_429 = 0
            # speed up after 10 consecutive successes
            if consecutive_success >= 10 and sleep_s > 0.8:
                sleep_s = max(0.8, sleep_s * 0.8)
                print(f"  -> speeding up to sleep {sleep_s:.2f}s", flush=True)
                consecutive_success = 0
            if (i + 1) % 20 == 0:
                print(f"[{i+1}/{n}] success={success} fail={fail} sleep={sleep_s:.2f}s", flush=True)
        else:
            fail += 1
            consecutive_success = 0
            if "429" in (err or ""):
                consecutive_429 += 1
                backoff = 30 * min(consecutive_429, 4)  # 30/60/90/120
                print(f"[{i+1}/{n}] 429! backoff {backoff}s, then bump base sleep", flush=True)
                time.sleep(backoff)
                sleep_s = min(6.0, sleep_s * 2)
                # retry once after backoff
                emb, err = embed_once(r["chunk_text"])
                if emb:
                    blob = np.asarray(emb, dtype=np.float32).tobytes()
                    conn.execute("UPDATE memory_chunks SET embedding=? WHERE chunk_id=?", (blob, r["chunk_id"]))
                    conn.commit()
                    success += 1; fail -= 1
                    consecutive_429 = 0
                    print(f"  -> retry succeeded", flush=True)
            else:
                print(f"[{i+1}/{n}] ERR: {err}", flush=True)
        time.sleep(sleep_s)
    print(f"\nDone: success={success} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
