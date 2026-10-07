#!/usr/bin/env python3
"""深空地面站验收器（compose 中的 verify 服务）。

围绕三项核心场景完成校验，并以退出码报告验收结果：
  A. 断回应恢复：提交后断连 → 重启 → 凭原标识恢复已持久化的后继，代次不再推进
  B. 并发同标识：两个并发相同请求只观察到同一结果
  C. 异标识重放：撤销授权族并给出原因，后继凭证随后同样被拒绝
另含：代码单元测试、API/HTTP 冒烟、页面可观察结果校验。

环境变量：
  APP_BASE_URL  被测服务地址（默认 http://127.0.0.1:8080）
  APP_DIR       应用源码目录（用于跑单元测试，默认 ../app）
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
APP_DIR = os.environ.get(
    "APP_DIR",
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")),
)
SFX = uuid.uuid4().hex[:8]  # 每次运行使用全新终端标识，避免与卷中历史数据冲突

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def req(method, path, payload=None, timeout=10):
    """返回 (status, json)。传输层错误（如断回应）直接抛异常。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def get_text(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def wait_health(timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status, body = req("GET", "/health", timeout=3)
            if status == 200 and body.get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def wait_down(timeout_s):
    """等待服务真正退出：连续两次健康检查失败才算已下线（轮询间隔需远小于重启窗口）。"""
    deadline = time.time() + timeout_s
    failures = 0
    while time.time() < deadline:
        try:
            status, _ = req("GET", "/health", timeout=2)
            if status == 200:
                failures = 0
            else:
                failures += 1
        except Exception:
            failures += 1
        if failures >= 2:
            return True
        time.sleep(0.15)
    return False


def rotate(terminal, credential, rotation_id):
    return req("POST", "/api/rotate", {
        "terminal_id": terminal,
        "credential": credential,
        "rotation_id": rotation_id,
    })


def create_family(terminal):
    return req("POST", "/api/families", {"terminal_id": terminal})


# ---------------------------------------------------------------- 单元测试

def step_unit_tests():
    print("\n== 1/6 代码单元测试 ==", flush=True)
    tests_dir = os.path.join(APP_DIR, "tests")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", tests_dir, "-v"],
        capture_output=True, text=True, cwd=APP_DIR,
    )
    tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-6:])
    print(tail, flush=True)
    record("单元测试（创建/轮换/重放/重启恢复/并发/撤销）", proc.returncode == 0)


# ---------------------------------------------------------------- 冒烟

def step_smoke():
    print("\n== 2/6 API/HTTP 冒烟 ==", flush=True)
    try:
        status, body = req("GET", "/health")
        record("GET /health 返回健康", status == 200 and body.get("status") == "ok",
               f"HTTP {status} {body}")
    except Exception as e:
        return record("GET /health 返回健康", False, repr(e))

    terminal = f"term-smoke-{SFX}"
    status, fam = create_family(terminal)
    ok = (status == 201 and fam.get("credential", "").startswith("rft_")
          and fam.get("generation") == 1 and fam.get("family_status") == "active")
    record("POST /api/families 创建授权族（代次 1，可用）", ok, f"HTTP {status}")
    if not ok:
        return

    status, rot = rotate(terminal, fam["credential"], f"rot-smoke-{SFX}")
    ok = (status == 200 and rot.get("outcome") == "accepted"
          and rot.get("generation") == 2
          and rot.get("credential") not in (None, fam["credential"]))
    record("POST /api/rotate 首次轮换被接受（代次递增）", ok, f"HTTP {status}")

    status, page = get_text("/")
    record("GET / 操作员页面可访问", status == 200 and "深空地面站" in page, f"HTTP {status}")


# ------------------------------------------------------- 场景 A：断回应恢复

def step_disconnect_recovery():
    print("\n== 3/6 场景 A：断回应（提交后断连）+ 重启恢复 ==", flush=True)
    terminal = f"term-drop-{SFX}"
    rid = f"rot-drop-{SFX}"
    status, fam = create_family(terminal)
    if status != 201:
        return record("场景A 前置：创建授权族", False, f"HTTP {status}")
    cred0 = fam["credential"]

    status, faults = req("POST", "/api/admin/faults", {"drop_after_commit": True})
    record("启用断回应故障", status == 200 and faults.get("drop_after_commit") is True)

    dropped = False
    try:
        rotate(terminal, cred0, rid)
    except Exception as e:
        dropped = True
        print(f"       首次请求连接中断（符合预期）：{type(e).__name__}: {e}", flush=True)
    record("断回应：首次轮换请求未送达响应", dropped)

    try:
        req("POST", "/api/admin/shutdown")
    except Exception:
        pass  # 进程退出可能先于响应送达
    record("服务进程已退出（等待下线确认）", wait_down(15))
    record("服务进程重启后恢复健康", wait_health(30))

    status, r1 = rotate(terminal, cred0, rid)
    ok = (status == 200 and r1.get("outcome") == "replayed"
          and r1.get("generation") == 2
          and r1.get("credential", "").startswith("rft_"))
    record("重启后凭原标识恢复已持久化的后继凭证（重放）", ok,
           f"HTTP {status} outcome={r1.get('outcome')} gen={r1.get('generation')}")
    if not ok:
        return
    successor = r1["credential"]

    status, r2 = rotate(terminal, cred0, rid)
    ok = (status == 200 and r2.get("outcome") == "replayed"
          and r2.get("credential") == successor and r2.get("generation") == 2)
    record("再次重传结果完全一致且代次不推进", ok, f"HTTP {status}")

    status, view = req("GET", f"/api/terminals/{terminal}/family")
    record("授权族代次保持为 2（不再推进）、状态可用",
           status == 200 and view.get("generation") == 2
           and view.get("family_status") == "active")

    status, r3 = rotate(terminal, successor, f"rot-drop-next-{SFX}")
    record("恢复的后继凭证仍可正常轮换（代次 → 3）",
           status == 200 and r3.get("outcome") == "accepted" and r3.get("generation") == 3)


