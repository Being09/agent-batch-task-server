# 通用批量任务服务器 — 架构设计

## 一、设计目标

让 Coding Agent 能可靠地处理任意类型的批量任务，同时满足：

| 目标 | 方案 |
|------|------|
| **通用化** | 接入新任务类型只需写一个 YAML 配置文件，Server 零修改 |
| **工程化质量保障** | 原子队列操作、超时回收、独立验证、进度持久化 |
| **零外部依赖** | Python/Node.js 双版本，仅使用标准库 |
| **最简分发** | 代码随 Skill 分发，一行命令启动 |
| **分布式并发安全** | PID 文件锁 + 原子写入，多 Worker 不重复处理 |

## 二、架构全景

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Orchestrator Agent                             │
│  1. 启动 Server（python/node 一行命令）                               │
│  2. 读取 Task Type Config → POST /config 注册                         │
│  3. 提交批量任务 → POST /batch                                        │
│  4. 发射 Worker Agent × N（run_in_background=true）                   │
│  5. 监控进度 → GET /progress                                          │
│  6. 收集结果 → GET /task/{id} 或聚合                                  │
└──────────┬───────────────────────────────────┬──────────────────────┘
           │ POST /batch                        │ GET /progress
           ↓                                   ↓
┌──────────────────────────────────────────────────────────────────────┐
│                  Generic Task Server（零依赖）                         │
│                                                                      │
│  ┌─────────────┐  ┌────────────────┐  ┌───────────────────────────┐  │
│  │ FileLock    │  │ 状态机 + 超时   │  │ Config-driven Validator  │  │
│  │ (PID 文件锁) │  │ 回收 (Reaper)  │  │ (YAML Schema 验证)       │  │
│  └─────────────┘  └────────────────┘  └───────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  queue.json（原子写入: tmp → rename）                          │ │
│  │  + queue.lock（PID 文件锁: O_CREAT | O_EXCL）                  │ │
│  └────────────────────────────────────────────────────────────────┘ │
└──────────┬───────────────────────────────────┬──────────────────────┘
           │ GET /next_task                      │ POST /result
           ↓                                    ↓
┌──────────────────────────────────────────────────────────────────────┐
│                    Worker Agent × N（可跨机器）                       │
│                                                                      │
│  加载 Skill + Task Type Config                                       │
│  Loop: pull → process(LLM) → validate → push → 直到 204              │
└──────────────────────────────────────────────────────────────────────┘
```

## 三、三层分离设计

核心思想：**Server 是通用的，Config 是唯一的扩展点，Skill 是通用的工作说明书。**

### Layer 1：Task Type Config（YAML）

每种批量任务类型 = 一个 YAML 配置文件。**这是接入新任务类型唯一需要编写的文件。**

```yaml
name: string                    # 任务类型名称（必填）
description: string             # 描述（必填）
version: string                 # Config 版本号

# ---- 输入拆分策略 ----
split:
  method: string                # 内置拆分方法（必填）
  source: string                # 输入源
  params: object                # 方法特定参数

# ---- 任务输入定义 ----
task_input:
  <field_name>: field_def       # 每个任务携带的数据字段

# ---- 任务输出定义（也是验证 Schema）----
task_output:
  <field_name>: field_def       # 结果字段 + 验证规则

# ---- 额外验证规则 ----
validation:
  - field: string
    rules:
      - <rule_name>: <value>

# ---- Worker 行为指令 ----
worker_prompt: string            # 注入 Skill 的 LLM 指令

# ---- 约束条件 ----
constraints:
  max_time_per_task: int        # 单任务超时秒数
  max_retries: int              # 最大重试次数

# ---- 结果聚合（可选）----
aggregation:
  method: string                # concat | merge
  output_format: string         # json | csv
  output_path: string
```

**字段定义（field_def）支持的属性：**

```yaml
field_name:
  type: string            # string, number, integer, boolean, array, object
  required: bool          # 是否必填
  description: string     # 字段描述
  enum: [string]          # 枚举值
  min_length: int         # 字符串最小长度
  max_length: int         # 字符串最大长度
  min_items: int          # 数组最小元素数
  max_items: int          # 数组最大元素数
  range: [min, max]       # 数值范围
  pattern: string         # 正则匹配
  items: field_def        # 数组元素 schema
  properties:             # 对象属性
    <name>: field_def
