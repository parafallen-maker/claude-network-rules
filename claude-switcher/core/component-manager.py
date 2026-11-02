#!/usr/bin/env python3
"""Verified component updates. Standard library only; never elevates privileges.

All mutation is serialized by the same state.lock used by account transactions.
A durable journal is written before replacing any file. An interrupted update is
recovered before another update; native components are never silently mixed.
"""
import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile

REPOSITORY = "YOURSJEYO/claude-switcher"
CONFIG_KEYS = ("PROXY_MODE", "PROXY_GROUP", "COUNTRY_TARGET", "TZ_TARGET", "LOCALE_TARGET")
CHECKS = ("代理内核", "TUN网卡", "出口IP", "IP一致", "时区", "地区", "IPv6", "DNS",
          "WebRTC", "保镖", "规则置顶", "客户端语言", "ASN属性", "生态可达性")
OPTIONAL = {"IP一致", "DNS", "WebRTC", "客户端语言", "ASN属性", "生态可达性"}
MAX_ARCHIVE = 64 * 1024 * 1024


class ComponentError(Exception):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


@contextlib.contextmanager
def state_lock(base):
    base = Path(base)
    if base.is_symlink() or (base.exists() and not base.is_dir()):
        raise ComponentError("状态目录不是普通目录")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    lock_path = base / "state.lock"
    inherited = os.environ.get("CLAUDE_SWITCHER_STATE_LOCK_FD")
    owned = inherited is None
    if inherited is not None:
        try:
            fd = int(inherited)
            if fd < 3:
                raise ValueError()
            a, b = os.fstat(fd), lock_path.stat(follow_symlinks=False)
            if ((a.st_dev, a.st_ino) != (b.st_dev, b.st_ino) or not stat.S_ISREG(a.st_mode)
                    or not stat.S_ISREG(b.st_mode) or a.st_uid != os.geteuid()):
                raise ValueError()
        except (OSError, ValueError):
            raise ComponentError("继承的状态锁无效")
    else:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.geteuid():
            raise ComponentError("状态锁归属或类型无效")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ComponentError("另一项账号或更新操作正在运行，请稍后重试")
        yield fd
    finally:
        if owned:
            os.close(fd)


def safe_relative(value):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ComponentError("组件路径无效")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or not p.parts or str(p) != value:
        raise ComponentError("组件路径越界")
    return p


