"""Reading SQL text the way PostgreSQL's scanner does: quotes, dollar quotes, nested
comments. Studio splits the console's batch with it; ``reset`` finds transaction control in
hand-written migrations."""

from __future__ import annotations

import re

# PostgreSQL's dolq_start / dolq_cont: any non-ASCII character counts as a letter.
_DOLLAR = re.compile(r"\$[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_\x80-\U0010ffff]*\$|\$\$")
# A -- comment ends at either newline character, as in PostgreSQL's scanner.
_LINE_END = re.compile(r"[\r\n]")


def _skip_quoted(text: str, i: int, quote: str, *, escapes: bool = False) -> int:
    """The index just past the closing ``quote``.

    ``escapes`` is for an ``E'...'`` string, where a backslash quotes the next character
    (``E'it\'s'``); a plain ``'...'`` has no backslash escapes, so it must not be treated
    as one.
    """
    i += 1
    while i < len(text):
        ch = text[i]
        if escapes and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if i + 1 < len(text) and text[i + 1] == quote:  # doubled: an escaped quote
                i += 2
                continue
            return i + 1
        i += 1
    return len(text)


def _skip_block_comment(text: str, i: int) -> int:
    """The index just past a ``/* ... */`` comment, nesting as PostgreSQL does."""
    depth, i = 1, i + 2
    while i < len(text) and depth:
        if text.startswith("/*", i):
            depth, i = depth + 1, i + 2
        elif text.startswith("*/", i):
            depth, i = depth - 1, i + 2
        else:
            i += 1
    return i


def _comment_end(text: str, i: int) -> int | None:
    """The index just past the comment starting at ``i``, or None if none starts there."""
    if text.startswith("--", i):
        end = _LINE_END.search(text, i)
        return len(text) if end is None else end.end()
    if text.startswith("/*", i):
        return _skip_block_comment(text, i)
    return None


def first_token(text: str) -> int:
    """The index of the first character that is neither whitespace nor inside a comment.

    PostgreSQL's comment rules (nested ``/* */``, ``--`` ending at ``\r`` or ``\n``) are the
    only ones that matter here: reading a comment differently from the server is how a
    ``COMMIT`` hid from the read-only check (B17).
    """
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif (end := _comment_end(text, i)) is not None:
            i = end
        else:
            return i
    return n


def split_statements(text: str) -> list[str]:
    """Split SQL on top-level semicolons, honouring single/double quotes, dollar-quoted
    bodies and comments, so the console can run several statements in one go."""
    parts: list[str] = []
    start, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            # E'...' (also e'...') has backslash escapes; a bare '...' does not.
            escapes = (
                ch == "'" and i > 0 and text[i - 1] in "Ee" and not _is_ident_char(text, i - 2)
            )
            i = _skip_quoted(text, i, ch, escapes=escapes)
        elif ch == "$":
            # ``a$b$`` is one identifier: a dollar quote only starts where a name cannot
            # continue.
            tag = None if _is_ident_char(text, i - 1) else _DOLLAR.match(text, i)
            if tag is None:
                i += 1
            else:
                close = text.find(tag.group(0), i + len(tag.group(0)))
                i = n if close == -1 else close + len(tag.group(0))
        elif (end := _comment_end(text, i)) is not None:
            i = end
        elif ch == ";":
            parts.append(text[start:i])
            start = i = i + 1
        else:
            i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if first_token(p) < len(p)]


def _is_ident_char(text: str, index: int) -> bool:
    """Whether ``text[index]`` can continue an identifier (``ident_cont`` in PostgreSQL's
    scanner, where every non-ASCII character counts as a letter)."""
    if index < 0:
        return False
    char = text[index]
    return char.isalnum() or char in "_$" or ord(char) >= 0x80


# Statements that end or start a transaction. A migration runs inside one, so these in its
# SQL commit part of it, or (under ``reset``) everything reset did before them.
_TRANSACTION_CONTROL = re.compile(
    r"(?:BEGIN|START|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE\s+TRANSACTION)\b",
    re.IGNORECASE,
)


_ATOMIC = re.compile(r"\bBEGIN\s+ATOMIC\b", re.IGNORECASE)
_ENDS = re.compile(r"\bEND\s*\Z", re.IGNORECASE)
_END = re.compile(r"END\b", re.IGNORECASE)


def transaction_control(text: str) -> str | None:
    """The first transaction-control statement in ``text`` (``COMMIT``...), or None.

    A SQL-standard function body (``BEGIN ATOMIC ... END``) holds semicolons outside any
    quotes, so its statements and closing ``END`` are skipped, not read as control."""
    atomic = False
    for statement in split_statements(text):
        body = statement[first_token(statement) :]
        if atomic:
            atomic = _END.match(body) is None  # the body ends at its own END
            continue
        if _ATOMIC.search(body):
            atomic = _ENDS.search(body) is None
            continue
        if (match := _TRANSACTION_CONTROL.match(body)) is not None:
            return match.group(0).upper()
    return None
