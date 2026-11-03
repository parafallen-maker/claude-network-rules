#!/usr/bin/env python3
"""Persistent Clash Verge environment preset. Secrets never leave local backups."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


cm = module('environment_components', 'component-manager.py')
policy = module('environment_policy', 'clash-policy.py')
Error = cm.ComponentError
SETTINGS = 'environment-settings.json'
BACKUP = 'environment-backup.json'
DEFAULTS = dict(proxy_group='🤖 Claude', browser_port=7898, configure_dns=True,
                auto_group=True, enable_browser_proxy=True)


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise Error('配置文件不是普通文件，已停止。')
    if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
        raise Error('配置文件过大，已停止。')
    return path.read_bytes() if path.exists() else None


def installed(home):
    raw = read(home / '.local/bin/claude-switch.sh')
    text = raw.decode('utf-8') if raw else ''
    values = cm.configs_from(text)
    return {key: values.get(key, '') for key in cm.CONFIG_KEYS}


def settings(value, group):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise Error('环境配置字段无效。')
    result = dict(DEFAULTS, proxy_group=group)
    result.update(value)
    if (not isinstance(result['proxy_group'], str) or not result['proxy_group']
            or len(result['proxy_group']) > 128 or re.search(r'[,\x00-\x1f]', result['proxy_group'])
            or result['proxy_group'] in ('DIRECT', 'REJECT', 'GLOBAL', 'PASS')):
        raise Error('需要实际的代理组名称。')
    if result['proxy_group'] != group:
        raise Error('代理组须与本机安装设置一致；更换组名请使用完整安装器。')
    if type(result['browser_port']) is not int or not 1024 <= result['browser_port'] <= 65535:
        raise Error('浏览器端口须在 1024–65535 之间。')
    for key in ('configure_dns', 'auto_group', 'enable_browser_proxy'):
        if type(result[key]) is not bool:
            raise Error('环境配置开关必须是布尔值。')
    if result['configure_dns'] and any(char in group for char in ('&', '#')):
        raise Error('此代理组名含 DNS 参数分隔符，请关闭托管 DNS 或通过完整安装器更换组名。')
    return result


def saved(base, group):
    raw = read(base / SETTINGS)
    return settings(json.loads(raw) if raw else {}, group)


def protect_paths(home, clash, base):
    for path in (base, clash, clash / 'profiles'):
        # Check each component under HOME, not only the final filename.
        current = path
        while current != home and current != current.parent:
            if current.is_symlink():
                raise Error('配置路径包含符号链接，已停止。')
            current = current.parent
    if not (clash / 'profiles').is_dir():
        raise Error('未找到 Clash Verge 配置，请先安装并打开 Clash Verge。')


def policy_name(base):
    raw = read(base / 'webrtc-policy')
    value = raw.decode().strip() if raw else 'proxy'
    if value not in ('proxy', 'reject'):
        raise Error('STUN 策略无效，请先修复。')
    return value


def quiet_policy(function, *args, **kwargs):
    stream = io.StringIO()
    try:
        with contextlib.redirect_stderr(stream):
            return function(*args, **kwargs)
    except SystemExit:
        message = stream.getvalue().strip()
        if message.startswith('FAIL:'):
            message = message[5:]
        raise Error(message or 'Clash 配置未通过验证。') from None


def scalar(value):
    value = value.strip()
    if value.startswith('"'):
        return json.loads(value)
    if value.startswith("'"):
        if not value.endswith("'"):
            raise ValueError('unsupported YAML scalar')
        return value[1:-1].replace("''", "'")
    if value in ('true', 'false'):
        return value == 'true'
    if value == '[]':
        return []
    if value == '{}':
        return {}
    if re.fullmatch(r'[0-9]+', value):
        return int(value)
    if not value or value.startswith(('!', '&', '*', '[', '{', '|', '>')):
        raise ValueError('unsupported YAML scalar')
    return re.split(r'\s+#', value, maxsplit=1)[0]


def section(text, key):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line == key + ':':
            result = []
            for following in lines[i + 1:]:
                if following and not following[0].isspace() and not following.startswith('- '):
                    break
                result.append(following)
            return result
        if line == key + ': []':
            return []
    return None


def generated_fields(clash, options):
    """Confirm exact fields in Verge's generated YAML, plus the local socket.

    The embedded marker binds this evidence to the applied options. This is not
    an upstream DNS/ICE test; runtime rules are independently checked via API.
    """
    try:
        path = clash / 'clash-verge.yaml'
        raw = read(path)
        text = raw.decode('utf-8-sig') if raw else ''
        marker = policy.read_yaml_scalar(path, 'claude-switcher-environment')
        if json.loads(marker) != options:
            return False
        if options['configure_dns']:
            lines = section(text, 'dns')
            if lines is None:
                return False
            fields, index = {}, 0
            while index < len(lines):
                line = lines[index]
                match = re.fullmatch(r'  ([a-z-]+):(?: (.*))?', line)
                index += 1
                if not match:
                    continue
                key, value = match.groups()
                if key not in ('enable', 'enhanced-mode', 'respect-rules', 'nameserver', 'nameserver-policy', 'fallback', 'direct-nameserver'):
                    continue
                if key in fields:
                    return False
                if value:
                    fields[key] = scalar(value)
                else:
                    items = []
                    while index < len(lines) and re.match(r'  (?:  )?- ', lines[index]):
                        items.append(scalar(re.sub(r'^  (?:  )?- ', '', lines[index])))
                        index += 1
                    fields[key] = items
            required = {'enable': True, 'enhanced-mode': 'fake-ip', 'respect-rules': True,
                        'nameserver': ['https://1.1.1.1/dns-query#' + options['proxy_group']],
                        'nameserver-policy': {}, 'fallback': [], 'direct-nameserver': []}
            # Verge's DNS override can fill direct-nameserver after the global
            # script. Empty means inherit nameserver; the identical proxied DoH
            # endpoint is equivalent. Any other resolver remains a mismatch.
            if fields.get('direct-nameserver') == required['nameserver']:
                fields['direct-nameserver'] = []
            if fields != required:
                return False
        lines = section(text, 'listeners')
        if lines is None:
            return False
        # Group entries first; nested settings on unrelated listeners are valid.
        entries, current, base_indent = [], None, None
        for line in lines:
            if not line.strip():
                continue
            match = re.match(r'^( *)(?:- )([a-z-]+): (.*)$', line)
            if match and (base_indent is None or len(match.group(1)) == base_indent):
                base_indent = len(match.group(1))
                current = [line]
                entries.append(current)
            elif current is not None:
                current.append(line)
            else:
                return False
        own = []
        for entry in entries:
            fields = {}
            nested = False
            for index, line in enumerate(entry):
                indent = base_indent if index == 0 else base_indent + 2
                pattern = r' ' * indent + (r'- ' if index == 0 else '') + r'([a-z-]+): (.*)'
                match = re.fullmatch(pattern, line)
                if not match:
                    nested = True
                    continue
                if match.group(1) in fields:
                    return False
                try:
                    fields[match.group(1)] = scalar(match.group(2))
                except ValueError:
                    nested = True
            if fields.get('name') == 'adspower-claude-local':
                if nested:
                    return False
                own.append(fields)
        if not options['enable_browser_proxy']:
            return not own
        desired = {'name':'adspower-claude-local', 'type':'http', 'listen':'127.0.0.1',
                   'port':options['browser_port'], 'proxy':options['proxy_group']}
        if own != [desired]:
            return False
        with socket.create_connection(('127.0.0.1', options['browser_port']), timeout=1):
            pass
        return True
    except (OSError, ValueError, TypeError, SystemExit):
        return False


def runtime(clash, base, options):
    try:
        state = quiet_policy(policy.verify_runtime, clash, options['proxy_group'], policy_name(base), require_node=False)
        if not generated_fields(clash, options):
            return False, False, 'DNS 或浏览器入口尚未在生成配置中确认。'
        try:
            quiet_policy(policy.validate_group, state, options['proxy_group'], policy_name(base))
        except Error as exc:
            return True, False, str(exc)
        return True, True, ''
    except (Error, OSError, ValueError):
        return False, False, '运行中的 Clash 尚未加载本次配置。'


def restart_clash():
    # User invoked Apply. Existing sessions are never stopped by this helper.
    # Refuse an active protected desktop/CLI, rather than letting a restart kill it.
    processes = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True, text=True, timeout=5)
    if processes.returncode:
        raise Error('无法确认受保护客户端状态，请先关闭 Claude 客户端再应用。')
    for line in processes.stdout.splitlines():
        name = Path(line.strip()).name.lower()
        if name in ('claude', 'claude code') or '/Claude.app/' in line:
            raise Error('请先退出 Claude 桌面版和 Claude Code，再应用网络配置。')
    result = subprocess.run(['/usr/bin/osascript', '-e', 'tell application "Clash Verge" to quit'],
                            capture_output=True, timeout=15)
    if result.returncode:
        raise Error('无法重新加载 Clash Verge；请检查系统自动化授权。')
    time.sleep(2)
    result = subprocess.run(['/usr/bin/open', '-a', 'Clash Verge'], capture_output=True, timeout=10)
    if result.returncode:
        raise Error('无法打开 Clash Verge，请手动重新激活订阅。')


def activate(clash, base, options):
    loaded, ready, message = runtime(clash, base, options)
    # The managed source may have changed even if the old runtime looks equal.
    restart_clash()
    for _ in range(25):
        loaded, ready, message = runtime(clash, base, options)
        if loaded:
            return loaded, ready, message
        time.sleep(1)
    return loaded, ready, message


def changes(options):
    rows = ['全局脚本统一使用 51 条分流规则：国内及局域网直连，Claude/IP 检测及其余流量代理。']
    if options['auto_group']:
        rows.append('订阅缺少安装时的代理组时自动创建；初始选择 REJECT，须自行选择节点。')
    if options['configure_dns']:
        rows.append('域名解析设为 fake-IP + 经代理组访问 Cloudflare DoH；节点服务器域名保留独立引导解析。')
    else:
        rows.append('DNS 沿用订阅与原有全局配置。')
    rows.append(('新增本机专用 HTTP 入口 127.0.0.1:' + str(options['browser_port']) + '，进入该端口的全部请求交给代理组。') if options['enable_browser_proxy'] else '移除本工具的浏览器专用入口。')
    rows.append('应用会重启 Clash Verge，连接会短暂中断；不会启动 Claude 或修改浏览器指纹。')
    return rows


def journal(base):
    raw = read(base / BACKUP)
    if raw is None:
        return None
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get('schema') != 1 or set(record.get('files', {})) != {'script', 'settings'}:
        raise Error('环境恢复记录无效，请保留备份并检查。')
    for entry in record['files'].values():
        if not isinstance(entry, dict) or set(entry) != {'before', 'after'}:
            raise Error('环境恢复记录无效。')
        for value in entry.values():
            if value is not None and not isinstance(value, str):
                raise Error('环境恢复记录无效。')
    return record


def raw_value(value):
    return value.encode('utf-8') if value is not None else None


def restore_files(record, paths):
    for name, path in paths.items():
        entry = record['files'][name]
        current = read(path)
        if current not in (raw_value(entry['before']), raw_value(entry['after'])):
            raise Error('配置在应用后被其他操作修改，拒绝覆盖。请保留恢复记录并人工合并。')
    for name, path in paths.items():
        value = raw_value(record['files'][name]['before'])
        if value is None:
            path.unlink(missing_ok=True)
        else:
            cm.atomic_write(path, value, 0o600)


def execute(action, value, home=None, activation=activate):
    home = Path(home or Path.home())
    base = home / '.claude-switcher'
    clash = home / 'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
    info = installed(home)
    group = info['PROXY_GROUP'] or DEFAULTS['proxy_group']
    current = saved(base, group)
    response = dict(ok=True, action=action, settings=current,
                    region=dict(country=info['COUNTRY_TARGET'], timezone=info['TZ_TARGET'], locale=info['LOCALE_TARGET']))
    if action == 'status':
        response.update(message='已读取本机环境配置。', managed=(base / SETTINGS).exists(), recovery_available=(base / BACKUP).exists())
        if (base / SETTINGS).exists():
            loaded, ready, message = runtime(clash, base, current)
            response.update(complete=ready, active=loaded)
            if not ready:
                response['manual_required'] = [message, '请在 Clash Verge 选择正确节点并重新激活订阅，再读取状态。']
        return response
    if action not in ('plan', 'apply', 'restore'):
        raise Error('不支持的环境配置操作。')
    protect_paths(home, clash, base)
    if info['PROXY_MODE'] != 'clash-verge':
        raise Error('本版一键网络配置支持 Clash Verge；请先通过完整安装器选择该客户端。')
    options = settings(value, group) if action != 'restore' else current
    paths = {'script': clash / 'profiles/Script.js', 'settings': base / SETTINGS}
    if action == 'plan':
        quiet_policy(policy.script_text, paths['script'], group, policy_name(base), options)
        response.update(settings=options, changes=changes(options), message='修改预览已准备，可应用。')
        return response
    with cm.state_lock(base):
        for pending in ('install-journal.json', 'update-journal.json', 'account-transaction.json'):
            if (base / pending).exists() or (base / pending).is_symlink():
                raise Error('存在未完成的安装、更新或账号事务，请先恢复。')
        record = journal(base)
        if action == 'restore':
            if record is None:
                response.update(message='没有需要恢复的环境配置。', changed=False)
                return response
            restore_files(record, paths)
            # Keep the journal until the original settings are successfully reloaded.
            restart_clash()
            (base / BACKUP).unlink()
            response.update(message='已恢复配置文件并重新打开 Clash Verge；请重新检查运行状态。',
                            settings=saved(base, group), changed=True, complete=False,
                            manual_required=['恢复后的配置需要重新运行环境检查。'])
            return response
        if record and record.get('phase') != 'applied':
            raise Error('上一次配置未完成，请先点击恢复，再重新应用。')
        if record:
            for name, path in paths.items():
                if read(path) != raw_value(record['files'][name]['after']):
                    raise Error('配置已被其他操作修改，拒绝覆盖；请先检查恢复记录。')
        script = quiet_policy(policy.script_text, paths['script'], group, policy_name(base), options)
        after = {'script': script, 'settings': json.dumps(options, ensure_ascii=False, indent=2) + '\n'}
        if all(read(paths[name]) == raw_value(after[name]) for name in paths):
            loaded, ready, message = runtime(clash, base, options)
            response.update(message='配置已保存。' if ready else '配置已保存，仍需完成运行检查。', changed=False, complete=ready, active=loaded)
            if not ready:
                response['manual_required'] = [message, '请在 Clash Verge 选择节点并重新激活订阅。']
            return response
        record = dict(schema=1, phase='prepared', files={
            name: dict(before=read(path).decode('utf-8') if read(path) is not None else None, after=after[name])
            for name, path in paths.items()})
        cm.write_json(base / BACKUP, record)
        try:
            for name, path in paths.items():
                cm.atomic_write(path, raw_value(after[name]), 0o600)
            loaded, ready, message = activation(clash, base, options)
            if not loaded:
                raise Error('Clash 未加载新配置，已尝试恢复原文件；请重新激活订阅。')
        except (Error, OSError, subprocess.SubprocessError):
            restore_files(record, paths)
            record['phase'] = 'restored-needs-activation'
            cm.write_json(base / BACKUP, record)
            # Do not conceal failed activation or delete the recovery journal.
            raise
        record['phase'] = 'applied'
        cm.write_json(base / BACKUP, record)
        response.update(settings=options, changes=changes(options), changed=True, active=True, complete=ready,
                        message='全局配置已应用并确认加载。' if ready else '全局配置已加载，仍需选择可用节点。')
        if not ready:
            response['manual_required'] = [message, '在 Clash Verge 的“' + group + '”组选择节点，再读取状态并运行完整环境检查。']
        return response


def main():
    try:
        if len(sys.argv) != 2:
            raise Error('需要环境配置操作名称。')
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise Error('输入过大。')
        result = execute(sys.argv[1], json.loads(raw or b'{}'))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Error, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(ok=False, complete=False, message=str(exc) if isinstance(exc, Error) else '配置未完成；请保留恢复记录并检查本机设置。'), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
