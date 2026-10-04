"""签署后冻结完整依赖快照。

快照递归收录签署日生效的全部下层已发布版本、用量行、替代关系，
以及被引用部件的主数据与单位换算，再对规范化 JSON 计算 SHA-256。
此后该版本的需求展开永远以快照为准，下层再出草稿或改版均不影响
已签署版本（部件停用等变更只能产生父级新版本）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .models import SNAPSHOT_FORMAT, BomError, Revision, Snapshot
from .store import Store


def build_snapshot(store: Store, root: Revision, signed_by: str) -> tuple[Snapshot, dict]:
    """构造快照及可持久化的 payload。调用前必须已通过完整性校验。"""
    parts: dict[str, dict] = {}
    revisions: dict[str, dict] = {}

    def collect_part(code: str) -> None:
        if code in parts:
            return
        part = store.get_part(code)
        if part is None:
            raise BomError("MISSING_PART", f"部件主数据缺失，无法冻结：{code}")
        parts[code] = part.to_dict()

    def walk(rev: Revision) -> None:
        if rev.rev_id in revisions:
            return
        revisions[rev.rev_id] = rev.to_dict()
        for line in rev.lines:
            collect_part(line.child)
        for child, subs in rev.substitutes.items():
            for sub in subs:
                collect_part(sub.alt)

    # 根版本先收录，再沿签署日生效的已发布版本逐层闭包
    collect_part(root.code)
    pending = [root]
    while pending:
        rev = pending.pop()
        if rev.rev_id in revisions:
            continue
        walk(rev)
        for line in rev.lines:
            collect_part(line.child)
            if store.has_revisions(line.child):
                child_rev = store.frozen_revision_at(line.child, root.valid_from)
                if child_rev is not None and child_rev.rev_id not in revisions:
                    pending.append(child_rev)
        # 替代料本身也可能是总成，同样需要闭包收录其已发布版本
        for subs in rev.substitutes.values():
            for sub in subs:
                collect_part(sub.alt)
                if store.has_revisions(sub.alt):
                    alt_rev = store.frozen_revision_at(sub.alt, root.valid_from)
                    if alt_rev is not None and alt_rev.rev_id not in revisions:
                        pending.append(alt_rev)

    payload = {
        "snapshot_format": SNAPSHOT_FORMAT,
        "code": root.code,
        "branch": root.branch,
        "version": root.version,
        "signed_by": signed_by,
        "valid_from": root.valid_from.isoformat(),
        "valid_to": root.valid_to.isoformat() if root.valid_to else None,
        "parts": parts,
        "revisions": revisions,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["checksum"] = checksum

    snap = Snapshot(
        code=root.code,
        version=root.version,
        branch=root.branch,
        signed_by=signed_by,
        signed_at=datetime.utcnow(),
        valid_from=root.valid_from,
        valid_to=root.valid_to,
        parts=parts,
        revisions=revisions,
        checksum=checksum,
    )
    return snap, payload


def verify_checksum(payload: dict) -> bool:
    """复核快照内容与校验和是否一致（防篡改/防存储损坏）。"""
    expected = payload.get("checksum")
    body = {k: v for k, v in payload.items() if k != "checksum"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() == expected
