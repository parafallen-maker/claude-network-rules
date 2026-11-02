#!/usr/bin/env python3
"""Account transactions shared by macOS and Linux (Python standard library only).

The shell owns application/network policy. This module owns the data transaction
and the process-wide state lock. Callers must stop the relevant application before
switch/recover. No command interprets legacy recovery folders as disposable data.
"""
import argparse
import contextlib
import datetime
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

ACCOUNTS = ("A", "B", "C", "D")
LOCK_ENV = "CLAUDE_SWITCHER_STATE_LOCK_FD"


class StateError(Exception):
    pass


class StateBusy(StateError):
    pass


def exists(path):
    return os.path.lexists(str(path))


def private_dir(path):
    path = Path(path)
    if exists(path) and (path.is_symlink() or not path.is_dir()):
        raise StateError("状态目录不是普通目录，已停止操作")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(path), 0o700)


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def atomic_write(path, data):
    path = Path(path)
    if path.is_symlink():
        raise StateError("状态文件不能是符号链接")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        sync_dir(path.parent)
    finally:
        if exists(temporary):
            temporary.unlink()


def atomic_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def inherited_lock(base):
    raw = os.environ.get(LOCK_ENV)
    if raw is None:
        return None
    try:
        fd = int(raw)
        if fd < 3:
            raise ValueError()
        actual = os.fstat(fd)
        path = Path(base) / "state.lock"
        expected = path.lstat()
        if (not stat.S_ISREG(actual.st_mode) or not stat.S_ISREG(expected.st_mode)
                or actual.st_uid != os.geteuid()
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
            raise ValueError()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, ValueError):
        raise StateError("继承的账号事务锁无效，已停止操作")


@contextlib.contextmanager
def state_lock(base):
    """Exclusive, nonblocking account/update lock; reuse a verified inherited FD."""
    base = Path(base)
    private_dir(base)
    inherited = inherited_lock(base)
    if inherited is not None:
        yield inherited
        return
    flags = os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(base / "state.lock"), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.geteuid():
            raise StateError("账号锁不是普通文件")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateBusy("另一项账号或更新操作正在进行，请等待完成后重试")
        yield fd
    finally:
        os.close(fd)


def lock_exec(base, command):
    if not command:
        raise StateError("缺少持锁执行命令")
    with state_lock(base) as fd:
        env = dict(os.environ)
        env[LOCK_ENV] = str(fd)
        child = subprocess.Popen(command, env=env, pass_fds=(fd,), start_new_session=True)
        cancelled = []
        previous = {}

        def forward(signum, frame):
            cancelled.append(time.monotonic())
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, forward)
        try:
            while child.poll() is None:
                if cancelled and time.monotonic() - cancelled[0] > 5:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    child.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            # Terminating a shell is not enough: an orphaned copier may still
            # be running. Keep the lock until the entire child process group
            # has exited (zombies cannot mutate files and do not count).
            if process_group_active(child.pid):
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 2
                while process_group_active(child.pid):
                    if time.monotonic() >= deadline:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    time.sleep(0.05)
            return child.returncode if child.returncode >= 0 else 128 - child.returncode
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def process_group_active(group):
    if sys.platform.startswith("linux"):
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            try:
                record = linux_proc_record(directory)
                if record and record["group"] == group and record["state"] != "Z":
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, StateError, ValueError, IndexError):
                try:
                    os.killpg(group, 0)
                    return True
                except ProcessLookupError:
                    return False
        return False
    try:
        listing = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=True)
    except (OSError, subprocess.SubprocessError):
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False
    return any(len(parts) >= 2 and parts[0] == str(group) and not parts[1].startswith("Z")
               for parts in (line.split() for line in listing.stdout.splitlines()))


