"""Install an intact built capability bundle into native host discovery roots.

Host wrappers never rewrite shared skills or runtime files. Pair uses its
existing filesystem-skill Cordis provider; no second extraction implementation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import uuid

from .contracts import bundle_integrity, read, save

MARKER = "reqpilot-host-install.json"
BEGIN = "# BEGIN REQPILOT MANAGED SKILLS"
END = "# END REQPILOT MANAGED SKILLS"


def pair_patch(original: str, skills: Path) -> str:
    """Preserve unrelated YAML verbatim, including Cordis !!js expressions."""
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise ValueError("Invalid ReqPilot managed block")
    if BEGIN in original:
        start, rest = original.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        original = start + tail.lstrip("\n")
    elif "id: reqpilot-skills" in original:
        raise ValueError("Unmanaged reqpilot-skills registration already exists")
    meaningful = "\n".join(
        line for line in original.splitlines() if not line.lstrip().startswith("#")
    ).strip()
    if meaningful == "[]":
        original = "\n".join(
            line for line in original.splitlines() if line.strip() != "[]"
        )
    return (
        original.rstrip()
        + "\n\n"
        + BEGIN
        + "\n"
        + (
            "- insert:\n"
            "    - id: reqpilot-skills\n"
            "      name: '@deepseek-ai/dsh-skill-filesystem'\n"
            "      config:\n"
            "        providerName: reqpilot\n"
            "        includeDefaultRoots: false\n"
            "        customSkillDirs:\n"
            f"          - {json.dumps(str(skills), ensure_ascii=False)}\n"
        )
        + END
        + "\n"
    )


def install(bundle: Path, target: Path, host: str, receipt_dir: Path) -> dict:
    receipt = bundle_integrity(bundle)
    target = target.expanduser().absolute()
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    transaction = uuid.uuid4().hex
    if target.exists() and not (target / MARKER).is_file():
        raise ValueError(f"Refusing to replace an unmanaged install: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".staging-" + transaction)
    shutil.copytree(
        bundle, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    result = {
        "host": host,
        "target": str(target),
        "bundle_sha256": receipt["bundle_sha256"],
        "bundle_version": read(bundle / ".codex-plugin/plugin.json")["version"],
        "transaction": transaction,
        "discovery_verified": False,
    }
    if host == "antigravity":
        save(
            staging / "plugin.json",
            {
                "name": "reqpilot",
                "description": "ReqPilotX: source-faithful customer SOR extraction, workbench and same-version Excel.",
            },
        )
    save(staging / MARKER, result)
    bundle_integrity(staging, receipt["bundle_sha256"])
    if target.exists():
        backup = receipt_dir / ("previous-" + transaction)
        shutil.move(str(target), str(backup))
        result["backup"] = str(backup)
    staging.rename(target)
    bundle_integrity(target, receipt["bundle_sha256"])
    save(receipt_dir / ("install-" + transaction + ".json"), result)
    return result


def configure_pair(dsh_home: Path, target: Path, receipt_dir: Path, profiles) -> list:
    results = []
    for name in profiles:
        if name not in {"web", "headless"}:
            raise ValueError("Only the known Pair web/headless profiles are supported")
        config = dsh_home / "profiles" / name / "cordis.patch.yml"
        original = config.read_text()
        updated = pair_patch(original, target / "skills")
        if updated != original:
            backup = receipt_dir / (name + "-" + uuid.uuid4().hex + ".patch.yml")
            shutil.copy2(config, backup)
            config.write_text(updated)
            results.append(
                {"profile": name, "path": str(config), "backup": str(backup)}
            )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--host", choices=["antigravity", "pair"], required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--dsh-home", type=Path)
    args = parser.parse_args()
    if args.host == "pair" and args.dsh_home is None:
        parser.error("Pair requires --dsh-home")
    result = install(args.bundle, args.target, args.host, args.receipts)
    if args.host == "pair":
        result["profiles"] = configure_pair(
            args.dsh_home, args.target, args.receipts, ["web", "headless"]
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
