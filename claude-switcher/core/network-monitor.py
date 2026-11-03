#!/usr/bin/env python3
"""Root-owned Claude monitor. Local checks and explicit-proxy exit probes are independent."""
import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
RUNTIME = Path('/var/run/claude-switcher-monitor')
FAST_INTERVAL = 1.0
EXIT_INTERVAL = 5.0
EXIT_MAX_AGE = 20.0
HEARTBEAT_MAX_AGE = 6.0
LOCAL_TIMEOUT = 3.0
KINDS = {'vpn_disconnected', 'node_changed', 'exit_ip_changed', 'monitor_unavailable'}
NON_PROXIES = {'direct', 'reject', 'block', 'pass', 'compatible', 'passrule', 'rejectdrop'}


def load_policy():
    spec = importlib.util.spec_from_file_location('monitor_policy', ROOT / 'clash-policy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def node_fingerprint(proxies, group):
    chain, seen = [], set()
    name = group
    while True:
        if name in seen or not isinstance(proxies.get(name), dict):
            raise ValueError('invalid proxy chain')
        seen.add(name)
        item = proxies[name]
        entry = {key: item.get(key) for key in ('id', 'type', 'provider-name', 'server', 'port')}
        entry['name'] = name
        if 'all' in item or 'now' in item:
            selected = item.get('now')
            if not isinstance(selected, str) or not selected:
                raise ValueError('missing selection')
            entry['now'] = selected
            chain.append(entry)
            name = selected
        else:
            if not item.get('type') or str(item['type']).lower() in NON_PROXIES:
                raise ValueError('selection is not a proxy')
            chain.append(entry)
            return digest(chain)


def probe_local(args):
    """Child process: a total parent deadline bounds all local reads together."""
    policy = load_policy()
    suffix = ('io.github.clash-verge-rev.clash-verge-rev' if args.proxy_mode == 'clash-verge'
              else 'com.follow.clash')
    clash = args.user_home / 'Library/Application Support' / suffix
    base = args.user_home / '.claude-switcher'
    policy_file = base / 'webrtc-policy'
    stun = policy.read_text(policy_file).strip() if policy_file.exists() else 'proxy'
    if stun not in {'reject', 'proxy'}:
        raise ValueError('invalid STUN policy')
    state = policy.verify_runtime(clash, args.proxy_group, stun,
                                  allow_plain_ports=args.proxy_mode == 'flclash')
    port = state['configs'].get('mixed-port') or state['configs'].get('port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('no explicit local HTTP proxy')
    health = subprocess.run(['/usr/bin/python3', str(ROOT / 'network-check.py'), 'health'],
                            capture_output=True, timeout=2)
    if health.returncode != 0 or not json.loads(health.stdout).get('ok'):
        raise ValueError('tunnel health failed')
    guard = subprocess.run(['/usr/bin/python3', str(ROOT / 'network-guard.py'), 'watch'],
                           capture_output=True, timeout=2)
    if guard.returncode:
        raise ValueError('existing firewall guard failed')
    kernel = 'verge-mihomo' if args.proxy_mode == 'clash-verge' else 'FlClashCore'
    if subprocess.run(['/usr/bin/pgrep', '-f', kernel], capture_output=True, timeout=1).returncode:
        raise ValueError('proxy kernel missing')
    return {'ok': True, 'node': node_fingerprint(state['proxies']['proxies'], args.proxy_group), 'port': port}


def child_arguments(args):
    return ['--owner-uid', str(args.owner_uid), '--user-home', str(args.user_home),
            '--proxy-mode', args.proxy_mode, '--proxy-group', args.proxy_group,
            '--expected-country', args.expected_country]


def local_snapshot(args):
    try:
        result = subprocess.run(['/usr/bin/python3', str(ROOT / 'network-monitor.py'), 'probe-local'] + child_arguments(args),
                                capture_output=True, timeout=LOCAL_TIMEOUT)
        value = json.loads(result.stdout) if result.returncode == 0 else {}
        if (value.get('ok') is True and re.fullmatch(r'[a-f0-9]{64}', value.get('node', ''))
                and type(value.get('port')) is int and 1 <= value['port'] <= 65535):
            return value
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        pass
    return {'ok': False}


def exit_sample(port):
    """Never fall back to a direct request, even if the TUN disappears."""
    try:
        result = subprocess.run([
            '/usr/bin/curl', '--silent', '--show-error', '--fail', '--max-time', '5',
            '--connect-timeout', '3', '--max-filesize', '8192', '--noproxy', '',
            '--proxy', 'http://127.0.0.1:' + str(port),
            'https://www.cloudflare.com/cdn-cgi/trace'], capture_output=True, timeout=6,
            env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
        if result.returncode or len(result.stdout) > 8192:
            return None
        values = dict(line.split('=', 1) for line in result.stdout.decode('ascii').splitlines() if '=' in line)
        address = ipaddress.ip_address(values.get('ip', ''))
        country = values.get('loc', '')
        if not address.is_global or not re.fullmatch(r'[A-Z]{2}', country):
            return None
        return {'ip': digest(str(address)), 'country': country}
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return None


class MonitorState:
    """Only hashes are persisted; no raw node names, credentials or IP addresses."""
    def __init__(self, saved=None):
        saved = saved or {}
        self.node = saved.get('node')
        self.ip = saved.get('ip')
        self.last_exit = float(saved.get('last_exit', 0))
        self.generation = int(saved.get('generation', 0))
        self.local_ok = False
        self.country_ok = False
        self.fault = None
        self.pending_since = None

    def local(self, snapshot):
        if not snapshot.get('ok'):
            if self.local_ok:
                self.generation += 1  # Invalidate a probe begun before disconnect.
            self.local_ok = False
            if self.fault != 'vpn_disconnected':
                self.fault = 'vpn_disconnected'
                return self.fault
            return None
        was_ok = self.local_ok
        self.local_ok = True
        if self.node != snapshot['node']:
            changed = self.node is not None
            self.node = snapshot['node']
            self.ip = None
            self.last_exit = 0
            self.country_ok = False
            self.pending_since = time.time()
            self.generation += 1
            self.fault = None
            return 'node_changed' if changed else None
        if not was_ok:
            # A reconnect must obtain a fresh exit sample before Claude can run.
            self.last_exit = 0
            self.country_ok = False
            self.pending_since = time.time()
        return None

    def sample(self, generation, sample, now, country):
        if generation != self.generation or not self.local_ok or sample is None:
            return None
        if sample['country'] != country:
            self.country_ok = False
            if self.fault != 'monitor_unavailable':
                self.fault = 'monitor_unavailable'
                return self.fault
            return None
        changed = self.ip is not None and self.ip != sample['ip']
        self.ip = sample['ip']
        self.last_exit = now
        self.country_ok = True
        self.fault = None
        return 'exit_ip_changed' if changed else None

    def ready(self, now):
        return bool(self.local_ok and self.country_ok and self.ip and 0 <= now - self.last_exit <= EXIT_MAX_AGE)

    def stale(self, now, started):
        age = now - (self.last_exit or self.pending_since or started)
        if self.local_ok and age > EXIT_MAX_AGE and self.fault != 'monitor_unavailable':
            self.fault = 'monitor_unavailable'
            return self.fault
        return None

    def private(self):
        return {'schema': 1, 'node': self.node, 'ip': self.ip,
                'last_exit': self.last_exit, 'generation': self.generation}


def secure_directory():
    try:
        RUNTIME.mkdir(mode=0o755)
    except FileExistsError:
        pass
    info = RUNTIME.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('unsafe monitor directory')


def read_root_json(path, private=False):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & (0o077 if private else 0o022)
                or info.st_size > 16384):
            raise ValueError('unsafe monitor file')
        with os.fdopen(fd, 'r') as handle:
            fd = None
            return json.load(handle)
    finally:
        if fd is not None:
            os.close(fd)


def write_root_json(path, value, mode):
    if path.is_symlink():
        raise ValueError('symlink monitor file')
    with tempfile.NamedTemporaryFile(mode='w', dir=RUNTIME, prefix='.monitor-', delete=False) as handle:
        temp = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def claude_pids(uid):
    result = subprocess.run(['/usr/bin/pgrep', '-u', str(uid), '-f', '^/Applications/Claude[.]app/Contents/'],
                            capture_output=True, text=True, timeout=1)
    if result.returncode not in (0, 1):
        raise ValueError('cannot inspect Claude processes')
    return [int(p) for p in result.stdout.split() if p.isdigit()]


def stop_claude(uid):
    """Freeze the real desktop process tree before terminating it; never run user code as root."""
    try:
        initial = claude_pids(uid)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, True
    if not initial:
        return False, False
    for _ in range(3):
        try:
            pids = claude_pids(uid)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False, True
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGSTOP)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.05)
        try:
            if not claude_pids(uid):
                return True, False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False, True
    return False, True