```

#### 内置拆分方法

| 方法 | 说明 | source 示例 | params |
|------|------|------------|--------|
| `text_chunk` | 文本分块 | `"./data/source.txt"` | `chunk_size`, `overlap`, `separator`, `respect_sentence` |
| `line_by_line` | 按行分割 | `"./data/urls.txt"` | `batch_lines`, `skip_empty`, `skip_comments` |
| `file_per_item` | 每文件一个任务 | `"./src/**/*.py"` | `include_content`, `encoding`, `max_file_size` |
| `json_array` | JSON 数组拆分 | `"./data/items.json"` | `item_path`, `items_per_task` |
| `csv_rows` | CSV 按行拆分 | `"./data/records.csv"` | `rows_per_task`, `delimiter`, `has_header` |
| `directory_tree` | 目录树 | `"./project"` | `include_files`, `pattern`, `max_depth` |

扩展新拆分方法：实现 `BaseSplitter` 子类 + 注册到 `SPLITTERS` 字典。

#### Config 示例：实体提取

```yaml
name: entity_extraction
description: "从文本中提取实体和关系，构建知识图谱"

split:
  method: text_chunk
  source: "./data/source_text.txt"
  params:
    chunk_size: 2000
    overlap: 200
    respect_sentence: true

task_input:
  chunk_id: { type: string }
  content: { type: string, required: true, description: "文本片段" }
  chunk_index: { type: integer, description: "在原文中的序号" }

task_output:
  entities:
    type: array
    required: true
    min_items: 1
    items:
      name:       { type: string, required: true }
      type:       { type: string, required: true, enum: [PERSON, ORG, LOC, EVENT] }
      confidence: { type: number, range: [0, 1] }
      mentions:   { type: array, items: { type: string } }
  relations:
    type: array
    items:
      from:     { type: string, required: true }
      to:       { type: string, required: true }
      type:     { type: string, enum: [KNOWS, WORKS_FOR, LOCATED_IN] }
      strength: { type: number, range: [1, 10] }

validation:
  - field: "entities"
    rules:
      - unique: ["name", "type"]
  - field: "relations"
    rules:
      - reference_exists: ["from", "entities.name"]
      - reference_exists: ["to", "entities.name"]

worker_prompt: |
  你是知识图谱构建专家。分析给定文本片段，提取所有命名实体和它们之间的关系。
  严格返回 JSON 格式。实体类型仅限 PERSON/ORG/LOC/EVENT。

constraints:
  max_time_per_task: 60
  max_retries: 3

aggregation:
  method: merge
  output_format: json
  output_path: "./output/kg.json"
```

#### Config 示例：代码审查

```yaml
name: code_review
description: "批量审查代码文件，找出 bug、安全问题、规范问题"

split:
  method: file_per_item
  source: "./src/**/*.py"
  params:
    include_content: true
    max_file_size: 50000

task_input:
  file_path: { type: string, required: true }
  file_content: { type: string, required: true }
  language: { type: string }

task_output:
  issues:
    type: array
    required: true
    items:
      severity:   { type: string, required: true, enum: [critical, warning, info, style] }
      line:       { type: integer, required: true, range: [1, 99999] }
      message:    { type: string, required: true, min_length: 10 }
      suggestion: { type: string }
  summary:
    type: string

worker_prompt: |
  你是高级代码审查员。逐行审查给定代码文件。
  关注：bug、安全漏洞、性能问题、代码规范。

constraints:
  max_time_per_task: 120

aggregation:
  method: concat
  output_format: json
  output_path: "./output/review_report.json"
```

#### Config 示例：批量翻译

```yaml
name: batch_translation
description: "批量翻译文本片段"

split:
  method: json_array
  source: "./data/articles.json"
  params:
    items_per_task: 1

task_input:
  id: { type: string }
  title: { type: string, required: true }
  content: { type: string, required: true }

task_output:
  translated_title: { type: string, required: true }
  translated_content: { type: string, required: true }
  quality_score: { type: number, range: [0, 1] }

worker_prompt: |
  将给定文本翻译为指定语言。保持原文语气和格式。

