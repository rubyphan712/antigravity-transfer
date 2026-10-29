#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agy_transfer.py
===============
Export, inspect, import (merge/upsert), and rollback tool for Google Antigravity
project workspaces and conversation history.

Features:
1. Selective migration: Transfer individual projects or conversations with safe upsert
   (never overwrites or deletes existing chats on the target machine).
2. Full backup & restore: Migrate all projects, chats, brain artifacts, and global configs (`config.json`, `mcp_config.json`).
3. Smart path & Protobuf remapping: Automatically rewrites paths and recalculates binary
   wire-format varint lengths when drive letters, folders, or Windows usernames differ (`C:\\Users\\OldUser` -> `C:\\Users\\NewUser`).
4. 100% Python Standard Library (Zero external dependencies).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from proto_remap import (
    build_path_replacement_pairs,
    parse_agyhub_summaries_pb,
    path_to_antigravity_uris,
    remap_protobuf_blob,
    remap_text_strings,
    serialize_agyhub_summaries_pb,
    uri_to_local_path,
)

# Ensure UTF-8 console output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_EXCLUDE_DIRS: Set[str] = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".next",
    ".nuxt",
    "dist",
    "build",
    "target",
    ".gradle",
    ".idea",
}

SUMMARIES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS `conversation_summaries` (
    `conversation_id` text,
    `title` text NOT NULL DEFAULT "",
    `preview` text NOT NULL DEFAULT "",
    `step_count` integer NOT NULL DEFAULT 0,
    `last_modified_time` datetime NOT NULL,
    `workspace_uris` text NOT NULL,
    `status` text NOT NULL DEFAULT "",
    `source` text NOT NULL DEFAULT "",
    `project_id` text NOT NULL DEFAULT "",
    `agent_name` text NOT NULL DEFAULT "",
    `parent_conversation_id` text NOT NULL DEFAULT "",
    `nesting_depth` integer NOT NULL DEFAULT 0,
    `battle_id` text NOT NULL DEFAULT "",
    `winning_conversation_id` text NOT NULL DEFAULT "",
    `not_fully_idle` numeric NOT NULL DEFAULT false,
    `killed` numeric NOT NULL DEFAULT false,
    `last_user_input_time` datetime NOT NULL,
    `last_user_input_step_index` integer NOT NULL DEFAULT -1,
    `app_data_dir` text NOT NULL DEFAULT "",
    `raw_summary` blob,
    `group_id` text NOT NULL DEFAULT "",
    PRIMARY KEY (`conversation_id`)
);
CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_user_input_time`
    ON `conversation_summaries`(`last_user_input_time`);
CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_modified_time`
    ON `conversation_summaries`(`last_modified_time`);
