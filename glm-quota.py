#!/usr/bin/env python3
"""glm-quota.py - Z.AI GLM Coding Plan usage viewer.

Standalone Python port of the omp glm-quota extension CLI runner
(C:\\Users\\Avra\\.omp\\agent\\extensions\\glm-quota.ts). Queries the three
Z.ai monitoring endpoints and prints a report directly to the console:
no markdown intermediate, no emoji, Unicode block characters only
(works in cmd, PowerShell, Git Bash, Linux, macOS).

Authentication: ZAI_API_KEY or Z_AI_API_KEY (international) or, as a
fallback, ZHIPU_API_KEY / ZHIPUAI_API_KEY / BIGMODEL_API_KEY (CN Zhipu
bigmodel.cn plan; the CN mirror open.bigmodel.cn is queried automatically).
The raw token is expected, no Bearer prefix. No other dependencies beyond
the stdlib.

Usage: python glm-quota.py [--summary] [--limits] [--usage] [--mcp] [--model] [--tools]
"""

import sys

if sys.version_info < (3, 7):
    sys.stderr.write(
        "error: python 3.7+ required, running %d.%d.%d\n" % sys.version_info[:3]
    )
    sys.exit(1)

import argparse
import json
import math
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

# ============================================================================
# Constants
# ============================================================================

# Keys are region-locked: each key only authenticates against its own region's host.
ENDPOINTS_GLOBAL = {
    "quotaLimit": "https://api.z.ai/api/monitor/usage/quota/limit",
    "modelUsage": "https://api.z.ai/api/monitor/usage/model-usage",
    "toolUsage": "https://api.z.ai/api/monitor/usage/tool-usage",
}

ENDPOINTS_CN = {
    "quotaLimit": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
    "modelUsage": "https://open.bigmodel.cn/api/monitor/usage/model-usage",
    "toolUsage": "https://open.bigmodel.cn/api/monitor/usage/tool-usage",
}

REQUEST_TIMEOUT_S = 10

FIVE_HOUR_LABEL = "Token usage(5 Hour)"
WEEKLY_LABEL = "Token usage(Weekly)"
MCP_LABEL = "MCP usage(1 Month)"
TOKENS_LIMIT = "TOKENS_LIMIT"
DEFAULT_TOKEN_LIMIT = 40_000_000

PROGRESS_WIDTH = 25
TREND_LEVELS = "▁▂▃▄▅▆▇"
TREND_FULL = "█"
EIGHTH_BLOCKS = "▏▎▍▌▋▊▉"
TREND_LEVELS_ASCII = "_.,-~+*"
TREND_FULL_ASCII = "#"

# ============================================================================
# Credentials
# ============================================================================

INT_API_KEY_VARS = ("ZAI_API_KEY", "Z_AI_API_KEY")
CN_API_KEY_VARS = ("ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY")


def get_credentials():
    """Return (token, endpoints); CN keys route to the open.bigmodel.cn mirror."""
    for var in INT_API_KEY_VARS:
        token = os.environ.get(var)
        if token:
            return token, ENDPOINTS_GLOBAL
    for var in CN_API_KEY_VARS:
        token = os.environ.get(var)
        if token:
            return token, ENDPOINTS_CN
    return None, ENDPOINTS_GLOBAL

# ============================================================================
# API client: fail-fast, no retries
# ============================================================================

