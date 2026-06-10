#!/usr/bin/env python3
"""
Batch Task Server — 零外部依赖
文件持久化 + PID 文件锁 + 分布式并发安全

并发保证:
  1. PID 文件锁: os.O_CREAT | os.O_EXCL 原子创建，OS 级保证
  2. 死锁回收: PID 存活检测 + 时间戳超时双重机制，自动释放过期锁
  3. 原子写入: tmp 文件 + os.replace()，防写入中断损坏
  4. 幂等操作: 重复添加/提交均安全

用法: python server.py [port]
"""

import json
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 日志 ──────────────────────────────────────
def _now():
    """UTC 时间，无 tzinfo（兼容旧代码的 isoformat）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def log(msg):
    """统一日志输出，带时间戳"""
    print(f"[{_now().strftime('%H:%M:%S')}] {msg}")

# ── 路径与配置 ──────────────────────────────────
DATA     = Path(".batch_data")
QUEUE    = DATA / "queue.json"
LOCK     = DATA / "queue.lock"
CFGS     = DATA / "configs"
TIMEOUT  = 120    # 任务超时秒数
REAPER_S = 30     # 回收检查间隔
LOCK_TIMEOUT = 5  # 获取锁超时秒数
LOCK_STALE_THRESHOLD = 10  # 锁文件超过此秒数视为过期


def init():
    """初始化数据目录和队列文件"""
    DATA.mkdir(exist_ok=True)
    CFGS.mkdir(exist_ok=True)
    if not QUEUE.exists():
        QUEUE.write_text('{"tasks":{}}', encoding="utf-8")


# ── PID 文件锁 ──────────────────────────────────
class FileLock:
    """
    基于 O_CREAT | O_EXCL 的跨进程文件锁。
    双重死锁检测:
      1. PID 存活检测 — kill(pid, 0) 判断持锁进程是否存活
      2. 时间戳超时 — 锁持有超过 LOCK_STALE_THRESHOLD 秒强制释放
    覆盖 PID 复用的极端情况。
    """

    def __init__(self, path, timeout=LOCK_TIMEOUT):
        self.path = path
        self.timeout = timeout
        self._held = False

    def acquire(self) -> bool:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                fd = os.open(
                    str(self.path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                os.write(fd, f"{os.getpid()}|{time.time()}".encode())
                os.close(fd)
                self._held = True
                return True
            except FileExistsError:
                if self._is_stale():
                    self.path.unlink()
                    continue
                time.sleep(0.05)
        return False

    def release(self):
        if self._held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._held = False

    def _is_stale(self) -> bool:
        """双重检测: PID 存活 + 时间戳超时"""
        try:
            parts = self.path.read_text().strip().split("|")
            pid = int(parts[0])
            ts = float(parts[1]) if len(parts) > 1 else 0
            # 检测 1: 持锁进程是否存活
            os.kill(pid, 0)  # 存活 → 无异常 / 死亡 → ProcessLookupError
            # 检测 2: 锁持有时间是否超时（防 PID 复用）
            if time.time() - ts > LOCK_STALE_THRESHOLD:
                return True
            return False
        except (ValueError, IndexError, ProcessLookupError,
                FileNotFoundError, PermissionError):
            return True  # 无法解析 → 视为过期

    def __enter__(self):
        assert self.acquire(), f"Lock timeout: {self.path}"
        return self

    def __exit__(self, *a):
        self.release()


# ── 原子文件写入 ─────────────────────────────────
def _atomic_write(path: Path, text: str):
    """原子写入: 临时文件 → rename（POSIX rename / Windows MoveFileExW 均原子）"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ── 队列读写（带锁） ─────────────────────────────
_lock = FileLock(LOCK)


def _read():
    """带锁只读（防止读到写中间状态）"""
    with _lock:
        return json.loads(QUEUE.read_text(encoding="utf-8"))


def _write(fn):
    """带锁修改: 读取 → 回调 → 原子写回"""
    with _lock:
        data = json.loads(QUEUE.read_text(encoding="utf-8"))
        result = fn(data)
        _atomic_write(QUEUE, json.dumps(data, ensure_ascii=False))
        return result


# ── 核心业务逻辑 ─────────────────────────────────
def add_tasks(batch_id, task_type, tasks, max_retries=3):
    """批量添加任务（幂等: 已存在的 task_id 自动跳过）"""

    def fn(data):
        now = _now().isoformat()
        count = 0
        for t in tasks:
            tid = t["task_id"]
            if tid not in data["tasks"]:
                data["tasks"][tid] = {
                    "task_id": tid,
                    "batch_id": batch_id,
                    "task_type": task_type,
                    "payload": t.get("payload", {}),
                    "result": None,
                    "state": "pending",
                    "retry_count": 0,
                    "max_retries": max_retries,
                    "created_at": now,
                    "dispatched_at": None,
                    "completed_at": None,
                    "last_error": None,
                }
                count += 1
        return count

    n = _write(fn)
    log(f"BATCH  +{n} tasks | batch={batch_id} type={task_type} total={len(tasks)}")
    return {"batch_id": batch_id, "accepted": n}


def get_next_task():
    """原子出队: 取首个 pending 任务，标记 dispatched"""

    def fn(data):
        now = _now().isoformat()
        for t in data["tasks"].values():
            if t["state"] == "pending":
                t["state"] = "dispatched"
                t["dispatched_at"] = now
                return {
                    "task_id": t["task_id"],
                    "task_type": t["task_type"],
                    "payload": t["payload"],
                }
        return None

    task = _write(fn)
    if task:
        log(f"DISPATCH task_id={task['task_id']} type={task['task_type']}")
    else:
        log("QUEUE  empty (no pending tasks)")
    return task


