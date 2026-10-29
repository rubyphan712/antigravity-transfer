#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proto_remap.py
==============
Wire-format safe Protobuf remapper and URI path normalizer for Google Antigravity
data migration (`agy_transfer`).

Technical Highlights:
- Zero external dependencies (100% Python Standard Library).
- Wire-format preservation:
  + Only parses and replaces length-delimited fields (wire_type == 2) that actually
    contain target old path strings.
  + All other binary fields (images, hashes, packed numbers, arbitrary bytes without target strings)
    are preserved 100% byte-for-byte (Zero False-Positive Corruption).
  + Automatically recalculates all nested `varint` length prefixes from child nodes to parents
    when path string lengths change.
- Supports reading and merging (upserting) `agyhub_summaries_proto.pb` (map<string, Summary>).
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Standard wire types in Protocol Buffers (proto2 / proto3)
WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN_DELIM = 2
WIRE_START_GROUP = 3
WIRE_END_GROUP = 4
WIRE_FIXED32 = 5


def encode_varint(value: int) -> bytes:
    """Encodes a non-negative integer into Protobuf varint bytes."""
    if value < 0:
        raise ValueError(f"Varint does not support negative numbers in length mode: {value}")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def decode_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """
    Decodes a varint from `data` starting at index `pos`.
    Returns `(value, new_pos)`. Raises `ValueError` if data is invalid.
    """
    result = 0
    shift = 0
    length = len(data)
    while True:
        if pos >= length:
            raise ValueError("Unexpected end of data while reading varint.")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("Varint exceeds 64 bits (invalid Protobuf data).")


def _contains_any_bytes(blob: bytes, byte_pairs: Sequence[Tuple[bytes, bytes]]) -> bool:
    """Quick check whether `blob` contains at least one source byte sequence to replace."""
    for old_b, new_b in byte_pairs:
        if old_b and old_b != new_b and old_b in blob:
            return True
    return False


