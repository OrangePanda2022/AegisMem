# AegisMem

基于知识图谱的长时记忆系统。将多轮对话拆解为带元数据的事实节点，以图结构组织记忆，通过随机游走 + 多路融合 + 多智能体打分检索最相关上下文，交由 LLM 合成回答。

## 架构

系统由三条管线组成：

**写入** — 消息 → LLM 事实抽取 → Tag 填充 → 三路召回候选 → 子图 LLM 演化决策 → 落库

**遗忘** — Ebbinghaus 衰减 + 拓扑中心度，写入后自动触发，将事实分为 L0–L3 四个 tier

**读取** — 查询 → 遗忘衰减 → 向量嵌入 → 多路召回 → MAS 打分 → 迭代检索 → CBA 预算分配 → LLM 合成答案

### 读取管线详细流程

```
查询
 │
 ├─ 1. 遗忘衰减（reference_time 锚定）
 │
 ├─ 2. 查询嵌入
 │
 ├─ 3. 召回
 │    ├─ 时间召回（时间窗内的事实）
 │    ├─ 投机召回（向量 draft → LLM 充分性判断，置信度 ≥ 0.8 则跳过全量召回）
 │    └─ 完整 4 路召回（BM25 + Trigram + 向量 + Tag，RRF 融合）
 │        └─ KB-aware 关键词映射：原始关键词与 KB 实体交叉验证，丢弃噪声词
 │
 ├─ 4. 图随机游走扩展
 │    P(u|v,q,t) = (1/Z) · Γ(v,u,t) · exp(
 │        λ_sem · cos(e_q, e_u)
 │      + λ_mem · R(u,t)
 │      + λ_struct · [ln ω(v,u) − α · ln deg(u)]
 │    )
 │
 ├─ 5. MAS 多智能体打分
 │    semantic_match(0.45) + edge_weight(0.20) + recency(0.15)
 │    + tier_boost(0.05) + activation_history(0.15)
 │
 ├─ 6. 迭代检索（最多 3 轮）
 │    ├─ 充分性检查 → 生成替代关键词
 │    ├─ 图邻居 Tag 扩展关键词
 │    └─ 定向召回新事实 → 合并 → 重打分
 │
 ├─ 7. 反向实体过滤
 │    LLM 推断答案应引用的实体 → 缺失实体触发二次召回
 │
 ├─ 8. CBA 上下文预算分配（默认 6000 tokens）
 │    偏好事实标注 [偏好]
 │
 └─ 9. LLM 答案合成（含 Anti-Hallucination 规则）
```

## 关键设计

### 事实抽取

LLM 将每条消息拆为最小不可分事实，填充 `Person/Object/Location/Event/Organization/Preference/HappendTime/MentionedTime` 元数据。偏好词（enjoy/like/hate 等）强制保留在 content 中。`HappendTime` 优先提取事件实际发生时间；缺失时用 `MentionedTime` 回填（P0 修复），避免 recency 评分坍缩到 0.5 地板值。

### KB-aware 关键词映射（P1）

LLM 抽取的关键词常与 KB 实体不匹配（如 "5-day trip" vs KB 中的 "Costa Rica"）。召回时先对关键词做 `hybrid_recall`，只保留 KB 中有近似匹配的关键词，并补充 top-5 相关实体名，提升检索命中率。

### Anti-Hallucination（P3）

答案合成 prompt 包含三条规则：只引用上下文实体、事实性问题必须提取精确信息而非给建议、信息不足时回答"无法确定"。防止 LLM 用自身知识覆盖检索上下文。

### 投机召回

前端加速：先跑向量一路作为 draft，LLM 判断充分性 + 置信度，置信度 ≥ 阈值（默认 0.8）时跳过完整 4-path，直接进图游走。简单问题省一轮实体抽取和多路融合开销。

### 迭代检索

后端兜底：MAS 排序后检查 top-N 充分性，不充分时 LLM 生成替代关键词 + 图邻居 Tag 扩展，定向召回新事实，合并重排，最多 3 轮。解决单次召回信息不足问题。

### 辩论模式

3 个 specialist（偏好摘要 / 事实回忆 / 时间推理）并行回答 → Judge 合并。默认关闭。

## 快速开始

### 环境要求

