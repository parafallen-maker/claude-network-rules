#!/usr/bin/env python3
"""Build a verified source or source-installer ZIP on any OS; never claims a native build."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("components", ROOT / "core/component-manager.py")
components = importlib.util.module_from_spec(spec)
spec.loader.exec_module(components)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kit", action="store_true", help="Package the source installer kit instead of the complete source tree")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    manifest = components.load_bundle(ROOT)
    paths = set(manifest["files"]) | {"COMPONENTS.json", "SHA256SUMS", "README.md", "README.en.md", "AGENTS.md", "SECURITY.md"}
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").rglob("*")
                 if p.is_file() and p.suffix in (".md", ".json"))
    revision = "unknown"
    if not args.kit:
        # Read working-tree content for tracked files so local changes are never
        # silently replaced by HEAD's older content. Do not include arbitrary untracked files.
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
        if result.returncode == 0:
            paths.update(x for x in result.stdout.decode().split("\0") if x)
        paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("*") if p.is_file() and p.suffix in (".py", ".ps1", ".swift", ".md"))
    git_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    if git_revision.returncode == 0:
        revision = git_revision.stdout.strip()
    label = "installer-kit" if args.kit else "source"
    output = args.output or ROOT / "build" / ("ClaudeSwitcher-" + manifest["version"] + "-" + label + ".zip")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = "ClaudeSwitcher-" + manifest["version"] + ("" if args.kit else "-source")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(paths):
            components.safe_relative(relative)
            path = ROOT / relative
            if path.is_symlink():
                raise components.ComponentError("源码包含符号链接")
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, prefix + "/" + relative)
        archive.writestr(prefix + "/SOURCE-INFO.json", json.dumps({"version": manifest["version"], "base_revision": revision,
                         "contents": "verified working tree", "native_build": False}, indent=2) + "\n")
    print(str(output))


if __name__ == "__main__":
    main()
