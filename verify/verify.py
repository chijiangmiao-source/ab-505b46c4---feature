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
    print("\n== 1/7 代码单元测试 ==", flush=True)
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
    print("\n== 2/7 API/HTTP 冒烟 ==", flush=True)
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
    print("\n== 3/7 场景 A：断回应（提交后断连）+ 重启恢复 ==", flush=True)
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
    print("\n== 4/7 场景 B：两个并发相同请求 ==", flush=True)
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
    print("\n== 5/7 场景 C：异标识重放 → 授权族撤销 ==", flush=True)
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


# --------------------------------------------------- 场景 D：轮换链审计

def chain_of(terminal):
    return req("GET", f"/api/terminals/{terminal}/rotation-chain")


def step_rotation_chain_audit():
    print("\n== 6/7 场景 D：轮换链审计（创建/轮换/撤销记录、指纹脱敏、幂等不增项） ==", flush=True)

    # 未知终端：明确的查询失败语义（404 unknown_terminal）。
    status, body = chain_of(f"term-unknown-{SFX}")
    record("轮换链：未知终端返回明确失败（404）",
           status == 404 and body.get("error") == "unknown_terminal", f"HTTP {status} {body}")

    terminal = f"term-chain-{SFX}"
    rid1, rid2 = f"rot-chain-1-{SFX}", f"rot-chain-2-{SFX}"
    status, fam = create_family(terminal)
    if status != 201:
        return record("场景D 前置：创建授权族", False, f"HTTP {status}")
    cred0 = fam["credential"]

    status, ch0 = chain_of(terminal)
    ok = (status == 200 and ch0.get("length") == 1
          and ch0["entries"][0]["type"] == "created"
          and ch0["entries"][0]["result_generation"] == 1
          and str(ch0["entries"][0]["result_credential_fp"]).startswith("sha256:"))
    record("首轮换链：仅创建记录（代次 1，初始凭证指纹）", ok, f"HTTP {status}")

    status, r1 = rotate(terminal, cred0, rid1)
    cred1 = r1.get("credential")
    status, r2 = rotate(terminal, cred1, rid2)
    cred2 = r2.get("credential")

    status, ch1 = chain_of(terminal)
    entries = ch1.get("entries", [])
    types = [e.get("type") for e in entries]
    gens = [e.get("result_generation") for e in entries]
    ok = (status == 200 and types == ["created", "rotated", "rotated"]
          and gens == [1, 2, 3] and ch1.get("length") == 3
          and not ch1.get("chain_invalidated"))
    record("首轮换链：按发生顺序含创建 + 每次轮换，结果代次递增", ok,
           f"HTTP {status} types={types} gens={gens}")

    # 指纹：前后凭证均为不可逆指纹且前后衔接；绝不含可用凭证明文。
    linked = (entries[1]["previous_credential_fp"] == entries[0]["result_credential_fp"]
              and entries[2]["previous_credential_fp"] == entries[1]["result_credential_fp"])
    fps = [e.get("previous_credential_fp") for e in entries[1:]] + \
          [e.get("result_credential_fp") for e in entries]
    fp_ok = linked and all(isinstance(f, str) and f.startswith("sha256:") for f in fps if f)
    record("轮换记录含前后凭证不可逆指纹且前后衔接", fp_ok)

    raw = json.dumps(ch1, ensure_ascii=False)
    no_leak = cred0 not in raw and cred1 not in raw and cred2 not in raw
    record("审计结果不泄露任何可用凭证（明文不出现）", no_leak)

    # 幂等重放（含重启后）只回放既有结果，不新增审计记录、不改变链路顺序。
    rotate(terminal, cred0, rid1)  # 同旧凭证 + 同标识重放
    status, ch2 = chain_of(terminal)
    record("同标识重放不新增审计记录、不改变链路顺序",
           status == 200 and ch2.get("entries") == entries and ch2.get("length") == 3)

    # 重启后重放：既有业务结果不变，链路仍无新增。
    try:
        req("POST", "/api/admin/shutdown")
    except Exception:
        pass
    record("服务进程已退出（场景D 重启演练）", wait_down(15))
    record("服务进程重启后恢复健康（场景D 重启演练）", wait_health(30))
    status, replay = rotate(terminal, cred0, rid1)
    replay_ok = (status == 200 and replay.get("outcome") == "replayed"
                 and replay.get("credential") == cred1
                 and replay.get("generation") == 2)
    record("重启后幂等重放取回既有后继凭证与代次", replay_ok, f"HTTP {status}")
    status, ch3 = chain_of(terminal)
    record("重启后重放仍不新增审计记录、链路顺序不变",
           status == 200 and ch3.get("entries") == entries and ch3.get("length") == 3,
           f"HTTP {status}")

    # 异标识重用触发撤销：撤销记录明确标记 + 全链失效。
    status, revoked = rotate(terminal, cred0, f"rot-DIFFERENT-{SFX}")
    record("异标识重用触发撤销（业务结果）",
           status == 409 and revoked.get("outcome") == "revoked", f"HTTP {status}")

    status, ch4 = chain_of(terminal)
    ev = ch4.get("entries", [])
    rev_entry = ev[-1] if ev else {}
    ok = (status == 200 and ch4.get("chain_invalidated") is True
          and ch4.get("family_status") == "revoked"
          and rev_entry.get("type") == "revoked"
          and rev_entry.get("reuse_trigger") is True
          and rev_entry.get("chain_invalidated") is True
          and rev_entry.get("original_rotation_id") == rid1
          and rev_entry.get("attempted_rotation_id") == f"rot-DIFFERENT-{SFX}"
          and "reuse" in (rev_entry.get("reason") or "")
          and rev_entry.get("result_credential_fp") is None)
    record("撤销记录明确标记异标识重用触发点", ok, f"HTTP {status}")

    # 历史轮换条目随全链失效；撤销后任何重放不再新增记录。
    hist_statuses = {e["result_generation"]: e["status"] for e in ev if e["type"] == "rotated"}
    rotate(terminal, cred1, rid2)  # 撤销后重放旧请求
    rotate(terminal, cred0, rid1)
    status, ch5 = chain_of(terminal)
    dead_ok = (hist_statuses.get(2) == "revoked" and hist_statuses.get(3) == "revoked"
               and ch5.get("length") == ch4.get("length")
               and ch5.get("chain_invalidated") is True)
    record("全链失效标记 + 撤销后重放不新增审计记录", dead_ok, f"HTTP {status}")


# ------------------------------------------------------- 页面可观察结果

def step_page_observability():
    print("\n== 7/7 页面可观察结果校验 ==", flush=True)
    try:
        status, html = get_text("/")
    except Exception as e:
        return record("获取操作员页面", False, repr(e))
    record("获取操作员页面", status == 200, f"HTTP {status}")

    needles = [
        "深空地面站", "授权族", "终端标识", "轮换标识",
        "已接受", "重放", "已撤销", "撤销原因", "断回应",
        "轮换链", "不可逆指纹", "全链", "异标识重用",
        'id="rotate-result"', 'id="revocation-reason"',
        'id="create-result"', 'id="status-result"', 'id="chain-result"',
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
        step_rotation_chain_audit()
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
