#!/usr/bin/env python3
"""Widget profiles (board #263, chunk A): «база = профиль» parameterization.

One profile = one Notion base (Sprint DS + LOG STATISTICS DS) + artifact layout
(release-data.json dir) + webhook target tag. Selected by env WIDGET_PROFILE
(default "sprint" = EXACT current production behavior, backward compatible).

Config file: config/widgets-profiles.json — {"profiles": {<name>: {...}}}.
Entry keys: data_source_id, log_statistics_dsid, artifact_dir ("." | "1c"),
target ("sprint" | "1c"). Built-in defaults below are the same values, so the
file is documentation + override, not a requirement.

Resolution order for every parameter (highest wins):
  1. explicit env override (NOTION_DATA_SOURCE_ID, LOG_STATISTICS_DSID,
     WIDGETS_JSON, QA_WWW_JSON, WIDGETS_JOURNAL, LOG_STATS_RUN_STATE,
     LOG_STATS_JOURNAL, WEBHOOK_RUN_STATE)
  2. config/widgets-profiles.json entry of the selected profile
  3. built-in defaults = legacy pre-profile values

State files (run-state + journals) are PER PROFILE: any profile other than the
default gets "-<profile>" inserted before the extension
(config/log-statistics-run.json -> config/log-statistics-run-1c.json). The
default profile keeps the legacy EXACT paths — strict backward compatibility
for the prod sprint contour. Two profiles never share a run-state file.

data_source_id for the DEFAULT profile resolves through the legacy chain
(NOTION_DATA_SOURCE_ID env > config/notion.json widget_data_source_id >
built-in) exactly as before; non-default profiles resolve env > profile >
built-in (config/notion.json is sprint-specific and must not leak into them).

CLI (for shell scripts):
  python3 widget_profile.py [profile]      -> print resolved context as JSON

Never prints tokens.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PROFILE = "sprint"

WORKSPACE_ROOT = "/home/chuck/.openclaw/workspace"
LEGACY_OUT_JSON = "/var/www/openclaw/widgets/release-data.json"
LEGACY_WS_JSON_REL = Path("widgets") / "release-data.json"
LEGACY_WIDGETS_JOURNAL_REL = Path("config") / "release-widget-journal.jsonl"
LEGACY_LOG_RUN_STATE_REL = Path("config") / "log-statistics-run.json"
LEGACY_LOG_JOURNAL_REL = Path("config") / "log-statistics-journal.jsonl"
LEGACY_WEBHOOK_RUN_STATE_REL = Path("config") / "status-webhook-run.json"
LEGACY_SPRINT_DSID = "2a8e17b6-8482-80b2-87ad-000b68f9d74e"
LEGACY_LOG_DSID = "3dee17b6-8482-80a3-9fc4-000bafe19b46"

# Built-in profile defaults (identical to config/widgets-profiles.json).
BUILTIN_PROFILES = {
    "sprint": {
        "data_source_id": LEGACY_SPRINT_DSID,
        "log_statistics_dsid": LEGACY_LOG_DSID,
        "artifact_dir": ".",
        "target": "sprint",
    },
    "1c": {
        "data_source_id": "f2be17b6-8482-82e2-9892-07777ffeaa90",
        "log_statistics_dsid": "c98e17b6-8482-820e-9f34-072fdef5db94",
        "artifact_dir": "1c",
        "target": "1c",
    },
}


def root() -> Path:
    return Path(os.environ.get("ROOT") or WORKSPACE_ROOT)


def profiles_config_path() -> Path:
    return Path(os.environ.get("WIDGET_PROFILES_CONFIG") or (root() / "config" / "widgets-profiles.json"))


def profile_name() -> str:
    """Selected profile: env WIDGET_PROFILE, default "sprint"."""
    return (os.environ.get("WIDGET_PROFILE") or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE


def profiles_config() -> dict:
    """profiles dict from config/widgets-profiles.json ({} when absent/broken)."""
    path = profiles_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    profiles = data.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def profile_entry(name: str | None = None) -> dict:
    """Built-in defaults overridden by the config-file entry (never raises)."""
    name = name or profile_name()
    entry = dict(BUILTIN_PROFILES.get(name) or {
        "data_source_id": LEGACY_SPRINT_DSID,
        "log_statistics_dsid": LEGACY_LOG_DSID,
        "artifact_dir": ".",
        "target": name,
    })
    file_entry = profiles_config().get(name)
    if isinstance(file_entry, dict):
        for key in ("data_source_id", "log_statistics_dsid", "artifact_dir", "target"):
            val = str(file_entry.get(key) or "").strip()
            if val:
                entry[key] = val
    return entry


def _state_path(legacy_rel: Path, name: str) -> Path:
    """Per-profile run-state/journal path. Default profile = legacy exact path."""
    legacy = root() / legacy_rel
    if name == DEFAULT_PROFILE:
        return legacy
    return legacy.with_name(f"{legacy.stem}-{name}{legacy.suffix}")


def artifact_rel(artifact_dir: str) -> Path:
    """release-data.json relative path inside the artifact root ("." -> root)."""
    d = (artifact_dir or ".").strip().strip("/") or "."
    rel = Path(d) / "release-data.json" if d != "." else Path("release-data.json")
    return rel


def resolve_data_source_id(name: str | None = None, cfg: dict | None = None) -> str:
    """Sprint (task) data source id. See module docstring for the order."""
    env = (os.environ.get("NOTION_DATA_SOURCE_ID") or "").strip()
    if env:
        return env
    name = name or profile_name()
    entry = profile_entry(name)
    if name == DEFAULT_PROFILE:
        # legacy chain preserved exactly (config/notion.json may repoint sprint)
        data = cfg
        if data is None and (root() / "config" / "notion.json").exists():
            try:
                loaded = json.loads((root() / "config" / "notion.json").read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except ValueError:
                data = {}
        if isinstance(data, dict):
            for key in ("widget_data_source_id", "data_source_id"):
                val = str(data.get(key) or "").strip()
                if val:
                    return val
    return str(entry.get("data_source_id") or LEGACY_SPRINT_DSID)


def context(name: str | None = None) -> dict:
    """Fully resolved profile context (env overrides applied). No secrets."""
    name = name or profile_name()
    entry = profile_entry(name)
    artifact = artifact_rel(str(entry.get("artifact_dir") or "."))
    return {
        "profile": name,
        "target": str(entry.get("target") or name),
        "data_source_id": resolve_data_source_id(name),
        "log_dsid": (os.environ.get("LOG_STATISTICS_DSID") or "").strip()
        or str(entry.get("log_statistics_dsid") or LEGACY_LOG_DSID),
        "artifact_dir": str(entry.get("artifact_dir") or "."),
        "ws_json": Path(os.environ.get("WIDGETS_JSON") or (root() / LEGACY_WS_JSON_REL.parent / artifact)),
        "out_json": Path(os.environ.get("QA_WWW_JSON") or (Path(LEGACY_OUT_JSON).parent / artifact)),
        "widgets_journal": Path(os.environ.get("WIDGETS_JOURNAL") or _state_path(LEGACY_WIDGETS_JOURNAL_REL, name)),
        "log_run_state": Path(os.environ.get("LOG_STATS_RUN_STATE") or _state_path(LEGACY_LOG_RUN_STATE_REL, name)),
        "log_journal": Path(os.environ.get("LOG_STATS_JOURNAL") or _state_path(LEGACY_LOG_JOURNAL_REL, name)),
        "webhook_run_state": Path(os.environ.get("WEBHOOK_RUN_STATE") or _state_path(LEGACY_WEBHOOK_RUN_STATE_REL, name)),
        "profiles_config": profiles_config_path(),
    }


if __name__ == "__main__":
    import sys

    sel = sys.argv[1] if len(sys.argv) > 1 else None
    ctx = context(sel)
    out = {k: (str(v) if isinstance(v, Path) else v) for k, v in ctx.items()}
    print(json.dumps(out, ensure_ascii=False))