#!/usr/bin/env node
/**
 * Batch Task Server — 零外部依赖
 * 文件持久化 + PID 文件锁 + 分布式并发安全
 *
 * 并发保证:
 *   1. PID 文件锁: fs.openSync('wx') 原子创建
 *   2. 死锁回收: PID 存活检测 + 时间戳超时双重机制
 *   3. 原子写入: tmp + renameSync
 *   4. 幂等操作: 重复添加/提交均安全
 *
 * 用法: node server.js [port]
 */

const http  = require("http");
const fs    = require("fs");
const path  = require("path");

// ── 路径与配置 ──────────────────────────────────
const DATA_DIR    = path.join(process.cwd(), "batch_data");
const QUEUE       = path.join(DATA_DIR, "queue.json");
const LOCK_FILE   = path.join(DATA_DIR, "queue.lock");
const CFGS_DIR    = path.join(DATA_DIR, "configs");
const TIMEOUT     = 120 * 1000;       // 任务超时（毫秒）
const REAPER_MS   = 30 * 1000;       // 回收间隔（毫秒）
const LOCK_TIMEOUT_MS = 5000;        // 获取锁超时
const LOCK_STALE_THRESHOLD = 10000;   // 锁超过此毫秒视为过期

function init() {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.mkdirSync(CFGS_DIR, { recursive: true });
    if (!fs.existsSync(QUEUE))
        fs.writeFileSync(QUEUE, '{"tasks":{}}');
}

// ── PID 文件锁（异步，不阻塞事件循环）──────────
async function acquireLock(timeout = LOCK_TIMEOUT_MS) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
        try {
            // 原子创建锁文件（'wx' = 排他创建，已存在则抛 EEXIST）
            fs.writeFileSync(LOCK_FILE, `${process.pid}|${Date.now()}`, { flag: "wx" });
            return true;
        } catch (e) {
            if (e.code !== "EEXIST") throw e;
            // 双重死锁检测
            if (isStaleLock()) {
                try { fs.unlinkSync(LOCK_FILE); } catch {}
                continue;
            }
            // 非阻塞等待（不冻结事件循环）
            await new Promise(r => setTimeout(r, 50));
        }
    }
    return false;
}

function isStaleLock() {
    /** 双重检测: PID 存活 + 时间戳超时 */
    try {
        const parts = fs.readFileSync(LOCK_FILE, "utf8").split("|");
        const pid = parseInt(parts[0]);
        const ts = parts.length > 1 ? parseInt(parts[1]) : 0;
        // 检测 1: 持锁进程是否存活
        process.kill(pid, 0);
        // 检测 2: 锁持有时间是否超时（防 PID 复用）
        if (Date.now() - ts > LOCK_STALE_THRESHOLD) return true;
        return false;
    } catch { return true; }
}

function releaseLock() {
    try { fs.unlinkSync(LOCK_FILE); } catch {}
}

async function withQueue(fn) {
    /** 带锁的队列操作: 获取锁 → 读取 → 回调修改 → 原子写回 → 释放锁 */
    if (!await acquireLock()) throw new Error("Lock timeout");
    try {
        const data = JSON.parse(fs.readFileSync(QUEUE, "utf8"));
        const result = fn(data);
        // 原子写入: tmp → rename
        const tmp = QUEUE + ".tmp";
        fs.writeFileSync(tmp, JSON.stringify(data));
        fs.renameSync(tmp, QUEUE);
        return result;
    } finally {
        releaseLock();
    }
}

// ── 核心业务逻辑 ─────────────────────────────────
async function addTasks(batchId, taskType, tasks, maxRetries = 3) {
    return await withQueue(data => {
        const now = new Date().toISOString();
        let n = 0;
        for (const t of tasks) {
            if (!data.tasks[t.task_id]) {
                data.tasks[t.task_id] = {
                    task_id: t.task_id, batch_id: batchId, task_type: taskType,
                    payload: t.payload || {}, result: null,
                    state: "pending", retry_count: 0, max_retries: maxRetries,
                    created_at: now, dispatched_at: null,
                    completed_at: null, last_error: null,
                };
                n++;
            }
        }
        return { batch_id: batchId, accepted: n };
    });
}

async function getNextTask() {
    return await withQueue(data => {
        const now = new Date().toISOString();
        for (const t of Object.values(data.tasks)) {
            if (t.state === "pending") {
                t.state = "dispatched";
                t.dispatched_at = now;
                return { task_id: t.task_id, task_type: t.task_type, payload: t.payload };
            }
        }
        return null;
    });
}

