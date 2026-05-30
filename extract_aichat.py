from __future__ import annotations

import base64
import argparse
import hashlib
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
import html
import json
import re
import tempfile
from dataclasses import dataclass, field
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator
from xml.etree import ElementTree as ET


def windows_roaming_appdata() -> Path:
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata)
        return Path.home() / "AppData" / "Roaming"
    return Path.home()


def windows_local_appdata() -> Path:
    if platform.system() == "Windows":
        appdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            return Path(appdata)
        return Path.home() / "AppData" / "Local"
    return Path.home()


def is_wsl() -> bool:
    if platform.system() != "Linux":
        return False

    release = platform.release().lower()
    return (
        "microsoft" in release
        or "wsl" in release
        or "WSL_DISTRO_NAME" in os.environ
        or "WSL_INTEROP" in os.environ
        or "WSLENV" in os.environ
    )


def windows_appdata_via_cmd() -> Path | None:
    if not is_wsl():
        return None

    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo", "%APPDATA%"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    appdata = result.stdout.strip()
    if not appdata or "%" in appdata:
        return None

    match = re.match(r"^([A-Za-z]):[\\/](.*)$", appdata)
    if not match:
        return None

    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/")
    return Path(f"/mnt/{drive}/{rest}")


def get_windows_home_from_wsl() -> Path | None:
    if not is_wsl():
        return None

    appdata = windows_appdata_via_cmd()
    if appdata is not None:
        home = appdata.parent.parent
        if home.exists():
            return home

    win_user = os.environ.get("USER")
    if not win_user:
        return None

    candidate = Path("/mnt/c/Users") / win_user
    return candidate if candidate.exists() else None


def get_windows_appdata_from_wsl() -> Path | None:
    if not is_wsl():
        return None

    appdata = windows_appdata_via_cmd()
    if appdata is not None and appdata.exists():
        return appdata

    win_home = get_windows_home_from_wsl()
    if not win_home:
        return None

    appdata = win_home / "AppData" / "Roaming"
    return appdata if appdata.exists() else None


def get_windows_local_appdata_from_wsl() -> Path | None:
    if not is_wsl():
        return None

    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo", "%LOCALAPPDATA%"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        result = None

    if result is not None:
        local_appdata = result.stdout.strip()
        if local_appdata and "%" not in local_appdata:
            match = re.match(r"^([A-Za-z]):[\\/](.*)$", local_appdata)
            if match:
                drive = match.group(1).lower()
                rest = match.group(2).replace("\\", "/")
                candidate = Path(f"/mnt/{drive}/{rest}")
                if candidate.exists():
                    return candidate

    win_home = get_windows_home_from_wsl()
    if not win_home:
        return None

    local_appdata = win_home / "AppData" / "Local"
    return local_appdata if local_appdata.exists() else None


def jetbrains_workspace_globs() -> list[Path]:
    home = Path.home()
    system = platform.system()
    globs: list[Path] = []

    if system == "Windows":
        return [windows_roaming_appdata() / "JetBrains" / "*" / "workspace" / "*.xml"]

    if system == "Linux":
        globs.extend(
            [
            home / ".config" / "JetBrains" / "*" / "workspace" / "*.xml",
            home / ".*" / "config" / "workspace" / "*.xml",
            ]
        )
        if is_wsl():
            win_appdata = get_windows_appdata_from_wsl()
            if win_appdata is not None:
                globs.append(win_appdata / "JetBrains" / "*" / "workspace" / "*.xml")
        return globs

    if system == "Darwin":
        return [
            home / "Library" / "Application Support" / "JetBrains" / "*" / "workspace" / "*.xml",
            home / "Library" / "Preferences" / "*" / "workspace" / "*.xml",
        ]

    return []


def jetbrains_log_dir(ide_name: str) -> Path | None:
    home = Path.home()
    system = platform.system()

    if system == "Windows":
        return windows_local_appdata() / "JetBrains" / ide_name / "log"

    if system == "Linux":
        if is_wsl():
            win_local_appdata = get_windows_local_appdata_from_wsl()
            if win_local_appdata is not None:
                return win_local_appdata / "JetBrains" / ide_name / "log"
        return home / ".cache" / "JetBrains" / ide_name / "log"

    if system == "Darwin":
        return home / "Library" / "Logs" / "JetBrains" / ide_name

    return None


def jetbrains_ide_log_files(ide_name: str) -> list[Path]:
    log_dir = jetbrains_log_dir(ide_name)
    if log_dir is None or not log_dir.is_dir():
        return []

    files = [path for path in sorted(log_dir.glob("idea*.log")) if path.is_file()]
    acp_log = log_dir / "acp" / "acp.log"
    if acp_log.is_file():
        files.append(acp_log)
    return files


def default_workspace_input() -> Path:
    globs = jetbrains_workspace_globs()
    if globs:
        return globs[0]
    return Path("**/.idea/workspace.xml")


DEFAULT_INPUT = default_workspace_input()
DEFAULT_OUTPUT_DIR = Path(r"C:\tmp\aichat") if platform.system() == "Windows" else Path("/tmp/aichat")
CHAT_SESSION_MARKER = '<component name="ChatSessionStateTemp">'
SESSION_UID_PREFIX = "Chat UID: `"
IDE_CACHE_FILENAME = ".aichat_export_cache.json"
IDE_CACHE_VERSION = 1
FLAT_MODEL_OUTPUT_KEY = "__flat__"
EVENT_PROMPT_TYPE = "com.intellij.ml.llm.chat.shared.ChatSessionUserPromptEvent"
EVENT_MESSAGE_BLOCK_TYPE = "com.intellij.ml.llm.chat.shared.ChatSessionMessageBlockEvent"
EVENTS_FILE_SUFFIX = ".events"
MARKDOWN_SUFFIX = ".md"
DEFAULT_FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S - "
WORKSPACE_SCAN_CHUNK_SIZE = 1024 * 1024
MARKDOWN_UID_SCAN_SIZE = 1024
SESSION_UID_RE = re.compile(
    r"(?:Session|Chat) UID:\s*`?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})`?",
    re.IGNORECASE,
)
EVENT_FILENAME_UID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)
LOG_ENTRY_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[\d+\]\s+")
MATTERHORN_LOG_RE = re.compile(r"(?:\bc\.i\.m\.l\.|\bcom\.intellij\.ml\.llm\.matterhorn\b)", re.IGNORECASE)
LOG_ENTRY_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[\d+\]\s+")
FILENAME_SAFE_RE = re.compile(r"[^\w .()-]+", re.UNICODE)
IDE_VERSION_SUFFIX_RE = re.compile(r"[0-9.]+$")
WINDOWS_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def ide_name_from_workspace_path(path: Path) -> str:
    if path.parent.name == ".idea":
        return path.parent.parent.name
    if path.parent.name == "workspace" and path.parent.parent.name == "config":
        return path.parent.parent.parent.name
    if path.parent.name == "workspace":
        return path.parent.parent.name
    return path.parent.name


def ide_output_name(ide_name: str, strip_versions: bool) -> str:
    if strip_versions:
        ide_name = IDE_VERSION_SUFFIX_RE.sub("", ide_name).rstrip()
    return ide_name


def workspace_name_from_workspace_path(path: Path) -> str:
    return sanitize_path_component(path.stem or path.name)


def ide_import_root_from_workspace_path(path: Path) -> Path:
    if path.parent.name == ".idea":
        return path.parent.parent.resolve()
    if path.parent.name == "workspace":
        return path.parent.parent.resolve()
    return path.parent.resolve()


def iter_jetbrains_ide_dirs() -> Iterator[Path]:
    seen: set[Path] = set()
    system = platform.system()
    home = Path.home()

    def add_root(root: Path) -> Iterator[Path]:
        if not root.is_dir():
            return
        for ide_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            resolved = ide_dir.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield ide_dir

    if system == "Windows":
        yield from add_root(windows_roaming_appdata() / "JetBrains")
        return

    if system == "Linux":
        yield from add_root(home / ".config" / "JetBrains")
        for hidden_dir in sorted(p for p in home.iterdir() if p.is_dir() and p.name.startswith(".")):
            if (hidden_dir / "config" / "workspace").is_dir() or (hidden_dir / "aia-task-history").is_dir():
                resolved = hidden_dir.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield hidden_dir
        if is_wsl():
            win_appdata = get_windows_appdata_from_wsl()
            if win_appdata is not None:
                yield from add_root(win_appdata / "JetBrains")
        return

    if system == "Darwin":
        yield from add_root(home / "Library" / "Application Support" / "JetBrains")
        yield from add_root(home / "Library" / "Preferences")


