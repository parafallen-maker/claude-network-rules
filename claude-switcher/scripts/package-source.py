#!/usr/bin/env python3
"""Build a verified source or source-installer ZIP on any OS; never claims a native build."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("components", ROOT / "core/component-manager.py")
components = importlib.util.module_from_spec(spec)
spec.loader.exec_module(components)


def source_paths(manifest, kit):
    paths = set(manifest["files"]) | {"COMPONENTS.json", "SHA256SUMS"}
    paths.update(name for name in ("README.md", "README.en.md", "AGENTS.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md")
                 if (ROOT / name).is_file())
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").rglob("*")
                 if p.is_file() and p.suffix in (".md", ".json", ".txt"))
    if not kit:
        # Read working-tree content for tracked files so local changes are never
        # silently replaced by HEAD's older content. Do not include arbitrary untracked files.
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
        if result.returncode == 0:
            paths.update(x for x in result.stdout.decode().split("\0") if x)
        paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("*") if p.is_file() and p.suffix in (".py", ".ps1", ".swift", ".md"))
    # This file is generated for this archive, including when the input itself
    # came from an earlier source archive.
    paths.discard("SOURCE-INFO.json")
    selected = []
    for relative in sorted(paths):
        components.safe_relative(relative)
        path = ROOT / relative
        parents = [ROOT.joinpath(*Path(relative).parts[:n]) for n in range(1, len(Path(relative).parts))]
        if path.is_symlink() or any(parent.is_symlink() for parent in parents):
            raise components.ComponentError("源码包含符号链接")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            selected.append(relative)
    return selected


def source_info(manifest):
    revision = "unknown"
    git_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    if git_revision.returncode == 0:
        revision = git_revision.stdout.strip()
    return json.dumps({"version": manifest["version"], "base_revision": revision,
                       "contents": "verified working tree", "native_build": False}, indent=2) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kit", action="store_true", help="Package the source installer kit instead of the complete source tree")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--output", type=Path)
    target.add_argument("--directory", type=Path, help="Stage verified files in a new directory instead of creating a ZIP")
    args = p.parse_args()
    manifest = components.load_bundle(ROOT)
    paths = source_paths(manifest, args.kit)
    metadata = source_info(manifest)
    if args.directory is not None:
        destination = args.directory.resolve()
        destination.mkdir(parents=True, exist_ok=False)
        for relative in paths:
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, path)
        (destination / "SOURCE-INFO.json").write_text(metadata, encoding="utf-8")
        components.load_bundle(destination)
        print(str(destination))
        return
    label = "installer-kit" if args.kit else "source"
    output = args.output or ROOT / "build" / ("ClaudeSwitcher-" + manifest["version"] + "-" + label + ".zip")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = "ClaudeSwitcher-" + manifest["version"] + ("" if args.kit else "-source")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in paths:
            archive.write(ROOT / relative, prefix + "/" + relative)
        archive.writestr(prefix + "/SOURCE-INFO.json", metadata)
    print(str(output))


if __name__ == "__main__":
    main()