async function submitResult(taskId, result) {
    return await withQueue(data => {
        const t = data.tasks[taskId];
        if (!t)     return { data: { task_id: taskId, status: "not_found" }, code: 404 };
        if (t.state === "completed")
            return { data: { task_id: taskId, status: "already_completed" }, code: 200 };
        t.state = "completed";
        t.result = result;
        t.completed_at = new Date().toISOString();
        return { data: { task_id: taskId, status: "completed" }, code: 200 };
    });
}

async function getProgress() {
    return await withQueue(data => {
        const c = {};
        let total = 0;
        for (const t of Object.values(data.tasks)) {
            c[t.state] = (c[t.state] || 0) + 1;
            total++;
        }
        const done = c.completed || 0;
        return {
            total, completed: done, dispatched: c.dispatched || 0,
            pending: c.pending || 0, failed: c.failed || 0,
            permanently_failed: c.permanently_failed || 0,
            progress_percent: total ? Math.round(done / total * 1000) / 10 : 0,
        };
    });
}

async function getStale() {
    return await withQueue(data => {
        const cutoff = Date.now() - TIMEOUT;
        return Object.values(data.tasks)
            .filter(t => t.state === "dispatched" && new Date(t.dispatched_at) < cutoff)
            .map(t => ({ task_id: t.task_id, dispatched_at: t.dispatched_at }));
    });
}

async function getAllResults() {
    return await withQueue(data => {
        const results = {};
        for (const [id, t] of Object.entries(data.tasks)) {
            if (t.state === "completed" && t.result) results[id] = t.result;
        }
        return results;
    });
}

// ── 超时回收（定时器，不阻塞事件循环）──────────
function startReaper() {
    setInterval(async () => {
        try {
            await withQueue(data => {
                const cutoff = Date.now() - TIMEOUT;
                for (const t of Object.values(data.tasks)) {
                    if (t.state !== "dispatched") continue;
                    if (new Date(t.dispatched_at) >= cutoff) continue;
                    t.retry_count++;
                    if (t.retry_count >= t.max_retries) {
                        t.state = "permanently_failed";
                        t.last_error = "Exceeded max retries";
                    } else {
                        t.state = "pending";
                        t.dispatched_at = null;
                    }
                }
            });
        } catch (e) { console.error("[reaper]", e.message); }
    }, REAPER_MS);
}

// ── HTTP Handler ──────────────────────────────────
async function handle(req, res) {
    const body = () => new Promise(resolve => {
        let b = "";
        req.on("data", c => b += c);
        req.on("end", () => { try { resolve(JSON.parse(b)); } catch { resolve({}); } });
    });
    const json = (code, d) => {
        res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify(d));
    };

    if (req.method === "GET") {
        if (req.url === "/health")                      return json(200, { status: "healthy" });
        if (req.url === "/next_task") {
            const t = await getNextTask();
            return t ? json(200, t) : (res.writeHead(204), res.end());
        }
        if (req.url === "/progress")                    return json(200, await getProgress());
        if (req.url === "/stale")                       return json(200, { stale: await getStale() });
        if (req.url === "/results")                     return json(200, await getAllResults());
        if (req.url.startsWith("/task/")) {
            const data = JSON.parse(fs.readFileSync(QUEUE, "utf8"));
            const t = data.tasks[req.url.split("/").pop()];
            return t ? json(200, t) : json(404, { error: "not found" });
        }
        if (req.url.startsWith("/config/")) {
            const p = path.join(CFGS_DIR, req.url.split("/").pop() + ".json");
            try { return json(200, JSON.parse(fs.readFileSync(p, "utf8"))); }
            catch { return json(404, { error: "not found" }); }
        }
        return json(404, { error: "not found" });
    }

    if (req.method === "POST") {
        const b = await body();
        if (req.url === "/batch")
            return json(200, await addTasks(b.batch_id || "", b.task_type || "", b.tasks || []));
        if (req.url === "/result") {
            const r = await submitResult(b.task_id || "", b.result || {});
            return json(r.code, r.data);
        }
        if (req.url === "/config") {
            fs.writeFileSync(
                path.join(CFGS_DIR, `${b.name}.json`),
                JSON.stringify(b.config, null, 2)
            );
            return json(200, { name: b.name, registered: true });
        }
        if (req.url === "/heartbeat") return json(200, { ack: true });
        return json(404, { error: "not found" });
    }

    json(405, { error: "method not allowed" });
}

// ── 启动入口 ─────────────────────────────────────
if (require.main === module) {
    const port = parseInt(process.argv[2]) || 5050;
    init();
    startReaper();
    const srv = http.createServer((req, res) =>
        handle(req, res).catch(e => {
            res.writeHead(500);
            res.end(JSON.stringify({ error: e.message }));
        })
    );
    srv.listen(port, "0.0.0.0", () => {
        console.log(`Batch Task Server → http://localhost:${port}`);
    });
}