def iter_workspace_files_from_ide_dir(ide_dir: Path) -> Iterator[Path]:
    workspace_dir = ide_dir / "workspace"
    if not workspace_dir.is_dir():
        workspace_dir = ide_dir / "config" / "workspace"
    if not workspace_dir.is_dir():
        return

    for xml_path in sorted(workspace_dir.glob("*.xml")):
        if xml_path.is_file():
            yield xml_path


@dataclass(frozen=True)
class ChatMessage:
    uid: str | None
    author: str
    display_content: str
    internal_content: str | None


@dataclass(frozen=True)
class ChatSession:
    title: str
    model_id: str
    uid: str | None
    timestamp_ms: int | None
    modified_at_ms: int | None
    source_action_type: str | None
    messages: list[ChatMessage]


@dataclass(frozen=True)
class RecoveredTurn:
    prompt: str
    blocks: list[str]

    def to_markdown(self) -> str:
        if not self.blocks:
            return ""
        lines: list[str] = []
        for block in self.blocks:
            block_lines = block.splitlines() or [""]
            lines.append(f"- {block_lines[0]}")
            for continuation in block_lines[1:]:
                lines.append(f"  {continuation}" if continuation else "  ")
        return "\n".join(lines)


@dataclass
class IdeCache:
    cache_root: Path
    model_output_uids: dict[str, dict[str, str | None]]
    prompt_to_events: dict[str, str]
    dirty: bool = False

    def model_index(self, model_component: str, flat_model_output: bool = False) -> dict[str, str | None]:
        if flat_model_output:
            model_component = FLAT_MODEL_OUTPUT_KEY
        return self.model_output_uids.setdefault(model_component, {})


@dataclass(frozen=True)
class OutputPlan:
    output_path: Path
    rename_source: Path | None


@dataclass(frozen=True)
class DebugLogSection:
    filename: str
    lines: list[str]


@dataclass(frozen=True)
class LogEntry:
    start_line: int
    timestamp: datetime | None
    lines: list[str]

    def text(self) -> str:
        return "\n".join(self.lines)


def parse_log_entry_timestamp(line: str) -> datetime | None:
    if len(line) < 23:
        return None
    stamp = line[:23]
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def log_entry_body(line: str) -> str:
    return LOG_ENTRY_PREFIX_RE.sub("", line, count=1).strip()


def log_entry_mentions_matterhorn(entry: LogEntry) -> bool:
    return any(MATTERHORN_LOG_RE.search(line) is not None for line in entry.lines)


def summarize_log_entry(entry: LogEntry) -> str:
    parts: list[str] = []
    for index, line in enumerate(entry.lines):
        body = line.strip()
        if not body:
            continue
        parts.append(body)
    return " | ".join(parts)


def log_entry_signature(entry: LogEntry) -> tuple[str, tuple[str, ...]]:
    first_line = log_entry_body(entry.lines[0]) if entry.lines else ""
    continuation_lines = tuple(line.strip() for line in entry.lines[1:] if line.strip())
    return first_line, continuation_lines


@dataclass(frozen=True)
class ExportJob:
    input_path: Path
    ide_name: str
    session: ChatSession
    recovered_turns: list[RecoveredTurn]
    debug_event_records_path: Path | None
    debug_log_sections: list[DebugLogSection]
    prompt_uid: str | None
    output_path: Path
    rename_source: Path | None
    existing_uids: dict[str, str | None]


@dataclass
class TaskHistoryIndex:
    candidate_paths: list[Path]
    uid_to_path: dict[str, Path]
    linked_paths: set[Path]
    debug_dir: Path | None
    dumped_paths: set[Path] = field(default_factory=set)


def verbose_print(verbose: bool, indent: int, message: str) -> None:
    if verbose:
        print(f"{' ' * indent}{message}", flush=True)


def iter_input_files(paths: list[Path]) -> Iterator[tuple[Path, str]]:
    seen: set[Path] = set()
    for path in paths:
        if path.is_dir():
            for xml_path in sorted(path.rglob("*.xml")):
                if xml_path not in seen:
                    seen.add(xml_path)
                    yield xml_path, ide_name_from_workspace_path(xml_path)
        elif path.is_file():
            if path not in seen:
                seen.add(path)
                yield path, ide_name_from_workspace_path(path)
        else:
            raise FileNotFoundError(path)


def explicit_ide_root_from_input(path: Path) -> Path | None:
    resolved = path.resolve()
    if resolved.is_file():
        if resolved.parent.name == "workspace" and resolved.parent.parent.name == "config":
            return resolved.parent.parent.parent.resolve()
        if resolved.parent.name == "workspace":
            return resolved.parent.parent.resolve()
        if resolved.parent.name == ".idea":
            return resolved.parent.parent.resolve()
        return resolved.parent.resolve()

    if not resolved.is_dir():
        return None

    if resolved.name == "workspace":
        if resolved.parent.name == "config":
            return resolved.parent.parent.resolve()
        return resolved.parent.resolve()

    if resolved.name == ".idea":
        return resolved.parent.resolve()

    if resolved.name == "config" and (resolved / "workspace").is_dir():
        return resolved.parent.resolve()

    if (resolved / "workspace").is_dir():
        return resolved.resolve()
    if (resolved / "config" / "workspace").is_dir():
        return resolved.resolve()
    if (resolved / ".idea").is_dir():
        return resolved.resolve()

    return resolved.resolve()


def should_flatten_output(paths: list[Path], keep_ide_dirs: bool) -> bool:
    if keep_ide_dirs or not paths:
        return False

    roots: set[Path] = set()
    for path in paths:
        if not path.exists():
            return False
        root = explicit_ide_root_from_input(path)
        if root is None:
            return False
        roots.add(root)

    return len(roots) == 1


def workspace_file_has_chat_marker(xml_path: Path) -> bool:
    marker = CHAT_SESSION_MARKER.encode("utf-8")
    carry = b""

    try:
        with xml_path.open("rb") as fh:
            while True:
                chunk = fh.read(WORKSPACE_SCAN_CHUNK_SIZE)
                if not chunk:
                    return False
                data = carry + chunk
                if marker in data:
                    return True
                if len(marker) > 1:
                    carry = data[-(len(marker) - 1) :]
                else:
                    carry = b""
    except OSError:
        return False


def debug_event_records_output_path(debug_dir: Path, source_path: Path) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in " .()-_" else "_" for ch in source_path.name)
    digest = hashlib.blake2s(str(source_path).encode("utf-8"), digest_size=4).hexdigest()
    return debug_dir / f"{safe_name}.{digest}.decoded.jsonl"


def build_task_history_index(
    task_history_root: Path | None,
    verbose: bool = False,
    debug_dir: Path | None = None,
) -> TaskHistoryIndex:
    verbose_print(verbose, 4, f"start build_task_history_index: {task_history_root or 'auto'}")
    candidate_paths: list[Path] = []
    uid_to_path: dict[str, Path] = {}

    try:
        for root in iter_task_history_roots(task_history_root):
            for candidate in sorted(root.glob(f"*{EVENTS_FILE_SUFFIX}")):
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                candidate_paths.append(resolved)
                match = EVENT_FILENAME_UID_RE.search(candidate.name)
                if match:
                    uid = match.group(1).lower()
                    uid_to_path.setdefault(uid, resolved)

        linked_paths = set(uid_to_path.values())
        return TaskHistoryIndex(
            candidate_paths=candidate_paths,
            uid_to_path=uid_to_path,
            linked_paths=linked_paths,
            debug_dir=debug_dir,
        )
    finally:
        verbose_print(
            verbose,
            4,
            f"end build_task_history_index: candidates={len(candidate_paths)} linked={len(uid_to_path)}",
        )