def load_bundle(root):
    root = Path(root).resolve()
    try:
        manifest = json.loads((root / "COMPONENTS.json").read_text())
        if manifest["schema"] != 1 or not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", manifest["version"]):
            raise ValueError()
        files = manifest["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 1000:
            raise ValueError()
        required = {"VERSION", "core/claude-switch.sh", "core/component-manager.py", "core/stun-probe.py",
                    "core/account_state.py", "core/network-check.py", "core/network-guard.py", "core/clash-policy.py", "core/audit-contract.py", "core/owned-policy.json",
                    "core/network-monitor.py", "app/network-notifier.swift", "daemons/local.claude-network-notifier.plist",
                    "app/main.swift", "daemons/claude-bodyguard.sh", "installer/install.py"}
        if not required.issubset(files):
            raise ValueError()
        for relative, expected in files.items():
            safe_relative(relative)
            if not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise ValueError()
            path = root / relative
            parents = [root.joinpath(*PurePosixPath(relative).parts[:n]) for n in range(1, len(PurePosixPath(relative).parts))]
            if path.is_symlink() or not path.is_file() or any(x.is_symlink() for x in parents):
                raise ComponentError("组件包含符号链接或缺失文件")
            if digest(path) != expected:
                raise ComponentError("组件 SHA256 校验失败: " + relative)
        if (root / "VERSION").read_text().strip() != manifest["version"]:
            raise ValueError()
        # Every executable/helper is covered, even when new helpers are added.
        for directory in ("core", "daemons", "installer", "app"):
            for path in (root / directory).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    if path.relative_to(root).as_posix() not in files:
                        raise ComponentError("组件未列入校验清单")
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise ComponentError("安装包组件清单无效或缺失")


def configs_from(text):
    result = {}
    for key in CONFIG_KEYS:
        match = re.search(r"^" + key + r"=(.+)$", text, re.M)
        if match:
            try:
                quoted = re.fullmatch(r'"((?:\\.|[^"\\])*)"\s*(?:#.*)?', match.group(1).strip())
                if quoted:
                    # shlex intentionally keeps \$ and \` inside double quotes;
                    # decode shell's literal escapes without ever evaluating shell text.
                    result[key] = re.sub(r'\\([$`"\\])', lambda m: m[1], quoted[1])
                    continue
                values = shlex.split(match.group(1), comments=True)
                if len(values) == 1:
                    result[key] = values[0]
            except ValueError:
                raise ComponentError("已安装配置无法解析")
    return result


def inject_configs(text, config):
    for key, value in config.items():
        if key not in CONFIG_KEYS or not isinstance(value, str) or any(x in value for x in ("\n", "\r", "\x00")):
            raise ComponentError("配置字段无效")
        value = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        text, count = re.subn(r"^" + key + r"=.*$", lambda _: key + '="' + value + '"', text, flags=re.M)
        if count != 1:
            raise ComponentError("安装包缺少唯一配置字段: " + key)
    return text


def validate_check(returncode, output, config=None):
    if returncode != 0:
        raise ComponentError("更新后环境检查进程退出失败")
    config = config or {}
    disabled = config.get("disabled", [])
    allow_failures = config.get("allowOptionalFailures", False)
    if not isinstance(disabled, list) or any(x not in OPTIONAL for x in disabled) or type(allow_failures) is not bool:
        raise ComponentError("检查设置无效")
    rows = {}
    for line in output.splitlines():
        match = re.fullmatch(r"([^=]+)=(ok|warn|fail|skip)(?::.*)?", line)
        if match and match[1] in CHECKS:
            if match[1] in rows:
                raise ComponentError("环境检查结果包含重复项")
            rows[match[1]] = match[2]
    if set(rows) != set(CHECKS):
        raise ComponentError("环境检查未完整返回全部 14 项")
    for name, status in rows.items():
        if name not in OPTIONAL and status != "ok":
            raise ComponentError("硬性环境检查未通过: " + name)
        if name in OPTIONAL and (status == "fail" or (status == "skip" and name not in disabled)):
            raise ComponentError("可选环境检查未通过或被擅自跳过: " + name)
    return rows


def check_installed(script, base, timeout=150):
    try:
        config = json.loads((Path(base) / "checks.json").read_text()) if (Path(base) / "checks.json").exists() else {}
    except (OSError, ValueError):
        raise ComponentError("检查设置无法读取")
    inherited = os.environ.get("CLAUDE_SWITCHER_STATE_LOCK_FD")
    descriptors = ()
    if inherited is not None:
        with state_lock(base) as fd:
            descriptors = (fd,)
    process = subprocess.Popen(["/bin/bash", str(script), "checkdetail"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True, pass_fds=descriptors)
    try:
        data, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ComponentError("环境检查超时")
    return validate_check(process.returncode, data.decode("utf-8", errors="replace"), config)


class FileTransaction:
    """Rollback all declared destinations, including a crash between rename/journal."""
    def __init__(self, base, destinations):
        self.base = Path(base)
        self.journal = self.base / "update-journal.json"
        if self.journal.exists():
            raise ComponentError("存在待恢复的更新事务")
        self.work = self.base / "updates" / uuid.uuid4().hex
        self.work.mkdir(parents=True, mode=0o700)
        entries = []
        for i, dest in enumerate(destinations):
            dest = Path(dest).absolute()
            if dest.is_symlink() or (dest.exists() and not dest.is_file()):
                raise ComponentError("更新目标不是普通文件")
            backup = self.work / (str(i) + ".bak")
            existed = dest.exists()
            mode = stat.S_IMODE(dest.stat().st_mode) if existed else 0o600
            if existed:
                shutil.copyfile(dest, backup)
                os.chmod(backup, 0o600)
                with backup.open("rb") as h:
                    os.fsync(h.fileno())
            entries.append({"path": str(dest), "backup": str(backup), "existed": existed, "mode": mode,
                            "backup_sha256": digest(backup) if existed else None})
        self.record = {"schema": 1, "phase": "prepared", "entries": entries, "work": str(self.work)}
        write_json(self.journal, self.record)

    def replace(self, dest, data, mode=0o600):
        if str(Path(dest).absolute()) not in {x["path"] for x in self.record["entries"]}:
            raise ComponentError("事务写入了未声明路径")
        atomic_write(dest, data, mode)

    def commit(self):
        self.record["phase"] = "committed"
        write_json(self.journal, self.record)
        self.finish_commit()

    def finish_commit(self):
        # Once the durable commit marker exists, cleanup failure is not a failed update.
        with contextlib.suppress(OSError):
            self.journal.unlink()
            shutil.rmtree(self.work)

    def rollback(self):
        for entry in self.record["entries"]:
            if entry["existed"] and digest(entry["backup"]) != entry.get("backup_sha256"):
                raise ComponentError("回滚备份校验失败，保留事务供人工恢复")
        for entry in reversed(self.record["entries"]):
            path = Path(entry["path"])
            if entry["existed"]:
                atomic_write(path, Path(entry["backup"]).read_bytes(), entry["mode"])
            elif path.exists():
                path.unlink()
        self.journal.unlink()
        shutil.rmtree(self.work)


def recover_update(base, bin_dir, extra_roots=()):
    journal = Path(base) / "update-journal.json"
    if not journal.exists():
        return False
    try:
        record = json.loads(journal.read_text())
        work = Path(record["work"])
        if record["schema"] != 1 or work.parent != Path(base).absolute() / "updates":
            raise ValueError()
        allowed_roots = {Path(base).absolute(), Path(bin_dir).absolute()} | {Path(x).absolute() for x in extra_roots}
        for entry in record["entries"]:
            p, b = Path(entry["path"]), Path(entry["backup"])
            if p.parent not in allowed_roots or b.parent != work or p.is_symlink() or b.is_symlink():
                raise ValueError()
            if entry["existed"] and not b.is_file():
                raise ValueError()
            if entry["existed"] and digest(b) != entry.get("backup_sha256"):
                raise ValueError()
        txn = object.__new__(FileTransaction)
        txn.base, txn.journal, txn.work, txn.record = Path(base), journal, work, record
        if record["phase"] == "committed":
            journal.unlink()
            shutil.rmtree(work)
        else:
            txn.rollback()
        return True
    except (OSError, ValueError, KeyError, TypeError):
        raise ComponentError("更新恢复记录异常，已保留备份且停止写入")


def installed_manifest(base):
    try:
        value = json.loads((Path(base) / "components.json").read_text())
        return value if isinstance(value.get("files"), dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def native_pending(manifest, installed):
    old = installed.get("files", {})
    pending = []
    if any(old.get(name) != h for name, h in manifest["files"].items() if name.startswith(("daemons/", "installer/")) or name in ("core/network-check.py", "core/network-guard.py", "core/network-monitor.py", "core/clash-policy.py", "core/owned-policy.json")):
        pending.append("守护进程/安装组件")
    if any(old.get(name) != h for name, h in manifest["files"].items() if name.startswith(("app/", "assets/"))):
        pending.append("界面 App")
    return pending


def apply_bundle(root, base, bin_dir, revision, checker=check_installed):
    root, base, bin_dir = Path(root), Path(base), Path(bin_dir)
    manifest = load_bundle(root)
    current = bin_dir / "claude-switch.sh"
    config = configs_from(current.read_text()) if current.exists() else {}
    items = {}
    for relative in manifest["files"]:
        if relative == "core/claude-switch.sh":
            items[current] = (inject_configs((root / relative).read_text(), config).encode(), 0o700)
        elif relative.startswith("core/") and PurePosixPath(relative).parent == PurePosixPath("core") and (relative.endswith(".py") or relative == "core/owned-policy.json"):
            items[base / Path(relative).name] = ((root / relative).read_bytes(), 0o600)
    metadata = dict(manifest, revision=revision, installed_at=int(time.time()))
    items[base / "components.json"] = ((json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode(), 0o600)
    items[base / "version.txt"] = ((manifest["version"] + "\n").encode(), 0o600)
    txn = FileTransaction(base, items)
    try:
        for dest, (data, mode) in items.items():
            txn.replace(dest, data, mode)
        checker(current, base)
        txn.commit()
    except BaseException:
        txn.rollback()
        raise
    return metadata


def fetch(url, limit=MAX_ARCHIVE):
    request = urllib.request.Request(url, headers={"User-Agent": "ClaudeSwitcher", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ComponentError("下载内容超过大小限制")
    return data


def version_key(value):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+]([A-Za-z0-9._-]+))?", value)
    if not m:
        return (0, 0, 0, 0, value)
    return tuple(int(m[x]) for x in (1, 2, 3)) + (0 if m[4] else 1, m[4] or "")


def resolve_revision(track):
    prefix = "https://api.github.com/repos/" + REPOSITORY
    if track == "stable":
        release = json.loads(fetch(prefix + "/releases/latest", 1024 * 1024))
        if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v[0-9A-Za-z._-]+", release.get("tag_name", "")):
            raise ComponentError("稳定发布元数据无效")
        ref, version = release["tag_name"], release["tag_name"][1:]
    else:
        ref, version = "main", None
    commit = json.loads(fetch(prefix + "/commits/" + ref, 4 * 1024 * 1024))
    sha = commit.get("sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ComponentError("无法固定更新提交")
    return sha, version


def download_bundle(revision, work):
    archive = Path(work) / "source.zip"
    archive.write_bytes(fetch("https://codeload.github.com/" + REPOSITORY + "/zip/" + revision))
    manifest_bytes = fetch("https://raw.githubusercontent.com/" + REPOSITORY + "/" + revision + "/COMPONENTS.json", 1024 * 1024)
    total = 0
    extract = Path(work) / "source"
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 5000:
            raise ComponentError("安装包文件数量异常")
        for info in z.infolist():
            relative = info.filename.rstrip("/")
            safe_relative(relative)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ComponentError("安装包包含符号链接")
            total += info.file_size
            if total > MAX_ARCHIVE:
                raise ComponentError("安装包展开体积过大")
            destination = extract / relative
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with z.open(info) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
    roots = list(extract.iterdir())
    if len(roots) != 1 or not roots[0].is_dir() or (roots[0] / "COMPONENTS.json").read_bytes() != manifest_bytes:
        raise ComponentError("固定提交与组件清单不一致")
    load_bundle(roots[0])
    return roots[0]


def run_update(args):
    base, bin_dir = args.base.absolute(), args.bin_dir.absolute()
    if (base / "account-transaction.json").exists():
        raise ComponentError("存在待恢复账号事务，请先执行 claude-switch.sh recover")
    if (base / "install-journal.json").exists():
        raise ComponentError("存在待恢复安装事务，请先运行 installer/install.py recover-install")
    if args.check and (base / "update-journal.json").exists():
        raise ComponentError("存在待恢复更新，请先运行 component-manager.py recover")
    if not args.check:
        with state_lock(base):
            recover_update(base, bin_dir)
    installed = installed_manifest(base)
    revision, version = resolve_revision(args.track)
    if version and installed.get("version") and version_key(version) <= version_key(installed["version"]):
        print("OK:当前版本不低于稳定发布 " + version + "（不会自动降级）")
        return 0
    with tempfile.TemporaryDirectory(prefix="claude-switcher-verified-") as work:
        root = download_bundle(revision, work)
        manifest = load_bundle(root)
        changed = [p for p, h in manifest["files"].items() if installed.get("files", {}).get(p) != h]
        if not changed:
            print("OK:全部组件与目标提交一致 " + revision[:12])
            return 0
        pending = native_pending(manifest, installed)
        if args.check:
            print("INFO:发现版本 " + manifest["version"] + " track=" + args.track + " commit=" + revision[:12])
            if pending:
                print("INFO:需要重新运行完整安装器: " + "、".join(pending))
            return 0
        with state_lock(base):
            if (base / "account-transaction.json").exists():
                raise ComponentError("存在待恢复账号事务，请先执行 claude-switch.sh recover")
            recover_update(base, bin_dir)
            installed = installed_manifest(base)
            pending = native_pending(manifest, installed)
            if pending:
                target = base / "pending-update" / revision
                if target.exists():
                    load_bundle(target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copytree(root, target)
                write_json(base / "pending-update.json", {"schema": 1, "revision": revision,
                           "version": manifest["version"], "components": pending, "source": str(target)})
                print("INFO:已验证并暂存完整安装包；当前版本未替换。需要重新运行安装器: " + "、".join(pending))
                print("INFO:安装入口 " + str(target / "installer/安装.command"))
                return 0
            apply_bundle(root, base, bin_dir, revision)
            print("OK:全部用户组件更新完成 " + manifest["version"] + " commit=" + revision[:12])
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("update", "verify", "recover"))
    parser.add_argument("--base", type=Path, default=Path.home() / ".claude-switcher")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--track", choices=("stable", "main"), default="stable")
    args = parser.parse_args()
    try:
        if args.action == "verify":
            if args.source is None:
                raise ComponentError("verify 需要 --source")
            print("OK:组件清单通过 " + load_bundle(args.source)["version"])
        elif args.action == "recover":
            with state_lock(args.base):
                recovered = recover_update(args.base, args.bin_dir)
            print("OK:" + ("已恢复中断更新" if recovered else "没有待恢复更新"))
        else:
            if args.source is not None:
                raise ComponentError("在线更新不接受未验证来源覆盖")
            return run_update(args)
    except (ComponentError, OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        # Do not echo HTTP bodies, proxy credentials, addresses or check output.
        print("FAIL:" + (str(exc) if isinstance(exc, ComponentError) else "组件操作失败，未确认成功；请查看本地事务状态"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
