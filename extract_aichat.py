from __future__ import annotations

import base64
import argparse
import os
import platform
from datetime import datetime, timezone
import html
import json
import re
from dataclasses import dataclass
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator
from xml.etree import ElementTree as ET


DEFAULT_INPUT = Path(os.environ.get("APPDATA", r"C:\Users\sfinktah\AppData\Roaming")) / "JetBrains" / "*" / "workspace" / "*.xml"
DEFAULT_OUTPUT_DIR = Path(r"C:\tmp\aichat")
CHAT_SESSION_MARKER = '<component name="ChatSessionStateTemp">'
SESSION_UID_PREFIX = "Session UID: `"


@dataclass(frozen=True)
class ChatMessage:
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


def iter_input_files(paths: list[Path]) -> Iterator[tuple[Path, str]]:
    seen: set[Path] = set()
    for path in paths:
        if path.is_dir():
            for xml_path in sorted(path.rglob("*.xml")):
                if xml_path not in seen:
                    seen.add(xml_path)
                    yield xml_path, xml_path.parent.parent.name
        elif path.is_file():
            if path not in seen:
                seen.add(path)
                yield path, path.parent.parent.name if path.parent.name == "workspace" else path.parent.name
        else:
            raise FileNotFoundError(path)


def should_flatten_output(paths: list[Path]) -> bool:
    if len(paths) != 1:
        return False

    path = paths[0]
    if not path.exists():
        return False

    resolved = path.resolve()
    if resolved.is_file():
        return resolved.parent.name == "workspace"

    if not resolved.is_dir():
        return False

    jetbrains_root = Path(os.environ.get("APPDATA", r"C:\Users\sfinktah\AppData\Roaming")) / "JetBrains"
    try:
        relative = resolved.relative_to(jetbrains_root)
    except ValueError:
        return False

    if len(relative.parts) == 1:
        return True

    return len(relative.parts) >= 2 and relative.parts[1] == "workspace"


def iter_default_workspace_files() -> Iterator[tuple[Path, str]]:
    jetbrains_root = Path(os.environ.get("APPDATA", r"C:\Users\sfinktah\AppData\Roaming")) / "JetBrains"
    if not jetbrains_root.exists():
        return

    for ide_dir in sorted(p for p in jetbrains_root.iterdir() if p.is_dir()):
        workspace_dir = ide_dir / "workspace"
        if not workspace_dir.is_dir():
            continue
        for xml_path in sorted(workspace_dir.glob("*.xml")):
            if not xml_path.is_file():
                continue
            try:
                text = xml_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if CHAT_SESSION_MARKER not in text:
                continue
            yield xml_path, ide_dir.name


def get_option_value(node: ET.Element, option_name: str) -> str | None:
    for option in node.findall("./option"):
        if option.get("name") == option_name:
            if "value" in option.attrib:
                return option.get("value")
            return (option.text or "").strip() or None
    return None


