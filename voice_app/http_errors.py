"""Display API error details, not an opaque '500 Server Error' URL."""


def check_response(response):
    if response.ok:
        return response
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):
        detail = "；".join(str(item.get("msg", item)) for item in detail[:3])
    if detail:
        raise RuntimeError(str(detail)[:1500])
    raise RuntimeError(f"本地服务请求失败（{response.status_code}），请查看诊断日志。")