def iter_default_workspace_files(verbose: bool = False) -> Iterator[tuple[Path, str]]:
    verbose_print(verbose, 4, "start scanning workspaces: JetBrains roots")
    seen: set[Path] = set()
    try:
        for ide_dir in iter_jetbrains_ide_dirs():
            for xml_path in iter_workspace_files_from_ide_dir(ide_dir):
                resolved = xml_path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                if workspace_file_has_chat_marker(xml_path):
                    yield xml_path, ide_name_from_workspace_path(xml_path)

        for xml_path in sorted(Path.cwd().glob("**/.idea/workspace.xml")):
            resolved = xml_path.resolve()
            if resolved in seen or not xml_path.is_file():
                continue
            seen.add(resolved)
            if workspace_file_has_chat_marker(xml_path):
                yield xml_path, ide_name_from_workspace_path(xml_path)
    finally:
        verbose_print(verbose, 4, "end scanning workspaces: JetBrains roots")


def get_option_value(node: ET.Element, option_name: str) -> str | None:
    for option in node.findall("./option"):
        if option.get("name") == option_name:
            if "value" in option.attrib:
                return option.get("value")
            return (option.text or "").strip() or None
    return None


def extract_chat_sessions(xml_path: Path, verbose: bool = False) -> list[ChatSession]:
    sessions: list[ChatSession] = []
    tree = ET.parse(xml_path)
    root = tree.getroot()

    for chat_node in root.findall(".//SerializedChat"):
        title_node = chat_node.find("./option[@name='title']/SerializedChatTitle")
        title = "Untitled Chat"
        if title_node is not None:
            extracted = get_option_value(title_node, "text")
            if extracted:
                title = extracted

        model_id = get_option_value(chat_node, "chatModelId") or "unknown-model"
        uid = get_option_value(chat_node, "uid")
        timestamp_ms = None
        modified_at_ms = None
        source_action_type = None
        modified_at_node = chat_node.find("./option[@name='modifiedAt']")
        if modified_at_node is not None:
            modified_at_raw = get_option_value(modified_at_node, "modifiedAt")
            if modified_at_raw is None:
                modified_at_raw = modified_at_node.get("value")
            if modified_at_raw and modified_at_raw.isdigit():
                modified_at_ms = int(modified_at_raw)
        statistic_node = chat_node.find("./option[@name='statisticInformation']/ChatStatisticInformation")
        if statistic_node is not None:
            timestamp_raw = get_option_value(statistic_node, "timestamp")
            if timestamp_raw and timestamp_raw.isdigit():
                timestamp_ms = int(timestamp_raw)
            source_action_type = get_option_value(statistic_node, "sourceActionType")
        messages: list[ChatMessage] = []

        messages_parent = chat_node.find("./option[@name='messages']/list")
        if messages_parent is not None:
            for msg_node in messages_parent.findall("./SerializedChatMessage"):
                message_uid = normalize_uuid_value(get_option_value(msg_node, "uid"))
                author = get_option_value(msg_node, "author") or "User"
                display_content = get_option_value(msg_node, "displayContent") or ""
                internal_content = get_option_value(msg_node, "internalContent")
                messages.append(
                    ChatMessage(
                        uid=message_uid,
                        author=author,
                        display_content=display_content,
                        internal_content=internal_content,
                    )
                )

        sessions.append(
            ChatSession(
                title=title,
                model_id=model_id,
                uid=uid,
                timestamp_ms=timestamp_ms,
                modified_at_ms=modified_at_ms,
                source_action_type=source_action_type,
                messages=messages,
            )
        )

    return sessions


def decode_event_records(
    path: Path,
    verbose: bool = False,
    debug_index: TaskHistoryIndex | None = None,
) -> Iterator[dict]:
    verbose_print(verbose, 6, f"start decode_event_records: {path}")
    count = 0
    records: list[dict] = []
    try:
        data = path.read_bytes().splitlines()
        if not data:
            return
        start = 1 if data[0] == b"AUI_EVENTS_V1" else 0
        for line in data[start:]:
            if not line.strip():
                continue
            try:
                record = json.loads(base64.b64decode(line))
            except Exception:
                continue
            count += 1
            records.append(record)
            yield record
    finally:
        if debug_index is not None and debug_index.debug_dir is not None:
            resolved = path.resolve()
            if resolved not in debug_index.dumped_paths:
                debug_index.dumped_paths.add(resolved)
                debug_index.debug_dir.mkdir(parents=True, exist_ok=True)
                debug_path = debug_event_records_output_path(debug_index.debug_dir, resolved)
                try:
                    with debug_path.open("w", encoding="utf-8") as fh:
                        fh.write(f"# source: {resolved}\n")
                        for record in records:
                            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                            fh.write("\n")
                except OSError:
                    pass
        verbose_print(verbose, 6, f"end decode_event_records: {path} records={count}")


def cache_root_for_ide(
    output_dir: Path,
    ide_name: str,
    flatten_ide_output: bool,
    strip_ide_versions: bool,
) -> Path:
    if flatten_ide_output:
        return output_dir
    return output_dir / sanitize_path_component(ide_output_name(ide_name, strip_ide_versions))


def cache_root_for_workspace(
    output_dir: Path,
    ide_name: str,
    workspace_path: Path,
    flatten_ide_output: bool,
    per_workspace_output: bool,
    no_ide_subdir: bool,
    strip_ide_versions: bool,
) -> Path:
    if no_ide_subdir:
        cache_root = output_dir
    else:
        cache_root = cache_root_for_ide(output_dir, ide_name, flatten_ide_output, strip_ide_versions)
    if per_workspace_output:
        cache_root = cache_root / workspace_name_from_workspace_path(workspace_path)
    return cache_root


def load_ide_cache(cache_root: Path, use_disk_cache: bool = True) -> IdeCache:
    cache_path = cache_root / IDE_CACHE_FILENAME
    model_output_uids: dict[str, dict[str, str | None]] = {}
    prompt_to_events: dict[str, str] = {}

    if use_disk_cache and cache_path.is_file():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict) and raw.get("version") == IDE_CACHE_VERSION:
            raw_models = raw.get("model_output_uids")
            if isinstance(raw_models, dict):
                for model_name, model_index in raw_models.items():
                    if not isinstance(model_name, str) or not isinstance(model_index, dict):
                        continue
                    cleaned: dict[str, str | None] = {}
                    for filename, session_uid in model_index.items():
                        if isinstance(filename, str):
                            cleaned[filename] = session_uid if isinstance(session_uid, str) or session_uid is None else None
                    model_output_uids[model_name] = cleaned
            raw_prompts = raw.get("prompt_to_events")
            if isinstance(raw_prompts, dict):
                for prompt, events_path in raw_prompts.items():
                    if isinstance(prompt, str) and isinstance(events_path, str):
                        prompt_to_events[prompt] = events_path

    return IdeCache(cache_root=cache_root, model_output_uids=model_output_uids, prompt_to_events=prompt_to_events)


def save_ide_cache(cache: IdeCache, use_disk_cache: bool = True) -> None:
    if not use_disk_cache or not cache.dirty:
        return

    cache.cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = cache.cache_root / IDE_CACHE_FILENAME
    payload = {
        "version": IDE_CACHE_VERSION,
        "model_output_uids": cache.model_output_uids,
        "prompt_to_events": cache.prompt_to_events,
    }

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=cache.cache_root, prefix=".aichat_export.", suffix=".tmp") as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write("\n")
        temp_name = tmp.name

    Path(temp_name).replace(cache_path)
    cache.dirty = False


