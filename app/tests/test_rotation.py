"""授权族轮换核心逻辑的单元测试（标准库 unittest，无第三方依赖）。"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service  # noqa: E402
from storage import Storage  # noqa: E402


class RotationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _family(self, terminal="term-1"):
        return service.create_family(self.storage, terminal)

    # ---- 创建 ----

    def test_create_family_issues_generation_one_credential(self):
        fam = self._family()
        self.assertEqual(fam["generation"], 1)
        self.assertTrue(fam["credential"].startswith("rft_"))
        self.assertEqual(fam["family_status"], "active")

    def test_terminal_can_bind_only_one_family(self):
        self._family("term-dup")
        with self.assertRaises(service.TerminalAlreadyBoundError):
            self._family("term-dup")

    def test_create_family_requires_terminal_id(self):
        with self.assertRaises(ValueError):
            self._family("  ")

    # ---- 首次轮换 ----

    def test_first_rotation_accepted_and_generation_increments(self):
        fam = self._family()
        res = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(res["outcome"], "accepted")
        self.assertEqual(res["generation"], 2)
        self.assertTrue(res["credential"].startswith("rft_"))
        self.assertNotEqual(res["credential"], fam["credential"])
        self.assertEqual(res["family_status"], "active")

    # ---- 幂等重放 ----

    def test_replay_returns_identical_successor_without_advancing(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        for _ in range(3):
            again = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
            self.assertEqual(again["outcome"], "replayed")
            self.assertEqual(again["credential"], first["credential"])
            self.assertEqual(again["generation"], first["generation"])
        view = service.get_family_by_terminal(self.storage, "term-1")
        self.assertEqual(view["generation"], 2)
        self.assertEqual(self.storage.count_rotations(fam["family_id"]), 1)

    def test_replay_survives_service_restart(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        # 模拟服务重启：关闭并重新打开同一数据库文件。
        self.storage.close()
        self.storage = Storage(self.db_path)
        again = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(again["outcome"], "replayed")
        self.assertEqual(again["credential"], first["credential"])
        self.assertEqual(again["generation"], first["generation"])

    def test_replay_returns_historical_successor_after_later_rotations(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        second = service.rotate(self.storage, "term-1", first["credential"], "rot-2")
        self.assertEqual(second["generation"], 3)
        again = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(again["outcome"], "replayed")
        self.assertEqual(again["credential"], first["credential"])
        self.assertEqual(again["generation"], 2)

    # ---- 并发同标识 ----

    def test_concurrent_identical_requests_observe_same_result(self):
        fam = self._family()
        barrier = threading.Barrier(2)

        def call():
            barrier.wait()
            return service.rotate(self.storage, "term-1", fam["credential"], "rot-1")

        results = []
        threads = [threading.Thread(target=lambda: results.append(call())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(r["outcome"] for r in results), ["accepted", "replayed"])
        self.assertEqual(results[0]["credential"], results[1]["credential"])
        self.assertEqual(results[0]["generation"], results[1]["generation"])
        self.assertEqual(results[0]["generation"], 2)
        view = service.get_family_by_terminal(self.storage, "term-1")
        self.assertEqual(view["generation"], 2)
        # 审计链路同样只记录那一次实际改变授权族的轮换。
        chain = service.get_rotation_chain(self.storage, "term-1")
        self.assertEqual(len(chain["records"]), 2)
        self.assertEqual(self.storage.count_rotation_events(fam["family_id"]), 1)

    # ---- 异标识重放 => 撤销 ----

    def test_reuse_with_different_rotation_id_revokes_family(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        res = service.rotate(self.storage, "term-1", fam["credential"], "rot-OTHER")
        self.assertEqual(res["outcome"], "revoked")
        self.assertEqual(res["family_status"], "revoked")
        self.assertIn("reuse", res["revocation_reason"])

        # 此前签发的后继凭证随后同样被拒绝。
        successor = service.rotate(self.storage, "term-1", first["credential"], "rot-2")
        self.assertEqual(successor["outcome"], "revoked")
        self.assertIn("reuse", successor["revocation_reason"])

        # 原（旧凭证, 原轮换标识）重放也被拒绝。
        replay = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(replay["outcome"], "revoked")

        # 状态查询可见撤销原因。
        view = service.get_family_by_terminal(self.storage, "term-1")
        self.assertEqual(view["family_status"], "revoked")
        self.assertIn("reuse", view["revocation_reason"])

    # ---- 错误路径 ----

    def test_unknown_terminal_rejected(self):
        with self.assertRaises(service.UnknownTerminalError):
            service.rotate(self.storage, "no-such-terminal", "rft_x", "rot-1")

    def test_unknown_credential_rejected(self):
        self._family()
        with self.assertRaises(service.InvalidCredentialError):
            service.rotate(self.storage, "term-1", "rft_not_issued", "rot-1")

    def test_rotate_requires_all_fields(self):
        fam = self._family()
        with self.assertRaises(ValueError):
            service.rotate(self.storage, "term-1", fam["credential"], "")
        with self.assertRaises(ValueError):
            service.rotate(self.storage, "term-1", "", "rot-1")

    # ---- 轮换链审计 ----

    def test_chain_starts_with_creation_record_and_no_plaintext(self):
        fam = self._family()
        chain = service.get_rotation_chain(self.storage, "term-1")
        self.assertEqual(chain["family_status"], "active")
        self.assertFalse(chain["chain_invalidated"])
        self.assertIsNone(chain["reuse_trigger_seq"])
        self.assertEqual(len(chain["records"]), 1)

        creation = chain["records"][0]
        self.assertEqual(creation["type"], "creation")
        self.assertEqual(creation["seq"], 0)
        self.assertEqual(creation["resulting_generation"], 1)
        self.assertEqual(creation["status"], "issued")
        self.assertTrue(creation["new_credential_fingerprint"].startswith("sha256:"))
        self.assertIsNone(creation["old_credential_fingerprint"])

        # 审计结果中不得出现任何可用凭证（明文）。
        import json
        self.assertNotIn(fam["credential"], json.dumps(chain, ensure_ascii=False))

    def test_chain_records_rotation_in_order_with_fingerprints(self):
        fam = self._family()
        r1 = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        r2 = service.rotate(self.storage, "term-1", r1["credential"], "rot-2")

        chain = service.get_rotation_chain(self.storage, "term-1")
        self.assertEqual([r["type"] for r in chain["records"]],
                         ["creation", "rotation", "rotation"])
        self.assertEqual([r["seq"] for r in chain["records"]], [0, 1, 2])

        first = chain["records"][1]
        self.assertEqual(first["rotation_id"], "rot-1")
        self.assertEqual(first["resulting_generation"], 2)
        self.assertEqual(first["status"], "accepted")
        self.assertTrue(first["old_credential_fingerprint"].startswith("sha256:"))
        self.assertTrue(first["new_credential_fingerprint"].startswith("sha256:"))
        self.assertNotEqual(first["old_credential_fingerprint"],
                            first["new_credential_fingerprint"])

        second = chain["records"][2]
        self.assertEqual(second["rotation_id"], "rot-2")
        self.assertEqual(second["resulting_generation"], 3)
        # 相邻记录的后/前凭证指纹首尾相接。
        self.assertEqual(second["old_credential_fingerprint"],
                         first["new_credential_fingerprint"])

        import json
        blob = json.dumps(chain, ensure_ascii=False)
        for secret in (fam["credential"], r1["credential"], r2["credential"]):
            self.assertNotIn(secret, blob)

    def test_chain_replays_do_not_add_records_even_after_restart(self):
        fam = self._family()
        service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        chain = service.get_rotation_chain(self.storage, "term-1")
        self.assertEqual(len(chain["records"]), 2)
        self.assertEqual(self.storage.count_rotation_events(fam["family_id"]), 1)

        # 重启后重放：只回放既有业务结果，不额外生成审计记录、不改变链路顺序。
        self.storage.close()
        self.storage = Storage(self.db_path)
        again = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(again["outcome"], "replayed")
        chain = service.get_rotation_chain(self.storage, "term-1")
        self.assertEqual(len(chain["records"]), 2)
        self.assertEqual([r["rotation_id"] for r in chain["records"][1:]], ["rot-1"])
        self.assertEqual(self.storage.count_rotation_events(fam["family_id"]), 1)

    def test_chain_marks_reuse_trigger_and_full_invalidation(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        service.rotate(self.storage, "term-1", fam["credential"], "rot-OTHER")

        chain = service.get_rotation_chain(self.storage, "term-1")
        self.assertEqual(chain["family_status"], "revoked")
        self.assertTrue(chain["chain_invalidated"])
        self.assertEqual(chain["reuse_trigger_seq"], 2)

        self.assertEqual([r["type"] for r in chain["records"]],
                         ["creation", "rotation", "revocation"])
        revocation = chain["records"][2]
        self.assertTrue(revocation["reuse_trigger"])
        self.assertEqual(revocation["rotation_id"], "rot-OTHER")
        self.assertEqual(revocation["status"], "revoked")
        self.assertEqual(revocation["resulting_generation"], 2)  # 撤销不推进代次
        self.assertIsNone(revocation["new_credential_fingerprint"])
        self.assertIn("reuse", revocation["reason"])
        self.assertIn("reuse", chain["revocation_reason"])
        self.assertFalse(chain["records"][1]["reuse_trigger"])

        # 撤销后的原重放与后继轮换均被拒绝，且都不再产生审计记录。
        self.assertEqual(service.rotate(self.storage, "term-1", fam["credential"], "rot-1")["outcome"],
                         "revoked")
        self.assertEqual(service.rotate(self.storage, "term-1", first["credential"], "rot-2")["outcome"],
                         "revoked")
        chain = service.get_rotation_chain(self.storage, "term-1")
        self.assertEqual(len(chain["records"]), 3)
        self.assertEqual(self.storage.count_rotation_events(fam["family_id"]), 2)

        import json
        self.assertNotIn(fam["credential"], json.dumps(chain, ensure_ascii=False))
        self.assertNotIn(first["credential"], json.dumps(chain, ensure_ascii=False))

    def test_chain_unknown_terminal_is_query_failure(self):
        with self.assertRaises(service.UnknownTerminalError):
            service.get_rotation_chain(self.storage, "no-such-terminal")


if __name__ == "__main__":
    unittest.main()
