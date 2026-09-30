"""Per-caller account routing: who is asking, and which accounts they may use.

A caller identifies itself with a request header (`X-Flow-Client`, falling back to
`X-Client`). Unidentified callers get the `default` policy, which is "any account" —
exactly how every caller behaved before this module existed. Policies live in the
`client_policies` table and are cached here in memory (single uvicorn process; the
admin POST reloads the cache the same way `db.reload_config_to_memory()` does for the
other config tables).

A token may additionally be RESERVED for one client (`tokens.reserved_client`): then
only that client can generate with it, and that client prefers it over shared tokens.

Each policy also has a CEILING (`max_tier`: ultra | pro | free), the highest account tier the
caller may use. Owner 2026-09-30: every Ultra, current and future, is for NAMED callers only,
so the `default` row is seeded with `max_tier='pro'`; a named caller with no row of its own
inherits `default`'s minimum tiers but has no ceiling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional

from .account_tiers import get_paygate_tier_label, get_paygate_tier_rank, normalize_user_paygate_tier

DEFAULT_CLIENT = "default"
CLIENT_HEADERS = ("x-flow-client", "x-client")
TIER_RULES = ("any", "paid", "ultra", "off")
MAX_TIERS = ("ultra", "pro", "free")
NO_ACCOUNT_CODE = "client_policy_no_account"
_CLIENT_MAX_LEN = 40
_CLIENT_ALLOWED = re.compile(r"^[a-z0-9._-]+$")

# rule -> minimum paygate rank (None = this media type is switched off for the client)
_RULE_RANK: Dict[str, Optional[int]] = {"any": 0, "paid": 1, "ultra": 2, "off": None}
_RANK_LABEL = {0: "any", 1: "Pro", 2: "Ult"}
_MAX_RANK = {"ultra": 2, "pro": 1, "free": 0}
_MAX_LABEL = {2: "Ultra", 1: "Pro", 0: "Free"}
NAMED_ONLY_REASON = "reserved for named clients (send X-Flow-Client)"


def normalize_client(raw: Optional[str]) -> str:
    """Lowercase slug, max 40 chars, [a-z0-9._-] only; anything else => "" (unidentified)."""
    value = (raw or "").strip().lower()[:_CLIENT_MAX_LEN]
    if not value or not _CLIENT_ALLOWED.match(value):
        return ""
    return value


def resolve_client(headers: Mapping[str, str]) -> str:
    """Read the caller id from request headers. `X-Flow-Client` wins over `X-Client`."""
    for name in CLIENT_HEADERS:
        client = normalize_client(headers.get(name))
        if client:
            return client
    return ""


def normalize_rule(rule: Optional[str]) -> str:
    value = (rule or "").strip().lower()
    return value if value in TIER_RULES else "any"


def normalize_max_tier(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    return value if value in MAX_TIERS else "ultra"


@dataclass
class ClientPolicy:
    client: str
    image_tier: str = "any"
    video_tier: str = "any"
    note: str = ""
    max_tier: str = "ultra"

    def rule_for(self, media: str) -> str:
        return self.video_tier if media == "video" else self.image_tier

    def required_rank(self, media: str) -> Optional[int]:
        """Minimum paygate rank for this media type, or None when switched off."""
        return _RULE_RANK[normalize_rule(self.rule_for(media))]

    def max_rank(self) -> int:
        """Highest paygate rank this caller may use (2 = Ultra = no ceiling)."""
        return _MAX_RANK[normalize_max_tier(self.max_tier)]

    def as_dict(self) -> dict:
        return {
            "client": self.client,
            "image_tier": normalize_rule(self.image_tier),
            "video_tier": normalize_rule(self.video_tier),
            "note": self.note or "",
            "max_tier": normalize_max_tier(self.max_tier),
        }


class ClientPolicyStore:
    """In-memory copy of `client_policies`; `load()` after every write."""

    def __init__(self):
        self._policies: Dict[str, ClientPolicy] = {DEFAULT_CLIENT: ClientPolicy(DEFAULT_CLIENT)}

    def replace(self, rows: Iterable[Mapping]) -> None:
        policies: Dict[str, ClientPolicy] = {}
        for row in rows:
            client = normalize_client(row.get("client")) or (DEFAULT_CLIENT if row.get("client") == DEFAULT_CLIENT else "")
            if not client:
                continue
            policies[client] = ClientPolicy(
                client=client,
                image_tier=normalize_rule(row.get("image_tier")),
                video_tier=normalize_rule(row.get("video_tier")),
                note=row.get("note") or "",
                max_tier=normalize_max_tier(row.get("max_tier")),
            )
        policies.setdefault(DEFAULT_CLIENT, ClientPolicy(DEFAULT_CLIENT))
        self._policies = policies

    async def load(self, db) -> None:
        self.replace(await db.get_client_policies())

    def get(self, client: str) -> ClientPolicy:
        """The client's own policy; a named client with no row gets `default`'s minimum tiers
        under its own name and NO ceiling; an unidentified caller gets `default` itself."""
        own = self._policies.get(client or DEFAULT_CLIENT)
        if own is not None:
            return own
        base = self._policies[DEFAULT_CLIENT]
        return ClientPolicy(client=client, image_tier=base.image_tier, video_tier=base.video_tier,
                            note="", max_tier="ultra")

    def all(self) -> List[ClientPolicy]:
        return sorted(self._policies.values(), key=lambda p: (p.client != DEFAULT_CLIENT, p.client))


client_policy_store = ClientPolicyStore()


def token_reserved_for(token) -> str:
    return normalize_client(getattr(token, "reserved_client", None) or "")


def client_block_reason(token, client: str, media: str, store: ClientPolicyStore = client_policy_store) -> Optional[str]:
    """Why `client` may not generate `media` with `token`, or None when it may.

    This is THE filter: LoadBalancer.select_token, LoadBalancer.get_unavailable_reason and
    the admin token diagnostics all call it, so the three can never drift.
    """
    reserved = token_reserved_for(token)
    if reserved and reserved != client:
        return f"reserved for {reserved}"
    policy = store.get(client)
    cap = tier_cap_reason(policy, getattr(token, "user_paygate_tier", None))
    if cap:
        return cap
    need = policy.required_rank(media)
    if need is None:
        return f"client policy: {media} generation is off for {policy.client}"
    if need > 0:
        tier = normalize_user_paygate_tier(getattr(token, "user_paygate_tier", None))
        if get_paygate_tier_rank(tier) < need:
            return (
                f"client policy: {policy.client} needs a {_RANK_LABEL[need]} account for {media}, "
                f"this account is {get_paygate_tier_label(tier)}"
            )
    return None


def tier_cap_reason(policy: ClientPolicy, paygate_tier: Optional[str]) -> Optional[str]:
    """Why this account's tier is above the caller's ceiling, or None."""
    rank = get_paygate_tier_rank(normalize_user_paygate_tier(paygate_tier))
    if rank <= policy.max_rank():
        return None
    if policy.client == DEFAULT_CLIENT:
        return NAMED_ONLY_REASON
    return f"client policy: {policy.client} is capped at {_MAX_LABEL[policy.max_rank()]} accounts"


def default_cap_reason(paygate_tier: Optional[str], store: ClientPolicyStore = client_policy_store) -> Optional[str]:
    """Admin diagnostics: is this account's tier kept away from unidentified callers?"""
    return tier_cap_reason(store.get(DEFAULT_CLIENT), paygate_tier)


def no_account_error(client: str, media: str, store: ClientPolicyStore = client_policy_store) -> dict:
    """Machine-readable extra fields for the 503 when the client policy leaves no account."""
    policy = store.get(client)
    need = policy.required_rank(media)
    return {
        "code": NO_ACCOUNT_CODE,
        "client": client or DEFAULT_CLIENT,
        "media": media,
        "need": "off" if need is None else normalize_rule(policy.rule_for(media)),
        "max": normalize_max_tier(policy.max_tier),
    }
