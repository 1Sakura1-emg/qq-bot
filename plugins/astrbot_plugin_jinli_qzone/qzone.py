import json
import re
from http.cookies import SimpleCookie

import httpx
import json5

BASE = "https://user.qzone.qq.com"
FEEDS = BASE + "/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
PUBLISH = BASE + "/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"


class QzoneError(RuntimeError):
    """A sanitized error that never includes account credentials."""


def parse_response(text: str) -> dict:
    """Decode JSON or JSONP without evaluating JavaScript.

    Args:
        text: The response body returned by Qzone.

    Returns:
        The decoded response object.

    Raises:
        QzoneError: If the body is not a successful Qzone response.
    """
    text = text.strip()
    match = re.fullmatch(r"[\w.]+\s*\((.*)\)\s*;?", text, flags=re.S)
    if match:
        text = match.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    # Qzone sometimes emits JavaScript undefined values inside JSONP objects.
    # Preserve quoted strings while converting only bare undefined tokens.
    text = re.sub(
        r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\bundefined\b""",
        lambda match: "null" if match.group(0) == "undefined" else match.group(0),
        text,
    )
    try:
        payload = json.loads(text)
    except ValueError:
        try:
            payload = json5.loads(text)
        except ValueError:
            raise QzoneError("QZONE_RESPONSE_INVALID_JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("code"), int):
        raise QzoneError("QZONE_RESPONSE_MISSING_CODE")
    if payload["code"] != 0:
        raise QzoneError(f"QZONE_REJECTED_{payload['code']}")
    return payload


async def request_qzone(bot, account_id: str, content: str | None = None) -> dict:
    """Check Qzone access, or publish one text post when content is supplied.

    Args:
        bot: The authenticated OneBot client for the configured QQ account.
        account_id: The only account that may be used.
        content: A text post, or None for a read-only connection check.

    Returns:
        A successful Qzone response object.

    Raises:
        QzoneError: If account validation, network access, or Qzone fails.
    """
    try:
        info = await bot.get_login_info()
        if str(info.get("user_id")) != account_id:
            raise QzoneError("ACCOUNT_MISMATCH")
        result = await bot.get_cookies(domain="user.qzone.qq.com")
    except QzoneError:
        raise
    except Exception:
        raise QzoneError("QQ_CONNECTION_UNAVAILABLE") from None

    cookies = {
        name: value.value
        for name, value in SimpleCookie(result.get("cookies") or "").items()
    }
    cookie_account = cookies.get("uin", "").lstrip("oO0")
    if cookie_account and cookie_account != account_id.lstrip("0"):
        raise QzoneError("COOKIE_ACCOUNT_MISMATCH")
    key = cookies.get("p_skey") or cookies.get("skey")
    if not key:
        raise QzoneError("QZONE_LOGIN_MISSING")
    cookies.setdefault("uin", "o" + account_id)
    token = 5381
    for character in key:
        token = ((token << 5) + token + ord(character)) & 0x7FFFFFFF
    headers = {
        "Referer": f"{BASE}/{account_id}",
        "Origin": BASE,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
    }
    try:
        async with httpx.AsyncClient(
            cookies=cookies, headers=headers, timeout=25, follow_redirects=False
        ) as client:
            if content is None:
                response = await client.get(
                    FEEDS,
                    params={
                        "uin": account_id,
                        "g_tk": token,
                        "scope": 0,
                        "view": 1,
                        "filter": "all",
                        "flag": 1,
                        "applist": "all",
                        "pagenum": 1,
                        "format": "json",
                        "useutf8": 1,
                        "outputhtmlfeed": 1,
                    },
                )
            else:
                response = await client.post(
                    PUBLISH,
                    params={"g_tk": token, "uin": account_id},
                    data={
                        "con": content,
                        "hostuin": account_id,
                        "format": "json",
                        "qzreferrer": f"{BASE}/{account_id}",
                        "syn_tweet_verson": "1",
                        "paramstr": "1",
                        "feedversion": "1",
                        "ver": "1",
                        "code_version": "1",
                        "ugc_right": "1",
                        "to_sign": "0",
                        "who_can_see": "1",
                    },
                )
            response.raise_for_status()
    except httpx.HTTPError:
        # Do not include the URL, because its query contains an auth token.
        raise QzoneError("QZONE_NETWORK_OR_HTTP_ERROR") from None
    try:
        return parse_response(response.text)
    except QzoneError as exc:
        if content is None and str(exc) == "QZONE_RESPONSE_INVALID_JSON":
            shape = re.sub(r"\w", "x", response.text[:160])
            raise QzoneError(
                f"QZONE_RESPONSE_INVALID_JSON length={len(response.text)} shape={shape!r}"
            ) from None
        raise
