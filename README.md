# Batch Task Skill

<p>
  <img src="https://img.shields.io/badge/Node.js-built%20in%20only-339933?logo=node.js&logoColor=white" alt="Node.js built-in" />
  <img src="https://img.shields.io/badge/zero-dependencies-success" alt="Zero dependencies" />
  <img src="https://img.shields.io/badge/file-lock%20concurrency-safe-brightgreen" alt="Concurrency safe" />
  <img src="https://img.shields.io/badge/crash-safe-orange" alt="Crash safe" />
</p>

> 面向 AI Agent 的零依赖批量任务服务器。拆分、分发、收集。一个 YAML 接入新任务。纯标准库。

⚡ **将超出 LLM 单次上下文长度的超大任务，拆分为可并行处理的小任务，突破上下文窗口限制。**

---

<!-- FOR HUMANS -->

## 这是什么

一个 Skill + 零依赖 HTTP Server，让 AI Agent 能**可靠地并行处理批量任务**。

### 解决什么问题

单个 Agent 处理批量任务时：上下文窗口放不下、崩溃就全丢、多个 Agent 会重复干活。这个 Skill 通过一个持久化队列 Server 把任务拆开、分发、收集，Worker 崩了自动回收，进度不丢。

### 适用场景

- 📂 **批量代码审查** — 50 个文件分发给多个 Agent 并行审查
- 🔗 **批量实体提取** — 长文档拆分后并行提取知识图谱
- 🌐 **批量翻译** — 大量文本并行翻译，不怕中断
- 🏷️ **批量数据标注** — 数据集拆分后多 Agent 并行标注
- 🧪 **批量测试生成** — 为每个源文件并行生成单元测试

### 怎么工作

```
Orchestrator（你）──提交任务──→ Server（队列）──分发──→ Workers（并行处理）
                                   ↕ 持久化              ↕ 崩溃自动回收
                               ←──收集结果──←─────────────
```

**三层分离**：YAML 配置定义任务类型 → Server 管理队列和并发 → Worker Skill 模板执行循环。接入新任务 = 写一个 YAML。

### 技术特点

- 🚫 **零依赖** — Node.js 内置模块，不装任何东西
- 🔒 **并发安全** — 文件锁原子操作，多 Worker 不重复
- 💾 **崩溃不丢** — 进度持久化到 `.batch_data/`
- ⏰ **超时回收** — Worker 挂了任务自动重新分发
- 📝 **YAML 接入** — 新任务类型只写配置

### 安装

```bash
npx skills add Being09/agent-batch-task-server
```

### 文件结构

```
batch-task/
├── SKILL.md              # Agent 工作流说明书（LLM 阅读此文件）
├── server/
│   ├── node/server.js    # 零依赖 Task Server
│   └── task_types/*.yaml  # 任务类型配置（可扩展）
└── README.md
```

### 运行时产物

所有运行时数据存放在 `.batch_data/`（已 gitignore）：

```
.batch_data/
├── queue.json       # 任务队列（持久化）
├── server.log       # 运行日志
└── results.json     # Orchestrator 保存的最终结果
```

### License

MIT

---

<!-- LLM-ONLY-START -->

## LLM Instructions

你是 Orchestrator 或 Worker。加载本 Skill 后，按以下指令执行。

### 模式一：Orchestrator

1. **启动 Server**（后台，不阻塞）
   - Windows: `Start-Process node -ArgumentList "{skill_path}/server/node/server.js","5050" -WindowStyle Hidden`
   - Linux/macOS: `node {skill_path}/server/node/server.js 5050 &`
   - 禁止使用 `Start-Process -NoNewWindow`（会阻塞）

2. **等待就绪** — `GET http://localhost:5050/health` → `{"status":"healthy"}`

3. **注册配置** — `POST /config`，读取 `server/task_types/*.yaml`

4. **提交任务** — `POST /batch { batch_id, task_type, tasks: [...] }`

5. **发射 Worker** — `run_in_background=true`，每个 Worker 加载本 Skill 模式二

6. **监控进度** — `GET /progress`，每 30 秒查询，直到 100%

7. **收集结果** — `GET /results`，保存到 `.batch_data/results.json`

8. **关闭 Server** — `POST /shutdown`

### 模式二：Worker

循环执行直到 `GET /next_task` 返回 204：

```
GET /next_task → 200: 处理 payload → POST /result → 继续
GET /next_task → 204: 退出
```

- 5xx: 等 5 秒重试（最多 3 次）
- 4xx: 记录错误，跳过
- 处理失败: 提交部分结果

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/batch` | 提交任务（幂等） |
| `GET` | `/next_task` | 拉取任务（204=空） |
| `POST` | `/result` | 提交结果（幂等） |
| `GET` | `/progress` | 查询进度 |
| `GET` | `/results` | 收集结果 |
| `GET` | `/health` | 健康检查 |
| `POST` | `/shutdown` | 关闭 Server |

### YAML Config Schema

```yaml
name: task_type_name
description: "描述"
split:
  method: text_chunk        # text_chunk | line_by_line | file_per_item | json_array | csv_rows | directory_tree
  source: "./data/*.txt"
  params:
    chunk_size: 2000
    overlap: 200
task_input:
  content: { type: string, required: true }
task_output:
  result: { type: string, required: true }
worker_prompt: |
  你的任务指令...
constraints:
  max_time_per_task: 60
  max_retries: 3
```

<!-- LLM-ONLY-END -->
