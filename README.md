# claude-code-memory-surface v4

> 让记忆主动找到你，而不是等你想起来去找它。

中文 | [English](README_en.md)

## 解决什么问题

外部记忆系统（[Ombre Brain](https://github.com/P0luz/Ombre-Brain)、自建 MCP 等）都能**存**记忆，但检索是被动的——模型必须知道该搜什么才会搜。

**问题是：context 压缩（compact）删掉的不只是记忆内容，而是"知道自己有过这段记忆"的元记忆。** 你不记得自己有过一段经历，就不会想到去搜，记忆就永远躺在库里不被触发。

本项目用 Claude Code 的 `UserPromptSubmit` hook，在**每条消息发送时**自动跑一次语义搜索 + LLM 重排序，把相关记忆主动推进上下文。不需要模型主动搜，不需要人类提醒"你去查一下"。

## v4 相比 v3

v3 解决的是"什么时候搜、搜到的算不算相关"（三档 Intent Gate + LLM reranker + 对话上下文）。v4 解决的是"相关了以后注多少、注哪句、谁最新"：

| 改进 | 问题 | 解法 |
|------|------|------|
| **指针 vs 全文** | 闲聊时整段旧记忆注进来会把话题带偏 | LIGHT 档只注一行指针（记忆 id + 支撑句），TRIGGER（明确回忆）才注全文。弱信号给线索，强意图才给内容 |
| **判官指句** | 指针钩子用 chunk 前 50 字，常常是元数据或无关段 | reranker 每条候选多回一个 `sent`：支撑判断的那一句的编号。候选正文按句编号喂进去，注入时逐字取那句 |
| **快照裁决进代码** | "追剧看到哪了"这类进度问题，模型会锚定先看到的旧快照，prompt 纪律治不住 | reranker 只标注 `snap`（是不是同一活动的进度快照）和 `date`；**代码**在多条快照打架时按日期取最新。日期只认记录日期 / 正文里的日期 / "昨天前天"倒推，模型编不出来 |
| **候选池 recency 偏置** | 最新进度碎片 cosine 排第 10 进不了池，被旧的同话题闲聊压着 | general 通道多取 12 条，按 `cosine + w·e^(-days/τ)` 重排后截回 8 条。只影响谁进池，不改 gate、不改 rerank |
| **应答剥离** | "哦对，记得了"被当成回忆请求，拿到 TRIGGER 特权 | 扫关键词前先剥掉应答词形；问句形式（"记得吗？"）保留 |
| **TRIGGER 特权** | 明确问"还记得 X 吗"时，X 因为早已出现在 session 里被去重掉 | TRIGGER 跳过 transcript 去重、reranker 超时重试一次 |
| **上下文提取** | 工程窗里 80KB 尾巴被 tool 输出挤到只剩一两条对话 | 读 300KB 尾、取最近 10 条、只取文本块、剥系统注入 / channel 壳 / 时间戳 |
| **静音正则** | 某些对话模式下不想要任何记忆弹窗 | `MUTE_RE` 命中时 LIGHT 直接闭嘴，TRIGGER 照常 |
| **回归测试** | 调 prompt 改一处坏一处 | `tests/` 里的 runner 驱动真实 hook 跑评测集（silent / inject / allow_pointer 三种期望） |

## 架构

```
用户发消息
  ↓
0. 归一化：剥 <channel> 壳、开头时间戳
  ↓
1. Intent Gate: SKIP / TRIGGER / LIGHT（应答剥离后扫关键词）
  ↓  [LIGHT] MUTE_RE 命中 → 静默
2. 双路搜索: event + general（并行；general 通道 recency 偏置后截取）
  ↓
3. 强过滤: archived / deep / transcript 去重（TRIGGER 跳过）/ resolved / 低重要度
  ↓
4. [LIGHT] Cosine 闸门: 最高分 < 0.72 → 跳过 reranker
  ↓
5. LLM Reranker（带最近 10 条对话）→ 每条候选 {score, sent, snap, date}
   → 代码快照裁决（≥2 条 snap 时日期最大者强制 2，其余 0）
   → 同档新者优先（都打 2 时 event/fragment 各取最新一条）
  ↓
6. 注入
   TRIGGER → 全文：max 1 event（brief）+ 1 fragment
   LIGHT   → 指针：⟦déjà vu⟧ 9/4 related memory 「支撑句…」 [id] — search it if relevant
```

## 设计不变量

这几条是 v1 到 v4 一路踩坑攒出来的，调参时别破：

- **辅助信号只影响候选排序，永不独立构成注入理由。** 关键词命中只是把候选送进池子，recency 偏置只决定池里的座次，注入资格永远由 reranker 判"缺了这条回答会不会明显变差"。
- **LIGHT 批级闸看裸 cosine，不看加成后的分。** 0.70 到 0.72 这道缝里住的全是 reranker 也救不回的边缘货。
- **模型做标注，代码做裁决。** 让模型比较"哪条最新"会锚定先看到的那条；让它标每条的日期和"是不是快照"，排序交给程序。反面教训：prompt 里写"新旧以程序裁决为准"，模型会干脆全打 0 等程序——所以 prompt 里仍然要求它照常打分，代码不看它的原分。
- **指针不算浮现。** LIGHT 指针没被点开就不该给那条记忆续命，如果你的后端有访问计数，别在指针路径上刷它。

## 基于 transcript 的去重

直接读 Claude Code 的 session transcript 判断哪些记忆已经在上下文里，不维护单独的状态文件。Transcript 是上下文的真实来源——消息回退后自动同步。v4 的指针行里带的 `[8hex]` id 落进 transcript，顺便充当同会话的去重锚。

## 安装

### 前提

- 本地装好 Claude Code
- 有一个支持语义搜索的记忆 MCP server（[Ombre Brain](https://github.com/P0luz/Ombre-Brain)、自建、或 `reference-server` 分支里的参考实现）
- （可选）LLM API key 用于 reranker（没有的话退化为 v1 的 cosine 阈值模式，v4 的指针 / 裁决都依赖 reranker）

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

### 核心

| 参数 | 作用 | 默认值 |
|------|------|--------|
| `MEMORY_MCP_URL` | 记忆 MCP server 地址 | （必填） |
| `SEARCH_TOOL_NAME` | 搜索工具名 | `semantic_search` |
| `READ_TOOL_NAME` | （可选）读单条记忆全文的工具名，用于 event brief | 空 = 用 chunk_text |
| `RERANKER_API_KEY` | LLM reranker API key | （空 = 退化为 cosine 阈值） |
| `RERANKER_API_URL` | OpenAI 兼容 API 地址 | `https://api.deepseek.com/chat/completions` |
| `RERANKER_MODEL` | reranker 模型 | `deepseek-v4-flash` |
| `RERANKER_DISABLE_THINKING` | 请求体带 `thinking: disabled`（推理模型不关会吃掉 max_tokens） | `1` |
| `USER_NAME` / `AI_NAME` | reranker prompt 和上下文标签里的角色名 | `user` / `assistant` |

### 注入形态（v4）

| 参数 | 作用 | 默认值 |
|------|------|--------|
| `LIGHT_POINTER_ONLY` | LIGHT 只注指针；设 0 回到 v3（两档都注全文） | `1` |
| `SENT_PTR_ENABLE` | 指针钩子用 reranker 指的支撑句（否则 chunk 前 50 字） | `1` |
| `SENT_HOOK_MAX` | 钩子最长字数 | `60` |
| `SNAP_ADJUDICATE` | 代码裁决进度快照新旧 | `1` |

### 调参

| 参数 | 作用 | 默认值 |
|------|------|--------|
| `LIGHT_COS_GATE` | LIGHT 模式跳过 reranker 的 cosine 阈值 | `0.72` |
| `GEN_FETCH` / `RECENCY_W` / `RECENCY_TAU` | general 通道多取条数 / 今日满额加成 / e 折减天数 | `12` / `0.03` / `14` |
| `CONTEXT_TURNS` / `CONTEXT_TAIL_KB` | 喂给 reranker 的最近消息条数 / 读 transcript 尾多大 | `10` / `300` |
| `TIMEOUT_RERANK` | reranker 超时（TRIGGER 超时重试一次） | `9.0` |
| `MUTE_RE` | 命中则 LIGHT 静默的正则 | 空 |
| `RECALL_EXCLUDE` | Intent Gate 排除的复合词（逗号分隔） | `记忆库,记忆浮现,记忆系统,...` |
| `RECALL_KW`（代码内） | 触发完整搜索的关键词列表 | 中英混合 |

## 接入不同的记忆后端

hook 唯一的硬要求是 MCP server 暴露一个搜索工具（默认名 `semantic_search`），接口格式：

**请求：**
```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {
    "name": "semantic_search",
    "arguments": {"query": "...", "limit": 8, "no_track": true, "category": "event"}
  }
}
```
`category` 只在 event 通道传；后端没有 event 类型就返回空数组，hook 照常工作。

**返回：**
```json
{
  "result": {
    "content": [{"type": "text", "text": "[{\"chunk_text\": \"...\", \"parent_memory_id\": \"...\", \"chunk_index\": 0, \"score\": 0.74, \"category\": \"fragment\", \"created_at\": \"2026-09-04T10:00:00\"}]"}]
  }
}
```
`created_at` 可选但强烈建议有：recency 偏置、快照裁决、同档新者优先、指针里的日期全靠它。其他可选字段：`tags`、`importance`、`archived`、`resolved`、`parent_id`。

`READ_TOOL_NAME` 若设置，hook 会用 `{"memory_id": ..., "no_track": true}` 调它拿 event 全文来 brief；返回 JSON 里的 `content` 字段或纯文本都行。

### 接入 Ombre Brain

Ombre Brain 的 `breath` 工具返回格式略有不同。你需要做一个简单的适配（把 breath 的返回值映射成上面的格式），或者写一个小的代理服务。

## 使用不同的 LLM 做 reranker

任何 OpenAI 兼容的 API 都可以：

- **DeepSeek**（推荐，便宜）：默认配置。v4 系列是推理模型，`RERANKER_DISABLE_THINKING=1` 必须开
- **OpenAI**：`RERANKER_API_URL=https://api.openai.com/v1/chat/completions` + `RERANKER_MODEL=gpt-4o-mini` + `RERANKER_DISABLE_THINKING=0`
- **本地模型**（Ollama 等）：`RERANKER_API_URL=http://localhost:11434/v1/chat/completions` + `RERANKER_MODEL=your-model` + `RERANKER_DISABLE_THINKING=0`

reranker 必须能稳定吐 JSON 数组；解析失败 hook 静默放弃（fail-soft），不会注入。

## Intent Gate 详解

- **SKIP**：空消息、噪音词（"嗯""好""哈哈"）、代码块、系统消息、6 字以下且无回忆关键词 → 不搜索
- **TRIGGER**：含回忆关键词（"之前""上次""记得""remember"等）→ 完整搜索 + rerank，跳过 cosine 闸和 transcript 去重，注全文
- **LIGHT**：其他消息 → 轻量搜索，cosine 最高分过阈值才调 reranker，过关只注指针

TRIGGER 不是强制召回：它少一道 cosine 门，但 reranker 那道门不变，没候选打 2 分照样静默。

关键词表里像"最近""进度"这种词日常句子里很常见，会误发 TRIGGER 特权。如果你的日志里这类误触发多，可以考虑把关键词降级为"只放宽闸门"，全文还是指针由 reranker 判——这是 roadmap 里的一项，还没做。

## 回归测试

```bash
MEMORY_MCP_URL=... RERANKER_API_KEY=... python3 tests/test_memory_surface.py
python3 tests/test_memory_surface.py --only noise-ack -v
```

runner 用 subprocess 跑真实 hook（真后端、真 reranker，每 case 1-6 秒），不复制任何逻辑。`tests/cases.example.json` 是模板，正例要改成你自己库里有的内容。三种期望：`silent`（一个字都不能注）、`inject`（必须注，可配 `must_contain` / `must_contain_any` / `must_not_contain`）、`inject` + `allow_pointer:true`（给一行指针算过、全文注入算挂）。

写 case 的纪律见 runner 文件头：正例不写死记忆 id、用原话、关键词锚语境不锚答案、操作类问题静默才对。新冤案从 debug log 里持续补进 cases。

## Debug

日志默认写在 `~/.claude/hooks/surface_debug.log`（满 1MB 滚动 .1/.2），每次调用记录 gate 结果、搜索命中、过滤原因、reranker 完整打分、快照裁决动作、最终注入。

浮现质量不对时看日志能快速定位：`0ev+0gen` 连片 + `search_srv_err` = 后端 embedding 挂了；`rerank_err` 零星出现是 reranker 偶发坏 JSON，成片出现看 key 和超时；`snap_adjudicate` 行能看到代码什么时候推翻了模型的选择。

## 实际运行数据（v3 时期，2026-06）

以下数据来自日常使用的 debug log（中文对话场景，记忆库约 500 条 fragment）。v4 的分流比例基本一致，注入形态变了（LIGHT 变指针）所以"注入条数"不再可比：

| 档位 | 占比 |
|------|------|
| SKIP | ~12% |
| LIGHT | ~79% |
| TRIGGER | ~8% |

reranker 打分：0 分 ~82%、1 分 ~9%、2 分 ~9%。完整流程 P50 3.6s / P90 5.1s，延迟主要在 reranker API。

## 已知限制

- **reranker 增加 2-4 秒延迟**：不能接受就去掉 `RERANKER_API_KEY` 回到 v1 模式（此时没有指针、没有裁决）。
- **event 支持依赖后端**：没有 event 类型的后端，event 通道返回空，hook 正常工作只是少了 event 优先。
- **快照裁决依赖 `created_at`**：后端不返回创建时间时，日期校验只能认正文里写的日期，裁决基本不会出手。
- **Reranker prompt 是英文的**：中文对话也能正常工作。想改语言直接编辑 `RERANK_RULES` / `RERANK_OUTPUT`。
- **Python 3.9 注意**：系统自带的 Python 3.9 直接运行含超长中文行的脚本会误报 `Non-UTF-8 code`，脚本第 2 行的 coding 声明就是治这个的，别删。

## Roadmap

- reranker 加 `redundant` 档：近期对话里已经提过的旧事打 0（语义层去重，现在只有 transcript id 的机械去重）
- 事件现实性状态（planned / not_occurred / occurred）：让"说要去做"和"做了"在裁决里分开
- TRIGGER / LIGHT 改由 reranker 从语义判定，正则只放宽闸门
- 情绪对冲：注入负面记忆时追加一行同话题正面记忆的指针（需要后端有 valence 字段）

## 同类项目

- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) — 完整的记忆 MCP server，有 hold/grow/breath/dream
- [claude-mem](https://github.com/thedotmack/claude-mem) — UserPromptSubmit + ChromaDB
- [ClawMem](https://github.com/yoloshii/ClawMem) — BM25 + 向量 + 重排序 + 意图分类
- [claude-hooks](https://github.com/mann1x/claude-hooks) — UserPromptSubmit + Qdrant + 注意力衰减
- [eggshell-memory](https://github.com/dankefox/eggshell-memory) — 逐字证据窗口 + 结构化判读 + 代码侧时序裁决，v4 的 `sent` / `snap` 思路参考了它

## 许可

MIT