CREATE TABLE IF NOT EXISTS search_schema_version (
    version INTEGER PRIMARY KEY
);
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    conversation_id UNINDEXED,
    chunk_index UNINDEXED,
    chunk_first_step UNINDEXED,
    chunk_last_step UNINDEXED,
    last_modified_time UNINDEXED,
    step_count UNINDEXED,
    last_step_created_at UNINDEXED,
    archived UNINDEXED,
    user_step_byte_offsets UNINDEXED,
    agent_step_byte_offsets UNINDEXED,
    title,
    workspace,
    project,
    user_body,
    agent_body,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '1 2 3'
);
"""

SUMMARY_COLUMNS: List[str] = [
    "conversation_id",
    "title",
    "preview",
    "step_count",
    "last_modified_time",
    "workspace_uris",
    "status",
    "source",
    "project_id",
    "agent_name",
    "parent_conversation_id",
    "nesting_depth",
    "battle_id",
    "winning_conversation_id",
    "not_fully_idle",
    "killed",
    "last_user_input_time",
    "last_user_input_step_index",
    "app_data_dir",
    "raw_summary",
    "group_id",
]

FTS_COLUMNS: List[str] = [
    "conversation_id",
    "chunk_index",
    "chunk_first_step",
    "chunk_last_step",
    "last_modified_time",
    "step_count",
    "last_step_created_at",
    "archived",
    "user_step_byte_offsets",
    "agent_step_byte_offsets",
    "title",
    "workspace",
    "project",
    "user_body",
    "agent_body",
]


class AntigravityPaths:
    """Centralized path manager for Antigravity configuration and data directories."""

    def __init__(
        self,
        gemini_dir: Optional[Path] = None,
        appdata_dir: Optional[Path] = None,
        user_home: Optional[Path] = None,
    ) -> None:
        self.user_home = Path(user_home) if user_home else Path.home()
        self.gemini_dir = Path(gemini_dir) if gemini_dir else (self.user_home / ".gemini")
        if appdata_dir:
            self.appdata_agy_dir = Path(appdata_dir)
        else:
            env_appdata = os.environ.get("APPDATA")
            if env_appdata:
                self.appdata_agy_dir = Path(env_appdata) / "Antigravity"
            else:
                self.appdata_agy_dir = self.user_home / "AppData" / "Roaming" / "Antigravity"

    @property
    def agy_dir(self) -> Path:
        return self.gemini_dir / "antigravity"

    @property
    def config_dir(self) -> Path:
        return self.gemini_dir / "config"

    @property
    def projects_dir(self) -> Path:
        return self.config_dir / "projects"

    @property
    def conversations_dir(self) -> Path:
        return self.agy_dir / "conversations"

    @property
    def brain_dir(self) -> Path:
        return self.agy_dir / "brain"

    @property
    def annotations_dir(self) -> Path:
        return self.agy_dir / "annotations"

    @property
    def summaries_db(self) -> Path:
        return self.agy_dir / "conversation_summaries.db"

    @property
    def summaries_pb(self) -> Path:
        return self.agy_dir / "agyhub_summaries_proto.pb"

    @property
    def state_pbtxt(self) -> Path:
        return self.agy_dir / "antigravity_state.pbtxt"

    @property
    def backups_dir(self) -> Path:
        return self.agy_dir / "backups"

    @property
    def app_storage_json(self) -> Path:
        return self.appdata_agy_dir / "app_storage.json"


def is_antigravity_running() -> bool:
    """Checks whether the Antigravity.exe process is actively running on Windows."""
    if sys.platform != "win32":
        return False
    try:
        res = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Antigravity.exe", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return "antigravity.exe" in res.stdout.lower()
    except Exception:
        return False


def _serialize_row_bytes(row_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Converts any binary `bytes` in an SQLite row dictionary into a JSON-safe Base64 wrapper."""
    out: Dict[str, Any] = {}
    for k, v in row_dict.items():
        if isinstance(v, (bytes, bytearray)):
            out[k] = {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
        else:
            out[k] = v
    return out


def _deserialize_row_bytes(row_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Restores `{"__b64__": "..."}` dictionaries back into raw `bytes` for SQLite queries."""
    out: Dict[str, Any] = {}
    for k, v in row_dict.items():
        if isinstance(v, dict) and set(v.keys()) == {"__b64__"} and isinstance(v["__b64__"], str):
            out[k] = base64.b64decode(v["__b64__"])
        else:
            out[k] = v
    return out


def discover_local_state(paths: AntigravityPaths) -> Dict[str, Any]:
    """
    Scans existing Projects and Conversations in `.gemini`.
    Returns a dictionary containing:
      - `projects`: dict `{project_id: {id, name, folder_uris, local_paths, json_path, conversations: [...]}}`
      - `conversations`: dict `{conv_id: summary_dict}`
      - `unassigned_conversations`: list of conversations not tied to any declared project
    """
    projects: Dict[str, Dict[str, Any]] = {}
    uri_to_proj_id: Dict[str, str] = {}

    if paths.projects_dir.exists():
        for pfile in sorted(paths.projects_dir.glob("*.json")):
            try:
                pdata = json.loads(pfile.read_text(encoding="utf-8"))
                pid = pdata.get("id") or pfile.stem
                resources = pdata.get("projectResources", {}).get("resources", [])
                folder_uris = [r.get("folderUri", "") for r in resources if r.get("folderUri")]
                local_paths = [uri_to_local_path(u) for u in folder_uris]
                projects[pid] = {
                    "id": pid,
                    "name": pdata.get("name", pid),
                    "folder_uris": folder_uris,
                    "local_paths": local_paths,
                    "raw_json": pdata,
                    "json_path": str(pfile),
                    "is_virtual": False,
                    "conversations": [],
                }
                for u in folder_uris:
                    uri_to_proj_id[u.lower()] = pid
                    enc_u, plain_u = path_to_antigravity_uris(uri_to_local_path(u))
                    uri_to_proj_id[enc_u.lower()] = pid
                    uri_to_proj_id[plain_u.lower()] = pid
            except Exception:
                continue

    conversations: Dict[str, Dict[str, Any]] = {}
    unassigned: List[Dict[str, Any]] = []

    if paths.summaries_db.exists():
        conn = sqlite3.connect(f"file:{paths.summaries_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            rows = cur.execute(
                "SELECT * FROM conversation_summaries ORDER BY last_modified_time DESC"
            ).fetchall()
            for r in rows:
                d = dict(r)
                cid = d["conversation_id"]
                raw_bytes = d.get("raw_summary")
                d["raw_summary_b64"] = (
                    base64.b64encode(raw_bytes).decode("ascii")
                    if isinstance(raw_bytes, (bytes, bytearray))
                    else None
                )
                del d["raw_summary"]
                d = _serialize_row_bytes(d)

                try:
                    ws_uris = json.loads(d.get("workspace_uris") or "[]")
                except Exception:
                    ws_uris = []
                d["workspace_uris_list"] = ws_uris
                d["workspace_local_paths"] = [uri_to_local_path(u) for u in ws_uris if u]

                conversations[cid] = d

                pid = d.get("project_id") or ""
                if pid not in projects:
                    # Match by workspace_uris if project_id is empty or outside-of-project
                    for u in ws_uris:
                        matched = uri_to_proj_id.get(u.lower())
                        if not matched:
                            enc_u, plain_u = path_to_antigravity_uris(uri_to_local_path(u))
                            matched = uri_to_proj_id.get(enc_u.lower()) or uri_to_proj_id.get(
                                plain_u.lower()
                            )
                        if matched:
                            pid = matched
                            break

                # If still not in declared projects but has workspace folder(s) attached,
                # create a virtual workspace folder entry so the user can select it by folder.
                if (not pid or pid not in projects) and d["workspace_local_paths"]:
                    primary_local = d["workspace_local_paths"][0]
                    folder_name = Path(primary_local.replace("\\", "/")).name or primary_local
                    virt_hash = hashlib.sha1(primary_local.lower().encode("utf-8")).hexdigest()[:8]
                    pid = f"ws-{virt_hash}"
                    if pid not in projects:
                        projects[pid] = {
                            "id": pid,
                            "name": folder_name,
                            "folder_uris": list(ws_uris),
                            "local_paths": list(d["workspace_local_paths"]),
                            "raw_json": None,
                            "json_path": None,
                            "is_virtual": True,
                            "conversations": [],
                        }
                        for u in ws_uris:
                            uri_to_proj_id[u.lower()] = pid
                            enc_u, plain_u = path_to_antigravity_uris(uri_to_local_path(u))
                            uri_to_proj_id[enc_u.lower()] = pid
                            uri_to_proj_id[plain_u.lower()] = pid

                if pid and pid in projects:
                    d["resolved_project_id"] = pid
                    projects[pid]["conversations"].append(d)
                else:
                    d["resolved_project_id"] = ""
                    unassigned.append(d)
        finally:
            conn.close()

    return {
        "projects": projects,
        "conversations": conversations,
        "unassigned_conversations": unassigned,
    }


def _backup_sqlite_file(src_db: Path, dst_db: Path) -> None:
    """
    Safely backs up SQLite `src_db` to `dst_db` using `sqlite3.Connection.backup()`.
    Captures WAL journal data even while Antigravity is actively running.
    """
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if dst_db.exists():
        dst_db.unlink()
    src_conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst_db))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def export_package(
    paths: AntigravityPaths,
    output_zip: Path,
    mode: str = "selective",
    project_ids: Optional[Sequence[str]] = None,
    conversation_ids: Optional[Sequence[str]] = None,
    include_workspace_files: bool = True,
    exclude_heavy_dirs: bool = True,
) -> Dict[str, Any]:
    """
    Bundles Antigravity data into a `.zip` archive.
    - `mode`: `"selective"` (chosen projects/conversations) or `"full"` (entire state).
    - `project_ids`: list of project IDs, names, or folder paths to export.
    - `conversation_ids`: list of individual conversation IDs to export.
    - `include_workspace_files`: whether to bundle workspace project files into the archive.
    - `exclude_heavy_dirs`: whether to skip `node_modules`, `.venv`, `__pycache__`, etc.
    Returns the `manifest` dictionary describing the archive contents.
    """
    state = discover_local_state(paths)
    all_projects = state["projects"]
    all_convs = state["conversations"]

    selected_proj_ids: Set[str] = set()
    selected_conv_ids: Set[str] = set()

    if mode == "full":
        selected_proj_ids.update(all_projects.keys())
        selected_conv_ids.update(all_convs.keys())
        # Scan for any additional .db files in conversations/ not yet in summaries
        if paths.conversations_dir.exists():
            for db_file in paths.conversations_dir.glob("*.db"):
                selected_conv_ids.add(db_file.stem)
    else:
        # Match projects by ID, name, or path
        if project_ids:
            queries = [q.strip().lower().rstrip("\\/") for q in project_ids if q.strip()]
            for pid, pinfo in all_projects.items():
                p_name = pinfo["name"].lower()
                p_paths = [lp.lower().rstrip("\\/") for lp in pinfo["local_paths"]]
                if (
                    pid.lower() in queries
                    or p_name in queries
                    or any(q in p_paths for q in queries)
                ):
                    selected_proj_ids.add(pid)
                    for c in pinfo["conversations"]:
                        selected_conv_ids.add(c["conversation_id"])

        if conversation_ids:
            for cid_q in conversation_ids:
                cid_clean = cid_q.strip()
                if cid_clean in all_convs:
                    selected_conv_ids.add(cid_clean)
                    c_item = all_convs[cid_clean]
                    pid = c_item.get("resolved_project_id") or c_item.get("project_id")
                    if pid and pid in all_projects:
                        selected_proj_ids.add(pid)
                else:
                    # Allow prefix matching (e.g. 8-char hex prefix)
                    for full_cid, cinfo in all_convs.items():
                        if full_cid.startswith(cid_clean):
                            selected_conv_ids.add(full_cid)
                            pid = cinfo.get("resolved_project_id") or cinfo.get("project_id")
                            if pid and pid in all_projects:
                                selected_proj_ids.add(pid)

    output_zip = Path(output_zip).resolve()
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    exclude_set = set(DEFAULT_EXCLUDE_DIRS) if exclude_heavy_dirs else set()

    # Extract FTS5 data for selected conversations
    exported_fts: Dict[str, List[Dict[str, Any]]] = {}
    if paths.summaries_db.exists() and selected_conv_ids:
        conn = sqlite3.connect(f"file:{paths.summaries_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            placeholders = ",".join(["?"] * len(selected_conv_ids))
            fts_query = f"SELECT * FROM conversations_fts WHERE conversation_id IN ({placeholders})"
            for row in conn.execute(fts_query, tuple(selected_conv_ids)):
                rdict = _serialize_row_bytes(dict(row))
                cid = rdict["conversation_id"]
                exported_fts.setdefault(cid, []).append(rdict)
        except Exception:
            pass
        finally:
            conn.close()

    exported_projects_meta: List[Dict[str, Any]] = []
    exported_convs_meta: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="agy_export_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # 1. Package selected projects and workspace folder files
            for pid in sorted(selected_proj_ids):
                pinfo = all_projects[pid]
                json_path_str = pinfo.get("json_path")
                p_json_path = Path(json_path_str) if json_path_str else None
                if p_json_path and p_json_path.is_file():
                    zf.write(p_json_path, f"gemini/config/projects/{pid}.json")

                folder_entries = []
                for idx, (f_uri, l_path) in enumerate(
                    zip(pinfo["folder_uris"], pinfo["local_paths"])
                ):
                    f_path = Path(l_path) if l_path else None
                    files_copied = 0
                    zip_folder_key = f"workspaces/{pid}_{idx}"
                    if (
                        include_workspace_files
                        and f_path
                        and f_path.exists()
                        and f_path.is_dir()
                    ):
                        files_copied = _copy_workspace_folder_until_done(
                            zf, f_path, zip_folder_key, exclude_set, skip_file=output_zip
                        )
                    folder_entries.append(
                        {
                            "index": idx,
                            "original_uri": f_uri,
                            "original_local_path": l_path,
                            "zip_prefix": zip_folder_key if files_copied > 0 else None,
                            "files_bundled": files_copied,
                        }
                    )

                exported_projects_meta.append(
                    {
                        "id": pid,
                        "name": pinfo["name"],
                        "folders": folder_entries,
                        "conversation_ids": [c["conversation_id"] for c in pinfo["conversations"]],
                    }
                )

            # 2. Package conversations (.db, brain/, annotations/)
            exported_summaries_payload: Dict[str, Any] = {}
            for cid in sorted(selected_conv_ids):
                c_summary = all_convs.get(cid)
                if c_summary:
                    exported_summaries_payload[cid] = {
                        "summary": c_summary,
                        "fts_rows": exported_fts.get(cid, []),
                    }

                # Online backup of SQLite conversation DB
                src_conv_db = paths.conversations_dir / f"{cid}.db"
                has_db = False
                if src_conv_db.exists():
                    tmp_conv_db = tmp_dir / f"{cid}.db"
                    _backup_sqlite_file(src_conv_db, tmp_conv_db)
                    zf.write(tmp_conv_db, f"gemini/antigravity/conversations/{cid}.db")
                    has_db = True

                # Bundle brain/<cid>/ directory
                src_brain = paths.brain_dir / cid
                brain_files_count = 0
                if src_brain.exists() and src_brain.is_dir():
                    for root, _, files in os.walk(src_brain):
                        root_p = Path(root)
                        for fn in files:
                            full_f = root_p / fn
                            rel_f = full_f.relative_to(src_brain).as_posix()
                            zf.write(full_f, f"gemini/antigravity/brain/{cid}/{rel_f}")
                            brain_files_count += 1

                # Bundle annotations/<cid>.pbtxt file
                src_ann = paths.annotations_dir / f"{cid}.pbtxt"
                has_ann = False
                if src_ann.exists():
                    zf.write(src_ann, f"gemini/antigravity/annotations/{cid}.pbtxt")
                    has_ann = True

                exported_convs_meta.append(
                    {
                        "conversation_id": cid,
                        "title": (c_summary or {}).get("title", cid),
                        "project_id": (c_summary or {}).get("project_id", ""),
                        "workspace_local_paths": (c_summary or {}).get(
                            "workspace_local_paths", []
                        ),
                        "step_count": (c_summary or {}).get("step_count", 0),
                        "last_modified_time": (c_summary or {}).get(
                            "last_modified_time", ""
                        ),
                        "has_db": has_db,
                        "brain_files_count": brain_files_count,
                        "has_annotation": has_ann,
                    }
                )

            # Save extracted summaries + FTS payload
            zf.writestr(
                "exported_summaries.json",
                json.dumps(exported_summaries_payload, indent=2, ensure_ascii=False),
            )

            # 3. If Full Backup: include global configs (.gemini/config)
            global_config_files: List[str] = []
            if mode == "full" and paths.config_dir.exists():
                for root, dirs, files in os.walk(paths.config_dir):
                    dirs[:] = [d for d in dirs if d not in ("node_modules", "projects")]
                    root_p = Path(root)
                    for fn in files:
                        full_f = root_p / fn
                        rel_f = full_f.relative_to(paths.config_dir).as_posix()
                        arc = f"gemini/config/{rel_f}"
                        zf.write(full_f, arc)
                        global_config_files.append(rel_f)

            manifest = {
                "format_version": "1.0",
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "mode": mode,
                "source_machine": {
                    "hostname": platform.node(),
                    "os": platform.system(),
                    "user_home": str(paths.user_home),
                    "gemini_dir": str(paths.gemini_dir),
                },
                "include_workspace_files": include_workspace_files,
                "exclude_heavy_dirs": exclude_heavy_dirs,
                "projects": exported_projects_meta,
                "conversations": exported_convs_meta,
                "global_config_files": global_config_files,
            }
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False),
            )

    return manifest


def _copy_workspace_folder_until_done(
    zf: zipfile.ZipFile,
    folder_path: Path,
    zip_prefix: str,
    exclude_dirs: Set[str],
    skip_file: Optional[Path] = None,
) -> int:
    """Helper to zip workspace files while safely skipping the output zip if located inside."""
    count = 0
    skip_resolved = skip_file.resolve() if skip_file else None
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        root_path = Path(root)
        for file_name in files:
            full_path = (root_path / file_name).resolve()
            if skip_resolved and full_path == skip_resolved:
                continue
            rel_path = full_path.relative_to(folder_path.resolve()).as_posix()
            arcname = f"{zip_prefix}/{rel_path}"
            try:
                zf.write(full_path, arcname)
                count += 1
            except (PermissionError, FileNotFoundError):
                continue
    return count


def inspect_package(zip_path: Path) -> Dict[str, Any]:
    """Reads and returns `manifest.json` from the `.zip` archive."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        raw = zf.read("manifest.json").decode("utf-8")
        return json.loads(raw)


def create_pre_import_backup(paths: AntigravityPaths) -> Path:
    """
    Creates an automated snapshot of the target machine's current state before import
    in `~/.gemini/antigravity/backups/pre_import_<timestamp>/`.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = paths.backups_dir / f"pre_import_{stamp}"
    bdir.mkdir(parents=True, exist_ok=True)

    if paths.summaries_db.exists():
        _backup_sqlite_file(paths.summaries_db, bdir / "conversation_summaries.db")
    if paths.summaries_pb.exists():
        shutil.copy2(paths.summaries_pb, bdir / "agyhub_summaries_proto.pb")
    if paths.projects_dir.exists():
        shutil.copytree(paths.projects_dir, bdir / "projects", dirs_exist_ok=True)
    if paths.app_storage_json.exists():
        shutil.copy2(paths.app_storage_json, bdir / "app_storage.json")

    return bdir


def rollback_backup(paths: AntigravityPaths, backup_folder: Path) -> None:
    """Rolls back conversation index and project configs from a `pre_import_*` directory."""
    if not backup_folder.exists():
        raise FileNotFoundError(f"Backup directory not found: {backup_folder}")

    b_db = backup_folder / "conversation_summaries.db"
    if b_db.exists():
        paths.agy_dir.mkdir(parents=True, exist_ok=True)
        for wal_ext in ("-wal", "-shm"):
            wal_f = paths.agy_dir / f"conversation_summaries.db{wal_ext}"
            if wal_f.exists():
                wal_f.unlink()
        shutil.copy2(b_db, paths.summaries_db)

    b_pb = backup_folder / "agyhub_summaries_proto.pb"
    if b_pb.exists():
        shutil.copy2(b_pb, paths.summaries_pb)

    b_proj = backup_folder / "projects"
    if b_proj.exists():
        paths.projects_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(b_proj, paths.projects_dir, dirs_exist_ok=True)

    b_app = backup_folder / "app_storage.json"
    if b_app.exists() and paths.app_storage_json.parent.exists():
        shutil.copy2(b_app, paths.app_storage_json)


def _remap_conversation_db(db_path: Path, str_pairs: Sequence[Tuple[str, str]]) -> None:
    """
    Updates paths inside `<conversation_id>.db`:
    - `trajectory_metadata_blob` table (`data` column)
    - `steps` table (`metadata`, `permissions`, `task_details`, `render_info`, `step_payload` columns)
    """
    if not str_pairs or not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        # 1. trajectory_metadata_blob table
        rows = cur.execute("SELECT id, data FROM trajectory_metadata_blob").fetchall()
        for row_id, blob in rows:
            if isinstance(blob, (bytes, bytearray)):
                new_blob = remap_protobuf_blob(bytes(blob), str_pairs)
                if new_blob != blob:
                    cur.execute(
                        "UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?",
                        (new_blob, row_id),
                    )

        # 2. steps table
        step_cols = ["metadata", "permissions", "task_details", "render_info", "step_payload"]
        step_rows = cur.execute(
            f"SELECT idx, {', '.join(step_cols)} FROM steps"
        ).fetchall()
        for srow in step_rows:
            idx = srow[0]
            updates = {}
            for col_name, col_val in zip(step_cols, srow[1:]):
                if isinstance(col_val, (bytes, bytearray)) and col_val:
                    new_val = remap_protobuf_blob(bytes(col_val), str_pairs)
                    if new_val != col_val:
                        updates[col_name] = new_val
            if updates:
                set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
                params = list(updates.values()) + [idx]
                cur.execute(f"UPDATE steps SET {set_clause} WHERE idx = ?", params)

        conn.commit()
    finally:
        conn.close()


def import_package(
    paths: AntigravityPaths,
    zip_path: Path,
    folder_overrides: Optional[Dict[str, str]] = None,
    extract_workspaces: bool = True,
    check_running_process: bool = True,
) -> Dict[str, Any]:
    """
    Imports or merges/upserts a `.zip` archive into the target Antigravity setup.

    - `folder_overrides`: mapping dict `{old_local_path: new_local_path}` or `{project_id: new_local_path}`.
      If not provided, preserves original source paths.
    - `extract_workspaces`: whether to extract bundled workspace files to the target folder.
    - `check_running_process`: blocks execution if `Antigravity.exe` is currently running.
    Returns a detailed import summary dictionary.
    """
    if check_running_process and is_antigravity_running():
        raise RuntimeError(
            "Antigravity.exe is currently running! Please completely close Antigravity "
            "before importing to prevent SQLite database lock and WAL journal conflicts."
        )

    zip_path = Path(zip_path).resolve()
    if not zip_path.exists():
        raise FileNotFoundError(f"Package file not found: {zip_path}")

    folder_overrides = folder_overrides or {}
    norm_overrides: Dict[str, str] = {
        k.strip().rstrip("\\/").lower(): v.strip().rstrip("\\/")
        for k, v in folder_overrides.items()
        if k.strip() and v.strip()
    }

    # Create safety backup before modifying local state
    backup_dir = create_pre_import_backup(paths)

    paths.agy_dir.mkdir(parents=True, exist_ok=True)
    paths.conversations_dir.mkdir(parents=True, exist_ok=True)
    paths.brain_dir.mkdir(parents=True, exist_ok=True)
    paths.annotations_dir.mkdir(parents=True, exist_ok=True)
    paths.projects_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        exported_summaries: Dict[str, Any] = json.loads(
            zf.read("exported_summaries.json").decode("utf-8")
        )

        src_user_home = manifest.get("source_machine", {}).get("user_home", "")
        dst_user_home = str(paths.user_home)

        # Resolve project folder mappings (old_local_path -> target_local_path)
        folder_mappings: List[Tuple[str, str]] = []
        resolved_project_targets: Dict[str, List[str]] = {}
        workspace_name_map: Dict[str, str] = {}

        for pmeta in manifest.get("projects", []):
            pid = pmeta["id"]
            p_targets: List[str] = []
            for f_entry in pmeta.get("folders", []):
                orig_path = f_entry.get("original_local_path", "").strip().rstrip("\\/")
                if not orig_path:
                    continue
                target_path = (
                    norm_overrides.get(pid.lower())
                    or norm_overrides.get(orig_path.lower())
                    or orig_path
                )
                folder_mappings.append((orig_path, target_path))
                p_targets.append(target_path)

                old_folder_name = Path(orig_path.replace("\\", "/")).name
                new_folder_name = Path(target_path.replace("\\", "/")).name
                if old_folder_name and new_folder_name and old_folder_name != new_folder_name:
                    workspace_name_map[old_folder_name] = new_folder_name

                # Extract workspace code files if bundled in the package
                zip_prefix = f_entry.get("zip_prefix")
                if extract_workspaces and zip_prefix:
                    target_dir = Path(target_path)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    prefix_slash = zip_prefix.rstrip("/") + "/"
                    for member in zf.namelist():
                        if member.startswith(prefix_slash) and not member.endswith("/"):
                            rel_part = member[len(prefix_slash) :]
                            dest_file = target_dir / rel_part
                            dest_file.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as src_f, open(dest_file, "wb") as dst_f:
                                shutil.copyfileobj(src_f, dst_f)

            resolved_project_targets[pid] = p_targets

        # Add mappings for unassigned conversations with workspace paths
        for cmeta in manifest.get("conversations", []):
            for orig_p in cmeta.get("workspace_local_paths", []):
                orig_clean = orig_p.strip().rstrip("\\/")
                if orig_clean and orig_clean.lower() in norm_overrides:
                    target_p = norm_overrides[orig_clean.lower()]
                    if (orig_clean, target_p) not in folder_mappings:
                        folder_mappings.append((orig_clean, target_p))

        # Generate comprehensive replacement pairs (URI encoded, URI plain, OS paths, .gemini path)
        str_pairs = build_path_replacement_pairs(
            folder_mappings=folder_mappings,
            old_user_home=src_user_home,
            new_user_home=dst_user_home,
        )

        # 1. Restore and remap paths in `gemini/config/projects/*.json`
        imported_proj_ids: List[str] = []
        for member in zf.namelist():
            if member.startswith("gemini/config/projects/") and member.endswith(".json"):
                raw_json_text = zf.read(member).decode("utf-8")
                new_json_text = remap_text_strings(raw_json_text, str_pairs)
                proj_obj = json.loads(new_json_text)
                pid = proj_obj.get("id") or Path(member).stem
                dest_json = paths.projects_dir / f"{pid}.json"
                dest_json.write_text(
                    json.dumps(proj_obj, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                imported_proj_ids.append(pid)

        # 2. Restore global configs if this is a Full Backup package
        for g_rel in manifest.get("global_config_files", []):
            arc = f"gemini/config/{g_rel}"
            if arc in zf.namelist():
                dest_g = paths.config_dir / g_rel
                dest_g.parent.mkdir(parents=True, exist_ok=True)
                content = zf.read(arc)
                if g_rel.endswith((".json", ".md", ".txt", ".yaml", ".yml")):
                    try:
                        txt = content.decode("utf-8")
                        txt = remap_text_strings(txt, str_pairs)
                        dest_g.write_text(txt, encoding="utf-8")
                        continue
                    except Exception:
                        pass
                dest_g.write_bytes(content)

        # 3. Extract and remap `<conv_id>.db`, `brain/<conv_id>/`, `annotations/<conv_id>.pbtxt`
        imported_conv_ids: List[str] = []
        for cmeta in manifest.get("conversations", []):
            cid = cmeta["conversation_id"]
            imported_conv_ids.append(cid)

            conv_arc = f"gemini/antigravity/conversations/{cid}.db"
            if conv_arc in zf.namelist():
                dest_db = paths.conversations_dir / f"{cid}.db"
                for ext in ("-wal", "-shm"):
                    w = paths.conversations_dir / f"{cid}.db{ext}"
                    if w.exists():
                        w.unlink()
                dest_db.write_bytes(zf.read(conv_arc))
                _remap_conversation_db(dest_db, str_pairs)

            ann_arc = f"gemini/antigravity/annotations/{cid}.pbtxt"
            if ann_arc in zf.namelist():
                dest_ann = paths.annotations_dir / f"{cid}.pbtxt"
                ann_bytes = zf.read(ann_arc)
                try:
                    ann_text = ann_bytes.decode("utf-8")
                    ann_text = remap_text_strings(ann_text, str_pairs)
                    dest_ann.write_text(ann_text, encoding="utf-8")
                except Exception:
                    dest_ann.write_bytes(ann_bytes)

        # Extract all brain files
        brain_prefix = "gemini/antigravity/brain/"
        text_exts = {".md", ".json", ".jsonl", ".txt", ".html", ".py", ".ps1", ".bat"}
        for member in zf.namelist():
            if member.startswith(brain_prefix) and not member.endswith("/"):
                rel_b = member[len(brain_prefix) :]
                dest_b = paths.brain_dir / rel_b
                dest_b.parent.mkdir(parents=True, exist_ok=True)
                raw_b = zf.read(member)
                if str_pairs and any(rel_b.endswith(ext) for ext in text_exts):
                    try:
                        txt_b = raw_b.decode("utf-8")
                        new_txt_b = remap_text_strings(txt_b, str_pairs)
                        dest_b.write_text(new_txt_b, encoding="utf-8")
                        continue
                    except Exception:
                        pass
                dest_b.write_bytes(raw_b)

        # 4. Upsert into `conversation_summaries.db` and `agyhub_summaries_proto.pb`
        pb_entries: Dict[str, bytes] = {}
        if paths.summaries_pb.exists():
            try:
                pb_entries = parse_agyhub_summaries_pb(paths.summaries_pb.read_bytes())
            except Exception:
                pb_entries = {}

        conn = sqlite3.connect(str(paths.summaries_db))
        try:
            conn.executescript(SUMMARIES_SCHEMA_SQL)
            cur = conn.cursor()
            cur.execute("INSERT OR IGNORE INTO search_schema_version (version) VALUES (2)")

            col_names_sql = ", ".join([f"`{c}`" for c in SUMMARY_COLUMNS])
            placeholders_sql = ", ".join(["?"] * len(SUMMARY_COLUMNS))
            upsert_sql = (
                f"INSERT OR REPLACE INTO `conversation_summaries` ({col_names_sql}) "
                f"VALUES ({placeholders_sql})"
            )

            fts_cols_sql = ", ".join(FTS_COLUMNS)
            fts_placeholders = ", ".join(["?"] * len(FTS_COLUMNS))
            fts_insert_sql = (
                f"INSERT INTO conversations_fts ({fts_cols_sql}) VALUES ({fts_placeholders})"
            )

            for cid, payload in exported_summaries.items():
                s_dict = _deserialize_row_bytes(dict(payload["summary"]))
                fts_rows = payload.get("fts_rows", [])

                # Remap `workspace_uris`
                ws_uris_str = s_dict.get("workspace_uris", "[]")
                if isinstance(ws_uris_str, str):
                    s_dict["workspace_uris"] = remap_text_strings(ws_uris_str, str_pairs)

                # Remap `raw_summary` (Protobuf binary blob)
                raw_b64 = s_dict.get("raw_summary_b64")
                raw_bytes: Optional[bytes] = (
                    base64.b64decode(raw_b64) if raw_b64 else None
                )
                if raw_bytes and str_pairs:
                    raw_bytes = remap_protobuf_blob(raw_bytes, str_pairs)
                s_dict["raw_summary"] = raw_bytes

                if raw_bytes is not None:
                    pb_entries[cid] = raw_bytes

                row_values = [s_dict.get(col) for col in SUMMARY_COLUMNS]
                cur.execute(upsert_sql, row_values)

                # Update FTS5 search index for this conversation
                cur.execute(
                    "DELETE FROM conversations_fts WHERE conversation_id = ?", (cid,)
                )
                for frow in fts_rows:
                    fcopy = _deserialize_row_bytes(dict(frow))
                    if workspace_name_map:
                        ws_val = fcopy.get("workspace", "")
                        pr_val = fcopy.get("project", "")
                        if ws_val in workspace_name_map:
                            fcopy["workspace"] = workspace_name_map[ws_val]
                        if pr_val in workspace_name_map:
                            fcopy["project"] = workspace_name_map[pr_val]
                    cur.execute(
                        fts_insert_sql, [fcopy.get(fc, "") for fc in FTS_COLUMNS]
                    )

            conn.commit()
        finally:
            conn.close()

        # Write merged `agyhub_summaries_proto.pb`
        if pb_entries:
            paths.summaries_pb.write_bytes(serialize_agyhub_summaries_pb(pb_entries))

        # 5. Optionally update `projectsOrder` in `%APPDATA%\Antigravity\app_storage.json`
        if paths.app_storage_json.exists() and imported_proj_ids:
            try:
                app_data = json.loads(paths.app_storage_json.read_text(encoding="utf-8"))
                order_raw = app_data.get("projectsOrder")
                order_list = json.loads(order_raw) if isinstance(order_raw, str) else []
                if isinstance(order_list, list):
                    for pid in imported_proj_ids:
                        if pid not in order_list:
                            order_list.append(pid)
                    app_data["projectsOrder"] = json.dumps(order_list)
                    paths.app_storage_json.write_text(
                        json.dumps(app_data, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
            except Exception:
                pass

    return {
        "backup_dir": str(backup_dir),
        "imported_projects": imported_proj_ids,
        "imported_conversations": imported_conv_ids,
        "resolved_project_targets": resolved_project_targets,
        "remapped_pairs_count": len(str_pairs),
    }


# ==============================================================================
# CLI and Interactive Menu Interface
# ==============================================================================


def print_local_inventory(paths: AntigravityPaths) -> Dict[str, Any]:
    """Prints current local Projects and Conversations to the console."""
    state = discover_local_state(paths)
    projects = state["projects"]
    unassigned = state["unassigned_conversations"]

    print("\n" + "=" * 78)
    print(f" CURRENT ANTIGRAVITY DATA ON THIS MACHINE ({paths.gemini_dir})")
    print("=" * 78)

    if unassigned:
        print(f"\n[STANDALONE CONVERSATIONS (NO FOLDER ATTACHED) ({len(unassigned)})]:")
        for idx, c in enumerate(unassigned, start=1):
            title = c.get("title") or "(Untitled)"
            print(
                f"  {idx:2d}. [{c['conversation_id'][:8]}...] {title} "
                f"({c.get('step_count', 0)} steps)"
            )

    proj_list = list(projects.values())
    if not proj_list:
        print("\n  (No projects or workspace folders found)")
    else:
        print(f"\n[PROJECTS & WORKSPACE FOLDERS ({len(proj_list)})]:")
        for idx, p in enumerate(proj_list, start=1):
            paths_str = ", ".join(p["local_paths"]) or "(No local path)"
            convs = p["conversations"]
            tag = " [Workspace Folder]" if p.get("is_virtual") else ""
            print(f"  {idx:2d}. [{p['name']}]{tag}  -->  Folder: {paths_str}")
            print(f"      ID: {p['id']} | Chats: {len(convs)} conversation(s)")
            for c in convs[:5]:
                title = c.get("title") or "(Untitled)"
                steps = c.get("step_count", 0)
                cid_short = c["conversation_id"][:8]
                print(f"       + [{cid_short}...] {title} ({steps} steps)")
            if len(convs) > 5:
                print(f"       + ... and {len(convs) - 5} more conversation(s).")

    print("=" * 78 + "\n")
    return state


def print_manifest_summary(zip_path: Path) -> Dict[str, Any]:
    """Prints detailed summary of a package `.zip` file."""
    manifest = inspect_package(zip_path)
    print("\n" + "=" * 78)
    print(f" PACKAGE SUMMARY: {zip_path.name}")
    print("=" * 78)
    src = manifest.get("source_machine", {})
    print(f"  - Created At        : {manifest.get('created_at')}")
    print(f"  - Mode              : {manifest.get('mode', '').upper()}")
    print(f"  - Source Host       : {src.get('hostname')} ({src.get('os')})")
    print(f"  - Source User Home  : {src.get('user_home')}")
    print(f"  - Includes Code     : {'Yes' if manifest.get('include_workspace_files') else 'No'}")

    projs = manifest.get("projects", [])
    print(f"\n  [Projects in Package ({len(projs)})]:")
    for idx, p in enumerate(projs, start=1):
        folders_info = [
            f"{f['original_local_path']} ({f.get('files_bundled', 0)} files)"
            for f in p.get("folders", [])
        ]
        print(f"    {idx}. {p['name']} (ID: {p['id']})")
        print(f"       Original Folder: {', '.join(folders_info)}")

    convs = manifest.get("conversations", [])
    print(f"\n  [Conversations in Package ({len(convs)})]:")
    for c in convs:
        cid_short = c["conversation_id"][:8]
        print(
            f"    - [{cid_short}...] {c.get('title')} "
            f"({c.get('step_count', 0)} steps | Brain files: {c.get('brain_files_count', 0)})"
        )
    print("=" * 78 + "\n")
    return manifest


def run_interactive_menu(paths: AntigravityPaths) -> None:
    """Interactive console menu interface."""
    while True:
        print("\n" + "#" * 78)
        print("   GOOGLE ANTIGRAVITY MIGRATION TOOL (EXPORT / IMPORT / MERGE)")
        print("#" * 78)
        print("  1. View local Projects & Conversation history")
        print("  2. Selective Export - Package selected project(s) & chats into a .zip")
        print("  3. Full Backup - Package all projects, chats, brain artifacts & configs")
        print("  4. Inspect package (.zip) contents")
        print("  5. Import / Merge package into this machine (with Smart Path Remap)")
        print("  6. Rollback to an automated pre-import backup")
        print("  0. Exit")
        print("-" * 78)

        choice = input("Enter your choice (0-6): ").strip()

        if choice == "0":
            print("Exited.")
            break

        elif choice == "1":
            print_local_inventory(paths)
            input("Press Enter to return to menu...")

        elif choice == "2":
            state = discover_local_state(paths)
            proj_list = list(state["projects"].values())
            unassigned_list = state["unassigned_conversations"]
            if not proj_list and not state["conversations"]:
                print("No data available to export.")
                continue

            sel_projs: List[str] = []
            if proj_list:
                print("\n" + "=" * 78)
                print(f" STEP 1: SELECT PROJECT / WORKSPACE FOLDER TO EXPORT ({len(proj_list)})")
                print("=" * 78)
                for idx, p in enumerate(proj_list, start=1):
                    paths_str = ", ".join(p["local_paths"]) or "(No local path)"
                    convs_count = len(p["conversations"])
                    print(
                        f"  {idx:2d}. [{p['name']}]  -->  Folder: {paths_str}  "
                        f"({convs_count} chat{'s' if convs_count != 1 else ''})"
                    )
                print("-" * 78)
                raw_sel = input(
                    "Enter project/folder number(s) to export (e.g. 1,2 or 'all', Enter to skip): "
                ).strip()
                if raw_sel.lower() == "all":
                    sel_projs = [p["id"] for p in proj_list]
                elif raw_sel:
                    for part in raw_sel.split(","):
                        tok = part.strip()
                        if not tok:
                            continue
                        if tok.isdigit():
                            idx = int(tok) - 1
                            if 0 <= idx < len(proj_list):
                                sel_projs.append(proj_list[idx]["id"])
                        else:
                            # Also allow matching by project name, ID, or folder path
                            tok_low = tok.lower().rstrip("\\/")
                            for p in proj_list:
                                p_paths = [lp.lower().rstrip("\\/") for lp in p["local_paths"]]
                                if (
                                    p["id"].lower() == tok_low
                                    or p["name"].lower() == tok_low
                                    or tok_low in p_paths
                                ):
                                    sel_projs.append(p["id"])

                if sel_projs:
                    chosen_names = [
                        f"{state['projects'][pid]['name']} ({', '.join(state['projects'][pid]['local_paths'])})"
                        for pid in sel_projs
                        if pid in state["projects"]
                    ]
                    print(f"  [✓] Selected {len(sel_projs)} folder(s): {', '.join(chosen_names)}")

            sel_convs: List[str] = []
            if unassigned_list:
                ask_standalone = True
                if sel_projs:
                    ans_st = (
                        input(
                            f"\nAlso include standalone chats (chats without folder, {len(unassigned_list)} available)? (y/N): "
                        )
                        .strip()
                        .lower()
                    )
                    ask_standalone = ans_st in ("y", "yes")

                if ask_standalone:
                    print("\n" + "=" * 78)
                    print(
                        f" STEP 2: SELECT STANDALONE CONVERSATIONS (NO FOLDER) ({len(unassigned_list)})"
                    )
                    print("=" * 78)
                    for idx, c in enumerate(unassigned_list, start=1):
                        title = c.get("title") or "(Untitled)"
                        print(
                            f"  {idx:2d}. [{c['conversation_id'][:8]}...] {title} "
                            f"({c.get('step_count', 0)} steps)"
                        )
                    print("-" * 78)
                    raw_convs = input(
                        "Enter standalone chat number(s) or ID (or 'all', Enter to skip): "
                    ).strip()
                    if raw_convs.lower() == "all":
                        sel_convs = [c["conversation_id"] for c in unassigned_list]
                    elif raw_convs:
                        for item in raw_convs.split(","):
                            tok = item.strip()
                            if not tok:
                                continue
                            if tok.isdigit() and 1 <= int(tok) <= len(unassigned_list):
                                sel_convs.append(unassigned_list[int(tok) - 1]["conversation_id"])
                            else:
                                sel_convs.append(tok)

            if not sel_projs and not sel_convs:
                print("[!] No project or conversation selected.")
                continue

            inc_code_ans = (
                input("Include workspace source code files in the .zip archive? (Y/n): ")
                .strip()
                .lower()
            )
            inc_code = inc_code_ans not in ("n", "no")

            default_zip = f"antigravity_selective_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            out_str = input(f"Output .zip file name (default: {default_zip}): ").strip()
            out_zip = Path(out_str) if out_str else Path.cwd() / default_zip
            if out_zip.suffix.lower() != ".zip":
                out_zip = out_zip.with_suffix(".zip")

            print(f"\n[*] Packaging data into: {out_zip} ...")
            manifest = export_package(
                paths=paths,
                output_zip=out_zip,
                mode="selective",
                project_ids=sel_projs,
                conversation_ids=sel_convs,
                include_workspace_files=inc_code,
            )
            print(
                f"[✓] Export completed! ({len(manifest['projects'])} project(s), "
                f"{len(manifest['conversations'])} conversation(s))"
            )
            print(f"    File path: {out_zip.resolve()}")
            input("\nPress Enter to return to menu...")

        elif choice == "3":
            inc_code_ans = (
                input(
                    "Include workspace source code for all project folders in the .zip? (Y/n): "
                )
                .strip()
                .lower()
            )
            inc_code = inc_code_ans not in ("n", "no")

            default_zip = f"antigravity_full_backup_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            out_str = input(f"Output .zip file name (default: {default_zip}): ").strip()
            out_zip = Path(out_str) if out_str else Path.cwd() / default_zip
            if out_zip.suffix.lower() != ".zip":
                out_zip = out_zip.with_suffix(".zip")

            print(f"\n[*] Packaging FULL BACKUP into: {out_zip} ...")
            manifest = export_package(
                paths=paths,
                output_zip=out_zip,
                mode="full",
                include_workspace_files=inc_code,
            )
            print(
                f"[✓] Full Backup completed! ({len(manifest['projects'])} project(s), "
                f"{len(manifest['conversations'])} conversation(s))"
            )
            print(f"    File path: {out_zip.resolve()}")
            input("\nPress Enter to return to menu...")

        elif choice == "4":
            z_str = input("Enter path to .zip file to inspect: ").strip().strip('"')
            if not z_str:
                continue
            z_path = Path(z_str)
            if not z_path.exists():
                print(f"[!] File not found: {z_path}")
                continue
            print_manifest_summary(z_path)
            input("Press Enter to return to menu...")

        elif choice == "5":
            z_str = input("Enter path to .zip file to import into this machine: ").strip().strip('"')
            if not z_str:
                continue
            z_path = Path(z_str)
            if not z_path.exists():
                print(f"[!] File not found: {z_path}")
                continue

            manifest = print_manifest_summary(z_path)
            overrides: Dict[str, str] = {}
            for p in manifest.get("projects", []):
                for f_entry in p.get("folders", []):
                    orig_p = f_entry.get("original_local_path", "")
                    if not orig_p:
                        continue
                    print(f"\nProject [{p['name']}] original path on source machine: {orig_p}")
                    new_p = input(
                        f"  -> Enter destination path on this machine (Press Enter to keep '{orig_p}'): "
                    ).strip().strip('"')
                    if new_p:
                        overrides[orig_p] = new_p

            confirm = (
                input("\nHave you completely closed Antigravity and are ready to import? (y/N): ")
                .strip()
                .lower()
            )
            if confirm not in ("y", "yes"):
                print("Import operation cancelled.")
                continue

            try:
                res = import_package(
                    paths=paths,
                    zip_path=z_path,
                    folder_overrides=overrides,
                )
                print("\n[✓] IMPORT & MERGE SUCCESSFUL!")
                print(f"    - Pre-import snapshot saved at: {res['backup_dir']}")
                print(f"    - Projects imported           : {len(res['imported_projects'])}")
                print(f"    - Conversations merged        : {len(res['imported_conversations'])}")
                print(f"    - Remapped path variants      : {res['remapped_pairs_count']}")
                print("\nYou can now open Google Antigravity to verify!")
            except Exception as exc:
                print(f"\n[IMPORT ERROR]: {exc}")
            input("\nPress Enter to return to menu...")

        elif choice == "6":
            if not paths.backups_dir.exists():
                print("\n[!] No pre_import backups found on this machine.")
                continue
            b_list = sorted(
                [d for d in paths.backups_dir.iterdir() if d.is_dir()],
                reverse=True,
            )
            if not b_list:
                print("\n[!] No pre_import backups found.")
                continue
            print("\nAutomated pre-import backups available:")
            for idx, b in enumerate(b_list, start=1):
                print(f"  {idx}. {b.name} ({b})")
            sel = input("Select backup number to rollback (or press Enter to cancel): ").strip()
            if sel.isdigit():
                b_idx = int(sel) - 1
                if 0 <= b_idx < len(b_list):
                    rollback_backup(paths, b_list[b_idx])
                    print(f"[✓] Successfully rolled back to: {b_list[b_idx].name}")
            input("\nPress Enter to return to menu...")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Google Antigravity Migration Tool: Export, Import, Merge, and Remap workspaces and chat histories."
    )
    parser.add_argument(
        "--gemini-dir",
        type=Path,
        default=None,
        help="Custom .gemini directory path (defaults to ~/.gemini).",
    )
    parser.add_argument(
        "--appdata-dir",
        type=Path,
        default=None,
        help="Custom AppData\\Roaming\\Antigravity directory path.",
    )

    sub = parser.add_subparsers(dest="command")

    # list
    sub.add_parser("list", help="List all local projects and conversation history.")

    # export
    p_exp = sub.add_parser("export", help="Export data into a .zip package.")
    p_exp.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Output .zip package path.",
    )
    p_exp.add_argument(
        "--full",
        action="store_true",
        help="Full backup mode (exports all projects, chats, brain files, and global configs).",
    )
    p_exp.add_argument(
        "--projects",
        nargs="*",
        default=[],
        help="List of project IDs, names, or paths to export (selective mode).",
    )
    p_exp.add_argument(
        "--conversations",
        nargs="*",
        default=[],
        help="List of conversation IDs (or 8-char prefixes) to export.",
    )
    p_exp.add_argument(
        "--no-workspace-files",
        action="store_true",
        help="Only export chat history and project metadata without bundling folder source files.",
    )
    p_exp.add_argument(
        "--include-heavy-dirs",
        action="store_true",
        help="Include heavy dependency directories like node_modules and .venv (excluded by default).",
    )

    # inspect
    p_ins = sub.add_parser("inspect", help="Inspect metadata inside a package .zip file.")
    p_ins.add_argument("zip_file", type=Path, help="Path to .zip package.")

    # import
    p_imp = sub.add_parser(
        "import", help="Import or merge a .zip package into the local Antigravity installation."
    )
    p_imp.add_argument("zip_file", type=Path, help="Path to .zip package.")
    p_imp.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD_PATH=NEW_PATH",
        help="Remap old source path to new path on this machine (can be repeated).",
    )
    p_imp.add_argument(
        "--no-extract-workspaces",
        action="store_true",
        help="Do not extract bundled workspace source files (useful if synced via Git or external drive).",
    )
    p_imp.add_argument(
        "--ignore-running",
        action="store_true",
        help="Bypass active Antigravity.exe process check.",
    )

    # rollback
    p_rb = sub.add_parser(
        "rollback", help="Rollback to an automated pre-import backup snapshot."
    )
    p_rb.add_argument(
        "backup_folder",
        type=Path,
        help="Path to pre_import_<timestamp> backup directory.",
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    paths = AntigravityPaths(gemini_dir=args.gemini_dir, appdata_dir=args.appdata_dir)

    if not args.command:
        run_interactive_menu(paths)
        return 0

    if args.command == "list":
        print_local_inventory(paths)
        return 0

    if args.command == "export":
        mode = "full" if args.full else "selective"
        if mode == "selective" and not args.projects and not args.conversations:
            parser.error(
                "In selective mode, please specify --projects <name/ID/path> or --conversations <id>, "
                "or use --full to export everything."
            )
        manifest = export_package(
            paths=paths,
            output_zip=args.output,
            mode=mode,
            project_ids=args.projects,
            conversation_ids=args.conversations,
            include_workspace_files=not args.no_workspace_files,
            exclude_heavy_dirs=not args.include_heavy_dirs,
        )
        print(
            f"[✓] Package '{args.output}' exported successfully: "
            f"{len(manifest['projects'])} project(s), {len(manifest['conversations'])} conversation(s)."
        )
        return 0

    if args.command == "inspect":
        print_manifest_summary(args.zip_file)
        return 0

    if args.command == "import":
        overrides: Dict[str, str] = {}
        for item in args.map:
            if "=" not in item:
                parser.error(f"Invalid --map format (expected OLD=NEW): {item}")
            old_p, new_p = item.split("=", 1)
            overrides[old_p.strip()] = new_p.strip()

        res = import_package(
            paths=paths,
            zip_path=args.zip_file,
            folder_overrides=overrides,
            extract_workspaces=not args.no_extract_workspaces,
            check_running_process=not args.ignore_running,
        )
        print(
            f"[✓] Import completed! Merged {len(res['imported_projects'])} project(s) and "
            f"{len(res['imported_conversations'])} conversation(s).\n"
            f"    Pre-import snapshot saved at: {res['backup_dir']}"
        )
        return 0

    if args.command == "rollback":
        rollback_backup(paths, args.backup_folder)
        print(f"[✓] Successfully rolled back from: {args.backup_folder}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
