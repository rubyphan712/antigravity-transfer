#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_transfer.py
======================
Automated test suite (Unit & Integration Tests) for `proto_remap.py` and `agy_transfer.py`.
All tests run in isolated temporary directories (`tempfile.TemporaryDirectory`)
and never touch real machine data.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Add root folder to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agy_transfer import (
    SUMMARIES_SCHEMA_SQL,
    AntigravityPaths,
    discover_local_state,
    export_package,
    import_package,
    inspect_package,
    rollback_backup,
)
from proto_remap import (
    WIRE_LEN_DELIM,
    WIRE_VARINT,
    build_path_replacement_pairs,
    decode_varint,
    encode_varint,
    parse_agyhub_summaries_pb,
    path_to_antigravity_uris,
    remap_protobuf_blob,
    serialize_agyhub_summaries_pb,
    uri_to_local_path,
)


def _make_proto_field_bytes(field_num: int, payload: bytes) -> bytes:
    tag = (field_num << 3) | WIRE_LEN_DELIM
    return encode_varint(tag) + encode_varint(len(payload)) + payload


def _make_proto_field_varint(field_num: int, val: int) -> bytes:
    tag = (field_num << 3) | WIRE_VARINT
    return encode_varint(tag) + encode_varint(val)


class TestProtoRemap(unittest.TestCase):
    """Tests Protobuf wire-format parsing, varint encoding, and URI path conversion."""

    def test_varint_roundtrip(self) -> None:
        for val in (0, 1, 127, 128, 300, 16384, 2**32 + 12345):
            encoded = encode_varint(val)
            decoded, pos = decode_varint(encoded, 0)
            self.assertEqual(decoded, val)
            self.assertEqual(pos, len(encoded))

    def test_uri_conversion_windows(self) -> None:
        enc, plain = path_to_antigravity_uris(r"D:\Transfer-Antigravity")
        self.assertEqual(enc, "file:///d%3A/Transfer-Antigravity")
        self.assertEqual(plain, "file:///d:/Transfer-Antigravity")
        self.assertEqual(uri_to_local_path(enc).lower(), r"d:\transfer-antigravity")
        self.assertEqual(uri_to_local_path(plain).lower(), r"d:\transfer-antigravity")

    def test_nested_protobuf_remap_longer_and_shorter(self) -> None:
        """
        Simulates exact structures of `trajectory_metadata_blob` and `raw_summary`:
        - Field 1 (submessage) contains:
            + Field 1 (string): "file:///d:/AlphaRepo"
            + Field 3 (empty bytes): b""
        - Field 2 (unrelated binary submessage with arbitrary bytes):
            + Field 1 (varint): 1793175205
            + Field 2 (random binary bytes): b"\x00\xff\x12\x0a\x99\x80"
        - Field 7 (string): "conv-123: file:///d%3A/AlphaRepo"
        - Field 8 (artifact path string): "C:\\Users\\Alice\\.gemini\\antigravity\\brain\\conv-123\\plan.md"
        """
        inner_uri = _make_proto_field_bytes(1, b"file:///d:/AlphaRepo") + _make_proto_field_bytes(3, b"")
        outer_f1 = _make_proto_field_bytes(1, inner_uri)

        random_bin_payload = _make_proto_field_varint(1, 1793175205) + _make_proto_field_bytes(
            2, b"\x00\xff\x12\x0a\x99\x80"
        )
        outer_f2 = _make_proto_field_bytes(2, random_bin_payload)

        outer_f7 = _make_proto_field_bytes(7, b"conv-123: file:///d%3A/AlphaRepo")
        outer_f8 = _make_proto_field_bytes(
            8, b"C:\\Users\\Alice\\.gemini\\antigravity\\brain\\conv-123\\plan.md"
        )

        original_blob = outer_f1 + outer_f2 + outer_f7 + outer_f8

        # Remap to a much longer path + migrate username from Alice -> BobDeveloper
        pairs = build_path_replacement_pairs(
            folder_mappings=[(r"D:\AlphaRepo", r"E:\Workspaces\SuperLongProjectName\AlphaRepoV2")],
            old_user_home=r"C:\Users\Alice",
            new_user_home=r"C:\Users\BobDeveloper",
        )

        remapped_blob = remap_protobuf_blob(original_blob, pairs)

        # Check new strings exist in remapped blob
        self.assertIn(b"file:///e:/Workspaces/SuperLongProjectName/AlphaRepoV2", remapped_blob)
        self.assertIn(
            b"conv-123: file:///e%3A/Workspaces/SuperLongProjectName/AlphaRepoV2", remapped_blob
        )
        self.assertIn(
            b"C:\\Users\\BobDeveloper\\.gemini\\antigravity\\brain\\conv-123\\plan.md",
            remapped_blob,
        )
        # Verify unrelated binary payload (outer_f2) is 100% byte-for-byte preserved
        self.assertIn(outer_f2, remapped_blob)

        # Verify wire-format structure integrity after length changes
        pos = 0
        fields_found = []
        while pos < len(remapped_blob):
            tag, pos = decode_varint(remapped_blob, pos)
            fn = tag >> 3
            wt = tag & 7
            self.assertEqual(wt, WIRE_LEN_DELIM)
            flen, pos = decode_varint(remapped_blob, pos)
            fval = remapped_blob[pos : pos + flen]
            pos += flen
            fields_found.append((fn, fval))

        self.assertEqual(pos, len(remapped_blob))
        self.assertEqual([f[0] for f in fields_found], [1, 2, 7, 8])

    def test_agyhub_summaries_pb_map_merge(self) -> None:
        initial_map = {
            "conv-existing-1": b"\x0a\x05hello",
            "conv-existing-2": b"\x0a\x05world",
        }
        raw_pb = serialize_agyhub_summaries_pb(initial_map)
        parsed = parse_agyhub_summaries_pb(raw_pb)
        self.assertEqual(parsed, initial_map)

        # Merge 1 additional conversation
        parsed["conv-imported-3"] = b"\x0a\x08imported"
        merged_pb = serialize_agyhub_summaries_pb(parsed)
        reparsed = parse_agyhub_summaries_pb(merged_pb)
        self.assertEqual(len(reparsed), 3)
        self.assertEqual(reparsed["conv-existing-1"], b"\x0a\x05hello")
        self.assertEqual(reparsed["conv-imported-3"], b"\x0a\x08imported")


