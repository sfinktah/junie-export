# JetBrains AI Chat Exporter

`extract_aichat.py` scans JetBrains workspace XML files under `%APPDATA%\JetBrains\*\workspace\*.xml`, extracts `SerializedChat` sessions, and writes each chat to markdown under an output directory you choose.

It handles two sources of content:

1. Direct XML chat text stored in `SerializedChatMessage` nodes.
2. Recovered Junie action history from JetBrains `aia-task-history` event logs when the XML assistant message is empty.

The output is grouped by IDE name and `chatModelId`, so the final path shape is:

```text
<output-dir>\<IDEName>\<chatModelId>\<title>.md
```

If you pass exactly one explicit IDE root or workspace file, the exporter skips the top-level `<IDEName>` folder and writes directly under `<output-dir>\<chatModelId>\`.

The default output directory is `C:\tmp\aichat\`, but you can point it anywhere. If you want the exported AI history to live with the project, use a directory inside the repository. That makes later searching and diffing much easier.

## What It Exports

- Chat title from `SerializedChatTitle`
- Session UID
- `chatModelId`
- `sourceActionType`
- Timestamp derived from `statisticInformation.timestamp`
- `modifiedAt` when it differs from `statisticInformation.timestamp`
- Messages in conversation order
- Junie assistant actions recovered from `aia-task-history`

## High-Level Flow

1. Discover candidate workspace XML files.
2. Filter to XMLs containing `<component name="ChatSessionStateTemp">`.
3. Parse every `SerializedChat`.
4. Extract metadata and message bodies.
5. For `agent_*` sessions, recover missing assistant content from matching `.events` files.
6. Render markdown and write one file per chat.

## Technical Details

### Workspace discovery

When no explicit paths are provided, the script walks:

```text
%APPDATA%\JetBrains\<IDE>\workspace\*.xml
```

Each XML file is read as text first and only parsed if it contains:

```xml
<component name="ChatSessionStateTemp">
```

That marker avoids parsing unrelated workspace XML files.

### XML parsing

The exporter uses `xml.etree.ElementTree` from the Python standard library.

For every `SerializedChat`, it reads:

- `title` from `./option[@name='title']/SerializedChatTitle/option[@name='text']`
- `chatModelId` from `./option[@name='chatModelId']`
- `uid` from `./option[@name='uid']`
- `statisticInformation.timestamp` from `./option[@name='statisticInformation']/ChatStatisticInformation`
- `messages` from `./option[@name='messages']/list/SerializedChatMessage`

Each message is reduced to:

- `author`
- `displayContent`
- `internalContent`

### Markdown rendering rules

The renderer applies these rules:

- The file starts with the chat title as `# ...`.
- The header includes:
  - source XML path
  - session UID
  - `chatModelId`
  - `sourceActionType`
  - timestamp rendered as an ISO 8601 UTC string
  - `modifiedAt` rendered underneath it when it differs
- Every message is prefixed with `<author> said:`.
- Non-Assistant message bodies are quoted with markdown blockquote syntax.
- If `displayContent` and `internalContent` differ, the exporter writes both values and labels them explicitly.

### Assistant content suppression and recovery

Chats are skipped only if they have no usable Assistant content and no recoverable agent event history.

For `chatModelId` values starting with `agent_`:

1. The exporter takes the first non-Assistant prompt text from the XML session.
2. It searches the task-history root for `.events` files whose first `ChatSessionUserPromptEvent.prompt` exactly matches that text.
3. It decodes the matching event file.
4. It reconstructs the turn stream by grouping:
   - `ChatSessionUserPromptEvent`
   - `ChatSessionMessageBlockEvent`
5. Each message block is summarized into a short markdown bullet line.

The event files are not SQLite. They are newline-delimited base64-encoded JSON records with an `AUI_EVENTS_V1` header.

### Event record formats

The exporter knows how to summarize these Junie block event kinds:

- `TerminalBlockUpdatedEvent`
- `AgentThoughtBlockUpdatedEvent`
- `ToolBlockUpdatedEvent`
- `ViewFilesBlockUpdatedEvent`
- `FileChangesBlockUpdatedEvent`
- `ResultBlockUpdatedEvent`