def get_time_window(now=None):
    """24h rolling window in local time: yesterday HH:00:00 → today HH:59:59."""
    now = now or datetime.now()
    start = (now - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    end = now.replace(minute=59, second=59, microsecond=0)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def fetch_json(url, token, query_params=None):
    if query_params:
        url = f"{url}?{query_params}"
    request = urllib.request.Request(
        url,
            # z.ai expects the raw token: no "Bearer" prefix.
        headers={
            "Authorization": token,
            "Accept-Language": "en-US,en",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        body = response.read()
    return json.loads(body)


def fetch_usage(token, endpoints):
    """Fetch all three endpoints in parallel; per-endpoint failure → None.

    Returns None only when every endpoint failed.
    """
    window = get_time_window()
    query = "startTime={}&endTime={}".format(
        urllib.parse.quote(window[0], safe=""),
        urllib.parse.quote(window[1], safe=""),
    )

    def safe_fetch(url, params=None):
        try:
            return fetch_json(url, token, params)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        quota_future = pool.submit(safe_fetch, endpoints["quotaLimit"])
        model_future = pool.submit(safe_fetch, endpoints["modelUsage"], query)
        tool_future = pool.submit(safe_fetch, endpoints["toolUsage"], query)
        quota = quota_future.result()
        model = model_future.result()
        tool = tool_future.result()

    if quota is None and model is None and tool is None:
        return None
    return window, quota, model, tool

# ============================================================================
# Response parsing (zod-equivalent tolerance: wrong types demote or drop rows)
# ============================================================================

def _is_number(value):
    # JSON booleans are not numbers (matches zod z.number()).
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def unwrap_envelope(value):
    """z.ai monitoring responses arrive as {code, data, message}; fields live in data."""
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    return value


def mcp_tool_label(model_code):
    if model_code == "search-prime":
        return "Network Searches"
    if model_code == "web-reader":
        return "Web Reads"
    if model_code == "zread":
        return "ZRead Calls"
    return model_code


def _try_tokens_limit(item):
    """TOKENS_LIMIT row: unit 3/number 5 → 5h window, unit 6/number 1 → weekly."""
    values = {}
    for key in ("unit", "number", "total", "nextResetTime"):
        value = item.get(key)
        if value is None:
            values[key] = None
        elif _is_number(value):
            values[key] = value
        else:
            return None  # schema failure → fall through to the next schema
    percentage = item.get("percentage")
    if not _is_number(percentage):
        percentage = 0
    unit, number = values["unit"], values["number"]
    if unit == 3 and number == 5:
        label = FIVE_HOUR_LABEL
    elif unit == 6 and number == 1:
        label = WEEKLY_LABEL
    elif unit is not None and number is not None:
        label = f"Token usage(unit={unit}, number={number})"
    else:
        label = "Token usage"
    return {
        "label": label,
        "raw_type": TOKENS_LIMIT,
        "percentage": percentage,
        "total": values["total"],
        "next_reset_time": values["nextResetTime"],
    }


def _try_time_limit(item):
    """TIME_LIMIT row: the monthly MCP quota."""
    current_value = item.get("currentValue")
    if current_value is not None and not _is_number(current_value):
        return None
    usage = item.get("usage")
    if usage is not None and not _is_number(usage):
        return None
    raw_details = item.get("usageDetails")
    details = None
    if raw_details is not None:
        if not isinstance(raw_details, list):
            return None
        details = []
        for entry in raw_details:
            if not isinstance(entry, dict) or not _is_number(entry.get("usage")):
                return None
            model_code = entry.get("modelCode")
            if not isinstance(model_code, str):
                model_code = "unknown"
            details.append({"label": mcp_tool_label(model_code), "usage": entry["usage"]})
    percentage = item.get("percentage")
    if not _is_number(percentage):
        percentage = 0
    return {
        "label": MCP_LABEL,
        "percentage": percentage,
        "current_value": current_value,
        "total": usage,
        "usage_details": details,
    }


def _fallback_limit(item):
    label = item.get("type")
    if not isinstance(label, str):
        label = "Unknown"
    percentage = item.get("percentage")
    if not _is_number(percentage):
        percentage = 0
    return {"label": label, "percentage": percentage}


def parse_limit(item):
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == TOKENS_LIMIT:
        parsed = _try_tokens_limit(item)
        if parsed is not None:
            return parsed
    if item_type == "TIME_LIMIT":
        parsed = _try_time_limit(item)
        if parsed is not None:
            return parsed
    return _fallback_limit(item)


def parse_quota(data):
    if not isinstance(data, dict):
        return {"level": None, "limits": []}
    level = data.get("level")
    if level is not None and not isinstance(level, str):
        return {"level": None, "limits": []}
    raw_limits = data.get("limits")
    if not isinstance(raw_limits, list):
        raw_limits = []
    limits = [parsed for parsed in map(parse_limit, raw_limits) if parsed is not None]
    return {"level": level, "limits": limits}


_TOTAL_FIELDS = (
    "totalTokensUsage",
    "totalModelCallCount",
    "totalNetworkSearchCount",
    "totalWebReadMcpCount",
    "totalZreadMcpCount",
)


def parse_totals(data):
    if not isinstance(data, dict):
        return {}
    tokens_usage = data.get("tokensUsage")
    if tokens_usage is not None and (
        not isinstance(tokens_usage, list) or not all(_is_number(v) for v in tokens_usage)
    ):
        return {}
    total_usage = data.get("totalUsage")
    if total_usage is not None and not isinstance(total_usage, dict):
        return {}
    result = {}
    if isinstance(total_usage, dict):
        for field in _TOTAL_FIELDS:
            value = total_usage.get(field)
            if _is_number(value):
                result[field] = value
    if tokens_usage is not None:
        result["hourlyTokens"] = tokens_usage
    return result

# ============================================================================
# Formatting
# ============================================================================

def fmt_num(value):
    """Thousands separators; integral floats print as integers (JS parity)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def fmt_raw(value):
    """Plain number-to-string (JS template literal parity)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def js_round(value):
    # JS Math.round rounds half up; Python round() is banker's rounding.
    return math.floor(value + 0.5)


def progress_bar(percentage, ascii_mode=False):
    clamped = min(100.0, max(0.0, percentage))
    if ascii_mode:
        filled = js_round(clamped / 100.0 * PROGRESS_WIDTH)
        return "#" * filled + " " * (PROGRESS_WIDTH - filled)
    cells = clamped / 100.0 * PROGRESS_WIDTH
    full = math.floor(cells)
    eighths = js_round((cells - full) * 8)
    if eighths == 8:
        full += 1
        eighths = 0
    bar = "█" * full + (EIGHTH_BLOCKS[eighths - 1] if eighths else "")
    return bar + " " * (PROGRESS_WIDTH - len(bar))


def format_reset_cell(reset_ms):
    if reset_ms is None:
        return "-"
    now_ms = datetime.now().timestamp() * 1000
    diff_minutes = math.floor((reset_ms - now_ms) / 60000)
    if diff_minutes <= 0:
        return "-"
    reset = datetime.fromtimestamp(reset_ms / 1000)
    clock = f"{reset.hour:02d}:{reset.minute:02d}"
    if diff_minutes >= 24 * 60:
        total_hours = diff_minutes // 60
        # datetime.weekday() is Mon=0; JS getDay() was Sun=0.
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"{total_hours // 24}d {total_hours % 24}h ({day_names[reset.weekday()]} {clock})"
    return f"{diff_minutes // 60}h {diff_minutes % 60}m ({clock})"


def trend_bars(hourly_tokens, ascii_mode=False):
    """Hourly tokens → bars split into top/bottom halves, max-normalized."""
    if not hourly_tokens:
        return None
    levels = TREND_LEVELS_ASCII if ascii_mode else TREND_LEVELS
    full = TREND_FULL_ASCII if ascii_mode else TREND_FULL
    max_value = max(hourly_tokens)
    steps = len(levels) - 1
    top = []
    bottom = []
    for value in hourly_tokens:
        if value <= 0 or max_value <= 0:
            level = 0
        else:
            level = max(1, math.ceil((value / max_value) * (steps * 2)))
        if level >= steps * 2:
            top.append(full)
        elif level > steps:
            top.append(levels[level - steps])
        else:
            top.append(" ")
        if level > steps:
            bottom.append(full)
        elif level > 0:
            bottom.append(levels[level])
        else:
            bottom.append(levels[0])
    return "".join(top), "".join(bottom)

# ============================================================================
# Report rendering — console tables with dash framing
# ============================================================================

def render_table(headers, aligns, rows, ascii_mode=False):
    widths = []
    for column, header in enumerate(headers):
        width = len(header)
        for row in rows:
            cell = row[column] if column < len(row) else ""
            width = max(width, len(cell))
        widths.append(max(3, width))

    def padded_cells(cells):
        padded = []
        for column, cell in enumerate(cells):
            width = widths[column]
            padded.append(cell.rjust(width) if aligns[column] == "right" else cell.ljust(width))
        return padded

    if ascii_mode:
        def row_line(cells):
            return "| " + " | ".join(padded_cells(cells)) + " |"

        lines = [row_line(headers)]
        lines.append("| " + " | ".join("-" * width for width in widths) + " |")
        lines.extend(row_line(row) for row in rows)
        rule = "-" * len(lines[0])
        lines.append(rule)
        lines.insert(0, rule)
        return "\n".join(lines)

    def box_line(cells):
        return "│ " + " │ ".join(padded_cells(cells)) + " │"

    top = "┌" + "┬".join("─" * (width + 2) for width in widths) + "┐"
    separator = "├" + "┼".join("─" * (width + 2) for width in widths) + "┤"
    bottom = "└" + "┴".join("─" * (width + 2) for width in widths) + "┘"
    lines = [box_line(headers), separator]
    lines.extend(box_line(row) for row in rows)
    return "\n".join([top, *lines, bottom])


def quota_window_label(limit):
    if limit["label"] == FIVE_HOUR_LABEL:
        return "5h Token"
    if limit["label"] == WEEKLY_LABEL:
        return "Weekly"
    if limit["label"] == MCP_LABEL:
        return "MCP (1 Month)"
    return limit["label"]


def find_limit(quota, label):
    for limit in quota["limits"]:
        if limit["label"] == label:
            return limit
    return None


def five_hour_limit(quota):
    """The 5-hour TOKENS_LIMIT window drives the headline token numbers."""
    limit = find_limit(quota, FIVE_HOUR_LABEL)
    if limit is None:
        for candidate in quota["limits"]:
            if candidate.get("raw_type") == TOKENS_LIMIT:
                return candidate
    return limit


TABLE_ORDER = ("limits", "usage", "mcp", "model", "tools")


def build_tables(quota, model, tool, ascii_mode=False):
    limit_rows = [
        [
            quota_window_label(limit),
            f"{limit['percentage']:.1f}%",
            progress_bar(limit["percentage"], ascii_mode),
            "-" if limit["label"] == MCP_LABEL else format_reset_cell(limit.get("next_reset_time")),
        ]
        for limit in quota["limits"]
    ]
    limits_table = render_table(
        ["Quota Limits", "Usage", "Progress", "Resets In"],
        ["left", "right", "left", "left"],
        limit_rows or [["No quota data available", "-", "-", "-"]],
        ascii_mode,
    )

    five_hour = five_hour_limit(quota)
    token_limit = five_hour.get("total") if five_hour else None
    if token_limit is None:
        token_limit = DEFAULT_TOKEN_LIMIT
    mcp = find_limit(quota, MCP_LABEL)
    if mcp is None or mcp.get("current_value") is None or mcp.get("total") is None:
        mcp_used = "-"
    else:
        mcp_used = f"{fmt_raw(mcp['current_value'])} / {fmt_raw(mcp['total'])}"
    tokens = model.get("totalTokensUsage")
    usage_table = render_table(
        ["Quota Usage", "Value"],
        ["left", "right"],
        [
            [
                "Tokens (24h)",
                "-" if tokens is None else f"{fmt_num(tokens)} (5h limit: {fmt_num(token_limit)})",
            ],
            ["MCP Used", mcp_used],
        ],
        ascii_mode,
    )

    mcp_rows = []
    if mcp is not None:
        mcp_rows = [
            [detail["label"], fmt_num(detail["usage"])]
            for detail in (mcp.get("usage_details") or [])
        ]
    mcp_table = render_table(
        ["MCP Tool Breakdown", "Count"],
        ["left", "right"],
        mcp_rows or [["No MCP data available", "-"]],
        ascii_mode,
    )

    trend = trend_bars(model.get("hourlyTokens"), ascii_mode)
    model_headers = ["Model Usage (24h)", "Value"]
    model_aligns = ["left", "right"]
    model_rows = []
    if tokens is not None:
        model_rows.append(["Total Tokens", fmt_num(tokens)])
    calls = model.get("totalModelCallCount")
    if calls is not None:
        model_rows.append(["Total Calls", fmt_num(calls)])
    if trend and len(model_rows) == 2:
        model_headers.append("Trend")
        model_aligns.append("left")
        model_rows[0].append(trend[0])
        model_rows[1].append(trend[1])
    model_table = render_table(
        model_headers,
        model_aligns,
        model_rows or [["No model usage data available", "-"]],
        ascii_mode,
    )

    tool_counts = (
        ("Network Searches", tool.get("totalNetworkSearchCount")),
        ("Web Reads", tool.get("totalWebReadMcpCount")),
        ("ZRead Calls", tool.get("totalZreadMcpCount")),
    )
    tool_rows = [
        [label, fmt_num(count)] for label, count in tool_counts if count is not None
    ]
    tool_table = render_table(
        ["Tool Usage (24h)", "Count"],
        ["left", "right"],
        tool_rows or [["No tool usage data available", "-"]],
        ascii_mode,
    )

    return {
        "limits": limits_table,
        "usage": usage_table,
        "mcp": mcp_table,
        "model": model_table,
        "tools": tool_table,
    }


def render_report(window, quota, model, tool, ascii_mode=False):
    level = quota.get("level")
    level_part = f" - {level[0].upper()}{level[1:].lower()}" if level else ""
    header = (
        f"Z.AI GLM Coding Plan{level_part}\n"
        f"Period: {window[0]} -> {window[1]}"
    )
    tables = build_tables(quota, model, tool, ascii_mode)
    return "\n\n".join([header] + [tables[key] for key in TABLE_ORDER])


def render_summary(quota):
    limit = five_hour_limit(quota)
    if limit is None:
        return None
    line = f"GLM 5h Token window at {limit['percentage']:.1f}%"
    reset = format_reset_cell(limit.get("next_reset_time"))
    if reset != "-":
        line += f", resets in {reset}"
    return line

# ============================================================================
# Entry point
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        prog="glm-quota.py",
        description="Z.AI GLM Coding Plan usage viewer.",
    )
    parser.add_argument("--summary", action="store_true",
                        help="one-line 5h window status, nothing else")
    parser.add_argument("--ascii", action="store_true",
                        help="pure ASCII output: #/- progress bars, ASCII trend ladder")
    parser.add_argument("--limits", action="store_true",
                        help="quota limits table only")
    parser.add_argument("--usage", action="store_true",
                        help="quota usage table only")
    parser.add_argument("--mcp", action="store_true",
                        help="MCP tool breakdown table only")
    parser.add_argument("--model", action="store_true",
                        help="model usage table only")
    parser.add_argument("--tools", action="store_true",
                        help="tool usage table only")
    return parser.parse_args()


def main():
    # mintty/pipes use the locale encoding; force UTF-8 so block glyphs survive.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")

    args = parse_args()

    token, endpoints = get_credentials()
    if not token:
        print(
            "error: no credentials - set ZAI_API_KEY/Z_AI_API_KEY (global) "
            "or ZHIPU_API_KEY/ZHIPUAI_API_KEY/BIGMODEL_API_KEY (CN)",
            file=sys.stderr,
        )
        return 1

    usage = fetch_usage(token, endpoints)
    if usage is None:
        print(
            "error: all usage endpoints failed - check network / subscription",
            file=sys.stderr,
        )
        return 1

    window, raw_quota, raw_model, raw_tool = usage
    quota = parse_quota(unwrap_envelope(raw_quota)) if raw_quota is not None else {"level": None, "limits": []}
    model = parse_totals(unwrap_envelope(raw_model)) if raw_model is not None else {}
    tool = parse_totals(unwrap_envelope(raw_tool)) if raw_tool is not None else {}

    if args.summary:
        summary = render_summary(quota)
        if summary is None:
            print("error: no 5h token window data", file=sys.stderr)
            return 1
        print(summary)
        return 0

    selected = [key for key in TABLE_ORDER if getattr(args, key)]
    if selected:
        tables = build_tables(quota, model, tool, args.ascii)
        print("\n\n".join(tables[key] for key in selected))
        return 0

    print(render_report(window, quota, model, tool, args.ascii))
    return 0


if __name__ == "__main__":
    sys.exit(main())