def _is_valid_utf8_text(data: bytes) -> Optional[str]:
    """
    Checks if `data` is a valid pure UTF-8 string (without binary control characters
    except \\t, \\n, \\r).
    Returns decoded `str` if valid, otherwise `None`.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None

    for ch in text:
        code = ord(ch)
        if code < 32 and ch not in ("\t", "\n", "\r"):
            return None
        if code == 0x7F:
            return None
    return text


def remap_text_strings(text: str, str_pairs: Sequence[Tuple[str, str]]) -> str:
    """Sequentially replaces `(old_str, new_str)` pairs in `text`."""
    result = text
    for old_s, new_s in str_pairs:
        if old_s and old_s != new_s and old_s in result:
            result = result.replace(old_s, new_s)
    return result


def remap_protobuf_blob(
    data: bytes,
    str_pairs: Sequence[Tuple[str, str]],
) -> bytes:
    """
    Replaces path strings inside a binary Protobuf blob (`data`)
    and automatically recalculates `varint` length headers of parent messages.

    Safety principles:
    1. If `data` does not contain any target source bytes -> return original `data` immediately.
    2. Sequentially parses Protobuf fields from byte 0 to end.
    3. For each `wire_type == 2` (length-delimited) field:
       - If payload does not contain target strings -> keep original bytes untouched.
       - If payload is valid UTF-8 text -> replace string and update length varint.
       - If payload is a nested sub-message -> recursively call `remap_protobuf_blob`
         and write updated length varint.
    4. If binary blob is not a valid Protobuf message, return original bytes to avoid corruption.
    """
    if not data or not str_pairs:
        return data

    byte_pairs: List[Tuple[bytes, bytes]] = [
        (old_s.encode("utf-8"), new_s.encode("utf-8"))
        for old_s, new_s in str_pairs
        if old_s and old_s != new_s
    ]
    if not _contains_any_bytes(data, byte_pairs):
        return data

    parsed = _try_remap_proto_message(data, str_pairs, byte_pairs)
    return parsed if parsed is not None else data


def _try_remap_proto_message(
    data: bytes,
    str_pairs: Sequence[Tuple[str, str]],
    byte_pairs: Sequence[Tuple[bytes, bytes]],
) -> Optional[bytes]:
    """
    Attempts to parse `data` as a complete Protobuf message from index 0 to `len(data)`.
    Returns new bytes on full success, or `None` if data does not match wire format.
    """
    pos = 0
    length = len(data)
    out = bytearray()

    try:
        while pos < length:
            tag_start = pos
            tag, pos = decode_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07

            if field_num <= 0:
                return None

            if wire_type == WIRE_VARINT:
                _, pos = decode_varint(data, pos)
                out.extend(data[tag_start:pos])

            elif wire_type == WIRE_FIXED64:
                if pos + 8 > length:
                    return None
                pos += 8
                out.extend(data[tag_start:pos])

            elif wire_type == WIRE_FIXED32:
                if pos + 4 > length:
                    return None
                pos += 4
                out.extend(data[tag_start:pos])

            elif wire_type == WIRE_LEN_DELIM:
                sub_len, payload_start = decode_varint(data, pos)
                payload_end = payload_start + sub_len
                if payload_end > length or sub_len < 0:
                    return None

                payload = data[payload_start:payload_end]
                pos = payload_end

                # If payload does not contain any old paths -> preserve byte-for-byte
                if not _contains_any_bytes(payload, byte_pairs):
                    out.extend(data[tag_start:payload_end])
                    continue

                # Check if payload is a plain UTF-8 text string
                as_text = _is_valid_utf8_text(payload)
                if as_text is not None:
                    new_text = remap_text_strings(as_text, str_pairs)
                    new_payload = new_text.encode("utf-8")
                    out.extend(encode_varint(tag))
                    out.extend(encode_varint(len(new_payload)))
                    out.extend(new_payload)
                else:
                    # Attempt recursive parsing as a nested Protobuf sub-message
                    remapped_sub = _try_remap_proto_message(payload, str_pairs, byte_pairs)
                    if remapped_sub is not None:
                        out.extend(encode_varint(tag))
                        out.extend(encode_varint(len(remapped_sub)))
                        out.extend(remapped_sub)
                    else:
                        # Fallback for mixed binary data: preserve original slice
                        out.extend(data[tag_start:payload_end])
            else:
                # Unsupported wire type or legacy group
                return None
    except ValueError:
        return None

    return bytes(out)


# ==============================================================================
# Folder <-> URI Normalization and Replacement Pair Generation
# ==============================================================================


def path_to_antigravity_uris(folder_path: str | Path) -> Tuple[str, str]:
    """
    Converts an OS filesystem folder path (e.g. `D:\\Transfer-Antigravity`)
    into the 2 URI standards used by Antigravity in SQLite/JSON:
    1. `encoded_uri`: `file:///d%3A/Transfer-Antigravity` (used in `projects/*.json`, `workspace_uris`, etc.)
    2. `plain_uri`:   `file:///d:/Transfer-Antigravity` (used in `raw_summary` and `trajectory_metadata_blob`)
    """
    raw = str(folder_path).strip().rstrip("\\/")
    norm = raw.replace("\\", "/")

    # Windows drive letter handling (e.g. D:/foo -> d:/foo)
    if len(norm) >= 2 and norm[1] == ":":
        drive_lower = norm[0].lower()
        rest = norm[2:].lstrip("/")
        quoted_parts = [urllib.parse.quote(p, safe="") for p in rest.split("/") if p]
        joined_quoted = "/".join(quoted_parts)
        joined_plain = "/".join([p for p in rest.split("/") if p])

        encoded_uri = f"file:///{drive_lower}%3A/{joined_quoted}" if joined_quoted else f"file:///{drive_lower}%3A"
        plain_uri = f"file:///{drive_lower}:/{joined_plain}" if joined_plain else f"file:///{drive_lower}:"
        return encoded_uri, plain_uri

    # POSIX path (macOS / Linux)
    rest = norm.lstrip("/")
    quoted_parts = [urllib.parse.quote(p, safe="") for p in rest.split("/") if p]
    encoded_uri = "file:///" + "/".join(quoted_parts)
    plain_uri = "file:///" + rest
    return encoded_uri, plain_uri


def uri_to_local_path(uri: str) -> str:
    """
    Converts an Antigravity URI (e.g. `file:///d%3A/Transfer-Antigravity` or `file:///d:/...`)
    back to a human-readable OS path (e.g. `d:\\Transfer-Antigravity` on Windows).
    """
    if not uri:
        return ""
    cleaned = uri.strip()
    if cleaned.startswith("file:///"):
        cleaned = cleaned[len("file:///"):]
    elif cleaned.startswith("file://"):
        cleaned = cleaned[len("file://"):]

    unquoted = urllib.parse.unquote(cleaned)
    if len(unquoted) >= 2 and unquoted[1] == ":":
        # Windows path
        return unquoted.replace("/", "\\")
    return "/" + unquoted.lstrip("/")


def build_path_replacement_pairs(
    folder_mappings: Sequence[Tuple[str, str]],
    old_user_home: Optional[str] = None,
    new_user_home: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """
    Generates a comprehensive list of `(old_str, new_str)` replacement pairs
    sorted by length in descending order (ensuring longer/more specific paths are matched first).

    Includes:
    - Encoded URI variants (`file:///d%3A/...`, `file:///D%3A/...`)
    - Plain URI variants (`file:///d:/...`, `file:///D:/...`)
    - Windows backslash (`d:\\...`, `D:\\...`) and JSON-escaped (`d:\\\\...`)
    - Forward-slash variants (`d:/...`, `D:/...`)
    - User profile `.gemini` paths when migrating across different usernames (`C:\\Users\\OldUser` -> `C:\\Users\\NewUser`)
    """
    pairs_dict: Dict[str, str] = {}

    def _add_path_variants(src_path: str, dst_path: str) -> None:
        src_clean = src_path.strip().rstrip("\\/")
        dst_clean = dst_path.strip().rstrip("\\/")
        if not src_clean or not dst_clean or src_clean.lower() == dst_clean.lower():
            if src_clean == dst_clean:
                return

        src_enc, src_plain = path_to_antigravity_uris(src_clean)
        dst_enc, dst_plain = path_to_antigravity_uris(dst_clean)

        pairs_dict[src_enc] = dst_enc
        pairs_dict[src_plain] = dst_plain

        # Uppercase drive letter URI variants (e.g. file:///D%3A/... and file:///D:/...)
        if len(src_clean) >= 2 and src_clean[1] == ":":
            src_drive_up = src_clean[0].upper()
            src_drive_lo = src_clean[0].lower()
            pairs_dict[src_enc.replace(f"file:///{src_drive_lo}%3A", f"file:///{src_drive_up}%3A", 1)] = dst_enc
            pairs_dict[src_enc.replace(f"file:///{src_drive_lo}%3A", f"file:///{src_drive_up}%3a", 1)] = dst_enc
            pairs_dict[src_plain.replace(f"file:///{src_drive_lo}:", f"file:///{src_drive_up}:", 1)] = dst_plain

            # OS filesystem path variants (both lowercase and uppercase drive letters)
            src_rest_bs = src_clean[2:].replace("/", "\\")
            src_rest_fs = src_clean[2:].replace("\\", "/")
            dst_bs = dst_clean.replace("/", "\\")
            dst_fs = dst_clean.replace("\\", "/")

            for d_char in (src_drive_lo, src_drive_up):
                s_bs = f"{d_char}:{src_rest_bs}"
                s_fs = f"{d_char}:{src_rest_fs}"
                pairs_dict[s_bs] = dst_bs
                pairs_dict[s_bs.replace("\\", "\\\\")] = dst_bs.replace("\\", "\\\\")
                pairs_dict[s_fs] = dst_fs
        else:
            src_bs = src_clean.replace("/", "\\")
            src_fs = src_clean.replace("\\", "/")
            dst_bs = dst_clean.replace("/", "\\")
            dst_fs = dst_clean.replace("\\", "/")
            pairs_dict[src_bs] = dst_bs
            pairs_dict[src_bs.replace("\\", "\\\\")] = dst_bs.replace("\\", "\\\\")
            pairs_dict[src_fs] = dst_fs

    # 1. Project workspace folder mappings
    for old_folder, new_folder in folder_mappings:
        if old_folder and new_folder:
            _add_path_variants(old_folder, new_folder)

    # 2. User home mappings (.gemini) if usernames differ across machines
    if old_user_home and new_user_home:
        old_gemini = os.path.join(old_user_home.rstrip("\\/"), ".gemini")
        new_gemini = os.path.join(new_user_home.rstrip("\\/"), ".gemini")
        _add_path_variants(old_gemini, new_gemini)

    # Filter out identity mappings and sort by length descending
    valid_pairs = [(k, v) for k, v in pairs_dict.items() if k and k != v]
    valid_pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return valid_pairs


# ==============================================================================
# Parse & Serialize `agyhub_summaries_proto.pb` (Map<conversation_id, raw_summary>)
# ==============================================================================


def parse_agyhub_summaries_pb(data: bytes) -> Dict[str, bytes]:
    """
    Parses Antigravity's `agyhub_summaries_proto.pb`.
    Wire format structure:
      Repeated Field 1 (tag 0x0a, wire_type 2): MapEntry
        - Inner Field 1 (tag 0x0a, wire_type 2): key = conversation_id (UTF-8 string)
        - Inner Field 2 (tag 0x12, wire_type 2): value = raw_summary (Protobuf bytes)
    Returns dict `{conversation_id: raw_summary_bytes}`.
    """
    entries: Dict[str, bytes] = {}
    if not data:
        return entries

    pos = 0
    length = len(data)
    while pos < length:
        tag, pos = decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type != WIRE_LEN_DELIM:
            break
        entry_len, pos = decode_varint(data, pos)
        entry_end = pos + entry_len
        if entry_end > length:
            break
        entry_bytes = data[pos:entry_end]
        pos = entry_end

        if field_num != 1:
            continue

        # Decode MapEntry (field 1 = conv_id, field 2 = raw_summary)
        epos = 0
        conv_id: Optional[str] = None
        raw_sum: Optional[bytes] = None
        while epos < len(entry_bytes):
            etag, epos = decode_varint(entry_bytes, epos)
            efield = etag >> 3
            ewire = etag & 0x07
            if ewire == WIRE_LEN_DELIM:
                elen, epos = decode_varint(entry_bytes, epos)
                eval_bytes = entry_bytes[epos : epos + elen]
                epos += elen
                if efield == 1:
                    conv_id = eval_bytes.decode("utf-8", errors="replace")
                elif efield == 2:
                    raw_sum = eval_bytes
            elif ewire == WIRE_VARINT:
                _, epos = decode_varint(entry_bytes, epos)
            elif ewire == WIRE_FIXED64:
                epos += 8
            elif ewire == WIRE_FIXED32:
                epos += 4
            else:
                break

        if conv_id and raw_sum is not None:
            entries[conv_id] = raw_sum

    return entries


def serialize_agyhub_summaries_pb(entries: Dict[str, bytes]) -> bytes:
    """
    Serializes a dict `{conversation_id: raw_summary_bytes}` back into the wire format
    expected by `agyhub_summaries_proto.pb`.
    """
    out = bytearray()
    for conv_id, raw_sum in entries.items():
        if not conv_id or raw_sum is None:
            continue
        cid_bytes = conv_id.encode("utf-8")
        # Inner field 1 (tag = (1 << 3) | 2 = 0x0a)
        entry_buf = bytearray()
        entry_buf.extend(encode_varint((1 << 3) | WIRE_LEN_DELIM))
        entry_buf.extend(encode_varint(len(cid_bytes)))
        entry_buf.extend(cid_bytes)
        # Inner field 2 (tag = (2 << 3) | 2 = 0x12)
        entry_buf.extend(encode_varint((2 << 3) | WIRE_LEN_DELIM))
        entry_buf.extend(encode_varint(len(raw_sum)))
        entry_buf.extend(raw_sum)

        # Outer field 1 (tag = 0x0a)
        out.extend(encode_varint((1 << 3) | WIRE_LEN_DELIM))
        out.extend(encode_varint(len(entry_buf)))
        out.extend(entry_buf)
    return bytes(out)
