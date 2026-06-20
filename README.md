# claude-code-memory-surface v3

> 让记忆主动找到你，而不是等你想起来去找它。

中文 | [English](README_en.md)

## 解决什么问题

外部记忆系统（[Ombre Brain](https://github.com/P0luz/Ombre-Brain)、自建 MCP 等）都能**存**记忆，但检索是被动的——模型必须知道该搜什么才会搜。

**问题是：context 压缩（compact）删掉的不只是记忆内容，而是"知道自己有过这段记忆"的元记忆。** 你不记得自己有过一段经历，就不会想到去搜，记忆就永远躺在库里不被触发。

本项目用 Claude Code 的 `UserPromptSubmit` hook，在**每条消息发送时**自动跑一次语义搜索 + LLM 重排序，把相关记忆主动推进上下文。不需要模型主动搜，不需要人类提醒"你去查一下"。

## v3 vs v1

v1 只做 cosine 相似度 + 手调阈值，噪音多、命中率差。

v3 做了四个核心改进：

| 改进 | 问题 | 解法 |
|------|------|------|
| **三档 Intent Gate** | "嗯""ok"这种不该触发搜索 | SKIP / TRIGGER / LIGHT 三档分流，节省 API 调用 |
| **LLM Reranker** | cosine 分高不代表真的相关 | 用 DeepSeek/OpenAI 等 LLM 判断"缺了这条记忆回答会不会变差" |
| **对话上下文** | "心痛"可能是撒娇不是真难过 | 从 transcript 提取最近 3 轮对话喂给 reranker 判断语境 |
| **Event 优先** | 碎片太多，信息密度低 | 支持 event（多条记忆聚合的概览），优先推送 |

## 架构

```
用户发消息
  ↓
UserPromptSubmit hook 触发
  ↓
1. Intent Gate: SKIP / TRIGGER / LIGHT
  ↓
2. 双路搜索: event + general (并行)
  ↓
3. 强过滤: 去重(transcript) + archived + deep + resolved
  ↓
4. [LIGHT] Cosine 闸门: 最高分 < 0.65 → 跳过 reranker
  ↓
5. LLM Reranker: 结合对话上下文打分 (0/1/2)
  ↓
6. 注入: max 1 event + 1 fragment → 上下文开头
```

## 基于 transcript 的去重

这是本项目的核心设计。直接读 Claude Code 的 session transcript 来判断哪些记忆已经在上下文里，不维护单独的状态文件。Transcript 是上下文的真实来源——消息回退后自动同步，不会出现"状态文件说推过但实际已不在上下文里"的问题。

## 安装

### 前提

- 本地装好 Claude Code
- 有一个支持语义搜索的记忆 MCP server（[Ombre Brain](https://github.com/P0luz/Ombre-Brain)、自建、或用仓库里的参考实现）
- （可选）LLM API key 用于 reranker（没有的话退化为 v1 的 cosine 阈值模式）

### 1. 安装 hook

```bash
git clone https://github.com/zziying/claude-code-memory-surface.git
cp claude-code-memory-surface/hook/memory_surface.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/memory_surface.py
```

### 2. 配置 Claude Code

在 `~/.claude/settings.json` 里加上：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMORY_MCP_URL='你的记忆后端URL' RERANKER_API_KEY='你的LLM API key' python3 ~/.claude/hooks/memory_surface.py"
          }
        ]
      }
    ]
  }
}
```

环境变量也可以写在 `.env` 文件里或系统环境中，不一定要内联。

### 3. 测试

```bash
echo '{"prompt":"你还记得上次我们聊了什么吗"}' \
  | MEMORY_MCP_URL='你的URL' RERANKER_API_KEY='你的key' \
  python3 ~/.claude/hooks/memory_surface.py
```

## 配置项

所有参数通过环境变量配置，详见 `.env.example`。

### 核心参数

| 参数 | 作用 | 默认值 |
|------|------|--------|
| `MEMORY_MCP_URL` | 记忆 MCP server 地址 | （必填） |
| `RERANKER_API_KEY` | LLM reranker API key | （空 = 退化为 cosine 阈值） |
| `RERANKER_API_URL` | OpenAI 兼容 API 地址 | `https://api.deepseek.com/chat/completions` |
| `RERANKER_MODEL` | reranker 模型 | `deepseek-chat` |
| `USER_NAME` / `AI_NAME` | reranker prompt 里的角色名 | `user` / `assistant` |

### 调参

| 参数 | 作用 | 默认值 |
|------|------|--------|
| `LIGHT_COS_GATE` | LIGHT 模式下跳过 reranker 的 cosine 阈值 | `0.65` |
| `RECALL_EXCLUDE` | Intent Gate 排除的复合词（逗号分隔） | `记忆库,记忆浮现,记忆系统` |
| `RECALL_KW`（代码内） | 触发完整搜索的关键词列表 | 见代码，默认中英混合 |

## 接入不同的记忆后端

hook 唯一的要求是你的 MCP server 暴露一个搜索工具（默认名 `semantic_search`），接口格式：

