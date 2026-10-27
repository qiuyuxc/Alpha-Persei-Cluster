"""Resolve one request's platform without sharing state between clients."""
import re

ALIASES = {
    "android": "android", "androidplayer": "android",
    "ios": "ios", "iphone": "ios", "ipad": "ios", "iphoneplayer": "ios",
    "pc": "pc", "windows": "pc", "win32": "pc", "win64": "pc",
    "windowsplayer": "pc", "windowseditor": "pc", "standalonewindows": "pc",
    "standalonewindows64": "pc",
}


def normalize_platform(value):
    if not isinstance(value, str):
        return None
    return ALIASES.get(value.strip().strip('"').lower())


def detect_platform(*parameters, headers=None, path=""):
    # CDN paths are explicit; never let a browser UA select another directory.
    for segment in str(path).split("?", 1)[0].lower().split("/"):
        if segment in ("android", "ios", "pc"):
            return segment
    for source in parameters:
        if not hasattr(source, "get"):
            continue
        for key in ("platformType", "platform", "osType"):
            value = source.get(key)
            if value is not None and value != "":
                platform = normalize_platform(value)
                if platform:
                    return platform
                raise ValueError("不支持的平台参数，请指定 android、ios 或 pc")
    header = {str(k).lower(): v for k, v in (headers or {}).items()}
    for key in ("x-client-platform", "x-platform", "sec-ch-ua-platform"):
        platform = normalize_platform(header.get(key))
        if platform:
            return platform
    ua = str(header.get("user-agent", "")).lower()
    if re.search(r"\bandroid\b", ua):
        return "android"
    if re.search(r"\bwindows(?:\s|/|;|\)|$)", ua):
        return "pc"
    if re.search(r"\b(?:iphone|ipad|ipod|ios)\b", ua):
        return "ios"
    if "cfnetwork/" in ua and "darwin/" in ua and "macintosh" not in ua:
        return "ios"
    if "macintosh" in ua and "mobile/" in ua:
        return "ios"
    # Channel identifiers differ across SDK distributions (including Android
    # clients using channel 7). Neither channel nor the previous request is OS.
    return None


def request_platform(request):
    platform = detect_platform(request.args, request.form,
                               request.get_json(silent=True), headers=request.headers)
    if platform is None:
        raise ValueError("无法识别客户端平台，请携带 platformType=android、ios 或 pc")
    return platform
