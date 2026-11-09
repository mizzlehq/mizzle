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


def _skip(text: str, i: int) -> int | None:
    """The index just past the quoted string, quoted identifier, dollar-quoted body or
    comment starting at ``i``, or None if none starts there."""
    ch = text[i]
    if ch in "'\"":
        # E'...' (also e'...') has backslash escapes; a bare '...' does not.
        escapes = ch == "'" and i > 0 and text[i - 1] in "Ee" and not _is_ident_char(text, i - 2)
        return _skip_quoted(text, i, ch, escapes=escapes)
    if ch == "$":
        # ``a$b$`` is one identifier: a dollar quote only starts where a name cannot continue.
        tag = None if _is_ident_char(text, i - 1) else _DOLLAR.match(text, i)
        if tag is None:
            return None
        close = text.find(tag.group(0), i + len(tag.group(0)))
        return len(text) if close == -1 else close + len(tag.group(0))
    return _comment_end(text, i)


def split_statements(text: str) -> list[str]:
    """Split SQL on top-level semicolons, honouring single/double quotes, dollar-quoted
    bodies and comments, so the console can run several statements in one go."""
    parts: list[str] = []
    start, i, n = 0, 0, len(text)
    while i < n:
        if (end := _skip(text, i)) is not None:
            i = end
        elif text[i] == ";":
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


def transaction_control(text: str) -> str | None:
    """The first transaction-control statement in ``text`` (``COMMIT``...), or None.

    A SQL-standard function body (``BEGIN ATOMIC ... END``) holds semicolons outside any
    quotes, so its statements and closing ``END`` are skipped, not read as control. Words
    are read only outside quotes and comments (K3), and a ``CASE ... END`` inside the body
    does not close it (K2)."""
    depth: int | None = None  # CASE nesting inside a BEGIN ATOMIC body; None outside one
    start, prev, i, n = True, "", 0, len(text)
    while i < n:
        if (end := _skip(text, i)) is not None:
            i = end
            continue
        ch = text[i]
        if not _is_ident_char(text, i):
            if ch == ";" and depth is None:
                start = True
            elif not ch.isspace():
                start = False
            i += 1
            continue
        j = i
        while j < n and _is_ident_char(text, j):
            j += 1
        word = text[i:j].upper()
        if depth is not None:
            if word == "CASE":
                depth += 1
            elif word == "END":
                depth = None if depth == 0 else depth - 1
        elif start and (match := _TRANSACTION_CONTROL.match(text, i)) is not None:
            return match.group(0).upper()
        elif word == "ATOMIC" and prev == "BEGIN":
            depth = 0
        start, prev, i = False, word, j
    return None
