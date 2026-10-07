"""授权族轮换核心逻辑。

语义约定：
- 授权族绑定唯一终端标识，初始代次为 1，状态 active。
- 轮换 = 凭「当前刷新凭证 + 稳定轮换标识」换取唯一后继凭证，代次 +1。
- 幂等：相同（旧凭证, 轮换标识）重传 —— 包括服务重启后的重试 ——
  返回与首次完全一致的后继凭证与代次，结果标记为 replayed，代次不再推进。
- 重用检测：已轮换的旧凭证搭配「不同」轮换标识再次出现，
  整个授权族立即撤销并记录原因；此前签发的后继凭证随之被拒绝。
"""
import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"

REASON_REUSE = "refresh_credential_reuse_detected"


class UnknownTerminalError(Exception):
    """终端标识未绑定任何授权族。"""


class TerminalAlreadyBoundError(Exception):
    """该终端已绑定授权族。"""


class InvalidCredentialError(Exception):
    """凭证不属于该终端的授权族。"""


class ConcurrentRotation(Exception):
    """并发下轮换记录已被其它请求提交（唯一约束兜底），重读即可。"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash(credential):
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def _new_credential():
    return "rft_" + secrets.token_hex(24)


def _validate(terminal_id, credential=None, rotation_id=None):
    if not terminal_id or not terminal_id.strip():
        raise ValueError("terminal_id is required")
    if credential is not None and not credential.strip():
        raise ValueError("credential is required")
    if rotation_id is not None and not rotation_id.strip():
        raise ValueError("rotation_id is required")


def create_family(storage, terminal_id):
    """创建绑定终端标识的授权族，签发初始刷新凭证（代次 1）。"""
    terminal_id = (terminal_id or "").strip()
    _validate(terminal_id)
    with storage.transaction() as db:
        if db.find_family_by_terminal(terminal_id):
            raise TerminalAlreadyBoundError(terminal_id)
        family_id = "fam_" + secrets.token_hex(8)
        credential = _new_credential()
        now = _now()
        db.insert_family(family_id, terminal_id, now)
        db.insert_credential(family_id, _hash(credential), 1, "current", now)
        return {
            "family_id": family_id,
            "terminal_id": terminal_id,
            "credential": credential,
            "generation": 1,
            "family_status": STATUS_ACTIVE,
        }


def get_family_by_terminal(storage, terminal_id):
    with storage.transaction() as db:
        family = db.find_family_by_terminal((terminal_id or "").strip())
        if not family:
            raise UnknownTerminalError(terminal_id)
        return _family_view(family)


def get_family(storage, family_id):
    with storage.transaction() as db:
        family = db.find_family(family_id)
        if not family:
            raise UnknownTerminalError(family_id)
        return _family_view(family)


def _family_view(family):
    return {
        "family_id": family["family_id"],
        "terminal_id": family["terminal_id"],
        "family_status": family["status"],
        "generation": family["generation"],
        "revocation_reason": family["revocation_reason"],
        "created_at": family["created_at"],
    }


def rotate(storage, terminal_id, credential, rotation_id):
    """执行轮换。返回 dict，outcome ∈ {accepted, replayed, revoked}。"""
    terminal_id = (terminal_id or "").strip()
    credential = (credential or "").strip()
    rotation_id = (rotation_id or "").strip()
    _validate(terminal_id, credential, rotation_id)

    # 进程级锁已保证串行；唯一约束是跨进程场景下的兜底，
    # 命中冲突时重读已提交记录，按重放返回。
    for _attempt in (1, 2):
        try:
            with storage.transaction() as db:
                return _rotate_once(db, terminal_id, credential, rotation_id)
        except ConcurrentRotation:
            continue
    raise RuntimeError("unreachable: rotation conflict did not resolve")


def _rotate_once(db, terminal_id, credential, rotation_id):
    family = db.find_family_by_terminal(terminal_id)
    if not family:
        raise UnknownTerminalError(terminal_id)

    # 授权族已撤销：任何凭证（含此前签发的后继）一律拒绝并给出原因。
    if family["status"] == STATUS_REVOKED:
        return _revoked_result(family)

    cred_hash = _hash(credential)
    cred = db.find_credential(family["family_id"], cred_hash)
    if not cred:
        raise InvalidCredentialError(terminal_id)

    if cred["status"] == "current":
        return _accept_rotation(db, family, terminal_id, cred, cred_hash, rotation_id)

    # 旧凭证已被轮换过：查找消费它的那条轮换记录。
    rotation = db.find_rotation_by_old_hash(family["family_id"], cred_hash)
    if rotation and rotation["rotation_id"] == rotation_id:
        # 幂等重放：原样返回首次提交的后继凭证与代次，不推进代次。
        return {
            "outcome": "replayed",
            "family_id": family["family_id"],
            "terminal_id": terminal_id,
            "credential": rotation["new_credential"],
            "generation": rotation["new_generation"],
            "family_status": family["status"],
            "rotation_id": rotation_id,
        }

    # 相同旧凭证 + 不同轮换标识 => 凭证重用，撤销整个授权族。
    reason = (
        f"{REASON_REUSE}: rotated credential of terminal '{terminal_id}' "
        f"presented again with a different rotation id"
    )
    db.revoke_family(family["family_id"], reason)
    db.revoke_all_credentials(family["family_id"])
    return _revoked_result({
        **family,
        "status": STATUS_REVOKED,
        "revocation_reason": reason,
    })


def _accept_rotation(db, family, terminal_id, cred, cred_hash, rotation_id):
    new_credential = _new_credential()
    new_generation = family["generation"] + 1
    now = _now()
    db.set_credential_status(cred["id"], "rotated")
    db.insert_credential(family["family_id"], _hash(new_credential),
                         new_generation, "current", now)
    try:
        db.insert_rotation(family["family_id"], terminal_id, rotation_id,
                           cred_hash, new_credential, _hash(new_credential),
                           new_generation, now)
    except sqlite3.IntegrityError:
        raise ConcurrentRotation()
    db.set_family_generation(family["family_id"], new_generation)
    return {
        "outcome": "accepted",
        "family_id": family["family_id"],
        "terminal_id": terminal_id,
        "credential": new_credential,
        "generation": new_generation,
        "family_status": STATUS_ACTIVE,
        "rotation_id": rotation_id,
    }


def _revoked_result(family):
    return {
        "outcome": "revoked",
        "family_id": family["family_id"],
        "terminal_id": family["terminal_id"],
        "credential": None,
        "generation": family["generation"],
        "family_status": STATUS_REVOKED,
        "revocation_reason": family["revocation_reason"],
    }
