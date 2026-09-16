"""Prompt registry: versioned prompt files (bundled + UI-created overlay) and which version is active. See registry.py."""
from __future__ import annotations

from services.prompts.registry import BUNDLED_DIR, KINDS, PromptRegistry, parse_prompt

CURRENT: PromptRegistry | None = None   # set once at app startup; loaders fall back to the bundled files when None


def set_current(reg: PromptRegistry | None) -> None:
    global CURRENT
    CURRENT = reg


def prompt(kind: str, version: str | None = None) -> tuple[str, str]:
    """(system, user) for `kind` - the registry's active/overlay version when a registry is installed, else the bundled file."""
    if CURRENT is not None:
        return CURRENT.prompt(kind, version)
    k = KINDS[kind]
    return parse_prompt((BUNDLED_DIR / k.dir / f"{version or k.default}{k.suffix}").read_text(encoding="utf-8"))


def text(kind: str, version: str | None = None) -> str:
    if CURRENT is not None:
        return CURRENT.text(kind, version)
    k = KINDS[kind]
    p = BUNDLED_DIR / k.dir / f"{version or k.default}{k.suffix}"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def active(kind: str, fallback: str | None = None) -> str:
    return CURRENT.active(kind) if CURRENT is not None else (fallback or KINDS[kind].default)
