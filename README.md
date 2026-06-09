# Agent Batch Task Server

通用化的 Pull-Push 架构实现：让 Coding Agent 可靠地处理批量任务。

## 核心思想

将大批量任务拆解为独立的小任务，通过 HTTP Pull-Push 协议在 Task Server 和 Worker Agent 之间协作：

```
Worker Agent ──GET /next_task──→ Task Server ──POST /result──← Worker Agent
```

Server 管理队列、状态机、超时回收、进度持久化；Agent 只负责 pull → process → push 循环。

## 解决的三个核心问题

| 问题 | 说明 |
|------|------|
| Context window 限制 | 总数据量不受单次上下文窗口限制 |
| 跨进程存活 | 进度持久化到磁盘，进程/终端/机器重启不丢进度 |
| 并行协调 | 服务端状态机是唯一真相来源，多 Worker 不会重复处理 |

## 设计文档

- [通用批量任务服务器架构设计](docs/generic-batch-server-design.md) — 三层分离设计、Task Type Config、文件锁并发安全、Python/Node.js 零依赖实现
## 项目结构

```
agent-batch-task-server/
├── docs/
│   └── generic-batch-server-design.md
├── skills/
│   └── batch-task/                # ← 可分发 Skill 包（整体复制到 ~/.config/opencode/skills/）
│       ├── SKILL.md                 # Skill 入口
│       └── server/
│           ├── python/
│           │   └── server.py          # Python 版（stdlib only）
│           ├── node/
│           │   └── server.js          # Node.js 版（built-in only）
│           └── task_types/
│               ├── entity_extraction.yaml
│               ├── code_review.yaml
│               └── batch_translation.yaml
└── README.md
```