def prime_model_uid_indexes(cache: IdeCache, cache_root: Path, verbose: bool = False, flat_model_output: bool = False) -> None:
    updated = False
    verbose_print(verbose, 4, f"start scanning existing .md files: {cache_root}")

    try:
        if not cache_root.is_dir():
            if cache.model_output_uids:
                cache.model_output_uids.clear()
                cache.dirty = True
            return

        if flat_model_output:
            flat_index = cache.model_output_uids.setdefault(FLAT_MODEL_OUTPUT_KEY, {})
            if any(key != FLAT_MODEL_OUTPUT_KEY for key in cache.model_output_uids):
                cache.model_output_uids = {FLAT_MODEL_OUTPUT_KEY: flat_index}
                updated = True

            disk_names: set[str] = set()
            scanned = 0
            added = 0
            removed = 0
            verbose_print(verbose, 4, f"start reading existing .md files: {cache_root}")

            for md_path in cache_root.glob(f"*{MARKDOWN_SUFFIX}"):
                if not md_path.is_file():
                    continue
                scanned += 1
                disk_names.add(md_path.name)
                if md_path.name in flat_index:
                    continue
                uid = read_session_uid_from_markdown(md_path)
                flat_index[md_path.name] = uid
                added += 1
                updated = True

            missing_names = [name for name in flat_index if name not in disk_names]
            if missing_names:
                for name in missing_names:
                    del flat_index[name]
                removed = len(missing_names)
                updated = True

            verbose_print(
                verbose,
                4,
                f"end reading existing .md files: {cache_root} scanned={scanned} added={added} removed={removed}",
            )
        else:
            if FLAT_MODEL_OUTPUT_KEY in cache.model_output_uids:
                del cache.model_output_uids[FLAT_MODEL_OUTPUT_KEY]
                updated = True

            for model_dir in sorted(path for path in cache_root.iterdir() if path.is_dir()):
                model_component = model_dir.name
                model_index = cache.model_output_uids.setdefault(model_component, {})
                disk_names: set[str] = set()
                scanned = 0
                added = 0
                removed = 0
                verbose_print(verbose, 4, f"start reading existing .md files: {model_dir}")

                for md_path in model_dir.glob(f"*{MARKDOWN_SUFFIX}"):
                    if not md_path.is_file():
                        continue
                    scanned += 1
                    disk_names.add(md_path.name)
                    if md_path.name in model_index:
                        continue
                    uid = read_session_uid_from_markdown(md_path)
                    model_index[md_path.name] = uid
                    added += 1
                    updated = True

                missing_names = [name for name in model_index if name not in disk_names]
                if missing_names:
                    for name in missing_names:
                        del model_index[name]
                    removed = len(missing_names)
                    updated = True

                verbose_print(
                    verbose,
                    4,
                    f"end reading existing .md files: {model_dir} scanned={scanned} added={added} removed={removed}",
                )

            for model_component in list(cache.model_output_uids):
                model_dir = cache_root / model_component
                if model_dir.is_dir():
                    continue
                if cache.model_output_uids[model_component]:
                    cache.model_output_uids[model_component] = {}
                    updated = True

        if updated:
            cache.dirty = True
    finally:
        verbose_print(verbose, 4, f"end scanning existing .md files: {cache_root}")


def resolve_git_executable(git_bin: str | None, start_cwd: Path) -> str:
    if not git_bin:
        return "git"

    candidate = Path(git_bin)
    if candidate.is_absolute():
        resolved = candidate
    elif any(sep in git_bin for sep in (os.sep, os.altsep) if sep) or git_bin.startswith("."):
        resolved = (start_cwd / candidate).resolve()
    else:
        return git_bin

    if resolved.is_dir():
        return str(resolved / ("git.exe" if platform.system() == "Windows" else "git"))

    return str(resolved)


def run_git(args: list[str], cwd: Path, git_executable: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git_executable, *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_root(start: Path, git_executable: str) -> Path | None:
    result = run_git(["rev-parse", "--show-toplevel"], start, git_executable)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def tracked_paths(repo: Path, paths: Iterable[Path], git_executable: str) -> set[Path]:
    rels: list[str] = []
    for path in paths:
        try:
            rels.append(str(path.resolve().relative_to(repo)))
        except ValueError:
            pass

    if not rels:
        return set()

    result = subprocess.run(
        [git_executable, "ls-files", "-z", "--", *rels],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="ignore").strip())

    return {repo / Path(item.decode("utf-8", errors="ignore")) for item in result.stdout.split(b"\0") if item}