**请求：**
```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {"query": "...", "limit": 5, "no_track": true}
  }
}
```

**返回：**
```json
{
  "result": {
    "content": [{"type": "text", "text": "[{\"chunk_text\": \"...\", \"parent_memory_id\": \"...\", \"chunk_index\": 0, \"score\": 0.74, \"category\": \"fragment\"}]"}]
  }
}
```

### 接入 Ombre Brain

Ombre Brain 的 `breath` 工具返回格式略有不同。你需要做一个简单的适配（把 breath 的返回值映射成上面的格式），或者写一个小的代理服务。具体适配方式取决于你的 OB 部署方式。

如果你的搜索工具不叫 `semantic_search`，设置 `SEARCH_TOOL_NAME` 环境变量。

## 使用不同的 LLM 做 reranker

任何 OpenAI 兼容的 API 都可以：

- **DeepSeek**（推荐，便宜）：`RERANKER_API_URL=https://api.deepseek.com/chat/completions`
- **OpenAI**：`RERANKER_API_URL=https://api.openai.com/v1/chat/completions` + `RERANKER_MODEL=gpt-4o-mini`
- **本地模型**（Ollama 等）：`RERANKER_API_URL=http://localhost:11434/v1/chat/completions` + `RERANKER_MODEL=your-model`

不配 `RERANKER_API_KEY` 的话，hook 退化为 v1 行为（cosine ≥ 0.7 直接推送）。

## Intent Gate 详解

三档设计避免每条消息都调 LLM：

- **SKIP**：空消息、噪音词（"嗯""好""哈哈"）、代码块、系统消息 → 不搜索
- **TRIGGER**：含回忆关键词（"之前""上次""记得"等）→ 完整搜索 + rerank
- **LIGHT**：其他消息 → 轻量搜索，cosine 最高分过阈值才调 reranker

LIGHT 模式的 cosine 闸门（默认 0.65）是关键——它在"不浪费 reranker 调用"和"不漏掉相关记忆"之间取平衡。如果 reranker 调用成本不是问题，可以把 `LIGHT_COS_GATE` 调低。

## Debug

日志默认写在 `~/.claude/hooks/surface_debug.log`，记录每次调用的 Intent Gate 结果、搜索数量、过滤情况、reranker 打分。

如果发现浮现质量不好（噪音多 / 该浮现的没浮现），看日志能快速定位是哪个环节出了问题。

## 实际运行数据

以下数据来自日常使用的 debug log（中文对话场景，记忆库约 500 条 fragment）：

**Intent Gate 分流：**

| 档位 | 占比 | 说明 |
|------|------|------|
| SKIP | ~12% | 噪音词、代码、系统消息，零延迟 |
| LIGHT | ~79% | 普通消息，轻量搜索 |
| TRIGGER | ~8% | 含回忆关键词，完整搜索 |

**Reranker 过滤效果：**

reranker 对候选记忆的打分分布：

| 分数 | 占比 | 含义 |
|------|------|------|
| 0（无关） | ~82% | 被过滤掉 |
| 1（沾边） | ~9% | 被过滤掉 |
| 2（直接有用） | ~9% | 注入上下文 |

reranker 过滤掉了九成候选，实际注入的基本都是语境相关的。

**延迟：**

| 路径 | 占比 | 延迟 |
|------|------|------|
| SKIP（Intent Gate 拦截） | ~12% | ~0ms |
| Cosine gate 拦截（只搜索） | ~18% | ~1-2s |
| 完整流程（含 reranker） | ~62% | P50 3.6s / P90 5.1s |

延迟主要来自 LLM reranker API 调用。搜索本身 1-2 秒。不配 reranker 可以回到 v1 的亚秒级响应。

**注入频率：**

平均每条消息注入 0.24 条记忆——大部分消息不注入任何东西，只在真正相关时才推送。

## 已知限制

- **reranker 增加 2-4 秒延迟**：每条 TRIGGER/LIGHT 消息会多等几秒。如果不能接受，去掉 `RERANKER_API_KEY` 回到 v1 模式。
- **event 支持依赖后端**：如果你的记忆后端没有 event 类型，event 搜索会返回空，hook 正常工作只是少了 event 优先的能力。
- **Reranker prompt 是英文的**：即使你的对话是中文，prompt 用英文也能正常工作（DeepSeek/GPT 都支持）。如果想改成中文或其他语言，直接编辑脚本里的 `RERANK_PROMPT_CTX` 和 `RERANK_PROMPT_NO_CTX`。

## 同类项目

- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) — 完整的记忆 MCP server，有 hold/grow/breath/dream
- [claude-mem](https://github.com/thedotmack/claude-mem) — UserPromptSubmit + ChromaDB
- [ClawMem](https://github.com/yoloshii/ClawMem) — BM25 + 向量 + 重排序 + 意图分类
- [claude-hooks](https://github.com/mann1x/claude-hooks) — UserPromptSubmit + Qdrant + 注意力衰减

## 许可

MIT