def linux_proc_record(directory):
    """Map /proc IDs to this PID namespace before using kill(2).

    A container may expose an ancestor's /proc mount. Comparing namespace links
    and using NSpid/NSpgid avoids signalling an unrelated process with the same
    numeric ID and makes child-group supervision correct in that environment.
    """
    if directory.stat().st_uid != os.getuid():
        return None
    details = {}
    for line in (directory / "status").read_text().splitlines():
        if line.startswith(("NSpid:", "NSpgid:")):
            key, values = line.split(":", 1)
            details[key] = [int(value) for value in values.split()]
    own_pid_line = next((line for line in Path("/proc/self/status").read_text().splitlines() if line.startswith("NSpid:")), "")
    if "NSpid" in details and own_pid_line and len(details["NSpid"]) != len(own_pid_line.split()) - 1:
        return None
    if os.readlink(str(directory / "ns/pid")) != os.readlink("/proc/self/ns/pid"):
        return None
    fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
    if "NSpid" not in details or "NSpgid" not in details:
        # Kernels before namespace ID fields are safe only with a matching proc
        # mount. Refuse ambiguous IDs rather than guessing.
        if int(Path("/proc/self/stat").read_text().split()[0]) != os.getpid():
            raise StateError("无法验证 /proc 的进程命名空间，已停止操作")
        details = {"NSpid": [int(directory.name)], "NSpgid": [int(fields[2])]}
    return dict(pid=details["NSpid"][-1], group=details["NSpgid"][-1], state=fields[0],
                start=fields[19], directory=str(directory), host_pid=int(directory.name), parent_host=int(fields[1]))