- Python ≥ 3.14
- [uv](https://github.com/astral-sh/uv)

### 安装

```bash
git clone https://github.com/OrangePanda2022/AegisMem.git
cd AegisMem
uv sync
```

### 配置

```bash
cp .env.example .env
```

编辑 `.env` 填入凭证：

```bash
# 主 LLM（OpenAI Chat Completions 协议网关）
llm_api_key=""
llm_base_url=""
llm_model=""

# 嵌入模型（OpenAI Embeddings 协议网关）
embedding_api_key=""
embedding_base_url=""
embedding_model="qwen3-embedding-8b"

# Judge（Anthropic Messages 协议网关）
judge_api_key=""
judge_base_url=""
judge_model=""
```

三组客户端协议独立、网关独立，可接任意兼容服务。

### 使用

```bash
# 写入
PYTHONPATH=. uv run python main.py ingest "明天上午10点和张总开会，地点在公司21楼会议室。"

# 查询
PYTHONPATH=. uv run python main.py answer "我什么时候和张总有会？"
```

数据落盘到 `memory.db`（SQLite + sqlite-vec）。

## LongMemEval 评测

### 准备数据

```bash
git clone https://github.com/xiaowu0162/LongMemEval.git /tmp/lme
mkdir -p LongMemEval/data
cp /tmp/lme/data/longmemeval_oracle.json LongMemEval/data/
```

### 运行评测

```bash
PYTHONPATH=. uv run python scripts/evaluate_longmemeval.py \
    --data longmemeval_oracle.json \
    --output /tmp/results.jsonl \
    --concurrency 24 \
    --per-item-timeout 720
```

- **断点续跑**：已有 question_id 自动跳过
- **错误隔离**：单题异常/超时写入 errors.jsonl，不影响其余
- **资源清理**：每题独立 SQLite，结束后自动删除
- **双层限流**：`--concurrency` + `settings.llm_max_concurrency`

### 打分

```bash
PYTHONPATH=. uv run python scripts/score_longmemeval.py \
    --hyp /tmp/results.jsonl \
    --ref LongMemEval/data/longmemeval_oracle.json \
    --concurrency 24
```

输出整体准确率及分桶准确率。

### 调试

```bash
# 单题
PYTHONPATH=. uv run python scripts/debug_one_question.py \
    --qid 0bb5a684 \
    --data longmemeval_oracle.json \
    --out /tmp/debug_0bb5a684.json

# 批量
PYTHONPATH=. uv run python scripts/debug_batch.py \
    --batch /tmp/wrong_ids.json \
    --data longmemeval_oracle.json \
    --out-dir /tmp/debug_wrong \
    --concurrency 8
```

输出 15+ 阶段的全流程 trace JSON。

## 参数参考

所有参数在 `internal/config/settings.py`，通过 `.env` 或环境变量覆盖。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `llm_max_concurrency` | 48 | LLM 客户端并发上限 |
| `embedding_max_concurrency` | 16 | 嵌入客户端并发上限 |
| `api_max_retries` | 8 | API 调用最大重试次数 |
| `api_retry_base_delay` | 2.0 | 指数退避基数（秒） |
| `llm_call_timeout_s` | 45.0 | 单次 LLM 调用硬超时 |
| `retrieve_total_token_budget` | 6000 | CBA token 预算 |
| `retrieve_max_graph_depth` | 4 | 图游走最大跳数 |
| `mas_weights` | sem=0.45, edge=0.20, rec=0.15, tier=0.05, act=0.15 | MAS 五维权重 |
| `speculative_confidence_threshold` | 0.8 | 投机召回置信度阈值 |
| `iterative_retrieval_max_rounds` | 3 | 迭代检索最大轮数 |
| `debate_mode_enabled` | False | 辩论模式开关 |

完整参数列表及检索融合系数、图游走转移概率参数等见源码。

## 项目结构

```
internal/
├── config/settings.py              # 全局配置（pydantic-settings）
├── domain/
│   ├── model/                      # Fact, Edge, Entity, MemBox, Tag, Tier, Buffer
│   ├── repositories/               # 仓储接口
│   └── services/
│       ├── mas_manager.py          # MAS 五维加权打分
│       └── context_budget_allocator.py
├── infra/
│   ├── container.py                # 依赖注入容器
│   ├── database/sqlite.py          # aiosqlite + sqlite-vec
│   ├── models/
│   │   ├── llm/                    # 主 LLM 客户端 + prompt 模板
│   │   ├── embedding/              # 嵌入客户端
│   │   └── judge/                  # Judge 评测客户端
│   └── repositories/               # SQLite 仓储实现
├── service/
│   ├── input/write_service.py      # 写入管线
│   ├── retrieve/recall.py          # 召回 + 投机 + 迭代
│   ├── retrieve/cba.py             # 上下文预算分配
│   └── forget/forget.py            # 遗忘衰减
└── util/
    ├── api_retry.py                # 信号量 + 指数退避
    ├── rrf.py                      # Reciprocal Rank Fusion
    ├── token_tracker.py            # Token 用量计数 + JSONL 日志
    └── debug_collector.py          # 单题全流程 dump

scripts/
├── evaluate_longmemeval.py         # 评测运行器
├── score_longmemeval.py            # Judge 打分
├── debug_one_question.py           # 单题调试
├── debug_batch.py                  # 批量调试
└── smoke_*.py                      # 冒烟测试
```

## 依赖

- `aiosqlite` + `sqlite-vec` — 异步 SQLite 向量存储
- `openai` — LLM (Chat Completions) + Embedding 客户端
- `anthropic` — Judge 客户端 (Messages)
- `pydantic-settings` — 配置管理
- `numpy` — 向量运算
- `json-repair` — 容错 JSON 解析

## 参考

- [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