def submit_result(task_id, result):
    """提交结果（幂等: 已完成任务不覆盖）"""

    def fn(data):
        t = data["tasks"].get(task_id)
        if not t:
            return ("not_found", 404)
        if t["state"] == "completed":
            return ("already_completed", 200)
        t["state"] = "completed"
        t["result"] = result
        t["completed_at"] = _now().isoformat()
        return ("completed", 200)

    status, code = _write(fn)
    log(f"RESULT  task_id={task_id} status={status} code={code}")
    return {"task_id": task_id, "status": status}, code


def get_progress():
    """进度统计"""

    def fn(data):
        counts = {}
        total = 0
        for t in data["tasks"].values():
            counts[t["state"]] = counts.get(t["state"], 0) + 1
            total += 1
        done = counts.get("completed", 0)
        return {
            "total": total,
            "completed": done,
            "dispatched": counts.get("dispatched", 0),
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "permanently_failed": counts.get("permanently_failed", 0),
            "progress_percent": round(done / total * 100, 1) if total else 0,
        }

    return _write(fn)


def get_task(task_id):
    """查询单个任务详情"""
    return _read()["tasks"].get(task_id)


def register_config(name, config):
    """注册任务类型配置"""
    (CFGS / f"{name}.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"name": name, "registered": True}
    log(f"CONFIG registered: {name}")


def get_config(name):
    """读取任务类型配置"""
    p = CFGS / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def get_stale():
    """查询超时卡住的任务"""

    def fn(data):
        cutoff = (_now() - timedelta(seconds=TIMEOUT)).isoformat()
        return [
            {"task_id": t["task_id"], "dispatched_at": t["dispatched_at"]}
            for t in data["tasks"].values()
            if t["state"] == "dispatched" and t["dispatched_at"] < cutoff
        ]

    return _write(fn)


def get_all_results():
    """获取所有已完成任务的结果"""

    def fn(data):
        return {
            tid: t["result"]
            for tid, t in data["tasks"].items()
            if t["state"] == "completed" and t["result"]
        }

    return _write(fn)


# ── 超时回收守护线程 ─────────────────────────────
def reaper():
    """定期回收超时的 dispatched 任务"""

    while True:
        time.sleep(REAPER_S)

        def _reap(data):
            cutoff = (_now() - timedelta(seconds=TIMEOUT)).isoformat()
            reaped = 0
            for t in data["tasks"].values():
                if t["state"] != "dispatched" or t["dispatched_at"] >= cutoff:
                    continue
                t["retry_count"] += 1
                if t["retry_count"] >= t["max_retries"]:
                    t["state"] = "permanently_failed"
                    t["last_error"] = "Exceeded max retries"
                    log(f"REAPER  FAIL task_id={t['task_id']} retries={t['retry_count']}")
                else:
                    t["state"] = "pending"
                    t["dispatched_at"] = None
                    log(f"REAPER  REQUEUE task_id={t['task_id']} retry={t['retry_count']}")
                reaped += 1
            if reaped:
                log(f"REAPER  {reaped} stale tasks reclaimed")

        try:
            _write(_reap)
        except Exception as e:
            log(f"REAPER  error: {e}")


# ── HTTP Handler ──────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _j(self, code, d):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(d, ensure_ascii=False).encode())

    def do_GET(self):
        if self.path == "/health":
            return self._j(200, {"status": "healthy"})
        if self.path == "/next_task":
            t = get_next_task()
            if t:
                return self._j(200, t)
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/progress":
            return self._j(200, get_progress())
        if self.path == "/stale":
            return self._j(200, {"stale": get_stale()})
        if self.path == "/results":
            return self._j(200, get_all_results())
        if self.path.startswith("/task/"):
            t = get_task(self.path.split("/")[-1])
            return self._j(200, t) if t else self._j(404, {"error": "not found"})
        if self.path.startswith("/config/"):
            c = get_config(self.path.split("/")[-1])
            return self._j(200, c) if c else self._j(404, {"error": "not found"})
        self._j(404, {"error": "not found"})

    def do_POST(self):
        b = self._body()
        if self.path == "/batch":
            return self._j(200, add_tasks(
                b.get("batch_id", ""), b.get("task_type", ""), b.get("tasks", [])
            ))
        if self.path == "/result":
            d, c = submit_result(b.get("task_id", ""), b.get("result", {}))
            return self._j(c, d)
        if self.path == "/config":
            return self._j(200, register_config(b.get("name", ""), b.get("config", {})))
        if self.path == "/heartbeat":
            return self._j(200, {"ack": True})
        self._j(404, {"error": "not found"})

    def log_message(self, code, size):
        """HTTP 请求日志（心跳不输出）"""
        if self.path == "/health":
            return
        log(f"HTTP {self.command} {self.path} → {code} ({size}B)")

# ── 启动入口 ─────────────────────────────────────
if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
    init()
    log(f"Batch Task Server → http://localhost:{port}")
    log(f"Data: {DATA.resolve()} | Lock timeout: {LOCK_TIMEOUT}s | Task timeout: {TIMEOUT}s")
    threading.Thread(target=reaper, daemon=True).start()
    log("Reaper daemon started")
    srv = HTTPServer(("0.0.0.0", port), Handler)
    log(f"Listening on 0.0.0.0:{port}")
    srv.serve_forever()
