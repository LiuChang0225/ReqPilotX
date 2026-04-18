#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / ".codex" / "skills-src"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync repo-managed Codex skills into CODEX_HOME.")
    parser.add_argument("--validate", action="store_true", help="Validate the repo-managed skill source tree.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing installed skills.")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Sync only the named skill. Can be passed multiple times.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skill_dirs = discover_skill_dirs(args.only)
    if not skill_dirs:
        sys.stderr.write("No skills found to sync.\n")
        return 1
    errors = validate_skill_dirs(skill_dirs)
    if errors:
        for error in errors:
            sys.stderr.write(f"{error}\n")
        return 1
    if args.validate:
        for skill_dir in skill_dirs:
            print(f"OK {skill_dir.name}")
        print("Validation completed.")
        return 0
    dest_root = codex_skill_root()
    dest_root.mkdir(parents=True, exist_ok=True)
    for skill_dir in skill_dirs:
        sync_skill(skill_dir, dest_root, overwrite=args.overwrite)
    print("Restart Codex to pick up new skills.")
    return 0


def discover_skill_dirs(only: list[str]) -> list[Path]:
    if not SOURCE_ROOT.exists():
        return []
    selected = {item.strip() for item in only if item.strip()}
    skill_dirs = [path for path in sorted(SOURCE_ROOT.iterdir()) if path.is_dir()]
    if selected:
        skill_dirs = [path for path in skill_dirs if path.name in selected]
    return skill_dirs


def validate_skill_dirs(skill_dirs: list[Path]) -> list[str]:
    errors: list[str] = []
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            errors.append(f"{skill_dir.name}: missing SKILL.md")
            continue
        metadata = parse_skill_frontmatter(skill_md)
        if not metadata.get("name"):
            errors.append(f"{skill_dir.name}: SKILL.md is missing frontmatter `name`")
        if not metadata.get("description"):
            errors.append(f"{skill_dir.name}: SKILL.md is missing frontmatter `description`")
        ref_dir = skill_dir / "references"
        if not ref_dir.exists():
            errors.append(f"{skill_dir.name}: missing references/ directory")
        else:
            if not (ref_dir / "track-contract.md").exists():
                errors.append(f"{skill_dir.name}: missing references/track-contract.md")
            if not (ref_dir / "gates.md").exists():
                errors.append(f"{skill_dir.name}: missing references/gates.md")
    return errors


def sync_skill(skill_dir: Path, dest_root: Path, *, overwrite: bool) -> None:
    dest_dir = dest_root / skill_dir.name
    if dest_dir.exists():
        if not overwrite:
            raise SystemExit(f"{dest_dir} already exists. Re-run with --overwrite.")
        shutil.rmtree(dest_dir)
    shutil.copytree(skill_dir, dest_dir)
    ensure_openai_yaml(dest_dir)
    print(f"Synced {skill_dir.name} -> {dest_dir}")


def ensure_openai_yaml(skill_dir: Path) -> None:
    agents_dir = skill_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    openai_yaml = agents_dir / "openai.yaml"
    if openai_yaml.exists():
        return
    metadata = parse_skill_frontmatter(skill_dir / "SKILL.md")
    name = metadata.get("name") or skill_dir.name
    description = metadata.get("description") or f"Use ${name} for this track."
    display_name = titleize(name)
    prompt = f"Use ${name} to execute this ReqPilotX governance task with the declared scope, gates, and handoff rules."
    openai_yaml.write_text(
        "\n".join(
            [
                "interface:",
                f'  display_name: "{display_name}"',
                f'  short_description: "{escape_yaml(description)}"',
                f'  default_prompt: "{escape_yaml(prompt)}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def parse_skill_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
    if not match:
        return {}
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata


def codex_skill_root() -> Path:
    codex_home = str(os.environ.get("CODEX_HOME") or "").strip()
    if codex_home:
        return Path(codex_home).expanduser().resolve() / "skills"
    return Path.home() / ".codex" / "skills"


def titleize(value: str) -> str:
    parts = [part for part in re.split(r"[-_]+", value) if part]
    return " ".join(part.capitalize() for part in parts) or value


def escape_yaml(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


if __name__ == "__main__":
    raise SystemExit(main())
