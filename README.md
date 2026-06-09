# Batch Task Skill

> 通用批量任务处理 Skill — 让 Coding Agent 通过 Pull-Push 模式可靠地处理任意类型的批量任务。

Python / Node.js 零外部依赖，一行命令启动，YAML 配置接入新任务类型。

## 适用场景

> 核心价值：**将超出 LLM 单次上下文长度的超大任务，拆分为可并行处理的小任务，突破上下文窗口限制。**

- **批量代码审查** — 50 个文件分发给多个 Agent 并行审查，结果自动汇总
- **批量实体提取** — 长文档拆分为片段，并行提取实体和关系，构建知识图谱
- **批量翻译** — 大量文本片段分发给 Agent 翻译，进度持久化不怕中断
- **批量数据标注** — 数据集拆分为小任务，多个 Agent 并行标注
- **批量测试生成** — 为每个源文件生成单元测试，并行处理

- **批量代码审查** — 50 个文件分发给多个 Agent 并行审查，结果自动汇总
- **批量实体提取** — 长文档拆分为片段，并行提取实体和关系，构建知识图谱
- **批量翻译** — 大量文本片段分发给 Agent 翻译，进度持久化不怕中断
- **批量数据标注** — 数据集拆分为小任务，多个 Agent 并行标注
- **批量测试生成** — 为每个源文件生成单元测试，并行处理

## 优点

- **零依赖** — Python / Node.js 纯标准库，无需 pip install / npm install
- **一行启动** — `python server.py 5050`，无需数据库、消息队列等外部服务
- **并发安全** — PID 文件锁 + 原子写入，多 Worker 不重复处理同一任务
- **崩溃不丢** — 任务进度持久化到磁盘，重启后自动恢复
- **超时回收** — Worker 崩溃后 Server 自动回收卡住的任务重新分发
- **YAML 接入** — 新任务类型只需写一个配置文件，Server 和 Skill 零修改

## 快速开始

### 1. 安装

将 `skills/batch-task/` 目录复制到 Skill 宿主的 skills 目录：

```bash
# OpenCode
cp -r skills/batch-task/ ~/.config/opencode/skills/batch-task/

# Claude Code
cp -r skills/batch-task/ ~/.claude/skills/batch-task/
```

### 2. 启动 Server

```bash
# Python（零依赖）
python ~/.config/opencode/skills/batch-task/server/python/server.py 5050

# 或 Node.js（零依赖）
node ~/.config/opencode/skills/batch-task/server/node/server.js 5050
```

输出：`Batch Task Server → http://localhost:5050`

### 3. 作为 Orchestrator 使用

加载本 Skill 后，按以下流程操作：

```
1. POST /config     → 注册任务类型配置（从 task_types/*.yaml 读取）
2. POST /batch      → 提交批量任务
3. 发射 Worker × N  → 每个 Worker 加载本 Skill 的 Worker 模式
4. GET /progress    → 监控进度
5. GET /results     → 收集结果
```

### 4. 作为 Worker 使用

Worker 循环（加载本 Skill 后自动执行）：

```
GET /next_task → 200(处理) / 204(退出)
  ↓
按 worker_prompt 处理 payload
  ↓
POST /result → 200(成功，继续循环)
```

## 为什么需要这个

Coding Agent 处理批量任务时面临三个核心问题：

| 问题 | 说明 |
|------|------|
| **上下文窗口限制** | 大批量数据无法一次性放入单个 Agent 上下文 |
| **跨进程存活** | Agent 进程崩溃或终端关闭时，进度全部丢失 |
| **并行协调** | 多个 Agent 同时工作时，无法避免重复处理同一任务 |

Batch Task Skill 通过 **Pull-Push 架构** 解决这三个问题：

- **Server 持久化队列** — 任务状态存储在磁盘文件中，重启不丢进度
- **原子队列操作** — 文件锁保证多 Worker 不会拉取到同一个任务
- **超时回收** — Worker 崩溃后，Server 自动回收卡住的任务并重新分发

## 原理

### Pull-Push 架构