# ------------------------------------------------------- 场景 B：并发同标识

def step_concurrent_same_id():
    print("\n== 4/6 场景 B：两个并发相同请求 ==", flush=True)
    terminal = f"term-conc-{SFX}"
    rid = f"rot-conc-{SFX}"
    status, fam = create_family(terminal)
    if status != 201:
        return record("场景B 前置：创建授权族", False, f"HTTP {status}")
    cred0 = fam["credential"]

    barrier = threading.Barrier(2)

    def call():
        barrier.wait()
        return rotate(terminal, cred0, rid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        (s1, b1), (s2, b2) = list(pool.map(lambda _: call(), range(2)))

    ok = s1 == 200 and s2 == 200
    record("两个并发请求均成功返回", ok, f"HTTP {s1}/{s2}")

    same = (b1.get("credential") == b2.get("credential")
            and b1.get("generation") == b2.get("generation") == 2)
    record("两者观察到同一后继凭证与代次", same,
           f"outcomes={b1.get('outcome')},{b2.get('outcome')} gen={b1.get('generation')}")

    record("恰好一次接受、一次重放",
           sorted([b1.get("outcome"), b2.get("outcome")]) == ["accepted", "replayed"])

    status, view = req("GET", f"/api/terminals/{terminal}/family")
    record("并发后授权族代次恰为 2（仅推进一次）",
           status == 200 and view.get("generation") == 2)


# ------------------------------------------------- 场景 C：异标识重放 → 撤销

def step_reuse_revocation():
    print("\n== 5/6 场景 C：异标识重放 → 授权族撤销 ==", flush=True)
    terminal = f"term-reuse-{SFX}"
    status, fam = create_family(terminal)
    if status != 201:
        return record("场景C 前置：创建授权族", False, f"HTTP {status}")
    cred0 = fam["credential"]

    status, r1 = rotate(terminal, cred0, f"rot-a-{SFX}")
    ok = status == 200 and r1.get("outcome") == "accepted"
    record("首次轮换被接受", ok, f"HTTP {status}")
    if not ok:
        return
    cred1 = r1["credential"]

    status, r2 = rotate(terminal, cred0, f"rot-b-DIFFERENT-{SFX}")
    ok = (status == 409 and r2.get("outcome") == "revoked"
          and "reuse" in (r2.get("revocation_reason") or ""))
    record("旧凭证 + 不同轮换标识 → 授权族撤销并给出原因", ok,
           f"HTTP {status} reason={r2.get('revocation_reason')}")

    status, r3 = rotate(terminal, cred1, f"rot-c-{SFX}")
    ok = status == 409 and r3.get("outcome") == "revoked"
    record("此前签发的后继凭证随后同样被拒绝", ok, f"HTTP {status}")

    status, r4 = rotate(terminal, cred0, f"rot-a-{SFX}")
    record("撤销后原（旧凭证, 原轮换标识）重放亦被拒绝",
           status == 409 and r4.get("outcome") == "revoked", f"HTTP {status}")

    status, view = req("GET", f"/api/terminals/{terminal}/family")
    ok = (status == 200 and view.get("family_status") == "revoked"
          and "reuse" in (view.get("revocation_reason") or ""))
    record("状态查询可见撤销状态与原因", ok, f"HTTP {status}")


# ------------------------------------------------------- 页面可观察结果

def step_page_observability():
    print("\n== 6/6 页面可观察结果校验 ==", flush=True)
    try:
        status, html = get_text("/")
    except Exception as e:
        return record("获取操作员页面", False, repr(e))
    record("获取操作员页面", status == 200, f"HTTP {status}")

    needles = [
        "深空地面站", "授权族", "终端标识", "轮换标识",
        "已接受", "重放", "已撤销", "撤销原因", "断回应",
        'id="rotate-result"', 'id="revocation-reason"',
        'id="create-result"', 'id="status-result"',
    ]
    missing = [n for n in needles if n not in html]
    record("页面呈现接受/重放/撤销等可观察元素", not missing,
           "缺失: " + ", ".join(missing) if missing else "全部命中")

    # 页面渲染所依赖的 API 字段齐备（与页面 JS 读取的字段一一对应）。
    terminal = f"term-page-{SFX}"
    _, fam = create_family(terminal)
    _, rot = rotate(terminal, fam["credential"], f"rot-page-{SFX}")
    fields = {"outcome", "credential", "generation", "family_status", "family_id", "terminal_id"}
    record("轮换响应包含页面展示所需字段", fields <= set(rot.keys()),
           f"字段: {sorted(fields & set(rot.keys()))}")


# ---------------------------------------------------------------- main

def main():
    print("=" * 64, flush=True)
    print("深空地面站 · 授权族轮换验收", flush=True)
    print(f"目标服务: {BASE}", flush=True)
    print("=" * 64, flush=True)

    if not wait_health(60):
        record("等待应用健康", False, f"{BASE}/health 在 60s 内未就绪")
    else:
        record("等待应用健康", True)
        step_unit_tests()
        step_smoke()
        step_disconnect_recovery()
        step_concurrent_same_id()
        step_reuse_revocation()
        step_page_observability()

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 64, flush=True)
    print(f"验收结果: {passed}/{total} 项通过", flush=True)
    for name, ok in RESULTS:
        if not ok:
            print(f"  ✗ {name}", flush=True)
    print("=" * 64, flush=True)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