constraints:
  max_time_per_task: 30
```

### Layer 2：Generic Task Server

Server **完全不知道任务类型是什么**，只管理通用 JSON 的队列、状态机、持久化。

#### 项目结构

```
server/
├── python/
│   └── server.py            # Python 版（~140 行，stdlib only）
├── node/
│   └── server.js            # Node.js 版（~170 行，built-in only）
└── task_types/
    ├── entity_extraction.yaml
    └── code_review.yaml
```

#### API 规范

| 方法 | 路径 | 说明 | 调用者 |
|------|------|------|--------|
| `GET` | `/health` | 健康检查 | 任意 |
| `POST` | `/batch` | 提交一批任务（Orchestrator 拆分后提交） | Orchestrator |
| `GET` | `/next_task` | 原子拉取下一个 pending 任务（204=空） | Worker |
| `POST` | `/result` | 提交任务结果（幂等） | Worker |
| `GET` | `/progress` | 查询整体进度 | Orchestrator |
| `GET` | `/task/{id}` | 查询单个任务详情 | 任意 |
| `GET` | `/stale` | 查询超时卡住的任务 | Orchestrator |
| `POST` | `/config` | 注册任务类型配置 | Orchestrator |
| `GET` | `/config/{name}` | 获取已注册配置 | Worker / Orchestrator |
| `POST` | `/heartbeat` | Worker 心跳 | Worker |

#### 核心请求/响应

**POST /batch — 提交任务**

```json
// Request
{
  "batch_id": "batch_001",
  "task_type": "entity_extraction",
  "tasks": [
    { "task_id": "chunk_001", "payload": { "content": "文本片段..." } },
    { "task_id": "chunk_002", "payload": { "content": "另一段..." } }
  ]
}

// Response 200
{
  "batch_id": "batch_001",
  "accepted": 2
}
```

**GET /next_task — Worker 拉取**

```json
// Response 200（有任务）
{
  "task_id": "chunk_001",
  "task_type": "entity_extraction",
  "payload": { "content": "文本片段..." }
}

// Response 204（无任务，空 body）
```

**POST /result — Worker 推送**

```json
// Request
{
  "task_id": "chunk_001",
  "result": {
    "entities": [{ "name": "张三", "type": "PERSON", "confidence": 0.95 }],
    "relations": []
  }
}

// Response 200（成功）
{
  "task_id": "chunk_001",
  "status": "completed"
}

// Response 200（幂等 — 已完成）
{
  "task_id": "chunk_001",
  "status": "already_completed"
}
```

**GET /progress — 进度**

```json
// Response 200
{
  "total": 100,
  "completed": 45,
  "dispatched": 5,
  "pending": 48,
  "failed": 2,
  "permanently_failed": 0,
  "progress_percent": 45.0
}
```

#### 数据模型

```json
{
  "task_id": "chunk_001",
  "batch_id": "batch_001",
  "task_type": "entity_extraction",
  "payload": { ... },
  "result": { ... },
  "state": "pending",
  "retry_count": 0,
  "max_retries": 3,
  "created_at": "2025-01-10T10:00:00Z",
  "dispatched_at": null,
  "completed_at": null,
  "last_error": null
}
```

#### 状态机

```
                    ┌──────────┐
           创建     │ Pending  │◄──────────┐
          ────────►│          │  超时回收   │
                    └────┬─────┘  (retry<max)│
                         │ GET /next_task    │
                    ┌────▼─────┐            │
           分发     │Dispatched│────────────┘
          ────────►│          │
                    └──┬───┬──┘
                       │   │
            提交结果    │   │  超时 (retry>=max)
            (验证通过)  │   │
                    ┌──▼───┴───────────┐
           完成     │ Completed│ Failed │──────►┌───────────────────┐
          ────────►│          │       │        │ PermanentlyFailed │
                    └──────────┘       │        │ (不可恢复)         │
                                       └────────►│                   │
                                                └───────────────────┘
```

### Layer 3：Worker Skill（通用模板）

Skill 模板本身不变，行为全靠 Config 注入：

```markdown
# Batch Task Worker — 通用模板

## Role
你是一个批量任务 Worker。从 Task Server 拉取任务 → 处理 → 提交结果 → 循环。