def publish_event(uid, kind, stopped, failed):
    if kind not in KINDS:
        raise ValueError('invalid event kind')
    # Idle network changes remain in the log/status, not in the notification
    # queue. Inspection/stop failures still need attention: absence is unknown.
    if stopped or failed:
        event = {'id': uuid.uuid4().hex, 'time': time.time(), 'kind': kind,
                 'claude_stopped': stopped, 'stop_failed': failed}
        write_root_json(RUNTIME / ('events-%d.json' % uid), {'schema': 1, 'latest': event}, 0o644)
    subprocess.run(['/usr/bin/logger', '-t', 'claude-switcher',
                    'Network monitor event: ' + kind + '; stopped=' + str(stopped) + '; stop_failed=' + str(failed)],
                   capture_output=True, timeout=1)


def read_status(uid, min_heartbeat=0, expected_configuration=None, expected_pid=None):
    value = read_root_json(RUNTIME / ('status-%d.json' % uid))
    now = time.time()
    if (value.get('schema') != 1 or value.get('owner_uid') != uid
            or not 0 <= now - float(value.get('heartbeat', 0)) <= HEARTBEAT_MAX_AGE
            or not value.get('ready')
            or float(value.get('heartbeat', 0)) < min_heartbeat
            or (expected_configuration is not None and value.get('configuration') != expected_configuration)
            or (expected_pid is not None and value.get('pid') != expected_pid)
            or not 0 <= now - float(value.get('last_exit_check', 0)) <= EXIT_MAX_AGE):
        raise ValueError('monitor not ready or evidence stale')
    return value


