#!/usr/bin/env python3
"""Reversible, explicit AdsPower profile proxy setup; never starts a browser.

API credentials are accepted only in a JSON object on stdin. Responses and the
private recovery journal deliberately exclude account data and API credentials.
"""
import argparse
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import urlencode, urlsplit


DEFAULT_ENDPOINT = 'http://127.0.0.1:50325'
DEFAULT_PORT = 7898
MAX_RESPONSE = 2 * 1024 * 1024
ID_RE = re.compile(r'[A-Za-z0-9_-]{1,128}')
PROXY_FIELDS = ('proxy_soft', 'proxy_type', 'proxy_host', 'proxy_port',
                'proxy_user', 'proxy_password', 'proxy_url', 'global_config')
WEBRTC_VALUES = {'disabled', 'proxy', 'forward', 'real'}
MANUAL_WEBRTC = 'AdsPower 未提供可读回的 WebRTC 设置；请在该环境的指纹设置中将 WebRTC 设为 Disabled，并用浏览器检测页验证。'


class BrowserError(Exception):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def endpoint(value):
    """Pin localhost to a literal address; neither DNS nor redirects are used."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
            raise ValueError()
        port = parsed.port if parsed.port is not None else 50325
        if not 1 <= port <= 65535:
            raise ValueError()
        host = '127.0.0.1' if parsed.hostname == 'localhost' else parsed.hostname
        return 'http://' + ('[' + host + ']' if ':' in host else host) + ':' + str(port)
    except (ValueError, TypeError, AttributeError):
        raise BrowserError('AdsPower API 地址必须是本机 HTTP 地址。') from None


def identifier(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise BrowserError('请选择有效的 AdsPower 环境。')
    return value


class Client:
    def __init__(self, address=DEFAULT_ENDPOINT, key='', interval=1.05):
        self.address = endpoint(address)
        parsed = urlsplit(self.address)
        self.host, self.port = parsed.hostname, parsed.port
        if not isinstance(key, str) or len(key) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise BrowserError('API 密钥格式无效。')
        self.key, self.interval, self.last_request = key, interval, 0.0

    def request(self, method, path, payload=None):
        remaining = self.interval - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        self.last_request = time.monotonic()
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
        conn = http.client.HTTPConnection(self.host, self.port, timeout=8)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            if response.status in (401, 403):
                raise BrowserError('AdsPower 拒绝访问，请检查 Local API 权限与密钥。')
            if response.status == 429:
                raise BrowserError('AdsPower API 请求过于频繁，请稍后重试。')
            if response.status != 200:
                raise BrowserError('AdsPower API 返回异常状态；未跟随重定向。')
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise BrowserError('AdsPower API 响应过大，已停止操作。')
            value = json.loads(raw)
            if not isinstance(value, dict) or type(value.get('code')) is not int or value['code'] != 0:
                raise BrowserError('AdsPower 未完成请求，请检查 API 权限、版本与环境状态。')
            if not isinstance(value.get('data'), dict):
                raise BrowserError('AdsPower API 响应格式无效。')
            return value['data']
        except (OSError, http.client.HTTPException):
            raise BrowserError('无法连接 AdsPower Local API，请启动 AdsPower 并开启 Local API。') from None
        except (ValueError, UnicodeError):
            raise BrowserError('AdsPower API 响应格式无效。') from None
        finally:
            conn.close()

    def profiles(self, profile_id=None, page=1):
        body = {'page': page, 'limit': 100}
        if profile_id:
            body['profile_id'] = [identifier(profile_id)]
        items = self.request('POST', '/api/v2/browser-profile/list', body).get('list')
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise BrowserError('AdsPower 未返回有效的环境列表。')
        return items

    def profile(self, profile_id):
        matches = [item for item in self.profiles(profile_id) if item.get('profile_id') == profile_id]
        if len(matches) != 1:
            raise BrowserError('AdsPower 环境不存在或无法唯一识别。')
        return matches[0]

    def running(self, profile_id):
        data = self.request('GET', '/api/v2/browser-profile/active?' + urlencode({'profile_id': profile_id}))
        status = data.get('status')
        if status not in {'Active', 'Inactive'}:
            raise BrowserError('无法确认 AdsPower 环境是否已关闭。')
        return status == 'Active'

    def require_closed(self, profile_id):
        if self.running(profile_id):
            raise BrowserError('请先关闭所选 AdsPower 环境，再配置或恢复。')

    def update(self, profile_id, settings):
        body = {'user_id': profile_id}
        if 'proxy' in settings:
            proxy = settings['proxy']
            if proxy['proxy_id']:
                body['proxyid'] = proxy['proxy_id']
            else:
                config = proxy['config']
                body['user_proxy_config'] = ({'proxy_soft': 'no_proxy'} if config['proxy_soft'] == 'no_proxy' else config)
        if 'webrtc' in settings:
            body['fingerprint_config'] = {'webrtc': settings['webrtc']}
        self.request('POST', '/api/v1/user/update', body)


def snapshot(profile):
    config = profile.get('user_proxy_config')
    if not isinstance(config, dict) or not isinstance(config.get('proxy_soft'), str):
        raise BrowserError('无法读取原代理配置，已停止以保留恢复能力。')
    # Unknown nonempty options may affect restoration; do not silently discard them.
    if any(key not in PROXY_FIELDS and value not in ('', None, 0) for key, value in config.items()):
        raise BrowserError('该环境包含尚不支持恢复的代理选项，请在 AdsPower 内手动配置。')
    values = {}
    for key in PROXY_FIELDS:
        value = config.get(key, '0' if key == 'global_config' else '')
        if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 16384:
            raise BrowserError('原代理设置格式不受支持，已停止操作。')
        values[key] = str(value)
    if not values['proxy_soft']:
        raise BrowserError('原代理设置不完整，已停止操作。')
    if values['proxy_soft'] == 'no_proxy':
        values = {key: ('no_proxy' if key == 'proxy_soft' else '0' if key == 'global_config' else '') for key in PROXY_FIELDS}
    proxy_id = profile.get('fbcc_proxy_acc_id') or ''
    if not isinstance(proxy_id, (str, int)) or (proxy_id and not ID_RE.fullmatch(str(proxy_id))):
        raise BrowserError('原代理编号无效，已停止操作。')
    result = {'proxy': {'config': values, 'proxy_id': str(proxy_id)}}
    fingerprint = profile.get('fingerprint_config')
    if (isinstance(fingerprint, dict) and isinstance(fingerprint.get('webrtc'), str)
            and fingerprint['webrtc'] in WEBRTC_VALUES):
        result['webrtc'] = fingerprint['webrtc']
    return result


def desired_settings(port, current):
    if type(port) is not int or not 1 <= port <= 65535:
        raise BrowserError('本机代理端口必须是 1 到 65535 的整数。')
    values = {key: '' for key in PROXY_FIELDS}
    values.update(proxy_soft='other', proxy_type='http', proxy_host='127.0.0.1',
                  proxy_port=str(port), global_config='0')
    result = {'proxy': {'config': values, 'proxy_id': ''}}
    if 'webrtc' in current:
        result['webrtc'] = 'disabled'
    return result


def matches(current, expected):
    return all(current.get(key) == value for key, value in expected.items())


def state_module():
    spec = importlib.util.spec_from_file_location('browser_account_state', Path(__file__).with_name('account_state.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_directory(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise BrowserError('浏览器恢复目录权限异常，已停止操作。')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


class Journal:
    def __init__(self, base, address, profile_id):
        self.base, self.address, self.profile_id = Path(base), address, profile_id
        digest = hashlib.sha256((address + '\n' + profile_id).encode()).hexdigest()
        self.directory = self.base / 'browser-proxy'
        self.path = self.directory / (digest + '.json')

    def read(self):
        if not self.directory.exists() and not self.directory.is_symlink():
            return None
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise BrowserError('浏览器恢复目录权限异常，已停止操作。')
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise BrowserError('浏览器恢复文件无法安全读取。') from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 256 * 1024):
                raise BrowserError('浏览器恢复文件权限异常，已停止操作。')
            with os.fdopen(fd, 'r', encoding='utf-8') as handle:
                fd = None
                value = json.load(handle)
            if (not isinstance(value, dict) or value.get('version') != 1
                    or value.get('endpoint') != self.address or value.get('profile_id') != self.profile_id
                    or not isinstance(value.get('before'), dict) or not isinstance(value.get('after'), dict)
                    or set(value['before']) != set(value['after'])
                    or not {'proxy'} <= set(value['before']) <= {'proxy', 'webrtc'}):
                raise ValueError()
            for settings in (value['before'], value['after']):
                proxy = settings['proxy']
                validated = snapshot({'user_proxy_config': proxy['config'], 'fbcc_proxy_acc_id': proxy['proxy_id'],
                                      'fingerprint_config': {'webrtc': settings.get('webrtc')}})
                if validated != settings:
                    raise ValueError()
            return value
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise BrowserError('浏览器恢复文件损坏，已保留文件并停止操作。') from None
        finally:
            if fd is not None:
                os.close(fd)

    def write(self, before, after):
        private_directory(self.directory)
        state_module().atomic_json(self.path, {'version': 1, 'endpoint': self.address,
                                              'profile_id': self.profile_id, 'before': before, 'after': after})

    def remove(self):
        self.path.unlink()
        state_module().sync_dir(self.directory)


def safe_settings(current, port, managed=False, running=False):
    desired = desired_settings(port, current)
    return {'proxy_configured': current['proxy'] == desired['proxy'],
            'webrtc': current.get('webrtc', 'unavailable'), 'managed': managed, 'running': running,
            'local_proxy_port': port}


def result(action, current, port, changed=False, managed=False, **extra):
    manual = ([] if current.get('webrtc') == 'disabled' else
              [MANUAL_WEBRTC if 'webrtc' not in current else 'WebRTC 尚未设为 Disabled；请关闭环境后应用配置。'])
    settings = safe_settings(current, port, managed)
    return {'ok': True, 'action': action, 'message': '已配置并读回验证本机代理。' if action == 'configure' else '已读取 AdsPower 环境设置。',
            'changed': changed, 'complete': settings['proxy_configured'] and not manual, 'manual_required': manual,
            'settings': settings, **extra}


def rollback(client, profile_id, journal, record):
    """Recover only known owned values, never overwrite a third-party change."""
    client.require_closed(profile_id)
    current = snapshot(client.profile(profile_id))
    if any(current.get(key) not in (record['before'][key], record['after'][key]) for key in record['before']):
        raise BrowserError('环境设置已被其他操作修改，未覆盖；请检查后恢复。', recovery_required=True)
    changes = {key: value for key, value in record['before'].items() if current.get(key) != value}
    if changes:
        client.update(profile_id, changes)
        current = snapshot(client.profile(profile_id))
        if not matches(current, record['before']):
            raise BrowserError('未能确认恢复结果，恢复记录已保留。', recovery_required=True)
    journal.remove()
    return bool(changes)


def execute(action, options, base=None, client=None):
    if not isinstance(options, dict):
        raise BrowserError('输入必须是 JSON 对象。')
    address = endpoint(options.get('endpoint', DEFAULT_ENDPOINT))
    client = client or Client(address, options.get('api_key', ''))
    port = options.get('port', DEFAULT_PORT)
    desired_settings(port, {})
    if action == 'list':
        page = options.get('page', 1)
        if type(page) is not int or not 1 <= page <= 10000:
            raise BrowserError('环境列表页码无效。')
        items = client.profiles(page=page)
        profiles = []
        for item in items:
            profile_id = identifier(item.get('profile_id'))
            name = item.get('name', '')
            profiles.append({'profile_id': profile_id, 'name': ''.join(c for c in str(name) if c.isprintable())[:100]})
        return {'ok': True, 'action': action, 'profiles': profiles, 'page': page, 'has_more': len(items) == 100,
                'message': '已读取 AdsPower 环境列表。'}
    profile_id = identifier(options.get('profile_id'))
    base = Path(base) if base is not None else Path.home() / '.claude-switcher'
    journal = Journal(base, address, profile_id)
    if action == 'status':
        current = snapshot(client.profile(profile_id))
        running = client.running(profile_id)
        record = journal.read()
        return result(action, current, port, managed=record is not None,
                      settings=safe_settings(current, port, record is not None, running))
    if action not in {'configure', 'restore'}:
        raise BrowserError('不支持的浏览器操作。')
    private_directory(base)
    state = state_module()
    try:
        with state.state_lock(base):
            state.component_recovery_check(base)
            client.require_closed(profile_id)
            current = snapshot(client.profile(profile_id))
            record = journal.read()
            if action == 'restore':
                if record is None:
                    return {'ok': True, 'action': action, 'changed': False, 'message': '没有需要恢复的托管设置。'}
                changed = rollback(client, profile_id, journal, record)
                return {'ok': True, 'action': action, 'changed': changed, 'message': '已恢复原代理与本工具托管的 WebRTC 设置。'}
            desired = desired_settings(port, current)
            if record is not None:
                if matches(current, record['after']) and desired == record['after']:
                    return result(action, current, port, managed=True)
                raise BrowserError('已有恢复记录；请先恢复原设置，再重新配置。', recovery_required=True)
            if matches(current, desired):
                return result(action, current, port)
            # Re-check immediately before writing; profile data must not change mid-setup.
            client.require_closed(profile_id)
            if snapshot(client.profile(profile_id)) != current:
                raise BrowserError('环境设置在配置期间发生变化，请重新读取后再试。')
            journal.write(current, desired)
            record = journal.read()
            try:
                client.update(profile_id, desired)
                verified = snapshot(client.profile(profile_id))
                if not matches(verified, desired):
                    raise BrowserError('AdsPower 写入后的设置与目标不一致。')
            except BrowserError as error:
                try:
                    rollback(client, profile_id, journal, record)
                except BrowserError:
                    raise BrowserError('配置未完成，且无法确认自动恢复；恢复记录已保留，请使用恢复操作。', recovery_required=True) from None
                raise BrowserError(str(error) + ' 已恢复原设置。', rolled_back=True) from None
            return result(action, verified, port, changed=True, managed=True)
    except state.StateError as error:
        raise BrowserError(str(error)) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('list', 'status', 'configure', 'restore'))
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise BrowserError('输入过大，已停止操作。')
        try:
            options = json.loads(raw or b'{}')
        except (ValueError, UnicodeError):
            raise BrowserError('输入不是有效 JSON。') from None
        output = execute(args.action, options)
    except BrowserError as error:
        output = {'ok': False, 'action': args.action, 'message': str(error), **error.details}
    except Exception:
        # Never serialize raw API exceptions or request objects containing credentials.
        output = {'ok': False, 'action': args.action, 'message': '浏览器配置操作未完成；请检查本机 API 与恢复文件权限。'}
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