def extract_chat_sessions(xml_path: Path) -> list[ChatSession]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    sessions: list[ChatSession] = []

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
                author = get_option_value(msg_node, "author") or "User"
                display_content = get_option_value(msg_node, "displayContent") or ""
                internal_content = get_option_value(msg_node, "internalContent")
                messages.append(
                    ChatMessage(
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


def decode_event_records(path: Path) -> Iterator[dict]:
    data = path.read_bytes().splitlines()
    if not data:
        return
    start = 1 if data[0] == b"AUI_EVENTS_V1" else 0
    for line in data[start:]:
        if not line.strip():
            continue
        try:
            yield json.loads(base64.b64decode(line))
        except Exception:
            continue


def summarize_block(event: dict) -> str | None:
    kind = event.get("kind")
    if kind == "com.intellij.ml.llm.aui.events.api.TerminalBlockUpdatedEvent":
        command = event.get("command") or ""
        status = event.get("status") or ""
        details = event.get("details") or ""
        if command and ("\n" in command or "`" in command):
            lines = ["Terminal:"]
            lines.append("```")
            lines.extend(command.splitlines() or [""])
            lines.append("```")
            if status:
                lines.append(f"status={status}")
            if details:
                lines.append(details)
            return "\n".join(lines)

        parts = [f"Terminal: `{command}`" if command else "Terminal"]
        if status:
            parts.append(f"status={status}")
        if details:
            parts.append(details)
        return " - ".join(parts)

    if kind == "com.intellij.ml.llm.aui.events.api.AgentThoughtBlockUpdatedEvent":
        text = (event.get("text") or "").strip()
        return f"Thought: {text}" if text else None

    if kind == "com.intellij.ml.llm.aui.events.api.ToolBlockUpdatedEvent":
        text = (event.get("text") or "").strip()
        details = (event.get("details") or "").strip()
        parts = []
        if text:
            parts.append(text)
        if details:
            parts.append(details)
        return f"Tool: {' | '.join(parts)}" if parts else None

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


def build_turn_summaries(events_path: Path) -> list[RecoveredTurn]:
    turns: list[RecoveredTurn] = []
    current_prompt: str | None = None
    current_blocks: list[str] = []

    for record in decode_event_records(events_path):
        record_type = record.get("type")
        if record_type == "com.intellij.ml.llm.chat.shared.ChatSessionUserPromptEvent":
            if current_prompt is not None:
                turns.append(RecoveredTurn(prompt=current_prompt, blocks=current_blocks))
            current_prompt = record.get("prompt") or ""
            current_blocks = []
            continue

        if record_type == "com.intellij.ml.llm.chat.shared.ChatSessionMessageBlockEvent":
            event = record.get("event") or {}
            summary = summarize_block(event)
            if summary:
                current_blocks.append(summary)

    if current_prompt is not None:
        turns.append(RecoveredTurn(prompt=current_prompt, blocks=current_blocks))

    return turns


def iter_task_history_roots(task_history_root: Path | None) -> Iterator[Path]:
    if task_history_root is not None:
        if task_history_root.exists():
            yield task_history_root
        return

    jetbrains_root = Path(os.environ.get("APPDATA", r"C:\Users\sfinktah\AppData\Roaming")) / "JetBrains"
    if not jetbrains_root.exists():
        return

    for ide_dir in sorted(p for p in jetbrains_root.iterdir() if p.is_dir()):
        candidate = ide_dir / "aia-task-history"
        if candidate.is_dir():
            yield candidate


def find_matching_task_history_file(
    task_history_root: Path | None,
    prompt: str,
) -> Path | None:
    for root in iter_task_history_roots(task_history_root):
        candidates = sorted(root.glob("*.events"))
        for candidate in candidates:
            for record in decode_event_records(candidate):
                if record.get("type") != "com.intellij.ml.llm.chat.shared.ChatSessionUserPromptEvent":
                    continue
                if record.get("prompt") == prompt:
                    return candidate
    return None


def sanitize_filename(title: str) -> str:
    cleaned = html.unescape(title).strip()
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "untitled-chat"


def sanitize_path_component(value: str) -> str:
    cleaned = html.unescape(value).strip()
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "unknown"


def read_session_uid_from_markdown(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    for line in text.splitlines():
        line = line.strip()
        if line.startswith(SESSION_UID_PREFIX) and line.endswith("`"):
            return line[len(SESSION_UID_PREFIX) : -1]
    return None


def quote_block(text: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def maybe_quote(author: str, text: str) -> str:
    if author == "Assistant":
        return text
    return quote_block(text)


def has_assistant_content(session: ChatSession) -> bool:
    for message in session.messages:
        if message.author != "Assistant":
            continue
        if (message.display_content and message.display_content.strip()) or (
            message.internal_content and message.internal_content.strip()
        ):
            return True
    return False


def recover_junie_turns(session: ChatSession, task_history_root: Path | None) -> list[RecoveredTurn]:
    if not session.model_id.startswith("agent_"):
        return []

    first_prompt = next((msg.display_content for msg in session.messages if msg.author != "Assistant" and msg.display_content.strip()), "")
    if not first_prompt:
        return []

    events_path = find_matching_task_history_file(task_history_root, first_prompt)
    if not events_path:
        return []

    return build_turn_summaries(events_path)


def format_message(message: ChatMessage) -> str:
    body = message.display_content
    parts: list[str] = [f"{message.author} said:"]

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


def render_session(session: ChatSession, source_name: str, recovered_turns: list[RecoveredTurn]) -> str:
    lines: list[str] = [f"# {session.title}", "", f"Source: `{source_name}`"]
    if session.uid:
        lines.append(f"Session UID: `{session.uid}`")
    lines.append(f"chatModelId: `{session.model_id}`")
    if session.source_action_type:
        lines.append(f"sourceActionType: `{session.source_action_type}`")
    if session.timestamp_ms is not None:
        lines.append(f"Date: `{datetime.fromtimestamp(session.timestamp_ms / 1000, tz=timezone.utc).isoformat()}`")
        if session.modified_at_ms is not None and session.modified_at_ms != session.timestamp_ms:
            lines.append(f"Modified at: `{datetime.fromtimestamp(session.modified_at_ms / 1000, tz=timezone.utc).isoformat()}`")
    lines.append("")

    assistant_turn_index = 0
    for index, message in enumerate(session.messages, start=1):
        if index > 1:
            lines.append("")
        body = message.display_content
        if message.author == "Assistant" and not body.strip() and assistant_turn_index < len(recovered_turns):
            recovered = recovered_turns[assistant_turn_index].to_markdown()
            if recovered.strip():
                body = recovered
        if message.author == "Assistant" and body.startswith("- "):
            lines.append("Assistant did:")
            lines.append("")
            lines.append(body)
        else:
            lines.append(format_message(ChatMessage(message.author, body, message.internal_content)))
        if message.author == "Assistant":
            assistant_turn_index += 1

    lines.append("")
    return "\n".join(lines)


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
) -> Path | None:
    base = sanitize_filename(title)
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"_{counter}"
        candidate = output_dir / f"{base}{suffix}.md"
        if candidate in used:
            counter += 1
            continue

        if candidate.exists():
            existing_uid = read_session_uid_from_markdown(candidate)
            if existing_uid == session_uid:
                if ignore_existing:
                    return None
                used.add(candidate)
                return candidate
            counter += 1
            continue

        used.add(candidate)
        return candidate


def has_existing_output_with_uid(
    output_dir: Path,
    title: str,
    session_uid: str | None,
) -> bool:
    base = sanitize_filename(title)
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"_{counter}"
        candidate = output_dir / f"{base}{suffix}.md"
        if not candidate.exists():
            return False

        existing_uid = read_session_uid_from_markdown(candidate)
        if existing_uid == session_uid:
            return True

        counter += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract JetBrains AI Chat / Junie sessions from workspace XML files.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[],
        help="Workspace XML file(s) or a directory containing workspace XML files. If omitted, scans %APPDATA%\\JetBrains\\*\\workspace\\*.xml and keeps only files containing ChatSessionStateTemp.",
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
        "--ignore-existing",
        action="store_true",
        help="Skip writing a file when an existing export has the same session UID.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress and summary output.",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    sessions_written = 0
    used_paths: set[Path] = set()
    recovered_by_model: Counter[str] = Counter()
    written_by_model: Counter[str] = Counter()
    flatten_ide_output = should_flatten_output(args.paths)
    current_ide_name: str | None = None
    current_ide_recovered = 0
    current_ide_written = 0
    current_ide_recovered_by_model: Counter[str] = Counter()
    current_ide_written_by_model: Counter[str] = Counter()

    def flush_current_ide_summary() -> None:
        nonlocal current_ide_name, current_ide_recovered, current_ide_written
        nonlocal current_ide_recovered_by_model, current_ide_written_by_model
        if current_ide_name is None or args.quiet:
            return
        if current_ide_recovered == 0:
            print("  No conversations were found.", flush=True)
        elif args.ignore_existing:
            print("  Recovered conversations by model:", flush=True)
            for model_id in sorted(current_ide_recovered_by_model):
                recovered = current_ide_recovered_by_model[model_id]
                written = current_ide_written_by_model.get(model_id, 0)
                print(f"    {model_id}: recovered={recovered}, written={written}", flush=True)
        else:
            print("  Recovered conversations by model:", flush=True)
            for model_id in sorted(current_ide_recovered_by_model):
                recovered = current_ide_recovered_by_model[model_id]
                print(f"    {model_id}: recovered={recovered}", flush=True)

    input_items: Iterator[tuple[Path, str]]
    if args.paths:
        input_items = iter_input_files(args.paths)
    else:
        input_items = iter_default_workspace_files()

    for input_path, ide_name in input_items:
        if ide_name != current_ide_name:
            flush_current_ide_summary()
            current_ide_name = ide_name
            current_ide_recovered = 0
            current_ide_written = 0
            current_ide_recovered_by_model = Counter()
            current_ide_written_by_model = Counter()
            if not args.quiet:
                print(f"Processing IDE: {ide_name}", flush=True)
        sessions = extract_chat_sessions(input_path)
        for session in sessions:
            if flatten_ide_output:
                model_dir = args.output_dir / sanitize_path_component(session.model_id)
            else:
                model_dir = args.output_dir / sanitize_path_component(ide_name) / sanitize_path_component(session.model_id)

            if args.ignore_existing and session.model_id.startswith("agent_"):
                if has_existing_output_with_uid(model_dir, session.title, session.uid):
                    continue

            recovered_turns = recover_junie_turns(session, args.task_history_root)
            if not has_assistant_content(session) and not recovered_turns:
                continue

            recovered_by_model[session.model_id] += 1
            current_ide_recovered += 1
            current_ide_recovered_by_model[session.model_id] += 1
            model_dir.mkdir(parents=True, exist_ok=True)
            output_path = resolve_output_path(
                model_dir,
                session.title,
                session.uid,
                args.ignore_existing,
                used_paths,
            )
            if output_path is None:
                continue
            output_path.write_text(
                render_session(session, source_name=str(input_path), recovered_turns=recovered_turns),
                encoding="utf-8",
            )
            if session.timestamp_ms is not None:
                try:
                    set_file_timestamp(str(output_path), session.timestamp_ms)
                except OSError:
                    pass
            written_by_model[session.model_id] += 1
            current_ide_written_by_model[session.model_id] += 1
            current_ide_written += 1
            sessions_written += 1

    flush_current_ide_summary()

    if not args.quiet:
        print(f"Grand total: Wrote {sessions_written} chat markdown file(s) to {args.output_dir}", flush=True)
        if recovered_by_model:
            print("Recovered conversations by model:", flush=True)
            for model_id in sorted(recovered_by_model):
                recovered = recovered_by_model[model_id]
                if args.ignore_existing:
                    written = written_by_model.get(model_id, 0)
                    print(f"  {model_id}: recovered={recovered}, written={written}", flush=True)
                else:
                    print(f"  {model_id}: recovered={recovered}", flush=True)
        else:
            print("Recovered conversations by model: none", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