def copy_tree(source, destination):
    """Create a new tree, never merge into or delete an existing destination."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_dir() or exists(destination):
        raise StateError("快照复制源或暂存目录异常")
    if sys.platform == "darwin":
        # A failed clone owns only its unique destination, so its cleanup is safe.
        clone_ok = False
        if os.environ.get("CLAUDE_SWITCHER_NO_CLONE") != "1":
            clone_ok = subprocess.run(["/bin/cp", "-cRp", str(source), str(destination)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not clone_ok:
            if exists(destination):
                shutil.rmtree(str(destination))
            subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=True)
    elif sys.platform.startswith("linux") and os.environ.get("CLAUDE_SWITCHER_NO_CLONE") != "1":
        copied = subprocess.run(["cp", "-a", "--reflink=auto", "--", str(source), str(destination)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if copied.returncode:
            if exists(destination):
                shutil.rmtree(str(destination))
            shutil.copytree(str(source), str(destination), symlinks=True, copy_function=shutil.copy2)
    else:
        shutil.copytree(str(source), str(destination), symlinks=True, copy_function=shutil.copy2)
    os.chmod(str(destination), 0o700)


def sync_tree(root):
    for directory, dirs, files in os.walk(str(root), followlinks=False, topdown=False):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        sync_dir(directory)


def component_recovery_check(base):
    if exists(base / "install-journal.json"):
        raise StateError("存在未完成安装事务，请先运行 installer/install.py recover-install 恢复")
    if exists(base / "update-journal.json"):
        raise StateError("存在未完成组件更新，请先运行 component-manager.py recover 恢复")


class AccountStore:
    def __init__(self, base, data, fault=None, copier=None):
        self.base = Path(base).absolute()
        self.data = Path(data).absolute()
        self.accounts = self.base / "accounts"
        self.journal = self.base / "account-transaction.json"
        self.fault = fault or (lambda phase: None)
        self.copier = copier or copy_tree
        private_dir(self.base)
        private_dir(self.accounts)
        private_dir(self.base / "transactions")
        if self.data == self.base or self.data in self.base.parents:
            raise StateError("运行数据目录不能包含账号状态目录")
        self.data.parent.mkdir(parents=True, exist_ok=True)
        if self.data.is_symlink():
            raise StateError("运行数据目录不能是符号链接")

    def account(self, value):
        if value not in ACCOUNTS:
            raise StateError("账号必须是 A、B、C 或 D")
        path = self.accounts / value
        if exists(path) and (path.is_symlink() or not path.is_dir()):
            raise StateError("账号快照不是普通目录")
        return path

    def current(self):
        path = self.base / "current"
        if not exists(path):
            return None
        if path.is_symlink() or not path.is_file():
            raise StateError("当前账号标记异常，保留数据并停止")
        value = path.read_text(encoding="utf-8").strip()
        if value not in ACCOUNTS:
            raise StateError("当前账号标记无效，需先恢复，不能猜测账号归属")
        return value

    def marker(self, account):
        path = self.base / "current"
        if account is None:
            if exists(path):
                if path.is_symlink():
                    raise StateError("当前账号标记异常")
                path.unlink()
                sync_dir(self.base)
        else:
            self.account(account)
            atomic_write(path, (account + "\n").encode("ascii"))

    def legacy_recovery_check(self):
        paths = [self.accounts / (a + suffix) for a in ACCOUNTS
                 for suffix in (".new", ".previous", ".tmp")]
        paths += [self.data.with_name(self.data.name + suffix) for suffix in (".new", ".old")]
        if any(exists(path) for path in paths):
            raise StateError("发现旧版 .new/.previous/.tmp/.old 恢复数据，已保留；请先恢复或移至备份目录")
        if exists(self.base / "keep-stage"):
            raise StateError("发现旧版 keep-stage 待恢复资料，已保留；请先核对并恢复，不能覆盖")

    def component_recovery_check(self):
        component_recovery_check(self.base)

    def clean(self):
        self.component_recovery_check()
        if exists(self.journal):
            raise StateError("存在未完成账号事务，请先执行一次账号切换以安全恢复")
        self.legacy_recovery_check()

    def paths(self, record):
        identifier = record.get("id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise StateError("事务记录标识无效，已保留现场")
        if record.get("version") != 1 or record.get("data") != str(self.data):
            raise StateError("事务记录与运行目录不匹配，已保留现场")
        self.account(record.get("target"))
        for field in ("old_account", "archive_account"):
            if record.get(field) is not None:
                self.account(record[field])
        if any(type(record.get(field)) is not bool for field in ("had_live", "had_archive")):
            raise StateError("事务记录字段无效，已保留现场")
        transaction = self.base / "transactions" / identifier
        if not transaction.is_dir() or transaction.is_symlink():
            raise StateError("事务恢复目录不存在或不安全")
        prefix = ".claude-switcher-" + identifier
        result = dict(transaction=transaction,
                      live_new=self.data.parent / (prefix + ".new"),
                      live_old=self.data.parent / (prefix + ".old"),
                      live_aborted=self.data.parent / (prefix + ".aborted"),
                      archive_new=transaction / "archive-new",
                      archive_old=transaction / "archive-old",
                      archive_aborted=transaction / "archive-aborted")
        if any(path.is_symlink() for path in result.values()):
            raise StateError("事务暂存路径包含符号链接，已停止恢复")
        return result

    def save(self, record, phase):
        record["phase"] = phase
        atomic_json(self.journal, record)

    def rename(self, source, destination):
        if exists(destination):
            raise StateError("事务目标已存在，拒绝覆盖未知数据")
        os.rename(str(source), str(destination))
        sync_dir(source.parent)
        if source.parent != destination.parent:
            sync_dir(destination.parent)

    def cleanup_committed(self, record, paths):
        if self.current() != record["target"] or not self.data.is_dir():
            raise StateError("已提交事务的账号标记或运行目录异常，保留恢复副本")
        archive = record["archive_account"]
        if archive is not None and not self.account(archive).is_dir():
            raise StateError("已提交事务缺少原账号快照，保留恢复副本")
        self.fault("cleanup_started")
        # Every removed path is derived from this validated transaction UUID.
        for key in ("live_new", "live_old", "archive_new", "archive_old"):
            path = paths[key]
            if exists(path):
                shutil.rmtree(str(path))
                sync_dir(path.parent)
        self.journal.unlink()
        sync_dir(self.base)
        self.fault("journal_removed")
        shutil.rmtree(str(paths["transaction"]))
        sync_dir(paths["transaction"].parent)

    def recover(self):
        self.component_recovery_check()
        if not exists(self.journal):
            return False
        if self.journal.is_symlink():
            raise StateError("事务记录不能是符号链接")
        try:
            record = json.loads(self.journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise StateError("事务记录损坏，保留所有恢复数据并停止")
        paths = self.paths(record)
        if record.get("phase") == "committed":
            self.cleanup_committed(record, paths)
            return True
        if record.get("phase") not in ("prepared", "live-backed-up", "live-installed", "archive-installed"):
            raise StateError("事务阶段无效，保留所有恢复数据并停止")
        # Infer completed renames from their durable paths, including crashes
        # between a rename and its subsequent journal update.
        if exists(paths["live_old"]):
            if exists(self.data):
                self.rename(self.data, paths["live_aborted"])
            self.rename(paths["live_old"], self.data)
        elif not record["had_live"]:
            if not exists(paths["live_new"]) and exists(self.data):
                self.rename(self.data, paths["live_aborted"])
        elif not self.data.is_dir():
            raise StateError("缺少原运行目录，保留事务供人工恢复")

        archive = record["archive_account"]
        if archive is not None:
            destination = self.account(archive)
            if exists(paths["archive_old"]):
                if exists(destination):
                    self.rename(destination, paths["archive_aborted"])
                self.rename(paths["archive_old"], destination)
            elif not record["had_archive"] and not exists(paths["archive_new"]) and exists(destination):
                self.rename(destination, paths["archive_aborted"])
        self.marker(record["old_account"])
        # Keep the rolled-back material as an explicit recovery backup. It can
        # contain data edited by an externally started application during a crash.
        self.rename(self.journal, paths["transaction"] / "rolled-back.json")
        return True

    def merge_keep(self, source, destination):
        keep = self.base / "keep-in-workspace.txt"
        if not keep.is_file():
            return 0
        count = 0

        def safe(path):
            return not path.is_symlink() and all(not child.is_symlink() for child in path.rglob("*"))

        def merge(src, dst):
            if dst.is_symlink():
                raise StateError("保留路径的目标包含符号链接，已停止切换")
            if src.is_dir():
                if exists(dst) and not dst.is_dir():
                    dst.unlink()
                dst.mkdir(parents=True, exist_ok=True)
                for child in src.iterdir():
                    merge(child, dst / child.name)
            else:
                if dst.is_dir():
                    shutil.rmtree(str(dst))
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))

        for line in keep.read_text(encoding="utf-8").splitlines():
            rel = line.strip()
            if not rel or rel.startswith("#"):
                continue
            components = Path(rel).parts
            if Path(rel).is_absolute() or not components or ".." in components or rel in (".", "./"):
                continue
            src = source / rel
            if any((source.joinpath(*components[:index + 1])).is_symlink() for index in range(len(components))):
                continue
            if not exists(src) or not safe(src):
                continue
            dst = destination / rel
            if any((destination.joinpath(*components[:index + 1])).is_symlink() for index in range(len(components))):
                raise StateError("保留路径的目标包含符号链接，已停止切换")
            merge(src, dst)
            count += 1
        return count

    def switch(self, target):
        self.component_recovery_check()
        self.account(target)
        recovered = self.recover()
        self.legacy_recovery_check()
        old = self.current()
        had_live = self.data.is_dir()
        if exists(self.data) and not had_live:
            raise StateError("运行数据不是普通目录")
        enrolled = old is None and had_live and any(self.data.iterdir())
        archive = "A" if enrolled else (old if had_live else None)
        if enrolled and self.account("A").exists():
            raise StateError("当前账号标记缺失但 A 快照已存在，拒绝猜测并覆盖")
        if old == target and had_live:
            return dict(account=target, changed=False, recovered=recovered, enrolled=False, kept=0)
        if old is not None and not had_live and not self.account(old).is_dir():
            raise StateError("原账号运行目录和快照都缺失，已停止切换")

        identifier = uuid.uuid4().hex
        transaction = self.base / "transactions" / identifier
        private_dir(transaction)
        record = dict(version=1, id=identifier, data=str(self.data), target=target,
                      old_account=old, archive_account=archive, had_live=had_live,
                      had_archive=bool(archive and self.account(archive).exists()))
        paths = self.paths(record)
        prepared = False
        try:
            if enrolled and target == "A":
                self.copier(self.data, paths["live_new"])
            elif self.account(target).is_dir():
                self.copier(self.account(target), paths["live_new"])
            else:
                private_dir(paths["live_new"])
            kept = self.merge_keep(self.data, paths["live_new"]) if had_live else 0
            if archive is not None:
                self.copier(self.data, paths["archive_new"])
                sync_tree(paths["archive_new"])
            sync_tree(paths["live_new"])
            self.save(record, "prepared")
            prepared = True
            self.fault("prepared")
            if had_live:
                self.rename(self.data, paths["live_old"])
            self.fault("live_backup")
            self.save(record, "live-backed-up")
            self.rename(paths["live_new"], self.data)
            self.fault("live_installed")
            self.save(record, "live-installed")
            if archive is not None:
                destination = self.account(archive)
                if record["had_archive"]:
                    self.rename(destination, paths["archive_old"])
                self.fault("archive_backup")
                self.rename(paths["archive_new"], destination)
            self.fault("archive_installed")
            self.save(record, "archive-installed")
            self.marker(target)
            self.fault("marker_written")
            self.save(record, "committed")
            self.fault("committed")
            self.cleanup_committed(record, paths)
            return dict(account=target, changed=True, recovered=recovered, enrolled=enrolled, kept=kept)
        except Exception:
            if prepared and exists(self.journal):
                self.recover()
            # Pre-journal failure has not changed any live/archived data. Keep
            # the unique staging as evidence instead of guessing what is safe.
            raise

    def snapshot_source(self, target):
        self.clean()
        source = self.account(target)
        if self.current() == target:
            raise StateError("该账号正在使用中，请先切换到其他账号再导出")
        if not source.is_dir():
            raise StateError("该账号没有已归档快照")
        return source

    def validate_import(self, target):
        self.clean()
        destination = self.account(target)
        if exists(destination) or self.current() == target:
            raise StateError("目标账号已有快照或正在使用，拒绝覆盖")
        if target == "A" and self.current() is None and self.data.is_dir() and any(self.data.iterdir()):
            raise StateError("首次运行的现有登录资料尚未收编为 A，请先完成首次切换再导入")
        return destination

    def import_snapshot(self, source, target):
        destination = self.validate_import(target)
        source = Path(source)
        marker = source / ".claude-switcher-snapshot.json"
        if source.is_symlink() or not marker.is_file() or marker.is_symlink():
            raise StateError("不是本项目导出的快照镜像")
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise StateError("镜像元数据无效")
        if metadata.get("format") != "claude-switcher-snapshot" or metadata.get("version") != 1 or metadata.get("account") != target:
            raise StateError("镜像元数据校验失败（账号不匹配或格式不支持）")
        # Archives are untrusted input; imported links must never escape the
        # account tree or make a later keep merge follow an external location.
        if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in source.rglob("*")):
            raise StateError("导入快照含符号链接或特殊文件，已拒绝并保留原快照")
        staged = self.accounts / (".import-" + uuid.uuid4().hex)
        try:
            self.copier(source, staged)
            (staged / ".claude-switcher-snapshot.json").unlink()
            sync_tree(staged)
            self.fault("import_staged")
            self.rename(staged, destination)
            self.fault("import_committed")
        finally:
            if exists(staged):
                shutil.rmtree(str(staged))
                sync_dir(self.accounts)

    def status(self):
        pending = False
        if exists(self.journal):
            if self.journal.is_symlink():
                raise StateError("事务记录不能是符号链接")
            record = json.loads(self.journal.read_text(encoding="utf-8"))
            self.paths(record)
            current = (record["target"] if record.get("phase") == "committed" else record["old_account"]) or "none"
            pending = True
        else:
            current = self.current() or "none"
        archived = [a for a in ACCOUNTS if self.account(a).is_dir()]
        fields = ["current=" + current] + ["has" + a + "=" + str(int(a in archived)) for a in ACCOUNTS]
        fields += ["accounts=" + (",".join(archived) or "none")]
        if pending:
            fields.append("pending=1")
        if exists(self.base / "install-journal.json"):
            fields.append("blocked=install")
        elif exists(self.base / "update-journal.json"):
            fields.append("blocked=update")
        for account in archived:
            path = self.account(account)
            size = subprocess.run(["du", "-sh", str(path)], text=True, capture_output=True)
            text = size.stdout.split()[0] if size.returncode == 0 and size.stdout.split() else "?"
            stamp = datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
            fields.append("acc" + account + "=" + text + "@" + stamp)
        return " ".join(fields)


def linux_browser_processes(proc_root=Path("/proc")):
    browsers = []
    for directory in proc_root.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            args = [part.decode("utf-8", "surrogateescape") for part in (directory / "cmdline").read_bytes().split(b"\0") if part]
            if not args or Path(args[0]).name not in ("chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
                continue
            if any(arg.startswith("--type=") for arg in args):
                continue
            record = linux_proc_record(directory)
            if record is None or record["state"] == "Z":
                continue
            profile = None
            for index, arg in enumerate(args[1:], 1):
                if arg.startswith("--user-data-dir="):
                    profile = arg.split("=", 1)[1]
                elif arg == "--user-data-dir" and index + 1 < len(args):
                    profile = args[index + 1]
            if profile is not None and not os.path.isabs(profile):
                profile = str((directory / "cwd").resolve() / profile)
            record["profile"] = profile
            browsers.append(record)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            raise StateError("无法可靠确认浏览器进程，已停止切换")
    return browsers


def quit_linux_browser(data, base, timeout=15):
    data, base = Path(data).absolute(), Path(base).absolute()
    browsers = linux_browser_processes()
    defaults = (base.parent / ".config/google-chrome", base.parent / ".config/chromium")
    if data in defaults and any(item["profile"] is None for item in browsers):
        raise StateError("普通浏览器可能正在使用默认配置目录；请手动完全退出后重试，切换器不会强制关闭它")
    targets = [item for item in browsers if item["profile"] is not None and Path(item["profile"]).resolve() == data.resolve()]
    # Track pre-existing renderer/helper descendants before the main process
    # exits and they are reparented. A surviving helper can still write profile
    # databases, so a disappearing main PID alone is insufficient.
    descendants = []
    parents = {item["host_pid"] for item in targets}
    if parents:
        records = []
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            try:
                record = linux_proc_record(directory)
                if record is not None and record["state"] != "Z":
                    records.append(record)
            except (FileNotFoundError, ProcessLookupError):
                continue
        while True:
            found = [item for item in records if item["parent_host"] in parents and item["host_pid"] not in parents]
            if not found:
                break
            descendants.extend(found)
            parents.update(item["host_pid"] for item in found)

    def alive(item):
        try:
            current = linux_proc_record(Path(item["directory"]))
            return current is not None and current["state"] != "Z" and current["start"] == item["start"]
        except FileNotFoundError:
            return False

    for item in targets:
        if alive(item):
            try:
                os.kill(item["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + timeout
    while any(alive(item) for item in targets + descendants):
        if time.monotonic() >= deadline:
            raise StateError("指定浏览器未能退出，账号资料未修改；请手动退出后重试")
        time.sleep(0.1)
    # A live Chromium profile lock is stronger evidence than a process-name
    # guess. Reject it even when the browser could not be attributed above.
    lock = data / "SingletonLock"
    if lock.is_symlink():
        name = os.readlink(str(lock))
        match = re.search(r"-(\d+)$", name)
        if match:
            try:
                os.kill(int(match.group(1)), 0)
            except ProcessLookupError:
                pass
            else:
                raise StateError("浏览器配置目录仍有活动锁，已停止切换")


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    boundary = raw.index("--") if "--" in raw else len(raw)
    remainder = raw[boundary + 1:] if boundary < len(raw) else []
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("lock-exec", "assert-lock", "config-ready", "switch", "recover", "status", "snapshot-source", "validate-import", "snapshot-import", "quit-browser"))
    parser.add_argument("--base", required=True)
    parser.add_argument("--data")
    parser.add_argument("--account")
    parser.add_argument("--source")
    args = parser.parse_args(raw[:boundary])
    try:
        if args.command == "lock-exec":
            return lock_exec(args.base, remainder)
        if args.command == "assert-lock":
            if inherited_lock(Path(args.base)) is None:
                raise StateError("账号事务未持有互斥锁")
            return 0
        if args.command == "config-ready":
            with state_lock(Path(args.base)):
                component_recovery_check(Path(args.base))
            return 0
        if not args.data:
            raise StateError("缺少运行数据目录")
        with state_lock(Path(args.base)):
            store = AccountStore(args.base, args.data)
            if args.command == "switch":
                result = store.switch(args.account)
                if result["recovered"]:
                    print("NOTE:上次中断事务已安全恢复")
                if result["enrolled"]:
                    print("NOTE:已将当前登录收编为账号A")
                print(("OK:" if result["changed"] else "INFO:") + "账号数据已就绪:" + result["account"])
            elif args.command == "recover":
                print("OK:" + ("上次中断事务已安全恢复" if store.recover() else "没有待恢复事务"))
            elif args.command == "status":
                print(store.status())
            elif args.command == "snapshot-source":
                print(store.snapshot_source(args.account))
            elif args.command == "validate-import":
                store.validate_import(args.account)
            elif args.command == "snapshot-import":
                store.import_snapshot(args.source, args.account)
                print("OK:已导入为账号" + args.account + "(切换时生效)")
            elif args.command == "quit-browser":
                quit_linux_browser(store.data, store.base)
            return 0
    except StateBusy as exc:
        print("FAIL:" + str(exc))
        return 75
    except (StateError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("FAIL:" + (str(exc) if isinstance(exc, StateError) else "账号文件操作失败，已保留恢复资料，请检查权限和可用空间"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