## Workflow
1. GET {server_url}/next_task → 200=处理, 204=退出
2. 按 worker_prompt 处理 payload
3. 构造结果 JSON，按 task_output schema 自验证
4. POST {server_url}/result { task_id, result }
5. 循环直到 204

## 任务指令（从 Config 注入）
{worker_prompt}

## 输出格式（从 Config 注入）
{task_output_schema_description}

## Constraints
- 单任务处理不超过 {max_time_per_task} 秒
- 处理失败时提交部分结果
- 结果必须是合法 JSON
```

## 四、分布式并发安全

### 4.1 文件锁机制

```
┌──────────────────────────────────────────────────────────┐
│  queue.lock（PID 文件锁）                                  │
│                                                          │
│  获取锁:                                                  │
│    os.open("queue.lock", O_CREAT | O_EXCL | O_WRONLY)    │
│    → 成功: 写入 "PID|timestamp" → 获得锁                  │
│    → 失败 (EEXIST):                                      │
│        → 读 PID → kill(pid, 0) 检测进程存活               │
│        → 进程死亡 → 删除锁文件 → 重试                     │
│        → 进程存活 → 等待 50ms → 重试                      │
│        → 锁持有超过 10 秒 → 强制释放（防 PID 复用）       │
│                                                          │
│  释放锁:                                                  │
│    os.unlink("queue.lock")                                │
└──────────────────────────────────────────────────────────┘
```

### 4.2 原子写入

```
写入流程:
  1. 写入 queue.json.tmp（完整写入新数据）
  2. os.replace(tmp, queue.json)（原子替换）

崩溃安全性:
  - 崩溃于步骤 1: queue.json 不受影响（上次数据完好）
  - 崩溃于步骤 2: replace 是原子操作（POSIX rename / Windows MoveFileExW）
  - 不存在部分写入的中间状态
```

### 4.3 并发场景验证

#### 场景 1：多 Worker 同时拉取任务

```
Worker-1 → acquireLock() → O_EXCL 成功 → 读队列 → 取 task-1 → 写队列 → 释放锁
Worker-2 → acquireLock() → O_EXCL 失败(EEXIST) → 等待 50ms → 重试 → 成功 → 读到已更新队列 → 取 task-2
```

**结论：✅ 安全。** `O_EXCL` 是 OS 内核级原子操作。

#### 场景 2：Worker 提交 vs Reaper 回收同一任务

```
两者都走同一把文件锁 → 串行化
- Worker 先: 标记 completed → Reaper 读到 completed → 跳过
- Reaper 先: 标记 pending(retry) → Worker 正常提交 → completed
```

**结论：✅ 安全。** 两种时序都正确。

#### 场景 3：Server 写入中途崩溃

| 崩溃时机 | queue.json | 恢复 |
|---------|-----------|------|
| 写 tmp 中途 | 上次数据完好 | 读 queue.json → 正常 |
| rename 中途 | 上次数据完好（rename 原子） | 读 queue.json → 正常 |
| rename 后 | 新数据 | 读 queue.json → 正常 |

**结论：✅ 安全。**

#### 场景 4：持锁进程崩溃（死锁）

```
Server PID=1234 持锁 → 崩溃 → lock.pid 残留
下一请求 → O_EXCL 失败 → 读 PID=1234 → kill(1234,0) → 进程死亡 → 删除锁 → 重试成功
```

**结论：✅ 安全。** PID 存活检测 + 自动释放过期锁。

### 4.4 安全保证汇总

| # | 保证 | 机制 |
|---|------|------|
| 1 | 不重复分发 | `O_CREAT\|O_EXCL` 原子锁 + 单一 queue.json |
| 2 | 不丢失任务 | tmp + replace 原子写入 |
| 3 | 死锁自恢复 | PID 存活检测 + 时间戳超时强制释放 |
| 4 | 状态一致性 | 读-改-写全程持锁 |
| 5 | 幂等操作 | task_id 去重 + state 检查 |

### 4.5 已知限制

| 限制 | 影响 | 适用范围 |
|------|------|---------|
| NFSv3 不保证原子性 | 但 Server 在本地运行，不使用 NFS | 本地磁盘 / SMB / NFSv4 均安全 |
| 非 Paxos/Raft 共识 | 不支持多 Server HA | 设计目标为单 Server + 多 Worker |
| PID 复用极端情况 | 锁超时等待 5 秒后失败（概率极低） | 加时间戳双重检测缓解 |

## 五、分发包结构

代码随 Skill 分发，一行命令启动：

```
batch-task-skill/
├── SKILL.md                    # Agent 工作流说明书
├── server/
│   ├── python/
│   │   └── server.py           # Python 版（~140 行，stdlib only）
│   └── node/
│       └── server.js           # Node.js 版（~170 行，built-in only）
├── task_types/
│   ├── entity_extraction.yaml
│   ├── code_review.yaml
│   └── batch_translation.yaml
└── README.md
```

### 启动方式

```bash
# Python（零依赖）
python server/python/server.py 5050