```
                    ┌──────────────────────────────┐
   POST /batch      │                              │   GET /progress
──────────────────► │      Task Server (零依赖)     │ ◄─────────────────
                    │  ┌─────────────────────────┐  │
                    │  │ queue.json (原子写入)    │  │
                    │  │ queue.lock (PID 文件锁)   │  │
                    │  │ Reaper (超时回收守护线程) │  │
                    │  └─────────────────────────┘  │
                    └──────┬───────────────────────┘
                           │                         │
              GET /next_task ◄─────────────────────► │ POST /result
                           │                         │
                    ┌──────┴──────┐        ┌───────┴──────┐
                    │  Worker #1  │  ...   │  Worker #N   │
                    └─────────────┘        └──────────────┘
```

**Orchestrator** 只负责编排（提交任务、发射 Worker、监控进度），**Worker** 只负责执行（拉取→处理→提交）。Server 是唯一的真相来源。

### 三层分离设计

| 层 | 职责 | 扩展方式 |
|----|------|---------|
| **Task Type Config (YAML)** | 定义怎么拆、任务长什么样、结果长什么样、Worker 行为 | 写新 YAML |
| **Generic Task Server** | 队列、状态机、超时回收、并发安全 | 零修改 |
| **Worker Skill** | Pull→Process→Push 循环模板 | 零修改 |

接入新任务类型 = **只写一个 YAML 文件**，Server 和 Skill 完全不动。

### 任务状态机

```
创建 → Pending → Dispatched → Completed
                  │              ↑
                  │  超时回收      │  重新分发(retry<max)
                  │  (retry+1)    │
                  └──────┘
                         │
                    retry>=max
                         ↓
                  PermanentlyFailed
```

### 并发安全机制

| 保证 | 机制 | 说明 |
|------|------|------|
| 不重复分发 | `O_CREAT\|O_EXCL` 原子文件锁 | OS 内核级保证，不可能两个进程同时成功 |
| 不丢失任务 | tmp + rename 原子写入 | 崩溃只可能发生在 rename 前后，不存在中间状态 |
| 死锁自恢复 | PID 存活检测 + 时间戳超时双重检测 | 覆盖进程崩溃和 PID 复用两种情况 |
| 状态一致性 | 读-改-写全程持锁 | 修改操作是原子的 |
| 幂等操作 | task_id 去重 + state 检查 | 重复提交安全 |

## 接入新任务类型

只需编写一个 YAML 配置文件，放到 `server/task_types/` 目录：

```yaml
name: my_task_type
description: "描述"

split:
  method: file_per_item
  source: "./data/**/*"

task_input:
  file_path:     { type: string, required: true }
  file_content:  { type: string, required: true }

task_output:
  result: { type: string, required: true }

worker_prompt: |
  处理给定文件，返回结果 JSON。

constraints:
  max_time_per_task: 60
  max_retries: 3
```

可用拆分方法: `text_chunk`, `line_by_line`, `file_per_item`, `json_array`, `csv_rows`, `directory_tree`

完整 Schema 参考 [SKILL.md](skills/batch-task/SKILL.md)。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `POST` | `/batch` | 提交一批任务（幂等） |
| `GET` | `/next_task` | 拉取下一个任务（204=空） |
| `POST` | `/result` | 提交任务结果（幂等） |
| `GET` | `/progress` | 查询整体进度 |
| `GET` | `/results` | 获取所有已完成结果 |
| `GET` | `/task/{id}` | 查询单个任务详情 |
| `POST` | `/config` | 注册任务类型配置 |
| `POST` | `/heartbeat` | Worker 心跳 |

## 文件结构

```
batch-task/
├── SKILL.md                    # Skill 入口（Orchestrator + Worker 工作流）
├── server/
│   ├── python/
│   │   └── server.py           # Python 版（stdlib only）
│   ├── node/
│   │   └── server.js           # Node.js 版（built-in only）
│   └── task_types/
│       ├── entity_extraction.yaml
│       ├── code_review.yaml
│       └── batch_translation.yaml
└── README.md
```

## 依赖

| 版本 | 外部依赖 | 标准库模块 |
|------|---------|-----------|
| Python | **无** | `http.server`, `json`, `os`, `time`, `threading`, `datetime`, `pathlib` |
| Node.js | **无** | `http`, `fs`, `path` |

## License

MIT