class TestEndToEndTransfer(unittest.TestCase):
    """
    End-to-end integration test:
    Simulates Machine A (Alice) exporting a project and chat history to Machine B (Bob)
    which already has existing conversations, with folder path remapping and rollback verification.
    """

    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="agy_test_e2e_")
        self.root = Path(self.tmp_obj.name)

        # --- Setup Machine A (Source - Alice) ---
        self.alice_home = self.root / "Users" / "Alice"
        self.alice_ws = self.root / "DriveD" / "AlphaProject"
        self.alice_paths = AntigravityPaths(
            gemini_dir=self.alice_home / ".gemini",
            appdata_dir=self.alice_home / "AppData" / "Roaming" / "Antigravity",
            user_home=self.alice_home,
        )
        self._populate_mock_machine(
            paths=self.alice_paths,
            workspace_dir=self.alice_ws,
            proj_id="proj-alpha-111",
            proj_name="AlphaProject",
            conv_id="conv-alpha-aaa",
            conv_title="Alpha Architecture Design",
        )

        # --- Setup Machine B (Target - Bob, already has Project Beta & Chat Beta) ---
        self.bob_home = self.root / "Users" / "Bob"
        self.bob_existing_ws = self.root / "DriveC" / "BetaProject"
        self.bob_paths = AntigravityPaths(
            gemini_dir=self.bob_home / ".gemini",
            appdata_dir=self.bob_home / "AppData" / "Roaming" / "Antigravity",
            user_home=self.bob_home,
        )
        self._populate_mock_machine(
            paths=self.bob_paths,
            workspace_dir=self.bob_existing_ws,
            proj_id="proj-beta-222",
            proj_name="BetaProject",
            conv_id="conv-beta-bbb",
            conv_title="Feature Development on Bob Machine",
        )

    def tearDown(self) -> None:
        self.tmp_obj.cleanup()

    def _populate_mock_machine(
        self,
        paths: AntigravityPaths,
        workspace_dir: Path,
        proj_id: str,
        proj_name: str,
        conv_id: str,
        conv_title: str,
    ) -> None:
        # 1. Create mock workspace code (+ node_modules to test directory exclusion)
        (workspace_dir / "src").mkdir(parents=True, exist_ok=True)
        (workspace_dir / "src" / "app.py").write_text("print('Hello Antigravity')\n", encoding="utf-8")
        (workspace_dir / ".agents" / "rules").mkdir(parents=True, exist_ok=True)
        (workspace_dir / ".agents" / "rules" / "coding.md").write_text("# Rule\n", encoding="utf-8")
        (workspace_dir / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
        (workspace_dir / "node_modules" / "pkg" / "heavy.js").write_text("// heavy", encoding="utf-8")

        enc_uri, plain_uri = path_to_antigravity_uris(workspace_dir)

        # 2. Create .gemini/config/projects/<proj_id>.json & mcp_config.json
        paths.projects_dir.mkdir(parents=True, exist_ok=True)
        proj_json = {
            "id": proj_id,
            "name": proj_name,
            "projectResources": {"resources": [{"folderUri": enc_uri}]},
            "permissionGrants": {"v2Migrated": True},
            "settings": {},
            "isWorkspaceOnly": False,
        }
        (paths.projects_dir / f"{proj_id}.json").write_text(
            json.dumps(proj_json, indent=2), encoding="utf-8"
        )
        (paths.config_dir / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {}}), encoding="utf-8"
        )

        # 3. Create conversation_summaries.db and agyhub_summaries_proto.pb
        paths.agy_dir.mkdir(parents=True, exist_ok=True)
        raw_sum_proto = (
            _make_proto_field_bytes(1, _make_proto_field_bytes(1, plain_uri.encode("utf-8")))
            + _make_proto_field_bytes(7, f"{conv_id}: {enc_uri}".encode("utf-8"))
            + _make_proto_field_bytes(8, proj_id.encode("utf-8"))
        )

        conn = sqlite3.connect(str(paths.summaries_db))
        conn.executescript(SUMMARIES_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO conversation_summaries (
                conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, project_id, last_user_input_time,
                app_data_dir, raw_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conv_id,
                conv_title,
                conv_title,
                5,
                "2026-10-28 08:00:00+00:00",
                json.dumps([enc_uri]),
                "CASCADE_RUN_STATUS_IDLE",
                proj_id,
                "2026-10-28 08:00:00+00:00",
                "antigravity",
                raw_sum_proto,
            ),
        )
        conn.execute(
            """
            INSERT INTO conversations_fts (
                conversation_id, chunk_index, chunk_first_step, chunk_last_step,
                last_modified_time, step_count, last_step_created_at, archived,
                user_step_byte_offsets, agent_step_byte_offsets,
                title, workspace, project, user_body, agent_body
            ) VALUES (?, 0, 0, 5, 1793175205, 5, 1793175205, 0, ?, ?, ?, ?, ?, 'q', 'a')
            """,
            (conv_id, b"\x00\x04\x08\x0c", b"\x02\x06\x0a\x0e", conv_title, proj_name, proj_name),
        )
        conn.commit()
        conn.close()

        paths.summaries_pb.write_bytes(serialize_agyhub_summaries_pb({conv_id: raw_sum_proto}))

        # 4. Create conversations/<conv_id>.db
        paths.conversations_dir.mkdir(parents=True, exist_ok=True)
        conv_db = paths.conversations_dir / f"{conv_id}.db"
        c_conn = sqlite3.connect(str(conv_db))
        c_conn.executescript(
            """
            CREATE TABLE trajectory_meta (
                trajectory_id text PRIMARY KEY, cascade_id text, trajectory_type integer, source integer
            );
            CREATE TABLE trajectory_metadata_blob (
                id text DEFAULT "main" PRIMARY KEY, data blob
            );
            CREATE TABLE steps (
                idx integer PRIMARY KEY, step_type integer NOT NULL DEFAULT 0,
                status integer NOT NULL DEFAULT 0, has_subtrajectory numeric NOT NULL DEFAULT false,
                metadata blob, error_details blob, permissions blob,
                task_details blob, render_info blob, step_payload blob,
                step_format integer NOT NULL DEFAULT 0
            );
            """
        )
        c_conn.execute(
            "INSERT INTO trajectory_meta VALUES (?, ?, 4, 1)", (f"traj-{conv_id}", conv_id)
        )
        c_conn.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (raw_sum_proto,))

        brain_artifact_path = str(paths.brain_dir / conv_id / "implementation_plan.md")
        step_payload_proto = _make_proto_field_bytes(1, brain_artifact_path.encode("utf-8"))
        c_conn.execute(
            "INSERT INTO steps (idx, step_type, status, step_payload) VALUES (0, 1, 1, ?)",
            (step_payload_proto,),
        )
        c_conn.commit()
        c_conn.close()

        # 5. Create brain/<conv_id>/ and annotations/<conv_id>.pbtxt
        c_brain = paths.brain_dir / conv_id
        c_brain.mkdir(parents=True, exist_ok=True)
        (c_brain / "implementation_plan.md").write_text(
            f"# Plan for {workspace_dir}\n", encoding="utf-8"
        )
        paths.annotations_dir.mkdir(parents=True, exist_ok=True)
        (paths.annotations_dir / f"{conv_id}.pbtxt").write_text(
            f'title:"{conv_title}"\n', encoding="utf-8"
        )

    def test_selective_export_and_merge_import_with_path_remap(self) -> None:
        zip_out = self.root / "alpha_export.zip"

        # 1. Selective Export Project Alpha from Machine A (Alice)
        manifest = export_package(
            paths=self.alice_paths,
            output_zip=zip_out,
            mode="selective",
            project_ids=["AlphaProject"],
            include_workspace_files=True,
            exclude_heavy_dirs=True,
        )
        self.assertTrue(zip_out.exists())
        self.assertEqual(len(manifest["projects"]), 1)
        self.assertEqual(len(manifest["conversations"]), 1)
        # Only 2 files bundled (src/app.py and .agents/rules/coding.md), node_modules excluded
        self.assertEqual(manifest["projects"][0]["folders"][0]["files_bundled"], 2)

        inspected = inspect_package(zip_out)
        self.assertEqual(inspected["projects"][0]["id"], "proj-alpha-111")

        # 2. Import package into Machine B (Bob) with path override
        bob_new_alpha_ws = self.root / "DriveE" / "BobWorkspace" / "AlphaRemapped"
        import_res = import_package(
            paths=self.bob_paths,
            zip_path=zip_out,
            folder_overrides={str(self.alice_ws): str(bob_new_alpha_ws)},
            extract_workspaces=True,
            check_running_process=False,
        )

        self.assertIn("proj-alpha-111", import_res["imported_projects"])
        self.assertIn("conv-alpha-aaa", import_res["imported_conversations"])

        # 3. Verify workspace source code was extracted to Bob's new destination
        self.assertTrue((bob_new_alpha_ws / "src" / "app.py").exists())
        self.assertTrue((bob_new_alpha_ws / ".agents" / "rules" / "coding.md").exists())
        self.assertFalse((bob_new_alpha_ws / "node_modules").exists())

        # 4. Verify `projects/proj-alpha-111.json` on Bob was updated with the new URI
        new_enc_uri, new_plain_uri = path_to_antigravity_uris(bob_new_alpha_ws)
        bob_proj_json = json.loads(
            (self.bob_paths.projects_dir / "proj-alpha-111.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            bob_proj_json["projectResources"]["resources"][0]["folderUri"], new_enc_uri
        )

        # 5. Verify `conversation_summaries.db` on Bob preserves Beta chat AND adds Alpha chat
        conn = sqlite3.connect(str(self.bob_paths.summaries_db))
        conn.row_factory = sqlite3.Row
        rows = {
            r["conversation_id"]: dict(r)
            for r in conn.execute("SELECT * FROM conversation_summaries").fetchall()
        }
        fts_rows = {
            r["conversation_id"]: dict(r)
            for r in conn.execute("SELECT * FROM conversations_fts").fetchall()
        }
        conn.close()

        self.assertIn("conv-beta-bbb", rows, "Existing chats on target machine must be preserved!")
        self.assertIn("conv-alpha-aaa", rows, "New chats must be merged into target DB!")
        self.assertEqual(rows["conv-alpha-aaa"]["workspace_uris"], json.dumps([new_enc_uri]))
        self.assertIn(new_plain_uri.encode("utf-8"), rows["conv-alpha-aaa"]["raw_summary"])
        self.assertIn(new_enc_uri.encode("utf-8"), rows["conv-alpha-aaa"]["raw_summary"])
        self.assertEqual(fts_rows["conv-alpha-aaa"]["workspace"], "AlphaRemapped")

        # 6. Verify `agyhub_summaries_proto.pb` contains both conversations
        pb_map = parse_agyhub_summaries_pb(self.bob_paths.summaries_pb.read_bytes())
        self.assertIn("conv-beta-bbb", pb_map)
        self.assertIn("conv-alpha-aaa", pb_map)
        self.assertIn(new_plain_uri.encode("utf-8"), pb_map["conv-alpha-aaa"])

        # 7. Verify `<conv_id>.db` on Bob updated user home path from Alice -> Bob
        alpha_db_on_bob = self.bob_paths.conversations_dir / "conv-alpha-aaa.db"
        c_conn = sqlite3.connect(str(alpha_db_on_bob))
        meta_blob = c_conn.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE id='main'"
        ).fetchone()[0]
        step_blob = c_conn.execute("SELECT step_payload FROM steps WHERE idx=0").fetchone()[0]
        c_conn.close()

        self.assertIn(new_plain_uri.encode("utf-8"), meta_blob)
        expected_bob_brain_str = str(
            self.bob_paths.brain_dir / "conv-alpha-aaa" / "implementation_plan.md"
        )
        self.assertIn(expected_bob_brain_str.encode("utf-8"), step_blob)

        # 8. Verify Rollback restores target machine to exact pre-import state
        backup_folder = Path(import_res["backup_dir"])
        rollback_backup(self.bob_paths, backup_folder)

        conn_after = sqlite3.connect(str(self.bob_paths.summaries_db))
        ids_after = [
            r[0]
            for r in conn_after.execute(
                "SELECT conversation_id FROM conversation_summaries"
            ).fetchall()
        ]
        conn_after.close()
        self.assertEqual(ids_after, ["conv-beta-bbb"])

    def test_virtual_workspace_folder_discovery_and_export(self) -> None:
        """
        Verifies that conversations with `project_id = 'outside-of-project'`
        but having a folder in `workspace_uris` are automatically grouped as a
        selectable Workspace Folder project rather than hidden in unassigned chats.
        """
        unregistered_ws = self.root / "DriveD" / "UnregisteredFolder"
        unregistered_ws.mkdir(parents=True, exist_ok=True)
        (unregistered_ws / "main.py").write_text("print('unregistered')\n", encoding="utf-8")
        enc_uri, _ = path_to_antigravity_uris(unregistered_ws)

        conn = sqlite3.connect(str(self.alice_paths.summaries_db))
        conn.execute(
            """
            INSERT INTO conversation_summaries (
                conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, project_id, last_user_input_time,
                app_data_dir, raw_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "conv-outside-999",
                "Chat Inside Unregistered Folder",
                "preview",
                12,
                "2026-10-29 09:00:00+00:00",
                json.dumps([enc_uri]),
                "CASCADE_RUN_STATUS_IDLE",
                "outside-of-project",
                "2026-10-29 09:00:00+00:00",
                "antigravity",
                b"",
            ),
        )
        conn.commit()
        conn.close()

        state = discover_local_state(self.alice_paths)
        self.assertEqual(len(state["unassigned_conversations"]), 0)
        virt_projs = [p for p in state["projects"].values() if p.get("is_virtual")]
        self.assertEqual(len(virt_projs), 1)
        self.assertEqual(virt_projs[0]["name"], "UnregisteredFolder")

        zip_out = self.root / "virt_export.zip"
        manifest = export_package(
            paths=self.alice_paths,
            output_zip=zip_out,
            mode="selective",
            project_ids=["UnregisteredFolder"],
            include_workspace_files=True,
        )
        self.assertEqual(len(manifest["projects"]), 1)
        self.assertEqual(manifest["projects"][0]["folders"][0]["files_bundled"], 1)


if __name__ == "__main__":
    unittest.main()
