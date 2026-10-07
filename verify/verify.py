#!/usr/bin/env python3
"""深空地面站验收器（compose 中的 verify 服务）。

围绕核心场景完成校验，并以退出码报告验收结果：
  A. 断回应恢复：提交后断连 → 重启 → 凭原标识恢复已持久化的后继，代次不再推进
  B. 并发同标识：两个并发相同请求只观察到同一结果
  C. 异标识重放：撤销授权族并给出原因，后继凭证随后同样被拒绝
  D. 轮换链审计：创建 + 每一次实际改变授权族的记录按序可查，仅指纹不外泄凭证；
     重放（含重启后）不入链；撤销记录标记异标识重用触发与全链失效；未知终端查询失败
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


# ------------------------------------------- 场景 D：轮换链审计

def step_rotation_chain_audit():
    print("\n== 6/7 场景 D：轮换链审计（顺序 / 幂等 / 重启 / 撤销标记 / 脱敏） ==", flush=True)
    chain_path = lambda t: f"/api/terminals/{t}/rotation-chain"

    # 未知终端：明确的查询失败语义，不返回任何链路数据。
    status, body = req("GET", chain_path(f"term-unknown-{SFX}"))
    record("轮换链：未知终端返回 404 unknown_terminal",
           status == 404 and body.get("error") == "unknown_terminal", f"HTTP {status} {body}")

    terminal = f"term-chain-{SFX}"
    status, fam = create_family(terminal)
    if status != 201:
        return record("场景D 前置：创建授权族", False, f"HTTP {status}")
    cred0 = fam["credential"]

    status, chain0 = req("GET", chain_path(terminal))
    recs0 = chain0.get("records", [])
    ok = (status == 200 and len(recs0) == 1 and recs0[0].get("type") == "creation"
          and recs0[0].get("resulting_generation") == 1
          and str(recs0[0].get("new_credential_fingerprint", "")).startswith("sha256:")
          and chain0.get("family_status") == "active"
          and chain0.get("chain_invalidated") is False
          and cred0 not in json.dumps(chain0))
    record("首轮换链：仅创建记录，初始凭证以指纹展示、无明文", ok, f"HTTP {status}")

    rid1 = f"rot-chain-1-{SFX}"
    status, r1 = rotate(terminal, cred0, rid1)
    if not (status == 200 and r1.get("outcome") == "accepted"):
        return record("场景D 首次轮换", False, f"HTTP {status} {r1}")
    cred1 = r1["credential"]

    # 多次同标识重放：只回放既有业务结果。
    for _ in range(2):
        s, rr = rotate(terminal, cred0, rid1)
        if not (s == 200 and rr.get("outcome") == "replayed"
                and rr.get("credential") == cred1 and rr.get("generation") == 2):
            return record("场景D 重放保持既有结果", False, f"HTTP {s} {rr.get('outcome')}")

    status, chain1 = req("GET", chain_path(terminal))
    recs1 = chain1.get("records", [])
    rotation_rec = recs1[1] if len(recs1) > 1 else {}
    ok = (len(recs1) == 2
          and [r.get("type") for r in recs1] == ["creation", "rotation"]
          and [r.get("seq") for r in recs1] == [0, 1]
          and rotation_rec.get("rotation_id") == rid1
          and rotation_rec.get("resulting_generation") == 2
          and rotation_rec.get("status") == "accepted"
          and str(rotation_rec.get("old_credential_fingerprint", "")).startswith("sha256:")
          and str(rotation_rec.get("new_credential_fingerprint", "")).startswith("sha256:")
          and rotation_rec.get("old_credential_fingerprint")
              == recs1[0].get("new_credential_fingerprint"))
    record("首轮换链：重放不新增记录、顺序不变；轮换记录含前后指纹/稳定标识/结果代次/状态",
           ok, f"{len(recs1)} 条记录")

    # 第二次轮换，验证链路按发生顺序延伸且指纹首尾相接。
    rid2 = f"rot-chain-2-{SFX}"
    status, r2 = rotate(terminal, cred1, rid2)
    if not (status == 200 and r2.get("outcome") == "accepted" and r2.get("generation") == 3):
        return record("场景D 第二次轮换", False, f"HTTP {status}")
    cred2 = r2["credential"]
    status, chain2 = req("GET", chain_path(terminal))
    recs2 = chain2.get("records", [])
    ok = (len(recs2) == 3
          and [r.get("rotation_id") for r in recs2[1:]] == [rid1, rid2]
          and recs2[2].get("old_credential_fingerprint")
              == recs2[1].get("new_credential_fingerprint")
          and recs2[2].get("resulting_generation") == 3)
    record("轮换链按发生顺序延伸，相邻记录后/前凭证指纹相接", ok)

    # 重启后的幂等重放：业务结果一致，且审计链路与重启前逐字节一致。
    before_restart = json.dumps(chain2, sort_keys=True)
    try:
        req("POST", "/api/admin/shutdown")
    except Exception:
        pass
    record("场景D 服务进程已退出", wait_down(15))
    record("场景D 服务进程重启后恢复健康", wait_health(30))
    s, rr = rotate(terminal, cred1, rid2)
    ok = (s == 200 and rr.get("outcome") == "replayed"
          and rr.get("credential") == cred2 and rr.get("generation") == 3)
    record("重启后相同（旧凭证, 轮换标识）重放只回放既有结果", ok, f"HTTP {s}")
    s, rr = rotate(terminal, cred0, rid1)
    record("重启后更早一代的重放同样只回放既有结果",
           s == 200 and rr.get("outcome") == "replayed"
           and rr.get("credential") == cred1 and rr.get("generation") == 2)
    status, chain3 = req("GET", chain_path(terminal))
    after_restart = json.dumps(chain3, sort_keys=True)
    record("重启重放不额外生成审计记录、不改变链路顺序",
           status == 200 and after_restart == before_restart
           and len(chain3.get("records", [])) == 3)

    # 异标识重用 → 撤销记录明确标记触发记录与全链失效。
    rid_bad = f"rot-chain-DIFFERENT-{SFX}"
    status, rb = rotate(terminal, cred0, rid_bad)
    record("场景D 异标识重用触发撤销", status == 409 and rb.get("outcome") == "revoked",
           f"HTTP {status}")
    status, chain4 = req("GET", chain_path(terminal))
    recs4 = chain4.get("records", [])
    rev_rec = recs4[-1] if recs4 else {}
    ok = (status == 200 and len(recs4) == 4
          and chain4.get("family_status") == "revoked"
          and chain4.get("chain_invalidated") is True
          and chain4.get("reuse_trigger_seq") == rev_rec.get("seq") == 3
          and rev_rec.get("type") == "revocation"
          and rev_rec.get("reuse_trigger") is True
          and rev_rec.get("rotation_id") == rid_bad
          and rev_rec.get("status") == "revoked"
          and rev_rec.get("resulting_generation") == 3   # 撤销不推进代次
          and rev_rec.get("new_credential_fingerprint") is None
          and "reuse" in (rev_rec.get("reason") or "")
          and not any(r.get("reuse_trigger") for r in recs4[:-1]))
    record("撤销记录标记异标识重用触发，全链失效状态与原因可见、代次不推进", ok)

    # 撤销后的任何重放/尝试都不得再入链。
    rotate(terminal, cred0, rid1)
    rotate(terminal, cred1, rid2)
    rotate(terminal, cred2, f"rot-chain-after-{SFX}")
    status, chain5 = req("GET", chain_path(terminal))
    record("撤销后拒绝的重放/轮换均不产生审计记录",
           status == 200 and len(chain5.get("records", [])) == 4
           and json.dumps(chain5, sort_keys=True) == json.dumps(chain4, sort_keys=True))

    # 凭证脱敏：全链路 JSON 中不出现任何曾签发的可用凭证明文，只允许 sha256 指纹。
    blob = json.dumps(chain5, ensure_ascii=False)
    fps = [r.get("old_credential_fingerprint") for r in recs4] + \
          [r.get("new_credential_fingerprint") for r in recs4]
    fps = [f for f in fps if f]
    no_leak = all(c not in blob for c in (cred0, cred1, cred2)) and not any(
        f for f in fps if not str(f).startswith("sha256:"))
    record("审计结果全程仅含不可逆指纹，不泄露可用凭证", no_leak)


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
        "轮换链", "全链失效", "异标识重用",
        'id="rotate-result"', 'id="revocation-reason"',
        'id="create-result"', 'id="status-result"',
        'id="chain-result"', 'id="chain-btn"', 'id="chain-invalidated"',
    ]
    missing = [n for n in needles if n not in html]
    record("页面呈现接受/重放/撤销及轮换链审计等可观察元素", not missing,
           "缺失: " + ", ".join(missing) if missing else "全部命中")

    # 页面渲染所依赖的 API 字段齐备（与页面 JS 读取的字段一一对应）。
    terminal = f"term-page-{SFX}"
    _, fam = create_family(terminal)
    _, rot = rotate(terminal, fam["credential"], f"rot-page-{SFX}")
    fields = {"outcome", "credential", "generation", "family_status", "family_id", "terminal_id"}
    record("轮换响应包含页面展示所需字段", fields <= set(rot.keys()),
           f"字段: {sorted(fields & set(rot.keys()))}")

    _, chain = req("GET", f"/api/terminals/{terminal}/rotation-chain")
    chain_fields = {"family_id", "terminal_id", "family_status", "generation",
                    "chain_invalidated", "records"}
    record_fields = {"seq", "type", "rotation_id", "old_credential_fingerprint",
                     "new_credential_fingerprint", "resulting_generation",
                     "status", "reason", "occurred_at", "reuse_trigger"}
    record("轮换链响应包含页面渲染所需字段",
           chain_fields <= set(chain.keys())
           and bool(chain.get("records"))
           and record_fields <= set(chain["records"][0].keys()),
           f"{len(chain.get('records', []))} 条记录")


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
