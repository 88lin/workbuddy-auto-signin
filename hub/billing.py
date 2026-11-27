"""当前积分余额：解析与查询。

口径：账单套餐剩余额度（CycleCapacityRemain 钳位到 CycleCapacitySize；脏数据用
CycleCapacityUsed 反修正；无周期字段回退 CapacityRemain），与官方客户端一致。
签到活动的 total_credits 是赛季累计，不是余额，这里不采用。

响应解析是纯函数，便于用构造的脏数据做单测，不依赖真实网络。
"""
import time
from datetime import datetime

import signin

from . import config


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _round_credits(value):
    number = float(value)
    return int(number) if number.is_integer() else round(number, 2)


def _resource_accounts(data):
    """兼容 Response / Data / data 多层信封，把套餐数组取出来。"""
    current = data
    for _ in range(4):
        if isinstance(current, dict):
            if isinstance(current.get("Accounts"), list):
                return current["Accounts"]
            for key in ("Response", "Data", "data", "response"):
                if isinstance(current.get(key), (dict, list)):
                    current = current[key]
                    break
            else:
                return None
        elif isinstance(current, list):
            return current
        else:
            return None
    return None


def _package_remain(item):
    """单套餐剩余额度：周期内钳位，脏数据按 used 反修正。"""
    size = _num(item.get("CycleCapacitySize"))
    if size > 0:
        remain = min(max(_num(item.get("CycleCapacityRemain")), 0.0), size)
        used = size - remain
        cycle_used = _num(item.get("CycleCapacityUsed"))
        if cycle_used > used:
            used = cycle_used
            if size >= used:
                remain = size - used
        return remain
    return _num(item.get("CapacityRemain"))


def parse_balance_response(response):
    """把 HTTP 响应解析成余额摘要。非 dict / 未知结构返回明确错误。"""
    if not isinstance(response, dict):
        return {"error": "unexpected response type: %s" % type(response).__name__}

    business_code = response.get("code")
    if business_code is None and isinstance(response.get("Response"), dict):
        business_code = response["Response"].get("Code")
    data = (response.get("data") or response.get("Data")
            or response.get("Response") or response)
    if business_code not in (None, 0):
        return {"error": "业务码 %s" % business_code}

    accounts = _resource_accounts(data)
    if accounts is None:
        return {"error": "响应结构无法识别"}

    total, expiries = 0.0, []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        remain = max(0.0, _package_remain(item))
        total += remain
        end_time = item.get("CycleEndTime")
        if remain > 0 and isinstance(end_time, str) and end_time.strip():
            expiries.append({"at": end_time.strip(), "amount": _round_credits(remain)})
    return {"balance": _round_credits(total), "expiries": expiries, "error": ""}


def build_payload(timestamp=None):
    """构造账单查询请求体；时间窗固定为当前起 101 年，覆盖长期套餐。"""
    ts = time.time() if timestamp is None else timestamp
    return {
        "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        "PackageEndTimeRangeEnd": datetime.fromtimestamp(
            ts + 365 * 101 * 24 * 3600).strftime("%Y-%m-%d %H:%M:%S"),
    }


def query_balance(entry, request=None):
    """查单个账号当前积分。request 可注入以便测试，不触碰真实网络。"""
    try:
        session = signin.resolve_session(signin.load_session(signin.credential_path(entry)))
        headers = signin.build_headers(session)
        headers.update({
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "zh-CN",
            "Origin": "https://www.codebuddy.cn",
            "Referer": "https://www.codebuddy.cn/",
        })
        sender = request or signin._request
        code, response = sender(config.BILLING_URL, headers, method="POST",
                                payload=build_payload())
        if not (200 <= code < 300):
            return {"error": "HTTP %s" % code}
        return parse_balance_response(response)
    except Exception as exc:
        return {"error": type(exc).__name__ + ": " + str(exc)[:150]}


def fetch_balances(entries, query=None, delay=1.0):
    """串行查询所有账号并退避，避免高频请求触发风控。"""
    query = query or query_balance
    balances = {}
    for entry in entries:
        balances[entry["name"]] = query(entry)
        if delay:
            time.sleep(delay)
    return balances
