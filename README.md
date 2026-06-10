# Batch Task Skill

<p>
  <img src="https://img.shields.io/badge/Node.js-built%20in%20only-339933?logo=node.js&logoColor=white" alt="Node.js built-in" />
  <img src="https://img.shields.io/badge/zero-dependencies-success" alt="Zero dependencies" />
  <img src="https://img.shields.io/badge/file-lock%20concurrency-safe-brightgreen" alt="Concurrency safe" />
  <img src="https://img.shields.io/badge/crash-safe-orange" alt="Crash safe" />
</p>

> 面向 AI Agent 的零依赖批量任务服务器。拆分、分发、收集。一个 YAML 接入新任务。纯标准库。

⚡ **将超出 LLM 单次上下文长度的超大任务，拆分为可并行处理的小任务，突破上下文窗口限制。**

## 适用场景

- 📂 **批量代码审查** — 50 个文件分发给多个 Agent 并行审查，结果自动汇总
- 🔗 **批量实体提取** — 长文档拆分为片段，并行提取实体和关系，构建知识图谱
- 🌐 **批量翻译** — 大量文本片段分发给 Agent 翻译，进度持久化不怕中断
- 🏷️ **批量数据标注** — 数据集拆分为小任务，多个 Agent 并行标注
- 🧪 **批量测试生成** — 为每个源文件生成单元测试，并行处理

## 优点

- 🚫 **零依赖** — Node.js 纯内置模块，无需 npm install
- 🏃 **一行启动** — `node server.js 5050`，无需数据库、消息队列等外部服务
- 🔒 **并发安全** — PID 文件锁 + 原子写入，多 Worker 不重复处理同一任务
- 💾 **崩溃不丢** — 任务进度持久化到磁盘，重启后自动恢复
- ⏰ **超时回收** — Worker 崩溃后 Server 自动回收卡住的任务重新分发
- 📝 **YAML 接入** — 新任务类型只需写一个配置文件，Server 和 Skill 零修改

## 快速开始

```bash
# 安装
npx skills add Being09/agent-batch-task-server

# 启动 Server（后台运行）
# Windows:
Start-Process node -ArgumentList "server/node/server.js","5050" -WindowStyle Hidden
# Linux/macOS:
node server/node/server.js 5050 &
```

> **Windows**: `Start-Process ... -WindowStyle Hidden` 脱离终端运行。
> **Linux/macOS**: `&` 后台运行，或用 Agent 的 `run_in_background=true`。
> **不要用** `Start-Process -NoNewWindow`（会阻塞）。

## 工作流程

```
Orchestrator                    Task Server                    Workers
    │                               │                            │
    ├── POST /config ────────────→  │  注册任务类型                │
    ├── POST /batch ─────────────→  │  提交批量任务               │
    ├── 发射 Worker × N ─────────→  │                            │
    │                               │  ←── GET /next_task ────── Worker 拉取
    │                               │                            │  处理 payload
    │                               │  ──── POST /result ──────→  Worker 提交
    ├── GET /progress ────────────→  │  监控进度                   │
    ├── GET /results ─────────────→  │  收集结果                   │
    ├── POST /shutdown ───────────→  │  关闭服务器                 │
    │                               │                            │
    └── 保存到 .batch_data/results.json
```

详细步骤、Worker 循环、错误处理 → 参考 [SKILL.md](skills/batch-task/SKILL.md)

## 原理

Pull-Push 架构 + 三层分离设计：

| 层 | 职责 | 扩展方式 |
|----|------|---------|
| **Task Type Config (YAML)** | 怎么拆、任务长什么样、Worker 行为 | 写新 YAML |
| **Generic Task Server** | 队列、状态机、超时回收、并发安全 | 零修改 |
| **Worker Skill** | Pull→Process→Push 循环模板 | 零修改 |

**并发安全**：`O_CREAT|O_EXCL` 原子文件锁 + tmp/rename 原子写入 + PID 存活检测死锁恢复。

**任务状态**：`Pending → Dispatched → Completed`（超时自动回收到 pending，超过重试上限则 `PermanentlyFailed`）

接入新任务类型 = **只写一个 YAML**。完整 Schema、API、示例 → [SKILL.md](skills/batch-task/SKILL.md)

## 文件结构

```
batch-task/
├── SKILL.md                    # Skill 入口（详细工作流、API、错误处理）
├── server/
│   ├── node/
│   │   └── server.js           # Node.js Server（built-in only）
│   └── task_types/
│       ├── entity_extraction.yaml
│       ├── code_review.yaml
│       └── batch_translation.yaml
└── README.md
```

## 依赖

| 版本 | 外部依赖 | 内置模块 |
|------|---------|---------|
| Node.js | **无** | `http`, `fs`, `path` |

## License

MIT
