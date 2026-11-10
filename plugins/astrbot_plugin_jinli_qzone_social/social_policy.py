import hashlib
import json
import re


def public_candidate(feed, self_id):
    """Extract a real recommendation handle and its readable text.

    Args:
        feed: The complete upstream recommendation, passed unchanged to writes.
        self_id: Account that must never comment on itself through recommendations.

    Returns:
        Candidate metadata, or None for ads, missing handles, or unreadable posts.
    """
    common = feed.get("cell_common") or {}
    union = common.get("union_id") or {}
    user = (feed.get("cell_user_info") or {}).get("user") or {}
    summary = feed.get("cell_summary") or {}
    if common.get("feed_type") == 3 or feed.get("cell_adv"):
        return None
    author = str(user.get("uin") or user.get("uid") or "")
    if (
        not union.get("uf_key")
        or not author
        or str(user.get("uin") or "") == str(self_id)
    ):
        return None
    text = "".join(
        str(item.get("content") or "")
        for item in summary.get("summary", [])
        if isinstance(item, dict) and item.get("type") == 0
    ).strip()
    if not text or summary.get("has_more"):
        return None
    identity = {"author": author, "uf_key": union["uf_key"]}
    key = (
        "public:"
        + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    )
    return {
        "key": key,
        "text": text[:3000],
        "author": str(user.get("nick") or "这位空友")[:80],
        "feed": feed,
    }


def own_reply_candidates(detail, self_id, since):
    """Select new comments on the account's own post without replying to self.

    Args:
        detail: Actual own-post detail containing upstream comment IDs.
        self_id: The bot account.
        since: Activation timestamp; earlier comments are left untouched.

    Returns:
        Reply candidates retaining the real parent identity and target author.
    """
    tid = str(detail.get("tid") or "")
    if not tid:
        return []
    candidates = []
    for parent in detail.get("comments", []):
        parent_id = parent.get("comment_id")
        parent_uin = parent.get("uin")
        if parent_id is None or str(parent_id) == "" or not parent_uin:
            continue
        items = [(parent, False)]
        for reply in parent.get("replies", []):
            # Do not interrupt nested conversations between two other people.
            if str(parent_uin) == str(self_id) or f"uin:{self_id}," in str(
                reply.get("content", "")
            ):
                items.append((reply, True))
        for item, nested in items:
            sender = item.get("uin")
            cid = item.get("comment_id")
            stamp = item.get("time", 0)
            if (
                str(sender) == str(self_id)
                or not sender
                or cid is None
                or str(cid) == ""
            ):
                continue
            if not isinstance(stamp, (int, float)) or stamp < since:
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            params = {
                "tid": tid,
                "target_uin": int(self_id),
                "comment_id": str(parent_id),
                "comment_uin": int(parent_uin),
            }
            if nested:
                if not item.get("name"):
                    continue
                params.update(reply_uin=int(sender), reply_name=str(item["name"]))
            candidates.append(
                {
                    "key": f"own:{tid}:{parent_id}:{cid}:{sender}:{int(stamp)}",
                    "text": content[:1500],
                    "post": str(detail.get("content") or "")[:2500],
                    "author": str(item.get("name") or "这位空友")[:80],
                    "params": params,
                }
            )
    return candidates


def validate_comment(text):
    """Validate a plain public reply without identifiers or generated links.

    Args:
        text: The direct model output, with no private history attached.

    Returns:
        Clean public text, or None when the model opts out or output is unsafe.
    """
    text = str(text or "").strip().strip('"“”')
    if text == "__SKIP__" or not 2 <= len(text) <= 140:
        return None
    if re.search(r"https?://|sk-|\d{5,}|<\s*(?:script|system)|DSML", text, re.I):
        return None
    return text
