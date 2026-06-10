---
name: batch-task
description: "Zero-dependency batch task server for AI agents. Split, dispatch, collect with file-lock concurrency safety. New task type = one YAML."
---

# Batch Task Worker & Orchestrator

> 通用批量任务处理 Skill — 让 Coding Agent 通过 Pull-Push 模式可靠地处理任意类型的批量任务。

## 概述

本 Skill 将大批量任务拆解为独立小任务，通过 HTTP Pull-Push 协议在 Task Server 和 Worker Agent 之间协作：

```
Worker Agent ──GET /next_task──→ Task Server ──POST /result──← Worker Agent
```

Task Server 管理队列、状态机、超时回收、进度持久化。Agent 只负责 pull → process → push 循环。

## 前置条件

- Python 3.8+ 或 Node.js 16+（二选一）
- Task Type Config 文件（YAML 格式）

## 分发内容

```
batch-task-skill/
├── SKILL.md                    # 本文件
├── server/
│   ├── python/
│   │   └── server.py           # Python 版 Server（~200 行，stdlib only）
│   ├── node/
│   │   └── server.js           # Node.js 版 Server（~220 行，built-in only）
│   └── task_types/
│       ├── entity_extraction.yaml
│       ├── code_review.yaml
│       └── batch_translation.yaml
└── README.md
```

## 模式一：Orchestrator（编排者）

### 步骤

1. **后台启动 Task Server**（不阻塞主 Agent 流程）

   Agent 应使用自身的后台执行机制启动 Server，而非依赖 shell 后台语法：

   | Agent 宿主 | 后台启动方式 |
   |-----------|-------------|
   | OpenCode / Claude Code | 使用 Bash/Shell 工具的 `run_in_background=true` 参数 |
   | 手动启动 | 新开终端窗口运行 |

   ```bash
   # Python（零依赖）
   # Windows PowerShell:
   Start-Process python -ArgumentList "{skill_path}/server/python/server.py","5050" -WindowStyle Hidden
   # Linux/macOS:
   python {skill_path}/server/python/server.py 5050 &

   # 或 Node.js（零依赖）
   # Windows PowerShell:
   Start-Process node -ArgumentList "{skill_path}/server/node/server.js","5050" -WindowStyle Hidden
   # Linux/macOS:
   node {skill_path}/server/node/server.js 5050 &
   ```

   > **Windows**: 使用 `Start-Process ... -WindowStyle Hidden` 脱离终端运行。
   > **Linux/macOS**: 使用 `&` 后台运行，或用 Agent 的 `run_in_background=true`。
   > **不要用** `Start-Process -NoNewWindow`（会阻塞调用者）。

2. **等待 Server 就绪**
   ```bash
   # 轮询健康检查直到 Server 可用（最多等待 10 秒）
   GET http://localhost:5050/health → {"status": "healthy"}
   ```

3. **注册任务类型配置**
   ```
   POST http://localhost:5050/config
   {
     "name": "entity_extraction",
     "config": { /* 读取 task_types/xxx.yaml 的内容 */ }
   }
   ```

4. **提交批量任务**
   ```
   POST http://localhost:5050/batch
   {
     "batch_id": "batch_001",
     "task_type": "entity_extraction",
     "tasks": [
       { "task_id": "chunk_001", "payload": { "content": "文本片段1" } },
       { "task_id": "chunk_002", "payload": { "content": "文本片段2" } }
     ]
   }
   ```

5. **发射 Worker Agent**
   - 使用 `run_in_background=true` 发射多个 Worker
   - 每个 Worker 加载本 Skill 的「模式二」
   - Worker 数量根据任务量和 Agent 资源决定

6. **监控进度**
   ```
   GET http://localhost:5050/progress → {"total": 100, "completed": 45, ...}
   ```
   每 30 秒查询一次，直到 `progress_percent == 100`

7. **收集结果**
   ```
   GET http://localhost:5050/results → { "chunk_001": {...}, "chunk_002": {...} }
   ```

