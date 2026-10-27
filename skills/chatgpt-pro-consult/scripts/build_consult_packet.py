#!/usr/bin/env python3
"""Build an auditable, text-only context packet for a ChatGPT Pro consultation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_TEXT_BYTES = 5 * 1024 * 1024

SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
SECRET_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("OpenAI-style key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
)
LANGUAGES = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "jsx",
    ".lua": "lua",
    ".md": "markdown",
    ".py": "python",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


@dataclass(frozen=True)
class SelectedFile:
    path: Path
    relative_path: str
    data: bytes
    sha256: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bundle exact selected text files into a Markdown consultation packet."
    )
    parser.add_argument("--root", required=True, help="Absolute workspace root.")
    parser.add_argument("--brief", required=True, help="Consultation brief text file.")
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Exact text file to embed. Repeat for additional files.",
    )
    parser.add_argument(
        "--attachment",
        action="append",
        default=[],
        help="Exact binary/text file to list for separate upload. Repeat as needed.",
    )
    parser.add_argument("--output", required=True, help="Output Markdown packet path.")
    parser.add_argument(
        "--max-text-bytes",
        type=int,
        default=DEFAULT_MAX_TEXT_BYTES,
        help=f"Maximum combined bytes for brief and embedded text (default {DEFAULT_MAX_TEXT_BYTES}).",
    )
    return parser.parse_args()


def resolve_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace root is not a directory: {root}")
    return root


def resolve_inside(root: Path, value: str, *, must_exist: bool = True) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace root: {candidate}") from exc
    if must_exist and not candidate.is_file():
        raise ValueError(f"Expected a regular file: {candidate}")
    return candidate


def relative_name(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def check_sensitive_path(path: Path) -> None:
    lowered = path.name.lower()
    if (
        lowered in SENSITIVE_NAMES
        or lowered.startswith(".env.")
        or path.suffix.lower() in SENSITIVE_SUFFIXES
    ):
        raise ValueError(f"Refusing credential-like file: {path}")


def check_secret_content(path: Path, data: bytes) -> None:
    matches = [label for label, pattern in SECRET_PATTERNS if pattern.search(data)]
    if matches:
        raise ValueError(
            f"Refusing possible secret material in {path}: {', '.join(matches)}"
        )


def load_selected(root: Path, value: str, *, scan_content: bool) -> SelectedFile:
    path = resolve_inside(root, value)
    check_sensitive_path(path)
    data = path.read_bytes()
    if scan_content:
        check_secret_content(path, data)
    return SelectedFile(
        path=path,
        relative_path=relative_name(root, path),
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def decode_text(selected: SelectedFile) -> tuple[str, str]:
    if b"\x00" in selected.data[:8192]:
        raise ValueError(
            f"Embedded input appears binary; pass it with --attachment: {selected.path}"
        )
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return selected.data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(
        f"Cannot decode embedded input as supported text; use --attachment: {selected.path}"
    )


def choose_fence(text: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def table_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def main() -> int:
    args = parse_args()
    if args.max_text_bytes <= 0:
        raise ValueError("--max-text-bytes must be positive")

    root = resolve_root(args.root)
    output = resolve_inside(root, args.output, must_exist=False)
    brief = load_selected(root, args.brief, scan_content=True)
    embedded = [load_selected(root, value, scan_content=True) for value in args.input]
    attachments = [
        load_selected(root, value, scan_content=True) for value in args.attachment
    ]

    all_inputs = [brief, *embedded, *attachments]
    paths = [item.path for item in all_inputs]
    if len(paths) != len(set(paths)):
        raise ValueError("The brief, inputs, and attachments must be unique files")
    if output in paths:
        raise ValueError("Output path cannot also be an input")

    brief_text, brief_encoding = decode_text(brief)
    decoded_inputs: list[tuple[SelectedFile, str, str]] = []
    text_bytes = len(brief.data)
    for selected in embedded:
        content, encoding = decode_text(selected)
        decoded_inputs.append((selected, content, encoding))
        text_bytes += len(selected.data)
    if text_bytes > args.max_text_bytes:
        raise ValueError(
            f"Selected text is {text_bytes} bytes, above --max-text-bytes "
            f"{args.max_text_bytes}; narrow the decision-critical context"
        )

    lines = [
        "<!-- Generated by chatgpt-pro-consult/build_consult_packet.py -->",
        "# ChatGPT Pro reasoning consultation packet",
        "",
        "## Consultation brief",
        "",
        brief_text.rstrip(),
        "",
        "## Selected context manifest",
        "",
        "| Path | Delivery | Bytes | SHA-256 | Encoding |",
        "| --- | --- | ---: | --- | --- |",
        (
            f"| {table_cell(brief.relative_path)} | brief | {len(brief.data)} | "
            f"`{brief.sha256}` | {brief_encoding} |"
        ),
    ]
    for selected, _, encoding in decoded_inputs:
        lines.append(
            f"| {table_cell(selected.relative_path)} | embedded | {len(selected.data)} | "
            f"`{selected.sha256}` | {encoding} |"
        )
    for selected in attachments:
        lines.append(
            f"| {table_cell(selected.relative_path)} | attach separately | "
            f"{len(selected.data)} | `{selected.sha256}` | binary/original |"
        )

    lines.extend(["", "## Embedded project context", ""])
    if not decoded_inputs:
        lines.extend(["No text files were selected for embedding.", ""])
    for selected, content, _ in decoded_inputs:
        fence = choose_fence(content)
        language = LANGUAGES.get(selected.path.suffix.lower(), "text")
        lines.extend(
            [
                f"### `{selected.relative_path}`",
                "",
                f"SHA-256: `{selected.sha256}`",
                "",
                f"{fence}{language}",
                content.rstrip(),
                fence,
                "",
            ]
        )

    if attachments:
        lines.extend(
            [
                "## Separate attachments",
                "",
                "Upload these exact files beside this packet:",
                "",
            ]
        )
        lines.extend(f"- `{selected.relative_path}`" for selected in attachments)
        lines.append("")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")

    result = {
        "status": "ok",
        "output": str(output),
        "embedded_files": [selected.relative_path for selected, _, _ in decoded_inputs],
        "attachments": [selected.relative_path for selected in attachments],
        "text_bytes": text_bytes,
        "packet_bytes": output.stat().st_size,
        "packet_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
