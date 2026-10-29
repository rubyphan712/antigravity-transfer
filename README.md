# Antigravity Transfer (`antigravity-transfer`) 🚀

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-brightgreen.svg)]()
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()

> A robust, lightweight migration & backup tool for **Google Antigravity**.  
> Safely export, import, merge, and remap workspace projects, conversation histories, artifacts, and brain logs across different computers, drives, or user accounts.

*Read this in other languages: [Tiếng Việt](README_VI.md).*

---

## 💡 Why `antigravity-transfer`?

Currently, Google Antigravity does not provide an official 1-click feature to export and migrate conversation histories and projects to a different computer (e.g., PC to laptop, changing drive letters `D:\` ↔ `C:\`, or switching Windows usernames `Alice` ↔ `Bob`).

If you attempt to copy the `.gemini` folder manually:
1. **Target Data Loss**: Copying SQLite databases (`conversation_summaries.db`) completely **overwrites and deletes** all existing chats already present on your target computer.
2. **Corrupted Protobuf Binary Blobs**: Conversation state, plans, and sidebar metadata are stored as binary Protocol Buffer blobs (`trajectory_metadata_blob` and `raw_summary`). These blobs embed hardcoded `folderUri` paths. When the path length changes across machines, standard text replacement corrupts the parent `varint` length headers, breaking sidebar navigation and artifact rendering.
3. **Database Locks & WAL Inconsistency**: If Antigravity is open, active transactions reside in `-wal` and `-shm` temporary files; simple file copies produce incomplete or corrupted snapshots.

**`antigravity-transfer` solves all of these challenges seamlessly.**

---

## ✨ Features

- 🎯 **Selective Migration**: Export specific projects and conversations into a compact `.zip` package.
- 📦 **Full Backup & Restore**: Backup everything including all projects, conversation databases, artifacts (`brain/`), and global configurations (`config.json`, `mcp_config.json`).
- 🧬 **Wire-Format Safe Protobuf Remapping (`proto_remap.py`)**:
  - Automatically recalculates all nested `varint` length prefixes when URI paths or usernames change.
  - Zero false-positive corruption: untouched binary fields (hashes, timestamps, non-path bytes) remain 100% byte-for-byte identical.
- 🔀 **Safe SQLite UPSERT**: Merges incoming conversations into the target database without overwriting or deleting existing chats.
- 🔍 **Fulltext Search (FTS5) Sync**: Automatically updates the fulltext search index for seamless conversation searching.
- 🛡️ **Automated Safety Snapshot & Rollback**: Every import automatically snapshots the target machine's state before applying changes, allowing one-click rollback if needed.
- ⚡ **Zero External Dependencies**: Built entirely with Python's standard library (`sqlite3`, `zipfile`, `urllib`, `argparse`).

---

## 📂 Antigravity Data Architecture

Antigravity links data across 5 distinct storage components:

| Component | Default Path (Windows) | Description |
| :--- | :--- | :--- |
| **Workspace Folder** | e.g. `D:\MyProject\` | Source code, project rules (`.agents/`), and local configurations. |
| **Project Definition** | `%USERPROFILE%\.gemini\config\projects\<id>.json` | Project identity and encoded URI (e.g. `file:///d%3A/MyProject`). |
| **Conversation DB** | `%USERPROFILE%\.gemini\antigravity\conversations\<id>.db` | SQLite database storing steps and `trajectory_metadata_blob`. |
| **Summaries & Search** | `%USERPROFILE%\.gemini\antigravity\conversation_summaries.db`<br>`%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb` | Sidebar index and FTS5 search index. |
| **Brain & Artifacts** | `%USERPROFILE%\.gemini\antigravity\brain\<id>\` | Implementation plans (`.md`), logs (`transcript.jsonl`), and media. |

---

## 🚀 Quickstart

### Prerequisites
- Python 3.10 or higher.
- Make sure Python is added to your system `PATH`.

---

### Method 1: Interactive Menu (Recommended for Windows)

Double-click **`run_transfer.bat`** (or execute `python agy_transfer.py` in your terminal):

```text
##############################################################################
   GOOGLE ANTIGRAVITY MIGRATION TOOL (EXPORT / IMPORT / MERGE)
##############################################################################
  1. View local Projects & Conversation history
  2. Selective Export - Package selected project(s) & chats into a .zip
  3. Full Backup - Package all projects, chats, brain artifacts & configs
  4. Inspect package (.zip) contents
  5. Import / Merge package into this machine (with Smart Path Remap)
  6. Rollback to an automated pre-import backup
  0. Exit
------------------------------------------------------------------------------
```

#### Step 1: On the Source Machine
1. Run `run_transfer.bat` and choose **Option 2** (Selective Export) or **Option 3** (Full Backup).
2. Follow prompts to select your projects and choose whether to bundle source code.
3. A `.zip` package will be created.

#### Step 2: On the Target Machine
1. Transfer the `.zip` archive and this tool folder to the target computer.
2. **Close Google Antigravity** completely (check system tray).
3. Run `run_transfer.bat` and choose **Option 5** (Import & Smart Remap).
4. Enter target folder locations when prompted (or press `Enter` to keep original paths).
5. Launch Google Antigravity: your projects and conversations appear in the sidebar alongside any pre-existing chats!

---

### Method 2: Command-Line Interface (CLI)

For power users, scripts, and CI/CD:

```bash
# 1. List all local projects and conversations
python agy_transfer.py list

# 2. Export a specific project by name, ID, or folder path
python agy_transfer.py export -o my_project.zip --projects "MyAwesomeApp"

# 3. Full backup (all projects, conversations, brain files, global configs)
python agy_transfer.py export -o full_backup.zip --full

# 4. Inspect an archive before importing
python agy_transfer.py inspect my_project.zip

# 5. Import and keep original folder paths
python agy_transfer.py import my_project.zip

# 6. Import and remap paths (e.g. from D:\ on PC to C:\Code on Laptop)
python agy_transfer.py import my_project.zip --map "D:\MyAwesomeApp=C:\Code\MyAwesomeApp"

# 7. Rollback if needed
python agy_transfer.py rollback "C:\Users\<User>\.gemini\antigravity\backups\pre_import_20261029_120000"
```

---

## 🧪 Testing

The repository includes a comprehensive automated test suite with mock environments:

```bash
python -m unittest discover tests
```

Tests cover:
- Protocol Buffer varint encoding/decoding.
- Complex nested protobuf length recalculation and wire-format preservation.
- Full end-to-end multi-machine migration simulation (Alice → Bob).
- SQLite UPSERT merge and FTS5 search index updates.
- Automated snapshot rollback validation.

---

## 🛡️ License

This project is open-source and released under the [MIT License](LICENSE).