8. **关闭服务器**
   所有任务完成后，调用 shutdown 端点优雅关闭 Server：
   ```
   POST http://localhost:5050/shutdown → {"status": "shutting_down"}
   ```


---

## 模式二：Worker（执行者）

### 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `server_url` | Task Server 地址 | `http://localhost:5050` |
| `task_type_config` | 任务类型配置文件路径 | — |

### 工作循环（严格遵守）

```
┌─────────────────────────────────────┐
│  Step 1: GET {server_url}/next_task │
│           ├─ 200 → 解析 task_id     │
│           │       + payload         │
│           └─ 204 → 退出（全部完成） │
├─────────────────────────────────────┤
│  Step 2: 按 worker_prompt 处理      │
│           使用 LLM 处理 payload     │
├─────────────────────────────────────┤
│  Step 3: 自验证                     │
│           检查结果是否符合 schema    │
├─────────────────────────────────────┤
│  Step 4: POST {server_url}/result   │
│           { task_id, result }       │
│           ├─ 200 → 成功，回到 Step 1 │
│           ├─ 400 → 阅读错误，修正   │
│           └─ 5xx → 等 5 秒重试       │
└─────────────────────────────────────┘
```

### 任务指令（从 Config 注入）

> 你是{task_type.name}专家。
> {task_type.worker_prompt}
> 严格按照 {task_type.task_output} schema 返回 JSON 格式结果。

### 输出格式约束（从 Config 注入）

- 必须返回合法 JSON
- 必须包含所有 `required: true` 的字段
- 枚举值必须在 `enum` 范围内
- 数值必须在 `range` 范围内

### 错误处理

| 错误 | 处理方式 |
|------|---------|
| 5xx 服务器错误 | 等待 5 秒后重试（最多 3 次） |
| 4xx 客户端错误 | 记录错误内容，继续下一个任务 |
| 处理失败 | 提交部分结果，标记失败 |
| 处理超时 | 不提交，Server 会自动回收 |

---

## API 速查

| 方法 | 路径 | 说明 | 调用者 |
|------|------|------|--------|
| `GET` | `/health` | 健康检查 | 任意 |
| `POST` | `/batch` | 提交一批任务（幂等） | Orchestrator |
| `GET` | `/next_task` | 拉取下一个任务（204=空） | Worker |
| `POST` | `/result` | 提交任务结果（幂等） | Worker |
| `GET` | `/progress` | 查询整体进度 | Orchestrator |
| `GET` | `/results` | 获取所有已完成结果 | Orchestrator |
| `GET` | `/task/{id}` | 查询单个任务详情 | 任意 |
| `GET` | `/stale` | 查询超时卡住的任务 | Orchestrator |
| `POST` | `/config` | 注册任务类型配置 | Orchestrator |
| `GET` | `/config/{name}` | 获取已注册配置 | Worker |
| `POST` | `/heartbeat` | Worker 心跳 | Worker |

---

## 接入新任务类型

只需编写一个 YAML 配置文件，Server 和 Skill 零修改：

```yaml
name: my_task_type
description: "描述"

split:
  method: file_per_item
  source: "./data/**/*"

task_input:
  file_path: { type: string, required: true }
  file_content: { type: string, required: true }

task_output:
  result: { type: string, required: true }

worker_prompt: |
  处理给定文件，返回结果。

constraints:
  max_time_per_task: 60
  max_retries: 3
```

可用拆分方法: `text_chunk`, `line_by_line`, `file_per_item`, `json_array`, `csv_rows`, `directory_tree`

---

## 工程化质量保障

| 保证 | 机制 |
|------|------|
| 不重复分发 | `O_CREAT\|O_EXCL` 原子文件锁 |
| 不丢失任务 | tmp + rename 原子写入 |
| 死锁自恢复 | PID 存活检测 + 时间戳超时双重检测 |
| 状态一致性 | 读-改-写全程持锁 |
| 幂等操作 | task_id 去重 + state 检查 |
| 超时回收 | 后台守护线程/定时器自动回收 dispatched 任务 |
