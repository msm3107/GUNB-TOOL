"""Canonical local destinations; never accepts a capability URL or external redirect."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit


def safe_return_path(value: str = "/panel") -> str:
    if not isinstance(value, str) or len(value) > 2048 or not value.isascii() or any(ord(c) < 33 or ord(c) == 127 or c == "\\" for c in value):
        raise ValueError("Invalid destination")
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.fragment:
        raise ValueError("Invalid destination")
    match = re.fullmatch(r"/panel(?:/paczki/([1-9][0-9]{0,18}))?", parts.path)
    if not match or (match[1] and int(match[1]) >= 2**63) or re.search(r"%(?![0-9a-fA-F]{2})", parts.query):
        raise ValueError("Invalid destination")
    if not parts.query:
        return parts.path
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=4)
    values = dict(pairs)
    allowed = {"page", "category", "case_id", "notice"} if match[1] else {"orders_before", "deliveries_before"}
    if len(pairs) != len(values) or not values.keys() <= allowed:
        raise ValueError("Invalid destination")
    for key, text in pairs:
        if key in {"page", "orders_before", "deliveries_before"}:
            limit = 200 if key == "page" else 2**63 - 1
            if not re.fullmatch(r"[1-9][0-9]{0,18}", text) or int(text) > limit:
                raise ValueError("Invalid destination")
        elif key == "category" and text not in {"all", "new", "change", "full"} or key == "case_id" and (not 1 <= len(text) <= 512 or not text.isprintable()) or key == "notice" and text != "issue":
            raise ValueError("Invalid destination")
    return parts.path + ("?" + urlencode(pairs) if pairs else "")


def login_url(destination: str) -> str:
    return "/logowanie?" + urlencode({"next": safe_return_path(destination)})