def run_monitor(args):
    if os.geteuid() != 0 or args.owner_uid <= 0:
        raise ValueError('root monitor requires a real account owner')
    account = pwd.getpwuid(args.owner_uid)
    if Path(account.pw_dir) != args.user_home:
        raise ValueError('home does not belong to account owner')
    secure_directory()
    lock = os.open(str(RUNTIME / ('lock-%d' % args.owner_uid)), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    private_path = RUNTIME / ('state-%d.json' % args.owner_uid)
    saved = None
    try:
        saved = read_root_json(private_path, private=True)
        if saved.get('schema') != 1 or any(saved.get(k) is not None and not re.fullmatch(r'[a-f0-9]{64}', saved[k]) for k in ('node', 'ip')):
            saved = None
    except (OSError, ValueError, TypeError):
        pass
    state = MonitorState(saved)
    started, next_probe = time.time(), 0.0
    future, sampled_generation = None, None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pending_initial_stop = False
    stop_pending = False

    def handle_event(kind):
        nonlocal stop_pending, pending_initial_stop
        stopped, failed = stop_claude(args.owner_uid)
        stop_pending = failed
        publish_event(args.owner_uid, kind, stopped, failed)
        pending_initial_stop = stopped or failed

    try:
        while True:
            loop_start = time.monotonic()
            snapshot = local_snapshot(args)
            now = time.time()
            event = state.local(snapshot)
            if event:
                handle_event(event)
                next_probe = 0
            if future is not None and future.done():
                try:
                    sample = future.result()
                except Exception:
                    sample = None
                event = state.sample(sampled_generation, sample, now, args.expected_country)
                future = None
                if event:
                    handle_event(event)
            event = state.stale(now, started)
            if event:
                handle_event(event)
            ready = state.ready(now) and not stop_pending
            if not ready:
                stopped, failed = stop_claude(args.owner_uid)
                stop_pending = failed
                if (stopped or failed) and not pending_initial_stop:
                    publish_event(args.owner_uid, state.fault or 'monitor_unavailable', stopped, failed)
                    pending_initial_stop = True
                elif not stopped and not failed:
                    # A quiet fault must not suppress a later real stop if the
                    # user opens Claude before network protection is restored.
                    pending_initial_stop = False
            else:
                pending_initial_stop = False
            if snapshot.get('ok') and future is None and time.monotonic() >= next_probe:
                sampled_generation = state.generation
                future = pool.submit(exit_sample, snapshot['port'])
                next_probe = time.monotonic() + EXIT_INTERVAL
            write_root_json(private_path, state.private(), 0o600)
            write_root_json(RUNTIME / ('status-%d.json' % args.owner_uid), {
                'schema': 1, 'owner_uid': args.owner_uid, 'pid': os.getpid(),
                'configuration': digest([args.proxy_mode, args.proxy_group, args.expected_country]),
                'heartbeat': time.time(), 'ready': ready, 'local_healthy': state.local_ok,
                'last_exit_check': state.last_exit, 'fault': state.fault,
                'stop_pending': stop_pending,
                'fast_interval_seconds': FAST_INTERVAL, 'exit_interval_seconds': EXIT_INTERVAL,
                'exit_max_age_seconds': EXIT_MAX_AGE,
            }, 0o644)
            time.sleep(max(0, FAST_INTERVAL - (time.monotonic() - loop_start)))
    finally:
        pool.shutdown(wait=False)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'probe-local', 'status'))
    parser.add_argument('--owner-uid', type=int, default=os.getuid())
    parser.add_argument('--user-home', type=Path, default=Path.home())
    parser.add_argument('--proxy-mode', choices=('clash-verge', 'flclash'), default='clash-verge')
    parser.add_argument('--proxy-group', default='🤖 Claude')
    parser.add_argument('--expected-country', default='US')
    parser.add_argument('--min-heartbeat', type=float, default=0)
    parser.add_argument('--expected-pid', type=int)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Z]{2}', args.expected_country):
        raise ValueError('invalid expected country')
    if args.action == 'status':
        read_status(args.owner_uid, args.min_heartbeat,
                    digest([args.proxy_mode, args.proxy_group, args.expected_country]), args.expected_pid)
        print('ok:持续监控在线；节点/VPN 每秒巡检、出口 IP 每 5 秒探测，异常停止 Claude')
    elif args.action == 'probe-local':
        print(json.dumps(probe_local(args)))
    else:
        run_monitor(args)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        print('fail:网络监控未就绪、证据过期或本地状态不可读取', file=sys.stderr)
        sys.exit(1)