Those summaries are inserted into recovered assistant sections as plain markdown bullets.

### Output path handling

Paths are sanitized for Windows compatibility. The exporter creates:

```text
C:\tmp\aichat\<IDEName>\<chatModelId>\
```

If you pass exactly one explicit IDE root or workspace file, it instead writes to:

```text
C:\tmp\aichat\<chatModelId>\
```

If a filename collision occurs, it appends either:

- the session UID prefix, or
- a numeric suffix

to preserve all exports without overwriting.

The per-IDE cache is a performance hint, not a source of truth. If it gets out of sync, delete it and rerun.

## Command Line Arguments

`extract_aichat.py` accepts these arguments:

- `paths`
  - Optional explicit XML files or directories to scan.
  - If omitted, the script scans `%APPDATA%\JetBrains\*\workspace\*.xml` automatically.
- `--output-dir`
  - Base directory for markdown exports.
  - By default the script creates `<output-dir>\<IDEName>\<chatModelId>\` underneath it.
  - If you pass exactly one explicit IDE root or workspace file, it writes directly to `<output-dir>\<chatModelId>\`.
  - This can be a project-local directory if you want the AI history stored alongside the code.
- `--task-history-root`
  - Optional root directory containing JetBrains `aia-task-history` files for recovery.
  - If omitted, the script auto-discovers `aia-task-history` directories under `%APPDATA%\JetBrains\<IDE>\`.
- `--ignore-existing`
  - Skip writing a file when an existing export already has the same session UID.
- `--no-disk-cache`
  - Disable reading and writing the on-disk `.aichat_export_cache.json` file.

The exporter keeps a per-IDE cache at:

```text
<output-dir>\<IDE>\.aichat_export_cache.json
```

When you pass exactly one explicit IDE root or workspace file and the exporter flattens the output layout, the cache lives directly under the output directory instead:

```text
<output-dir>\.aichat_export_cache.json
```

That cache stores:

- per-model markdown filename to session UID indexes
- prompt-to-`aia-task-history` file lookups

It is there to make repeat runs cheaper, especially the common "just grab the new conversations" workflow and reruns with `--ignore-existing`. If you manually move, delete, or edit exported markdown files outside the exporter, delete the cache file as well so the next run rebuilds it from disk. Use `--no-disk-cache` when you want the exporter to ignore the cache entirely for a run.
- `-q`, `--quiet`
  - Suppress progress and summary output.

After writing each markdown file, the exporter also sets the file timestamp from `statisticInformation.timestamp`. On Windows, it attempts to set creation, modified, and accessed times; on other platforms, it sets modified and accessed times.

## Usage

Run from the repository root:

```powershell
python .\aichat_export\extract_aichat.py
```

To export a specific workspace file or directory:

```powershell
python .\aichat_export\extract_aichat.py C:\Users\sfinktah\AppData\Roaming\JetBrains\PhpStorm2026.1\workspace\2W9cqLpuxpUxyNVO6Pi0AKhtraW.xml
```

To override the output directory:

```powershell
python .\aichat_export\extract_aichat.py --output-dir C:\tmp\aichat_export
```

To point at a different task-history root:

```powershell
python .\aichat_export\extract_aichat.py --task-history-root C:\Users\sfinktah\AppData\Roaming\JetBrains\PhpStorm2026.1\aia-task-history
```

To preserve existing exports when the same session UID already exists:

```powershell
python .\aichat_export\extract_aichat.py --ignore-existing
```

## File Collision Rules

The exporter never overwrites a file that already belongs to a different session UID.

The lookup order is:

1. `<title>.md`
2. `<title>_1.md`
3. `<title>_2.md`
4. and so on

For each candidate file:

- If the file exists and has the same session UID, the exporter rewrites it unless `--ignore-existing` is set.
- If the file exists and has a different session UID, the exporter tries the next suffix.
- If `--ignore-existing` is set and the matching UID already exists, nothing is written.

## Notes

- The script is intentionally stdlib-only.
- `requirements.txt` exists as a boundary file, but there are no third-party dependencies.
- The recovery logic is prompt-matched, not UID-matched, because the XML message UIDs are not directly present as primary keys in the `.events` stream.
