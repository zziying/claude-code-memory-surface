# claude-code-memory-surface

> 利用Claude Code的`UserPromptSubmit` hook + transcript-based dedup，让相关记忆**在你每条消息发出时主动浮现**到模型上下文里。

[English](README.md) | 中文

## 这是什么

跟Claude Code对话时，模型只能"记住"当前上下文窗口里的内容。Memory MCP server（比如[Ombre-Brain](https://github.com/P0luz/Ombre-Brain)）解决了一部分问题——把记忆持久化存储起来，让模型通过tool call检索。但还有个gap：

**模型必须主动想起来要去检索**。如果它没意识到"我应该搜一下记忆库"，相关context就还是被埋着。

本项目要堵这个gap：写一个Claude Code的`UserPromptSubmit` hook，**每次你发消息时**自动跑一次语义搜索，把最相关的几个chunks注入到模型的context里——**在模型开始回应你之前**。不需要model主动call任何tool，相关记忆就已经"在那里"了。

```
你发消息
  ↓
UserPromptSubmit hook触发（在Claude看到你消息之前）
  ↓
hook把你的消息embed → 在记忆库里跑语义搜索
  ↓
top相关chunks（经过score+dedup筛选）注入context
  ↓
Claude同时读到你的消息+被浮现的记忆
```

## Novel的部分

绝大多数memory MCP（Ombre-Brain、mem0、letta等）都把记忆检索的责任交给**模型**。本项目把这个责任交给**平台**。

| 机制 | 实现位置 | 触发时机 |
|---|---|---|
| Tool-based检索（绝大多数） | MCP tool | 模型自己决定 |
| `SessionStart` hook（Ombre-Brain） | Claude Code hook | 每个session开始时一次 |
| **`UserPromptSubmit` hook（本项目）** | Claude Code hook | **每条用户消息** |

另一个novel的点是**transcript-based dedup**。不维护单独的state file（rewind后会失效），而是直接读Claude Code session的transcript文件，从中parse出已经push过的chunk_id。这样：

- **rewind自动适配**：rewind掉一条消息，hook会自动"忘记"曾push过那些chunks（因为transcript里没了），下次相关query又会重新push。
- **不需要state file**：没东西要清理，没东西会损坏。
- **briefing-aware**：hook也识别full memory引用（比如briefing tool输出的`[5b4e983f]`），那个memory里的所有chunks都会被skip。

## 状态：抛砖引玉

**本hook的tunables——`KEYWORDS`、`SCORE_THRESHOLD`、`MIN_LEN_TRIGGER`——是针对一个具体使用者的对话风格调出来的。你的会不一样。**

reference memory MCP server同理：它的schema设计（deep/daily/diary/memo分层、valence/arousal情感坐标、分段衰减曲线）是一种特定的取舍。你跟Claude Code的关系、你聊的话题、你的对话节奏——都不一样。

本repo的定位是**一个working example供你fork和定制**。README里架构讲得够清楚，可以让你自己的Claude Code帮你改。**别期待装上就完美工作**——把它当起点。

## 仓库结构

```
claude-code-memory-surface/
├── hook/
│   └── memory_surface.py         ← novel contribution: UserPromptSubmit hook
├── reference/
│   └── server.py                 ← reference memory MCP server (schema参考Ombre-Brain)
├── scripts/
│   ├── backfill_chunks.py        ← 给现有memory切chunk+embed（一次性）
│   └── backfill_retry.py         ← 重试NULL embedding（rate limit recovery）
├── systemd/
│   └── memory-mcp.service.example
├── .env.example
├── .gitignore
├── LICENSE                       ← MIT
└── README.md / README_zh.md
```

## 安装

### 前置条件

- 一台Linux server跑memory MCP（1GB RAM足够）
- Python 3.10+，需要`numpy`
- Gemini API key（免费tier够用，<https://aistudio.google.com/apikey>）
- Claude Code已安装

### 1. 部署reference memory MCP server（如果你已经有自己的可跳过）

```bash
git clone https://github.com/<your-username>/claude-code-memory-surface.git
cd claude-code-memory-surface
cp .env.example .env
# 编辑.env —— 设置MCP_TOKEN（任意32字符hex）和GEMINI_API_KEY
python3 reference/server.py    # 跑在3458端口
```

要长期运行的话，把`systemd/memory-mcp.service.example`复制到`/etc/systemd/system/memory-mcp.service`，改路径，然后`systemctl enable --now memory-mcp`。

如果想HTTPS访问，前面挂nginx反向代理3458端口。

### 2. 把MCP server加到Claude Code

编辑`~/.claude.json`（或用`claude mcp add`）：

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

### 3. 安装hook

```bash
mkdir -p ~/.claude/hooks
cp hook/memory_surface.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/memory_surface.py
```

编辑`~/.claude/settings.json`：

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

### 4. 单独test hook

```bash
echo '{"prompt":"<某个应该能match到记忆的query>"}' \
  | MEMORY_MCP_URL=http://localhost:3458/<your_MCP_TOKEN> python3 ~/.claude/hooks/memory_surface.py
```

应该看到`[memory-surface] auto-surfaced relevant chunks:`+chunks内容。如果什么都没有，试试更长的query或者包含`KEYWORDS`里某个词。

### 5. （可选）给现有memory backfill embedding

如果你的数据库里已经有memory但没embedding：

```bash
MEMORY_DB_PATH=./memories.db GEMINI_API_KEY=<your_key> python3 scripts/backfill_chunks.py
```

中途撞Gemini rate limit的话用retry script：

```bash
MEMORY_DB_PATH=./memories.db GEMINI_API_KEY=<your_key> python3 scripts/backfill_retry.py
```

## 怎么定制成你自己的风格

下面这些八成是你想改的。

### Hook script (`hook/memory_surface.py`)

| 参数 | 控制什么 | 怎么调 |
|---|---|---|
| `KEYWORDS` | "我在query记忆"的信号词，含这些词的message无视长度直接trigger | 加上你引用过往时常用的词。默认偏中文+几个英文。 |
| `SCORE_THRESHOLD` | push一个chunk所需的最小相似度 | 越高越严（噪音少但miss多）。我用0.7；模糊query多用0.65；要求精准用0.75。 |
| `MIN_LEN_TRIGGER` | 短于这个就skip（除非keyword命中） | 6字符过滤"嗯"/"ok"/"哈哈"；以英文为主提到15。 |
| `MAX_CHUNKS` | 单次message最多push几个chunks | 2比较保守；3-4适合chunk很多的场景。 |

### Memory schema (`reference/server.py`)

| 决定 | 默认 | 替代选项 |
|---|---|---|
| 分类 | `deep` / `daily` / `diary` / `memo` | 也可以只`notes`/`facts`，或按主题`work`/`personal`/`tech`。 |
| 衰减曲线 | 分段：短期看时间、长期看情感 | 线性 / 指数 / 不衰减。 |
| Chunking策略 | `## headers` → `【】 sections` → 段落（最大600字） | 按你实际的markdown风格调。 |
| Embedding模型 | `gemini-embedding-001`（3072维，free tier） | 任何兼容OpenAI embeddings endpoint的model。 |

重点是：**别keep我的决定如果不适合你**。Fork、改、重新部署。

## 兼容性——用你自己的memory backend

hook只要求你的MCP server暴露一个`semantic_search` tool，签名是：

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

**Response（`text`字段是JSON-encoded chunk list）:**
```json
{
  "result": {
    "content": [{"type": "text", "text": "[{\"chunk_text\": \"...\", \"parent_memory_id\": \"...\", \"chunk_index\": 0, \"score\": 0.74, \"category\": \"...\"}]"}]
  }
}
```

如果你已经有支持chunk-level embedding的memory MCP server（比如可以适配Ombre-Brain），把它的检索包成这个签名的tool，hook就能用。

## 架构细节

### 为什么per-message hook比tool-based检索好

如果retrieval是个model要call的tool，model需要**决定**什么时候call它——这个决定不可靠。模型默认基于context里现有的内容工作。如果user提"Joffy"但context里没"Joffy"，model很可能含糊回答而不去搜memory。

Hook完全bypass了这个decision。等model看到你的message时，相关chunks已经在context里了，model就当作普通context读。

### 为什么transcript-based dedup比state file好

朴素的dedup会维护一个`pushed_chunks.json`文件。两个failure mode：

1. **Rewind**：用户rewind了一条触发push的message。state file还记着"这chunk push过"但实际它已不在context里。下次相关message hook会skip → user感觉没recall。
2. **TTL重置**：state file某段时间无活动后wipe。但Claude Code session能持续3-4天，session中途wipe会重复push → 噪音。

直接读transcript文件解决了两个问题。Transcript就是model context的真实source of truth。在里面的就是"已push"，不在的就是"未push"——包括那些被rewind掉的。

Hook用两个regex pattern：
- `\[(\w{6,}_\d+)\]` 匹配hook自己push的chunks（格式`[memory-id_chunk-index]`）
- `\[([a-f0-9]{8})\]` 匹配briefing-style tool输出的full memory ID（格式`[memory-id]`）

匹配第二个pattern的视为"整个memory已在context里"——它的所有chunks都skip。

### Quality gates

Embedding搜索返回的是"相似"的chunks，不一定"相关"。常见phrase比如"最近怎么样"会拉出所有"最近状态"主题的内容，跟原query subject无关。过滤策略：

- **Score threshold (0.7)**：弱匹配（典型噪音false positive）落在0.6-0.7区间。0.7干净分割它们和真正相关的（0.7+）。
- **Length gating**：`"嗯"`、`"ok"`这些在API call之前就过滤掉（省rate limit + 延迟）。
- **Keyword whitelist**：明确表达"我在query历史"的词（"还记得"、"上次"、"remember"）bypass长度门槛。

## 已知limit

- **Gemini free-tier rate limit**对`gemini-embedding-001`比较严，实测稳定~75 RPM。短时间高并发（一次写很多大memory）会撞429。`backfill_retry.py`有adaptive backoff处理这个。
- **暂不支持多模态记忆**：图片/语音/视频embed还没做。
- **没有跨memory的link**：chunks表没记录memory之间的显式关系。可以作为后续feature加。
- **更换embedding模型是destructive operation**：改`EMBED_MODEL`需要全部re-embed（chunks表存的是raw float32 BLOB，没有model tag）。

## 前人工作 / 致谢

- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) by P0luz —— schema设计（valence/arousal标签、分段衰减、chunk-level embedding）直接受这个项目启发。Ombre-Brain实现了`SessionStart`-hook based的surfacing机制，本身就是个很棒的Memory MCP server。`claude-code-memory-surface`的novel方向是per-message `UserPromptSubmit` hook + transcript-based dedup，**不是memory schema本身**。
- [mem0](https://github.com/mem0ai/mem0) —— provider-agnostic memory layer。abstraction不一样（更像structured fact extractor + vector backend）。
- [letta (memgpt)](https://github.com/letta-ai/letta) —— 完整的agent framework with tiered memory。scope不同。

## License

MIT.
