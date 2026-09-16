"""One registry for every prompt the platform sends to a model - editable from the admin UI without a restart.

Bundled prompts live in the repo (`prompts/…`). Versions written from the UI go to the data volume
(`data/prompts/…`, the *overlay*) so they survive image updates and a bundled file is never edited in place -
the project rule is "keep old versions next to new ones". Which version is active per prompt kind, plus the
institution context, is persisted in `data/prompt_config.json`; `.env` values are the defaults.

Prompt files are Markdown with `## system` and `## user` sections and `{placeholder}` slots filled by code.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

BUNDLED_DIR = Path(__file__).resolve().parents[2] / "prompts"
_LOCK = threading.RLock()
_SECTION = re.compile(r"^## (\w+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    subdir: str                 # folder under prompts/
    prefix: str                 # file-name prefix of this kind's versions ("" = any .md except other kinds' prefixes)
    default: str                # the version .env / code defaults to
    used_by: str
    placeholders: tuple[str, ...]
    sections: bool = True       # True = must contain ## system and ## user; False = free text (style guide, roster)
    suffix: str = ".md"

    @property
    def dir(self) -> Path:
        return Path(self.subdir) if self.subdir else Path(".")


KINDS: dict[str, Kind] = {k.key: k for k in (
    Kind("video-analysis", "Video analysis", "video-analysis", "v", "v2", "every video: watch → knowledge blocks (BlockEngine)",
         ("institution_context", "known_people", "source_name")),
    Kind("people-pass", "People pass", "video-analysis", "people_", "people_v1", "second pass that names the people in a video against the roster",
         ("institution_context", "known_people", "source_name")),
    Kind("blog", "Blog writer", "content-generation", "blog_", "blog_v2", "articles drafted from the knowledge base (Blog Agent)",
         ("institution_context", "style_guide", "brief", "evidence", "target_words", "formats", "images")),
    Kind("shots", "Shot picker", "content-generation", "shots_", "shots_v1", "describing stills cut from a video for article pictures",
         ("institution_context",)),
    Kind("cuts", "Reel cuts", "video-composer", "cuts_", "cuts_v1", "asking Gemini for better reel windows in the studio",
         ("institution_context", "min_seconds", "max_seconds", "target_seconds", "max_cuts", "blocks", "rule_based")),
    Kind("style-guide", "Style guide", "content-generation", "style_guide", "style_guide", "house style handed to the blog writer as {style_guide}",
         (), sections=False),
    Kind("known-people", "Known people", "", "known_people", "known_people", "roster the analyst may recognise (one name per line, # comments)",
         (), sections=False, suffix=".txt"),
)}


class PromptConfig(BaseModel):
    active: dict[str, str] = Field(default_factory=dict)      # kind -> version (only when it differs from the default)
    institution_context: str | None = None                     # None = use .env


def split_sections(text: str) -> dict[str, str]:
    parts = _SECTION.split(text)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def parse_prompt(text: str) -> tuple[str, str]:
    """(system, user) from a prompt file; raises ValueError when a section is missing."""
    parts = split_sections(text)
    if "system" not in parts or "user" not in parts:
        raise ValueError("a prompt needs a '## system' and a '## user' section")
    return parts["system"], parts["user"]


class PromptRegistry:
    def __init__(self, overlay_dir: Path, config_file: Path, *, defaults: dict[str, str] | None = None, institution_context: str = ""):
        self.overlay_dir = overlay_dir
        self.config_file = config_file
        self.env_defaults = dict(defaults or {})            # kind -> version from .env (overrides the code default)
        self.env_institution_context = institution_context
        self.config = self._load()
        self.listeners: list = []                           # callables run after something changed (engine / agent reload)

    # ------------------------------------------------------------------ files
    def _load(self) -> PromptConfig:
        if self.config_file.exists():
            try:
                return PromptConfig.model_validate_json(self.config_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - a corrupt config means "defaults"
                pass
        return PromptConfig()

    def _save(self) -> None:
        with _LOCK:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_file.with_suffix(".tmp")
            tmp.write_text(self.config.model_dump_json(indent=1), encoding="utf-8")
            tmp.replace(self.config_file)

    def _filename(self, kind: Kind, version: str) -> str:
        return f"{version}{kind.suffix}"

    def path(self, kind_key: str, version: str) -> Path | None:
        """Where this version lives: the overlay wins over the bundled file. None when neither exists."""
        kind = KINDS[kind_key]
        for base in (self.overlay_dir, BUNDLED_DIR):
            p = base / kind.dir / self._filename(kind, version)
            if p.exists():
                return p
        return None

    def versions(self, kind_key: str) -> list[dict[str, Any]]:
        kind = KINDS[kind_key]
        others = [k.prefix for k in KINDS.values() if k.subdir == kind.subdir and k.key != kind_key and k.prefix and not kind.prefix.startswith(k.prefix)]
        seen: dict[str, dict[str, Any]] = {}
        for base, source in ((BUNDLED_DIR, "bundled"), (self.overlay_dir, "custom")):
            d = base / kind.dir
            if not d.is_dir():
                continue
            for f in sorted(d.glob(f"*{kind.suffix}")):
                name = f.name[: -len(kind.suffix)]
                if kind.prefix and not name.startswith(kind.prefix):
                    continue
                if not kind.prefix and any(name.startswith(o) for o in others):
                    continue
                if kind.key == "video-analysis" and name.startswith("people_"):
                    continue
                if kind.key == "style-guide" and not name.startswith("style_guide"):
                    continue
                st = f.stat()
                seen[name] = {"version": name, "source": source, "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")}
        return sorted(seen.values(), key=lambda v: (v["version"] != self.default(kind_key), _natural(v["version"])))

    def read(self, kind_key: str, version: str) -> str:
        p = self.path(kind_key, version)
        if p is None:
            raise FileNotFoundError(f"{kind_key}: no version '{version}'")
        return p.read_text(encoding="utf-8")

    def validate(self, kind_key: str, text: str) -> list[str]:
        """Problems that must block saving; placeholder gaps are returned by `warnings()` instead."""
        kind = KINDS[kind_key]
        problems: list[str] = []
        if not text.strip():
            problems.append("the prompt is empty")
        if kind.sections:
            try:
                parse_prompt(text)
            except ValueError as exc:
                problems.append(str(exc))
        return problems

    def warnings(self, kind_key: str, text: str) -> list[str]:
        kind = KINDS[kind_key]
        return [f"placeholder {{{p}}} is not used" for p in kind.placeholders if "{" + p + "}" not in text]

    def save(self, kind_key: str, version: str, text: str, *, overwrite: bool = False) -> Path:
        """Write a version into the overlay. Bundled versions can never be overwritten; custom ones only with overwrite=True."""
        kind = KINDS[kind_key]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", version):
            raise ValueError("version names: letters, digits, _ . - (e.g. v3, blog_v3, cuts_v2)")
        if kind.prefix and not version.startswith(kind.prefix):
            raise ValueError(f"versions of '{kind.label}' must start with '{kind.prefix}' (e.g. {kind.prefix}{'3' if kind.prefix.endswith('_') or kind.prefix == 'v' else '_v2'})")
        problems = self.validate(kind_key, text)
        if problems:
            raise ValueError("; ".join(problems))
        bundled = BUNDLED_DIR / kind.dir / self._filename(kind, version)
        if bundled.exists() and kind.sections:
            raise FileExistsError(f"'{version}' is a bundled version and cannot be edited - save it under a new name")
        target = self.overlay_dir / kind.dir / self._filename(kind, version)
        if target.exists() and not overwrite and kind.sections:
            raise FileExistsError(f"'{version}' already exists - choose another name or confirm overwriting")
        # free-text kinds (roster, style guide) are single documents: saving under the same name overrides the bundled copy
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        if not kind.sections:
            self._notify()
        return target

    def delete(self, kind_key: str, version: str) -> None:
        kind = KINDS[kind_key]
        target = self.overlay_dir / kind.dir / self._filename(kind, version)
        if not target.exists():
            raise FileNotFoundError("only custom versions can be deleted")
        if version == self.active(kind_key) and kind.sections:
            raise ValueError("that version is active - activate another one first")
        target.unlink()
        if not kind.sections:
            self._notify()   # the bundled roster / style guide is back in force

    # ------------------------------------------------------------------ active versions & context
    def default(self, kind_key: str) -> str:
        return self.env_defaults.get(kind_key) or KINDS[kind_key].default

    def active(self, kind_key: str) -> str:
        return self.config.active.get(kind_key) or self.default(kind_key)

    def set_active(self, kind_key: str, version: str) -> None:
        if self.path(kind_key, version) is None:
            raise FileNotFoundError(f"{kind_key}: no version '{version}'")
        if version == self.default(kind_key):
            self.config.active.pop(kind_key, None)
        else:
            self.config.active[kind_key] = version
        self._save()
        self._notify()

    @property
    def institution_context(self) -> str:
        return self.config.institution_context if self.config.institution_context is not None else self.env_institution_context

    def set_institution_context(self, text: str | None) -> None:
        self.config.institution_context = text.strip() if text and text.strip() and text.strip() != self.env_institution_context else None
        self._save()
        self._notify()

    def on_change(self, fn) -> None:
        self.listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn(self)
            except Exception:  # noqa: BLE001 - one bad listener must not block the others
                import logging
                logging.getLogger(__name__).exception("prompt change listener failed")

    # ------------------------------------------------------------------ what the code asks for
    def prompt(self, kind_key: str, version: str | None = None) -> tuple[str, str]:
        """(system, user) of the active (or given) version."""
        return parse_prompt(self.read(kind_key, version or self.active(kind_key)))

    def text(self, kind_key: str, version: str | None = None) -> str:
        p = self.path(kind_key, version or self.active(kind_key))
        return p.read_text(encoding="utf-8") if p else ""

    def known_people(self) -> str:
        """Roster as the bullet list the analysis prompt expects ('- none listed' when empty)."""
        names = [ln.strip() for ln in self.text("known-people").splitlines() if ln.strip() and not ln.startswith("#")]
        return "\n".join(f"- {n}" for n in names) or "- none listed"

    def describe(self) -> dict[str, Any]:
        out = []
        for k in KINDS.values():
            out.append({"kind": k.key, "label": k.label, "used_by": k.used_by, "placeholders": list(k.placeholders), "sections": k.sections,
                        "default": self.default(k.key), "active": self.active(k.key), "versions": self.versions(k.key), "prefix": k.prefix})
        return {"kinds": out, "institution_context": self.institution_context, "institution_context_default": self.env_institution_context,
                "overlay_dir": str(self.overlay_dir), "config_file": str(self.config_file)}

    def suggest_version(self, kind_key: str) -> str:
        kind = KINDS[kind_key]
        nums = [int(m.group(1)) for v in self.versions(kind_key) if (m := re.search(r"(\d+)$", v["version"]))]
        n = (max(nums) + 1) if nums else 2
        if kind.prefix == "v":
            return f"v{n}"
        if kind.prefix.endswith("_"):
            return f"{kind.prefix}v{n}" if any(v["version"].startswith(kind.prefix + "v") for v in self.versions(kind_key)) else f"{kind.prefix}{n}"
        return f"{kind.prefix}_v{n}"


def _natural(s: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
