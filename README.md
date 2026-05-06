# claude-code-memory-surface

> 让记忆主动找到你，而不是等你想起来去找它。

中文 | [English](README_en.md)

## 解决什么问题

Claude Code 的记忆类 MCP server 都有同一个前提：**模型得自己决定什么时候去搜记忆**。如果它没意识到该搜，相关的上下文就一直埋着。

已经有不少项目（[claude-mem](https://github.com/thedotmack/claude-mem)、[ClawMem](https://github.com/yoloshii/ClawMem) 等）通过 `UserPromptSubmit` hook 解决了这个问题——在每条消息发出时自动做一次语义搜索，把相关记忆注入到上下文里。

本项目也是这个思路，但在去重机制上做了不同的选择：**直接读 Claude Code 的 session transcript 来判断哪些内容已经推过**，不维护单独的状态文件。

```
你发消息
  ↓
UserPromptSubmit hook 触发（模型还没看到你的消息）
  ↓
hook 对你的消息做 embedding → 在记忆库里语义搜索
  ↓
最相关的片段（经过打分和去重）注入上下文
  ↓
模型同时读到你的消息和浮现的记忆
```

## 基于 transcript 的去重

这是本项目和同类方案的主要区别。

维护一个 `pushed_chunks.json` 之类的状态文件做去重，有两个容易出问题的地方：

1. **消息回退**：用户撤回了一条触发推送的消息，状态文件里还记着"这个片段推过了"，但它实际上已经不在上下文里了。下次相关消息来了 hook 会跳过——用户感觉记忆消失了。
2. **超时清理**：如果状态文件按时间清理，但 Claude Code 的 session 可以持续好几天，在 session 中间清理会导致片段重复推送。

本项目的做法是直接读 transcript 文件。Transcript 就是上下文的真实状态：在里面的就是推过的，不在的就是没推过的，包括那些被撤回的。不需要额外的状态文件，不需要清理，不会失效。

hook 用两个正则来识别已推送的内容：
- `\[(\w{6,}_\d+)\]`：匹配 hook 自己推过的片段（格式 `[memory-id_chunk-index]`）
- `\[([a-f0-9]{8})\]`：匹配其他工具推过的完整记忆引用（如果你的记忆系统有类似 briefing 的功能，可以避免重复推送；没有的话这条规则不会匹配到任何东西，不影响使用）

## 定位

**这是一个起点，不是一个成品。**

每个人的记忆系统都不一样。hook 里的触发关键词、相似度阈值、消息长度门槛都是针对一个人的对话习惯调出来的。附带的 memory MCP server 是一个最小化的参考实现——只有写入、读取、搜索这几个基本功能，方便你跑起来验证 hook 的效果。实际使用中你大概率会换成自己的记忆后端。

把它当作一个可以 fork 来改的模板就好。

## 仓库结构

```
claude-code-memory-surface/
├── hook/
│   └── memory_surface.py         ← 核心：UserPromptSubmit hook 脚本
├── reference/
│   └── server.py                 ← 最小化的 memory MCP server（参考实现）
├── scripts/
│   ├── backfill_chunks.py        ← 给已有记忆补切片和 embedding（一次性）
│   └── backfill_retry.py         ← 重试失败的 embedding（应对限频）
├── systemd/
│   └── memory-mcp.service.example
├── .env.example
├── .gitignore
└── LICENSE                       ← MIT
```

## 安装

### 前提

- 你已经有一个支持 `semantic_search` 的记忆 MCP server（自己搭的或者用 Ombre-Brain 等现成方案都行）
- 本地装好 Claude Code

> 如果还没有记忆后端，仓库里附了一个最小化的参考实现（`reference/server.py`），需要 Python 3.10+ / numpy / Gemini API key，具体配置见 `.env.example`。

### 1. 安装 hook

```bash
git clone https://github.com/Qizhan7/claude-code-memory-surface.git
cp claude-code-memory-surface/hook/memory_surface.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/memory_surface.py
```

### 2. 配置 hook

在 `~/.claude/settings.json` 里加上（把 URL 换成你自己的记忆后端地址）：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMORY_MCP_URL=<你的记忆后端URL> python3 ~/.claude/hooks/memory_surface.py"
          }
        ]
      }
    ]
  }
}
```

### 3. 测试

```bash
echo '{"prompt":"某个应该能匹配到记忆的句子"}' \
  | MEMORY_MCP_URL=<你的记忆后端URL> python3 ~/.claude/hooks/memory_surface.py
```

正常的话会输出 `[memory-surface] auto-surfaced relevant chunks:` 和匹配到的片段。

## 参数调整

hook 脚本里有几个参数你大概率想改：

| 参数 | 作用 | 怎么调 |
|---|---|---|
| `KEYWORDS` | 触发关键词——消息里含这些词就无视长度直接搜 | 加上你平时提到过去的事时常用的表达。默认偏中文。 |
| `SCORE_THRESHOLD` | 推送片段的最低相似度 | 越高越严格。默认 0.7；模糊查询多降到 0.65，要求精准调到 0.75。 |
| `MIN_LEN_TRIGGER` | 消息短于这个长度就跳过（除非命中关键词） | 默认 6，过滤掉"嗯""好"这些。英文为主的话提到 15。 |
| `MAX_CHUNKS` | 单条消息最多推几个片段 | 默认 2。片段多的话可以开到 3-4。 |

## 接入自己的记忆后端

hook 唯一的要求是你的 MCP server 暴露一个 `semantic_search` 工具，接口格式如下：

**请求：**
```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {"query": "...", "limit": 5}
  }
}
```

**返回（`text` 字段是 JSON 编码的片段列表）：**
```json
{
  "result": {
    "content": [{"type": "text", "text": "[{\"chunk_text\": \"...\", \"parent_memory_id\": \"...\", \"chunk_index\": 0, \"score\": 0.74}]"}]
  }
}
```

只要你的记忆后端能包成这个接口，hook 就能直接用。附带的 `reference/server.py` 就是按这个接口写的最小实现，可以作为适配参考。

## 已知限制

- **Gemini 免费额度限频**比较严，实测 `gemini-embedding-001` 稳定在大约 75 次/分钟。短时间写很多大段记忆会撞 429，`backfill_retry.py` 有自适应退避处理。
- **换 embedding 模型要全部重新生成**：片段表里存的是原始向量，没有标注用的是哪个模型。

## 同类项目

- [claude-mem](https://github.com/thedotmack/claude-mem) — UserPromptSubmit + ChromaDB，思路最接近
- [ClawMem](https://github.com/yoloshii/ClawMem) — 功能最丰富，BM25 + 向量 + 重排序 + 意图分类
- [claude-hooks](https://github.com/mann1x/claude-hooks) — UserPromptSubmit + Qdrant + 注意力衰减
- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) — SessionStart hook 推送 + 完整的记忆 MCP server

## 许可

MIT