# Node.js（零依赖）
node server/node/server.js 5050
```

### 依赖清单

| 版本 | 外部依赖 | 标准库模块 |
|------|---------|-----------|
| Python | **无** | `http.server`, `json`, `os`, `time`, `threading`, `datetime`, `pathlib` |
| Node.js | **无** | `http`, `fs`, `path` |

## 六、完整生命周期（以实体提取为例）

```
T=0s    【Orchestrator】
        1. python server/python/server.py 5050
        2. POST /config { name: "entity_extraction", config: {...} }
        3. POST /batch { task_type: "entity_extraction", tasks: [47个] }

T=2s    【Orchestrator】
        4. fire Worker Agent × 3（每个加载 Skill + Config）
        5. 自由去做其他事

T=5s    【Worker-1】GET /next_task → chunk_0001 (pending → dispatched)
        【Worker-2】GET /next_task → chunk_0002
        【Worker-3】GET /next_task → chunk_0003
        三者互不冲突（文件锁串行化）

T=30s   【Worker-1】POST /result { chunk_0001, result } → completed

T=60s   【Worker-2】POST /result { chunk_0003, type: "INVALID" }
        → Config schema 验证失败 → 400 + 错误详情
        → Worker-2 修正后重新提交 → completed

T=90s   【Worker-3 崩溃】— chunk_0007 停在 dispatched

T=210s  【Server Reaper】chunk_0007 超时 120s → retry=1, 回到 pending

T=215s  【Worker-1】GET /next_task → chunk_0007（自动回收）→ 正常完成

T=20min 【Worker-1】GET /next_task → 204 → 退出
        全部完成

T=21min 【Orchestrator】GET /progress → 47/47, 100%
```

## 七、为什么是 Skill + HTTP Server 而不是 MCP

| 维度 | MCP Server | Skill + HTTP Server |
|------|-----------|-------------------|
| 启动成本 | 需要协议层、SSE 传输 | Markdown 文件 + 一个脚本 |
| 调试 | 需要 MCP Inspector | curl / 浏览器直接看 |
| Agent 兼容性 | 仅 MCP 兼容框架 | 任何能发 HTTP 的 Agent |
| 依赖 | MCP SDK | 无 |
| 自动发现 | 有（但 Server 只有一个，不需要） | Skill 描述 API 即可 |
| 多客户端并发 | SSE | HTTP 天然支持 |

**结论：** 在本场景中，MCP 增加复杂度但不增加价值。Skill 提供工作流指导，HTTP 提供最简单的通信协议。

## 八、纯 Skill 方案的局限（为什么需要 Server）

| 能力 | 纯 Skill | Skill + Server |
|------|---------|---------------|
| 任务拆分 | ✅ Agent 自己拆 | ✅ Server 拆或 Agent 拆 |
| 基本流程 | ✅ 可跑通 demo | ✅ 生产级 |
| **不重复处理** | **❌ 无原子保证** | **✅ 文件锁** |
| **超时回收** | **❌ 无后台线程** | **✅ Reaper 守护线程** |
| **独立验证** | **❌ 自己验证自己** | **✅ Config-driven schema** |
| **崩溃恢复** | **⚠️ 状态不一致** | **✅ 原子写入 + 幂等** |

Server 的核心价值是**工程化质量保障**——三个 Agent 自身无法提供的能力。
