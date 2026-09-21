"""権限マトリクス（references.md §6-4）をデータとして持ち、判定を1か所に集める。

役割キー: owner / member / invited / link / anon / ai
  - 他組織のメンバーは anon として扱う。
  - 「×」のセルは False。条件つきの「△」は、リソースを見る関数。
判定はすべてサーバー側で、要求のたびに行う。
"""

from __future__ import annotations

from dataclasses import dataclass, field

SCOPES = ["link_30d", "org_only", "invited_only"]  # 右ほど狭い


@dataclass
class Actor:
    kind: str  # member / invited / link / anon / ai
    org_id: str | None = None
    member_id: str | None = None
    role: str | None = None  # owner / member
    invite_id: str | None = None
    card_id: str | None = None  # invited / link が見られるカード
    periodic: bool = False  # ai: コードが起動する定期の矛盾検出


ANON = Actor(kind="anon")


def _same_org(a: Actor, r: dict) -> bool:
    return a.org_id is not None and a.org_id == r.get("org_id")


def _is_creator_or_registrant(a: Actor, r: dict) -> bool:
    return _same_org(a, r) and a.member_id in (r.get("creator_id"), r.get("registrant_id"))


def _member_view_card(a: Actor, r: dict) -> bool:
    if not _same_org(a, r):
        return False
    if r.get("scope") in ("link_30d", "org_only"):
        return True
    return _is_creator_or_registrant(a, r)  # invited_only: 作成者・登録者のみ（他のメンバーは見られない [仮]）


def _member_watcher(a: Actor, r: dict) -> bool:
    return _same_org(a, r) and a.member_id in r.get("watcher_member_ids", [])


def _invited_watcher(a: Actor, r: dict) -> bool:
    return a.invite_id is not None and a.invite_id in r.get("watcher_invite_ids", [])


def _registrant_only(a: Actor, r: dict) -> bool:
    return _same_org(a, r) and a.member_id == r.get("registrant_id")


def _creator_only(a: Actor, r: dict) -> bool:
    return _same_org(a, r) and a.member_id == r.get("creator_id")


def _issuer_only(a: Actor, r: dict) -> bool:
    return _same_org(a, r) and a.member_id == r.get("issuer_id")


def _invited_card(a: Actor, r: dict) -> bool:
    return a.kind == "invited" and a.card_id is not None and a.card_id == r.get("card_id")


def _link_card(a: Actor, r: dict) -> bool:
    return a.kind == "link" and a.card_id is not None and a.card_id == r.get("card_id")


def _ai_periodic(a: Actor, r: dict) -> bool:
    return a.periodic and _same_org(a, r)


NO = {"invited": False, "link": False, "anon": False, "ai": False}

# action -> 役割キー -> True / False / 条件関数(actor, resource)
MATRIX: dict[str, dict] = {
    "view_public_info": {"owner": True, "member": True, "invited": True, "link": True, "anon": True, "ai": True},
    "register_object": {"owner": True, "member": True, **NO},
    "disable_tag": {"owner": _same_org, "member": _registrant_only, **NO},
    "view_card": {"owner": _same_org, "member": _member_view_card, "invited": _invited_card,
                  "link": _link_card, "anon": False, "ai": _same_org},
    "create_edit_card": {"owner": _same_org, "member": _same_org, **NO},
    "draft_card": {"owner": False, "member": False, "invited": False, "link": False, "anon": False, "ai": _same_org},
    "delete_card": {"owner": _same_org, "member": _creator_only, **NO},
    "narrow_scope": {"owner": _same_org, "member": _is_creator_or_registrant, **NO},
    "propose_narrow_scope": {"owner": False, "member": False, "invited": False, "link": False, "anon": False, "ai": _same_org},
    "widen_scope": {"owner": _same_org, "member": False, **NO},
    "request_widen_scope": {"owner": False, "member": _is_creator_or_registrant, **NO},
    "issue_share": {"owner": _same_org, "member": _is_creator_or_registrant, **NO},
    "revoke_share": {"owner": _same_org, "member": _issuer_only, **NO},
    "create_invite": {"owner": _same_org, "member": _is_creator_or_registrant, **NO},
    "send_notification": {"owner": _same_org, "member": _is_creator_or_registrant, **NO},
    "unmask": {"owner": _same_org, "member": _creator_only, **NO},
    "view_decisions": {"owner": _same_org, "member": lambda a, r: _is_creator_or_registrant(a, r) or _member_watcher(a, r),
                       "invited": _invited_watcher, "link": False, "anon": False, "ai": False},
    "manage_watchers": {"owner": _same_org, "member": _creator_only, **NO},
    "view_audit": {"owner": _same_org, "member": False, **NO},
    "manage_members": {"owner": _same_org, "member": False, **NO},
    "manage_owners": {"owner": _same_org, "member": False, **NO},
    "read_object_history": {"owner": False, "member": False, "invited": False, "link": False, "anon": False, "ai": _same_org},
    "search_org_records": {"owner": False, "member": False, "invited": False, "link": False, "anon": False, "ai": _ai_periodic},
}


def role_key(actor: Actor, resource: dict | None) -> str:
    """他組織のメンバーは anon として扱う。"""
    if actor.kind == "member":
        if resource is not None and resource.get("org_id") not in (None, actor.org_id):
            return "anon"
        return actor.role or "member"
    return actor.kind


def can(actor: Actor, action: str, resource: dict | None = None) -> bool:
    rule = MATRIX[action][role_key(actor, resource)]
    if callable(rule):
        return bool(rule(actor, resource or {}))
    return bool(rule)


def denied_cells() -> list[tuple[str, str]]:
    """マトリクスの「×」（無条件で False）のセル。境界テストの入力になる。"""
    return [(action, role) for action, row in MATRIX.items() for role, rule in row.items() if rule is False]


def scope_is_narrower(new: str, current: str) -> bool:
    return new in SCOPES and current in SCOPES and SCOPES.index(new) > SCOPES.index(current)
