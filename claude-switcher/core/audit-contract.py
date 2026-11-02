#!/usr/bin/env python3
"""Validate the single, complete launch audit contract used by both platforms."""
import argparse
import json
from pathlib import Path
import re
import sys


ALL_CHECKS = ("代理内核", "TUN网卡", "出口IP", "IP一致", "时区", "地区", "IPv6", "DNS",
              "WebRTC", "保镖", "规则置顶", "客户端语言", "ASN属性", "生态可达性")
OPTIONAL = frozenset(("IP一致", "DNS", "WebRTC", "客户端语言", "ASN属性", "生态可达性"))
HARD = frozenset(ALL_CHECKS) - OPTIONAL


def load_config(path):
    if path is None or not Path(path).exists():
        return {"disabled": [], "allowOptionalFailures": False, "logging": True}
    data = Path(path).read_bytes()
    if len(data) > 65536:
        raise ValueError("检查配置过大")
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) - {"disabled", "allowOptionalFailures", "logging"}:
        raise ValueError("检查配置字段无效")
    disabled = value.get("disabled", [])
    if not isinstance(disabled, list) or any(not isinstance(name, str) or name not in OPTIONAL for name in disabled):
        raise ValueError("禁用清单包含硬项或未知项")
    for name in ("allowOptionalFailures", "logging"):
        if name in value and type(value[name]) is not bool:
            raise ValueError("检查配置布尔字段无效")
    return value


def validate_report(text, config):
    if len(text.encode("utf-8")) > 65536:
        return False, ["审计输出过大"], {}
    statuses = {}
    errors = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([^=\r\n]+)=(ok|warn|fail|skip)(?::[^\r\n]*)?", line)
        if not match or match[1] not in ALL_CHECKS:
            errors.append("包含未知或格式错误的审计结果")
            continue
        name, state = match[1], match[2]
        if name in statuses:
            errors.append("检查项重复:" + name)
        statuses[name] = state
    missing = set(ALL_CHECKS) - set(statuses)
    if missing:
        errors.append("缺少检查项:" + ",".join(name for name in ALL_CHECKS if name in missing))
    disabled = set(config.get("disabled", []))
    for name, state in statuses.items():
        if name in HARD and state != "ok":
            errors.append("硬性检查必须通过:" + name)
        elif state == "fail":
            errors.append("检查失败:" + name)
        elif state == "skip" and name not in disabled:
            errors.append("未经用户配置授权的跳过:" + name)
    return not errors, list(dict.fromkeys(errors)), statuses


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        report = sys.stdin.read(65537)
        ok, errors, statuses = validate_report(report, config)
        if not ok:
            print("FAIL:" + ";".join(errors))
            return 1
        details = [name + "=" + statuses[name] for name in ALL_CHECKS if statuses[name] != "ok"]
        print("OK:完整14项审计通过" + ("(" + ",".join(details) + ")" if details else ""))
        return 0
    except (OSError, ValueError, TypeError):
        print("FAIL:检查配置或审计输出无效")
        return 1


if __name__ == "__main__":
    sys.exit(main())
