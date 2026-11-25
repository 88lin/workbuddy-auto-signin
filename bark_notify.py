"""Small standard-library client for native Bark JSON notifications."""

import json
import os
import urllib.request


DEFAULT_SERVER = "https://api.day.app"
DEFAULT_GROUP = "签到与额度"


def targets(config):
    """Return unique (device_key, group) targets, preserving configured order."""
    found = {}
    default_group = str(config.get("bark_group") or DEFAULT_GROUP)
    singles = [config.get("bark_key")]
    keys = config.get("bark_keys") or []
    if isinstance(keys, (str, bytes)):
        keys = [keys]
    for key in singles + list(keys):
        key = str(key or "").strip()
        if key:
            found.setdefault(key, default_group)
    extras = config.get("bark_extra") or []
    if isinstance(extras, dict):
        extras = [extras]
    for item in extras:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            if key:
                found.setdefault(key, str(item.get("group") or default_group))
    return list(found.items())


def _post_json(url, payload, timeout=20):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", "replace")


def push_one(server, key, title, body, *, level="timeSensitive", group=DEFAULT_GROUP,
             subtitle=None, url=None, post_json=None):
    """Post one native Bark JSON notification and validate the response code."""
    key = str(key or "").strip()
    if not key or level not in {"passive", "active", "timeSensitive"}:
        return False
    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "group": group or DEFAULT_GROUP,
        "level": level,
        "isArchive": 1,
    }
    if subtitle:
        payload["subtitle"] = subtitle
    if url:
        payload["url"] = url
    try:
        status, raw = (post_json or _post_json)(str(server or DEFAULT_SERVER).rstrip("/") + "/push", payload)
        result = json.loads(raw)
        return 200 <= int(status) < 300 and isinstance(result, dict) and result.get("code") == 200
    except (Exception,):
        return False


def push(config, title, body, *, level="timeSensitive", subtitle=None,
         group=None, url=None, post_json=None, on_result=None):
    """Try every configured device; one failure never prevents later sends."""
    target_config = dict(config)
    if group:
        target_config["bark_group"] = group
    device_targets = targets(target_config)
    results = []
    for key, group in device_targets:
        ok = push_one(config.get("bark_server") or DEFAULT_SERVER, key, title, body,
                      level=level, group=group or config.get("bark_group") or DEFAULT_GROUP,
                      subtitle=subtitle, url=url, post_json=post_json)
        results.append(ok)
        if on_result:
            on_result(key, ok, title)
    return any(results)


def notify(source, title, body, *, group=DEFAULT_GROUP, level="active", subtitle=None, url=None):
    """Convenience interface shared with sibling Bark integrations in this repo."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qoder-watch-config.json")
    try:
        with open(config_path, encoding="utf-8") as config_file:
            config = json.load(config_file)
        if not isinstance(config, dict):
            return False
    except (OSError, ValueError):
        return False
    return push(config, title, body, group=group, level=level,
                subtitle=subtitle or source, url=url)