def rename_many(rename_pairs: Iterable[tuple[Path, Path]], cwd: Path, git_executable: str, use_git: bool) -> None:
    pairs: list[tuple[Path, Path]] = [(Path(src).resolve(), Path(dst).resolve()) for src, dst in rename_pairs]
    if not pairs:
        return

    if use_git:
        repo = git_root(cwd, git_executable)
        if repo is None:
            raise RuntimeError(f"{cwd} is not inside a git repository")

        tracked = tracked_paths(cwd, (src for src, _ in pairs), git_executable)
        for src, dst in pairs:
            if not src.exists():
                raise FileNotFoundError(src)

            dst.parent.mkdir(parents=True, exist_ok=True)

            try:
                src_rel = src.relative_to(cwd)
                dst_rel = dst.relative_to(cwd)
            except ValueError:
                src.rename(dst)
                continue

            if src in tracked:
                result = run_git(["mv", "--", str(src_rel), str(dst_rel)], cwd, git_executable)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip())
            else:
                src.rename(dst)
            if not dst.exists():
                raise RuntimeError(f"rename completed but destination is missing: {dst}")
        return

    for src, dst in pairs:
        if not src.exists():
            raise FileNotFoundError(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        if not dst.exists():
            raise RuntimeError(f"rename completed but destination is missing: {dst}")


def find_matching_task_history_file(
    cache: IdeCache,
    task_history_index: TaskHistoryIndex,
    task_history_root: Path | None,
    prompt: str,
    session_uid: str | None,
    verbose: bool = False,
) -> Path | None:
    verbose_print(verbose, 4, f"start find_matching_task_history_file: {prompt!r}")
    if session_uid is not None:
        direct_path = task_history_index.uid_to_path.get(session_uid)
        if direct_path is not None and direct_path.is_file():
            if prompt not in cache.prompt_to_events:
                cache.prompt_to_events[prompt] = str(direct_path)
                cache.dirty = True
            verbose_print(verbose, 4, f"end find_matching_task_history_file: {prompt!r} -> {direct_path}")
            return direct_path

    cached = cache.prompt_to_events.get(prompt)
    result: Path | None = None
    try:
        if cached:
            cached_path = Path(cached)
            if cached_path.is_file():
                result = cached_path
                return result

        for candidate in task_history_index.candidate_paths:
            if candidate in task_history_index.linked_paths:
                continue
            if not candidate.is_file():
                continue
            for record in decode_event_records(candidate, verbose=verbose, debug_index=task_history_index):
                    if record.get("type") != EVENT_PROMPT_TYPE:
                        continue
                    candidate_prompt = record.get("prompt")
                    if not isinstance(candidate_prompt, str):
                        continue
                    if candidate_prompt not in cache.prompt_to_events:
                        cache.prompt_to_events[candidate_prompt] = str(candidate)
                        cache.dirty = True
                    if candidate_prompt == prompt:
                        result = candidate
                        return result
        return None
    finally:
        verbose_print(verbose, 4, f"end find_matching_task_history_file: {prompt!r} -> {result}")


def summarize_block(event: dict) -> str | None:
    kind = event.get("kind")
    if kind == "com.intellij.ml.llm.aui.events.api.TerminalBlockUpdatedEvent":
        command = event.get("command") or ""
        status = event.get("status") or ""
        details = event.get("details") or ""
        return render_terminal_block(command, status, details)

    if kind == "com.intellij.ml.llm.aui.events.api.AgentThoughtBlockUpdatedEvent":
        text = (event.get("text") or "").strip()
        return f"Thought: {text}" if text else None

    if kind == "com.intellij.ml.llm.aui.events.api.ToolBlockUpdatedEvent":
        text = (event.get("text") or "").strip()
        details = (event.get("details") or "").strip()
        if not text and not details:
            return None

        lines = ["Tool:"]
        if text:
            lines.append("```shell")
            lines.extend(text.splitlines() or [""])
            lines.append("```")
        if details:
            lines.append("```")
            lines.extend(details.splitlines() or [""])
            lines.append("```")
        return "\n".join(lines)

    if kind == "com.intellij.ml.llm.aui.events.api.ViewFilesBlockUpdatedEvent":
        files = event.get("files") or []
        paths = [item.get("relativePath") for item in files if isinstance(item, dict) and item.get("relativePath")]
        return "Viewed files: " + ", ".join(paths) if paths else None

    if kind == "com.intellij.ml.llm.aui.events.api.FileChangesBlockUpdatedEvent":
        changes = event.get("changes") or []
        paths: list[str] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("afterRelativePath") or change.get("beforeRelativePath")
            if path:
                paths.append(path)
        if paths:
            return "Changed files: " + ", ".join(paths)
        return None

    if kind == "com.intellij.ml.llm.aui.events.api.ResultBlockUpdatedEvent":
        result = (event.get("result") or "").strip()
        changes = event.get("changes") or []
        paths: list[str] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("afterRelativePath") or change.get("beforeRelativePath")
            if path:
                paths.append(path)
        parts = []
        if result:
            parts.append(result)
        if paths:
            parts.append("files: " + ", ".join(paths))
        return f"Result: {' | '.join(parts)}" if parts else None

    text = event.get("text")
    if isinstance(text, str) and text.strip():
        return f"{kind}: {text.strip()}"
    return None


def render_terminal_block(command: str, status: str, details: str) -> str:
    heading = "Terminal"
    status = (status or "").strip()
    if status and status != "COMPLETED":
        heading = f"Terminal _({status.lower()})_"

    lines = [f"{heading}:"]
    lines.append("```shell")
    lines.extend(command.splitlines() or [""])
    lines.append("```")
    if details.strip():
        lines.append(details.strip())
    return "\n".join(lines)


def build_turn_summaries(
    events_path: Path,
    verbose: bool = False,
    debug_index: TaskHistoryIndex | None = None,
) -> list[RecoveredTurn]:
    verbose_print(verbose, 4, f"start build_turn_summaries: {events_path}")
    turns: list[RecoveredTurn] = []
    current_prompt: str | None = None
    current_blocks: list[str] = []

    try:
        for record in decode_event_records(events_path, verbose=verbose, debug_index=debug_index):
            record_type = record.get("type")
            if record_type == EVENT_PROMPT_TYPE:
                if current_prompt is not None:
                    turns.append(RecoveredTurn(prompt=current_prompt, blocks=current_blocks))
                current_prompt = record.get("prompt") or ""
                current_blocks = []
                continue

            if record_type == EVENT_MESSAGE_BLOCK_TYPE:
                event = record.get("event") or {}
                summary = summarize_block(event)
                if summary:
                    current_blocks.append(summary)

        if current_prompt is not None:
            turns.append(RecoveredTurn(prompt=current_prompt, blocks=current_blocks))

        return turns
    finally:
        verbose_print(verbose, 4, f"end build_turn_summaries: {events_path} turns={len(turns)}")


def iter_task_history_roots(task_history_root: Path | None) -> Iterator[Path]:
    if task_history_root is not None:
        if task_history_root.exists():
            yield task_history_root
        return

    for ide_dir in iter_jetbrains_ide_dirs():
        candidate = ide_dir / "aia-task-history"
        if candidate.is_dir():
            yield candidate


def format_local_timestamp(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_filename_component(value: str) -> str:
    cleaned = html.unescape(value).strip()
    cleaned = FILENAME_SAFE_RE.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return "unknown"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_BASENAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def sanitize_filename(title: str) -> str:
    cleaned = sanitize_filename_component(title)
    return cleaned or "untitled-chat"


def sanitize_path_component(value: str) -> str:
    return sanitize_filename_component(value)


def preprocess_output_title(title: str) -> str:
    cleaned = html.unescape(title).strip()
    cleaned = cleaned.replace("*", "").replace("?", "")
    if cleaned.startswith("_"):
        cleaned = cleaned[1:]
        if cleaned.startswith("_"):
            cleaned = cleaned[1:]
    return cleaned


def normalize_uuid_value(value: str | None) -> str | None:
    if not value:
        return None
    match = EVENT_FILENAME_UID_RE.search(value)
    if match:
        return match.group(1).lower()
    return value.strip() or None


def extract_session_uid(text: str) -> str | None:
    match = SESSION_UID_RE.search(text)
    if match:
        return match.group(1).lower()
    return None


def read_session_uid_from_markdown(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            text = fh.read(MARKDOWN_UID_SCAN_SIZE).decode("utf-8", errors="ignore")
    except OSError:
        return None
    return extract_session_uid(text)


def format_file_date_prefix(timestamp_ms: int | None) -> str:
    stamp = format_local_timestamp(timestamp_ms)
    if not stamp:
        return ""
    return f"{stamp} - "


def build_output_stem(title: str, timestamp_ms: int | None, file_dates: bool) -> str:
    prefix = format_file_date_prefix(timestamp_ms) if file_dates else ""
    return sanitize_filename(f"{prefix}{preprocess_output_title(title)}")


def find_existing_output_path(model_dir: Path, existing_uids: dict[str, str | None], session_uid: str | None) -> Path | None:
    if session_uid is None:
        return None
    for name, existing_uid in existing_uids.items():
        if existing_uid == session_uid:
            return model_dir / name
    return None


def has_existing_output_with_uid(existing_uids: dict[str, str | None], session_uid: str | None) -> bool:
    if session_uid is None:
        return False
    return any(existing_uid == session_uid for existing_uid in existing_uids.values())


def plan_output_path(
    model_dir: Path,
    title: str,
    timestamp_ms: int | None,
    session_uid: str | None,
    ignore_existing: bool,
    used: set[Path],
    existing_uids: dict[str, str | None],
    file_dates: bool,
) -> OutputPlan | None:
    base = build_output_stem(title, timestamp_ms, file_dates)
    existing_same_uid = find_existing_output_path(model_dir, existing_uids, session_uid)
    counter = 0

    while True:
        suffix = "" if counter == 0 else f"_{counter}"
        candidate = model_dir / f"{base}{suffix}{MARKDOWN_SUFFIX}"
        if candidate in used:
            counter += 1
            continue

        if candidate.name in existing_uids:
            existing_uid = existing_uids[candidate.name]
            if existing_uid is not None and existing_uid == session_uid:
                if ignore_existing:
                    return None
                used.add(candidate)
                return OutputPlan(output_path=candidate, rename_source=None)
            counter += 1
            continue

        if existing_same_uid is not None and existing_same_uid != candidate:
            used.add(candidate)
            return OutputPlan(output_path=candidate, rename_source=existing_same_uid)

        used.add(candidate)
        return OutputPlan(output_path=candidate, rename_source=None)


def quote_block(text: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def maybe_quote(author: str, text: str) -> str:
    if author == "Assistant":
        return text
    return quote_block(text)


def first_user_prompt(session: ChatSession) -> ChatMessage | None:
    for message in session.messages:
        if message.author != "Assistant":
            return message
    return None


def user_prompts(session: ChatSession) -> list[ChatMessage]:
    return [message for message in session.messages if message.author != "Assistant"]


def prompt_text_for_matching(message: ChatMessage | None) -> str:
    if message is None:
        return ""
    if message.display_content.strip():
        return message.display_content
    if message.internal_content:
        return message.internal_content
    return ""


def session_matches_find(session: ChatSession, find_pattern: re.Pattern[str] | None) -> bool:
    if find_pattern is None:
        return True
    for message in user_prompts(session):
        prompt = prompt_text_for_matching(message)
        if prompt and find_pattern.search(prompt) is not None:
            return True
    return False


def has_assistant_content(session: ChatSession) -> bool:
    for message in session.messages:
        if message.author != "Assistant":
            continue
        if (message.display_content and message.display_content.strip()) or (
            message.internal_content and message.internal_content.strip()
        ):
            return True
    return False


def matching_log_terms(session: ChatSession) -> list[str]:
    terms: list[str] = []
    prompt_message = first_user_prompt(session)
    for message in user_prompts(session):
        prompt = prompt_text_for_matching(message)
        if prompt:
            terms.append(prompt)
    if session.uid:
        terms.append(session.uid)
    prompt_uid = prompt_message.uid if prompt_message is not None else None
    if prompt_uid:
        terms.append(prompt_uid)
    return list(dict.fromkeys(terms))


def collect_log_sections(
    ide_name: str,
    session: ChatSession,
    context_entries: int = 2,
    max_gap_seconds: int = 180,
    max_matterhorn_gap_seconds: int = 60,
) -> list[DebugLogSection]:
    log_files = jetbrains_ide_log_files(ide_name)
    if not log_files:
        return []

    terms = [term for term in matching_log_terms(session) if term]
    if not terms:
        return []

    sections: list[DebugLogSection] = []
    for log_file in log_files:
        try:
            lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue

        entries: list[LogEntry] = []
        current_entry_start = 0
        current_entry_timestamp: datetime | None = None
        current_entry_lines: list[str] = []
        for line_number, line in enumerate(lines):
            if LOG_ENTRY_START_RE.match(line) and current_entry_lines:
                entries.append(LogEntry(start_line=current_entry_start, timestamp=current_entry_timestamp, lines=current_entry_lines))
                current_entry_start = line_number
                current_entry_timestamp = parse_log_entry_timestamp(line)
                current_entry_lines = [line]
            else:
                if not current_entry_lines:
                    current_entry_start = line_number
                    current_entry_timestamp = parse_log_entry_timestamp(line)
                current_entry_lines.append(line)
        if current_entry_lines:
            entries.append(LogEntry(start_line=current_entry_start, timestamp=current_entry_timestamp, lines=current_entry_lines))

        matching_indices = [
            index
            for index, entry in enumerate(entries)
            if any(term.lower() in entry.text().lower() for term in terms)
        ]
        if not matching_indices:
            continue

        start_index = max(0, matching_indices[0] - context_entries)
        stop_index = len(entries)
        previous_timestamp = entries[start_index].timestamp
        last_matterhorn_timestamp = entries[start_index].timestamp if log_entry_mentions_matterhorn(entries[start_index]) else None

        for entry_index in range(start_index, len(entries)):
            entry = entries[entry_index]
            if entry_index > start_index and previous_timestamp is not None and entry.timestamp is not None:
                gap_seconds = (entry.timestamp - previous_timestamp).total_seconds()
                if gap_seconds > max_gap_seconds:
                    stop_index = entry_index
                    break
            if last_matterhorn_timestamp is not None and entry.timestamp is not None:
                matterhorn_gap_seconds = (entry.timestamp - last_matterhorn_timestamp).total_seconds()
                if matterhorn_gap_seconds > max_matterhorn_gap_seconds:
                    stop_index = entry_index
                    break
            previous_timestamp = entry.timestamp or previous_timestamp
            if log_entry_mentions_matterhorn(entry) and entry.timestamp is not None:
                last_matterhorn_timestamp = entry.timestamp

        rendered: list[str] = []
        previous_signature: tuple[str, tuple[str, ...]] | None = None
        previous_rendered_index: int | None = None
        previous_count = 0

        def flush_previous() -> None:
            nonlocal previous_signature, previous_rendered_index, previous_count
            if previous_signature is None or previous_rendered_index is None:
                return
            if previous_count > 1:
                rendered[previous_rendered_index] = f"{rendered[previous_rendered_index]} [x{previous_count}]"
            previous_signature = None
            previous_rendered_index = None
            previous_count = 0

        for entry in entries[start_index:stop_index]:
            signature = log_entry_signature(entry)
            summary = summarize_log_entry(entry)
            if not summary:
                continue
            if previous_signature == signature and previous_rendered_index is not None:
                previous_count += 1
                continue

            flush_previous()
            rendered.append(f"    {entry.start_line + 1}: {summary}")
            previous_signature = signature
            previous_rendered_index = len(rendered) - 1
            previous_count = 1

        flush_previous()

        sections.append(DebugLogSection(filename=log_file.name, lines=rendered))

    return sections


def recover_junie_turns(
    session: ChatSession,
    task_history_root: Path | None,
    cache: IdeCache,
    task_history_index: TaskHistoryIndex,
    verbose: bool = False,
) -> tuple[list[RecoveredTurn], Path | None]:
    if not session.model_id.startswith("agent_"):
        return [], None

    first_prompt = prompt_text_for_matching(first_user_prompt(session))
    if not first_prompt:
        return [], None

    events_path = find_matching_task_history_file(
        cache,
        task_history_index,
        task_history_root,
        first_prompt,
        session.uid,
        verbose=verbose,
    )
    if not events_path:
        return [], None

    debug_event_records_path = None
    if task_history_index.debug_dir is not None:
        debug_event_records_path = debug_event_records_output_path(task_history_index.debug_dir, events_path.resolve())

    return (
        build_turn_summaries(events_path, verbose=verbose, debug_index=task_history_index),
        debug_event_records_path,
    )


def format_message(message: ChatMessage, debug_uid: str | None = None) -> str:
    body = message.display_content
    if message.author == "Assistant" and not body.strip() and not (message.internal_content and message.internal_content.strip()):
        return ""
    heading = "Assistant did" if message.author == "Assistant" and body.startswith("- ") else f"{message.author} said"
    if debug_uid:
        heading = f"{heading} [*{debug_uid}*]"
    parts: list[str] = [f"{heading}:"]

    if message.internal_content is not None and message.internal_content != message.display_content:
        parts.append("")
        parts.append("Note: displayContent and internalContent differ.")
        parts.append("displayContent:")
        parts.append(maybe_quote(message.author, body))
        parts.append("")
        parts.append("internalContent:")
        parts.append(maybe_quote(message.author, message.internal_content))
        return "\n".join(parts)

    parts.append("")
    parts.append(maybe_quote(message.author, body))
    return "\n".join(parts)


def format_debug_sections(sections: list[DebugLogSection]) -> str:
    if not sections:
        return ""

    lines: list[str] = ["### Debug"]
    for section in sections:
        lines.append(f"#### {section.filename}")
        if section.lines:
            lines.extend(section.lines)
        else:
            lines.append("    <no matching lines>")
        lines.append("")

    if lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def render_session(
    session: ChatSession,
    source_name: str,
    prompt_uid: str | None,
    recovered_turns: list[RecoveredTurn],
    debug_event_records_path: Path | None = None,
    debug_log_sections: list[DebugLogSection] | None = None,
    debug_mode: bool = False,
) -> str:
    header_lines: list[str] = [f"# {session.title}", ""]
    info_lines = [f"Source: {source_name}"]
    if debug_event_records_path is not None:
        info_lines.append(f"Debug event records: {debug_event_records_path}")
    if session.uid:
        info_lines.append(f"Chat UID: {session.uid}")
    if prompt_uid:
        info_lines.append(f"Prompt UID: {prompt_uid}")
    info_lines.append(f"chatModelId: {session.model_id}")
    if session.source_action_type:
        info_lines.append(f"sourceActionType: {session.source_action_type}")
    if session.timestamp_ms is not None:
        info_lines.append(f"Date: {format_local_timestamp(session.timestamp_ms)}")
        if session.modified_at_ms is not None and session.modified_at_ms != session.timestamp_ms:
            info_lines.append(f"Modified at: {format_local_timestamp(session.modified_at_ms)}")

    for index, info_line in enumerate(info_lines):
        header_lines.append(f"    {info_line}")
        # These blank lines turn out to be very unattractive.  Unfortunately the JetBrains markdown viewer doesn't
        # understand line breaks correctly.
        # if index != len(info_lines) - 1:
        #     header_lines.append("")
    header_lines.append("")

    assistant_turn_index = 0
    emitted_message = False
    current_prompt_uid = None
    for message in session.messages:
        body = message.display_content
        if message.author != "Assistant":
            current_prompt_uid = message.uid
        message_debug_uid = current_prompt_uid if debug_mode else None
        if message.author == "Assistant" and not body.strip() and assistant_turn_index < len(recovered_turns):
            recovered = recovered_turns[assistant_turn_index].to_markdown()
            if recovered.strip():
                body = recovered
        formatted = ""
        if message.author == "Assistant" and body.startswith("- "):
            assistant_heading = "Assistant did"
            if message_debug_uid:
                assistant_heading = f"{assistant_heading} [*{message_debug_uid}*]"
            formatted = "\n".join([f"{assistant_heading}:", "", body])
        else:
            formatted = format_message(ChatMessage(message.uid, message.author, body, message.internal_content), debug_uid=message_debug_uid)
        if not formatted:
            if message.author == "Assistant":
                assistant_turn_index += 1
            continue
        if emitted_message:
            header_lines.append("")
        header_lines.append(formatted)
        emitted_message = True
        if message.author == "Assistant":
            assistant_turn_index += 1

    debug_section = format_debug_sections(debug_log_sections or [])
    if debug_section:
        header_lines.append("")
        header_lines.append(debug_section)

    header_lines.append("")
    return "\n".join(header_lines)


def set_file_timestamp(path: str, timestamp_ms: int) -> None:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    ts = dt.timestamp()

    os.utime(path, (ts, ts))

    if platform.system() != "Windows":
        return

    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    def datetime_to_filetime(value: datetime) -> FILETIME:
        filetime = int((value.timestamp() + 11644473600) * 10_000_000)
        return FILETIME(filetime & 0xFFFFFFFF, filetime >> 32)

    create_file = ctypes.windll.kernel32.CreateFileW
    set_file_time = ctypes.windll.kernel32.SetFileTime
    close_handle = ctypes.windll.kernel32.CloseHandle

    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    set_file_time.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    set_file_time.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        path,
        0x40000000,
        0x00000007,
        None,
        3,
        0x00000080,
        None,
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        return

    filetime = datetime_to_filetime(dt)
    try:
        if not set_file_time(handle, ctypes.byref(filetime), ctypes.byref(filetime), ctypes.byref(filetime)):
            return
    finally:
        close_handle(handle)


def resolve_output_path(
    output_dir: Path,
    title: str,
    session_uid: str | None,
    ignore_existing: bool,
    used: set[Path],
    existing_uids: dict[str, str | None],
) -> Path | None:
    base = sanitize_filename(title)
    missing = object()
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"_{counter}"
        candidate = output_dir / f"{base}{suffix}.md"
        if candidate in used:
            counter += 1
            continue

        existing_uid = existing_uids.get(candidate.name, missing)
        if existing_uid is missing and candidate.exists():
            existing_uid = read_session_uid_from_markdown(candidate)
            existing_uids[candidate.name] = existing_uid

        if existing_uid is not missing:
            if existing_uid is not None and existing_uid == session_uid:
                if ignore_existing:
                    return None
                used.add(candidate)
                return candidate
            counter += 1
            continue

        used.add(candidate)
        return candidate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract JetBrains AI Chat / Junie sessions from workspace XML files.",
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="Show this help message and exit.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[],
        help="Workspace XML file(s) or a directory containing workspace XML files. If omitted, scans platform-specific JetBrains workspace locations and project-local .idea/workspace.xml files, then keeps only files containing ChatSessionStateTemp.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for markdown exports.",
    )
    parser.add_argument(
        "--task-history-root",
        type=Path,
        default=None,
        help="Optional root directory containing aia-task-history files. If omitted, scans JetBrains IDE directories for aia-task-history automatically.",
    )
    parser.add_argument(
        "--find",
        type=str,
        default=None,
        help="Only process chats whose prompts match this regular expression.",
    )
    parser.add_argument(
        "--ignore-existing",
        action="store_true",
        help="Skip writing a file when an existing export has the same session UID.",
    )
    parser.add_argument(
        "--no-ide-dirs",
        action="store_true",
        help="Write all exports directly under the output directory instead of creating separate directories per IDE.",
    )
    parser.add_argument(
        "--ide-dirs",
        dest="ide_dirs",
        action="store_true",
        help="Keep the top-level IDE directory layer.",
    )
    parser.add_argument(
        "--no-ide-versions",
        action="store_true",
        help="Strip trailing version numbers from IDE output directories, turning names like PhpStorm2026.1 into PhpStorm.",
    )
    parser.add_argument(
        "--ide-versions",
        dest="no_ide_versions",
        action="store_false",
        help="Keep trailing version numbers in IDE output directories.",
    )
    parser.add_argument(
        "--workspace-dirs",
        action="store_true",
        help="Nest exports under a workspace directory named after each workspace XML file.",
    )
    parser.add_argument(
        "--no-workspace-dirs",
        dest="workspace_dirs",
        action="store_false",
        help="Do not nest exports under a workspace directory.",
    )
    parser.add_argument(
        "--no-model-dirs",
        action="store_true",
        help="Write all exports directly under the current output scope instead of creating separate directories per chat model.",
    )
    parser.add_argument(
        "--model-dirs",
        dest="no_model_dirs",
        action="store_false",
        help="Keep the chat model directory layer.",
    )
    parser.set_defaults(
        ide_dirs=False,
        no_ide_dirs=False,
        no_ide_versions=False,
        workspace_dirs=False,
        no_model_dirs=False,
    )
    file_dates_group = parser.add_mutually_exclusive_group()
    file_dates_group.add_argument(
        "--file-dates",
        dest="file_dates",
        action="store_true",
        default=True,
        help="Prefix output filenames with the local timestamp of each conversation.",
    )
    file_dates_group.add_argument(
        "--no-file-dates",
        dest="file_dates",
        action="store_false",
        help="Do not prefix output filenames with timestamps.",
    )
    parser.add_argument(
        "--no-disk-cache",
        action="store_true",
        help="Disable reading and writing the on-disk .aichat_export_cache.json file.",
    )
    parser.add_argument(
        "--git",
        action="store_true",
        help="Use git mv for tracked file renames and validate that each output directory is inside a git repository.",
    )
    parser.add_argument(
        "--git-bin",
        type=str,
        default=None,
        help="Optional git executable path or directory. Relative paths are resolved against the process CWD at startup.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress and summary output.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Emit tracing for workspace scanning, XML parsing, cache indexing, and task-history recovery.",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Write decoded event record files to debug-event-records under each output scope directory.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write rendered chats to stdout instead of markdown files.",
    )
    args = parser.parse_args()

    find_pattern: re.Pattern[str] | None = None
    if args.find is not None:
        try:
            find_pattern = re.compile(args.find)
        except re.error as exc:
            raise SystemExit(f"invalid --find regular expression: {exc}") from exc

    if args.stdout:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if not args.stdout:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    git_executable = resolve_git_executable(args.git_bin, Path.cwd())
    if args.git and not args.stdout:
        try:
            git_version = run_git(["--version"], Path.cwd(), git_executable)
        except FileNotFoundError as exc:
            raise SystemExit(str(exc)) from exc
        if git_version.returncode != 0:
            message = git_version.stderr.strip() or git_version.stdout.strip() or "unable to run git"
            raise SystemExit(message)

    sessions_written = 0
    used_paths: set[Path] = set()
    recovered_by_model: Counter[str] = Counter()
    written_by_model: Counter[str] = Counter()
    verbose = args.verbose
    debug = args.debug
    stdout_mode = args.stdout
    flatten_ide_output = should_flatten_output(args.paths, args.ide_dirs)
    per_workspace_output = args.workspace_dirs
    no_ide_subdir = args.no_ide_dirs
    strip_ide_versions = args.no_ide_versions
    no_model_dirs = args.no_model_dirs
    current_ide_name: str | None = None
    current_output_scope_key: Path | None = None
    current_ide_cache: IdeCache | None = None
    current_ide_task_history_index: TaskHistoryIndex | None = None
    current_ide_jobs: list[ExportJob] = []
    current_ide_rename_ops: list[tuple[Path, Path, dict[str, str | None]]] = []
    current_ide_recovered = 0
    current_ide_written = 0
    current_ide_recovered_by_model: Counter[str] = Counter()
    current_ide_written_by_model: Counter[str] = Counter()
    status_stream = sys.stderr if stdout_mode else sys.stdout

    def flush_current_ide_state() -> None:
        nonlocal current_ide_name, current_output_scope_key, current_ide_cache, current_ide_task_history_index
        nonlocal current_ide_jobs, current_ide_rename_ops
        nonlocal current_ide_recovered, current_ide_written
        nonlocal current_ide_recovered_by_model, current_ide_written_by_model
        nonlocal sessions_written
        if not stdout_mode and current_ide_cache is not None and current_ide_rename_ops:
            for src, dst, existing_uids in current_ide_rename_ops:
                rename_many(
                    [(src, dst)],
                    current_ide_cache.cache_root,
                    git_executable,
                    use_git=args.git,
                )
                if not dst.exists():
                    raise RuntimeError(f"rename completed but destination is missing: {dst}")
                old_uid = existing_uids.pop(src.name, None)
                existing_uids[dst.name] = old_uid
                current_ide_cache.dirty = True

        if current_ide_cache is not None and current_ide_jobs:
            for job in current_ide_jobs:
                rendered = render_session(
                    job.session,
                    source_name=str(job.input_path),
                    prompt_uid=job.prompt_uid,
                    recovered_turns=job.recovered_turns,
                    debug_event_records_path=job.debug_event_records_path,
                    debug_log_sections=job.debug_log_sections,
                    debug_mode=debug,
                )
                if stdout_mode:
                    if sessions_written:
                        print("", flush=True)
                    print(rendered, end="", flush=True)
                else:
                    job.output_path.write_text(
                        rendered,
                        encoding="utf-8",
                    )
                    if not job.output_path.exists():
                        raise RuntimeError(f"write completed but destination is missing: {job.output_path}")
                    if job.session.timestamp_ms is not None:
                        try:
                            set_file_timestamp(str(job.output_path), job.session.timestamp_ms)
                        except OSError:
                            pass
                    job.existing_uids[job.output_path.name] = job.session.uid
                    current_ide_cache.dirty = True
                written_by_model[job.session.model_id] += 1
                current_ide_written_by_model[job.session.model_id] += 1
                current_ide_written += 1
                sessions_written += 1

        if current_ide_cache is not None:
            if not stdout_mode:
                save_ide_cache(current_ide_cache, use_disk_cache=not args.no_disk_cache)

        if current_ide_cache is not None:
            current_ide_cache = None
        current_ide_task_history_index = None
        current_ide_jobs = []
        current_ide_rename_ops = []
        current_output_scope_key = None

        if current_ide_name is None or args.quiet:
            return
        if current_ide_recovered == 0:
            print("  No conversations were found.", file=status_stream, flush=True)
        elif args.ignore_existing:
            print("  Recovered conversations by model:", file=status_stream, flush=True)
            for model_id in sorted(current_ide_recovered_by_model):
                recovered = current_ide_recovered_by_model[model_id]
                written = current_ide_written_by_model.get(model_id, 0)
                print(f"    {model_id}: recovered={recovered}, written={written}", file=status_stream, flush=True)
        else:
            print("  Recovered conversations by model:", file=status_stream, flush=True)
            for model_id in sorted(current_ide_recovered_by_model):
                recovered = current_ide_recovered_by_model[model_id]
                print(f"    {model_id}: recovered={recovered}", file=status_stream, flush=True)

    input_items: Iterator[tuple[Path, str]]
    if args.paths:
        input_items = iter_input_files(args.paths)
    else:
        input_items = iter_default_workspace_files(verbose=verbose)

    try:
        for input_path, ide_name in input_items:
            output_scope_key = cache_root_for_workspace(
                args.output_dir,
                ide_name,
                input_path,
                flatten_ide_output,
                per_workspace_output,
                no_ide_subdir,
                strip_ide_versions,
            )

            if output_scope_key != current_output_scope_key:
                flush_current_ide_state()
                current_ide_name = ide_name
                current_output_scope_key = output_scope_key
                current_ide_cache = load_ide_cache(
                    output_scope_key,
                    use_disk_cache=not args.no_disk_cache,
                )
                prime_model_uid_indexes(
                    current_ide_cache,
                    current_ide_cache.cache_root,
                    verbose=verbose,
                    flat_model_output=no_model_dirs,
                )
                if current_ide_cache.dirty and not stdout_mode:
                    save_ide_cache(current_ide_cache, use_disk_cache=not args.no_disk_cache)
                debug_dir = None if stdout_mode else (current_ide_cache.cache_root / "debug-event-records" if debug else None)
                current_ide_task_history_index = build_task_history_index(
                    args.task_history_root,
                    verbose=verbose,
                    debug_dir=debug_dir,
                )
                if args.git and not stdout_mode:
                    if git_root(current_ide_cache.cache_root, git_executable) is None:
                        raise SystemExit(f"{current_ide_cache.cache_root} is not inside a git repository")
                current_ide_recovered = 0
                current_ide_written = 0
                current_ide_recovered_by_model = Counter()
                current_ide_written_by_model = Counter()
                current_ide_jobs = []
                current_ide_rename_ops = []
            if not args.quiet:
                print(
                    f"Processing workspace: {input_path.resolve()} [IDE: {ide_name}]",
                    file=status_stream,
                    flush=True,
                )
            verbose_print(verbose, 4, f"start extract_chat_sessions: {input_path}")
            sessions = extract_chat_sessions(input_path, verbose=verbose)
            verbose_print(verbose, 4, f"end extract_chat_sessions: {input_path} sessions={len(sessions)}")
            for session in sessions:
                if not session_matches_find(session, find_pattern):
                    continue

                model_component = sanitize_path_component(session.model_id)
                if no_model_dirs:
                    model_dir = current_ide_cache.cache_root
                elif flatten_ide_output:
                    model_dir = args.output_dir / model_component
                else:
                    model_dir = current_ide_cache.cache_root / model_component

                existing_uids = current_ide_cache.model_index(model_component, flat_model_output=no_model_dirs)

                if args.ignore_existing and session.model_id.startswith("agent_"):
                    if has_existing_output_with_uid(existing_uids, session.uid):
                        continue

                recovered_turns, debug_event_records_path = recover_junie_turns(
                    session,
                    args.task_history_root,
                    current_ide_cache,
                    current_ide_task_history_index,
                    verbose=verbose,
                )
                if not has_assistant_content(session) and not recovered_turns:
                    continue

                prompt = first_user_prompt(session)
                prompt_uid = prompt.uid if prompt is not None else None
                debug_log_sections = collect_log_sections(ide_name, session) if debug and find_pattern is not None else []

                plan = plan_output_path(
                    model_dir,
                    session.title,
                    session.timestamp_ms,
                    session.uid,
                    args.ignore_existing,
                    used_paths,
                    existing_uids,
                    args.file_dates,
                )
                if plan is None:
                    continue
                recovered_by_model[session.model_id] += 1
                current_ide_recovered += 1
                current_ide_recovered_by_model[session.model_id] += 1
                if not stdout_mode:
                    model_dir.mkdir(parents=True, exist_ok=True)
                if not stdout_mode and plan.rename_source is not None and plan.rename_source != plan.output_path:
                    current_ide_rename_ops.append((plan.rename_source, plan.output_path, existing_uids))
                current_ide_jobs.append(
                    ExportJob(
                        input_path=input_path,
                        ide_name=ide_name,
                        session=session,
                        recovered_turns=recovered_turns,
                        debug_event_records_path=debug_event_records_path,
                        debug_log_sections=debug_log_sections,
                        prompt_uid=prompt_uid,
                        output_path=plan.output_path,
                        rename_source=plan.rename_source,
                        existing_uids=existing_uids,
                    )
                )

        flush_current_ide_state()
    except Exception:
        if current_ide_cache is not None and current_ide_cache.dirty and not stdout_mode:
            try:
                save_ide_cache(current_ide_cache, use_disk_cache=not args.no_disk_cache)
            except Exception:
                pass
        raise

    if not args.quiet:
        if stdout_mode:
            print(f"Grand total: Wrote {sessions_written} chat markdown session(s) to stdout", file=status_stream, flush=True)
        else:
            print(f"Grand total: Wrote {sessions_written} chat markdown file(s) to {args.output_dir}", flush=True)
        if recovered_by_model:
            print("Recovered conversations by model:", file=status_stream, flush=True)
            for model_id in sorted(recovered_by_model):
                recovered = recovered_by_model[model_id]
                if args.ignore_existing:
                    written = written_by_model.get(model_id, 0)
                    print(f"  {model_id}: recovered={recovered}, written={written}", file=status_stream, flush=True)
                else:
                    print(f"  {model_id}: recovered={recovered}", file=status_stream, flush=True)
        else:
            print("Recovered conversations by model: none", file=status_stream, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
