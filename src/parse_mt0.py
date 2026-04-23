"""HSpice .mt0 measurement-table parser.

Parses PrimeSim HSPICE `.mt0` output files into an immutable
`Mt0Result`. Handles the HSpice wrap convention (column names and
data rows span multiple physical lines with no explicit delimiter)
by flattening all non-header tokens and reshaping against the
column count.

Privacy posture: `Mt0ParseError.__str__` emits only the error
category + optional stage + line number — never the raw offending
snippet. `.mt0` payloads can carry absolute paths inside `.TITLE`
lines, and the upstream caller is not guaranteed to have run the
scrubber before handing text to the parser. The raw snippet is
still retained on `err.snippet` for in-process debugging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence


_HEADER_RE = re.compile(
    r"^\$DATA1\s+"
    r"SOURCE\s*=\s*'(?P<source>[^']*)'\s+"
    r"VERSION\s*=\s*'(?P<version>[^']*)'"
    r"(?:\s+PARAM_COUNT\s*=\s*(?P<param_count>\d+))?"
    r"\s*$"
)

_TITLE_RE = re.compile(r"^\.TITLE\s+'(?P<title>[^']*)'\s*$")

# Strict HSpice-style numeric literal: sign, integer/fractional digits,
# optional decimal point, optional exponent. Intentionally rejects
# `inf`, `nan`, `Infinity`, etc. — Python's `float()` accepts those but
# HSpice `.mt0` output never emits them, and accepting them would let a
# column literally named `inf` flip the column/data boundary.
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


class Mt0ParseError(Exception):
    """Raised when a `.mt0` payload cannot be parsed.

    Attributes (all public, safe to read in-process):

        category : short machine-readable error label
        line_no  : 1-based source line (None for whole-file errors)
        snippet  : raw offending input — retained for debugging only,
                   never included in `str(err)` since `.mt0` bodies
                   may carry absolute paths the scrubber hasn't seen
        stage    : optional pipeline stage tag, mirroring
                   `hspice_scrub.ScrubError.stage`
    """

    def __init__(
        self,
        category: str,
        *,
        line_no: int | None = None,
        snippet: str | None = None,
        stage: str = "",
    ) -> None:
        self.category = category
        self.line_no = line_no
        self.snippet = snippet
        self.stage = stage
        head = f"[{stage}] " if stage else ""
        line_str = f" at line {line_no}" if line_no is not None else ""
        super().__init__(f"{head}parse failed{line_str}: {category}")


@dataclass(frozen=True)
class Mt0Result:
    header: Mapping[str, str]
    title: str
    columns: Sequence[str]
    rows: Sequence[Sequence[float]]
    alter_number: int

    @property
    def param_count(self) -> int:
        raw = self.header.get("param_count")
        if raw is None or raw == "":
            return 0
        return int(raw)

    @property
    def measure_count(self) -> int:
        return len(self.columns) - self.param_count - 2


def _looks_like_float(tok: str) -> bool:
    """Strict HSpice-numeric check.

    Rejects `inf`, `nan`, and other non-decimal literals that Python's
    `float()` would happily accept but which `.mt0` output never
    contains. Using loose `float()` here would let a column named
    `inf` be misclassified as data and flip the header/data boundary.
    """
    return bool(_FLOAT_RE.match(tok))


def parse_mt0(text: str) -> Mt0Result:
    """Parse an HSpice `.mt0` measurement table.

    The file layout is:

        $DATA1 SOURCE='...' VERSION='...' PARAM_COUNT=<int>
        .TITLE '<string>'
        <col_name> <col_name> ... (possibly wrapped across lines)
        <float>    <float>    ... (possibly wrapped)
        ...

    The last column is always `alter#`, whose value is identical on
    every row of a single-alter file.
    """
    if text is None:
        raise Mt0ParseError("empty input")
    lines = text.splitlines()
    if not lines:
        raise Mt0ParseError("empty input")

    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines):
        raise Mt0ParseError("empty input")

    header_line = lines[idx]
    m = _HEADER_RE.match(header_line.strip())
    if not m:
        raise Mt0ParseError(
            "malformed $DATA1 header",
            line_no=idx + 1,
            snippet=header_line.strip(),
        )
    header: dict[str, str] = {
        "source": m.group("source"),
        "version": m.group("version"),
    }
    if m.group("param_count") is not None:
        header["param_count"] = m.group("param_count")
    idx += 1

    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines):
        raise Mt0ParseError(
            "missing .TITLE line",
            line_no=idx + 1,
            snippet="",
        )
    title_line = lines[idx]
    tm = _TITLE_RE.match(title_line.strip())
    if not tm:
        raise Mt0ParseError(
            "malformed .TITLE line",
            line_no=idx + 1,
            snippet=title_line.strip(),
        )
    title = tm.group("title")
    idx += 1

    tokens: list[str] = []
    for body_line in lines[idx:]:
        stripped = body_line.strip()
        if not stripped:
            continue
        tokens.extend(stripped.split())

    if not tokens:
        raise Mt0ParseError("no column or data tokens after .TITLE")

    boundary = None
    for i, tok in enumerate(tokens):
        if _looks_like_float(tok):
            boundary = i
            break
    if boundary is None:
        raise Mt0ParseError(
            "no numeric data tokens found after column header",
            snippet=" ".join(tokens[:6]),
        )
    if boundary == 0:
        raise Mt0ParseError(
            "no column names found before numeric data",
            snippet=tokens[0],
        )

    columns = tokens[:boundary]
    data_tokens = tokens[boundary:]
    n_cols = len(columns)

    if columns[-1].lower() != "alter#":
        raise Mt0ParseError(
            "last column is not 'alter#'",
            snippet=columns[-1],
        )

    # PARAM_COUNT range check — guards against a malformed header that
    # would otherwise produce a negative or nonsensical measure_count.
    # The header regex already constrains PARAM_COUNT to \d+ (>=0), so
    # here we only need the upper bound.
    pc_raw = header.get("param_count")
    if pc_raw is not None:
        pc = int(pc_raw)
        if pc > n_cols - 2:
            raise Mt0ParseError(
                f"PARAM_COUNT ({pc}) exceeds column count minus 2 ({n_cols - 2})",
                snippet=pc_raw,
            )

    if len(data_tokens) % n_cols != 0:
        raise Mt0ParseError(
            f"data token count {len(data_tokens)} "
            f"is not a multiple of column count {n_cols}",
            snippet=" ".join(data_tokens[-min(6, len(data_tokens)) :]),
        )

    rows: list[list[float]] = []
    for row_start in range(0, len(data_tokens), n_cols):
        chunk = data_tokens[row_start : row_start + n_cols]
        try:
            row = [float(t) for t in chunk]
        except ValueError as exc:
            bad_tok = None
            for t in chunk:
                if not _looks_like_float(t):
                    bad_tok = t
                    break
            raise Mt0ParseError(
                f"could not parse float in data row {len(rows) + 1}",
                snippet=bad_tok or str(exc),
            ) from None
        rows.append(row)

    if not rows:
        raise Mt0ParseError("no data rows parsed")

    alter_raw = rows[0][-1]
    if not float(alter_raw).is_integer():
        raise Mt0ParseError(
            "alter# column is not an integer",
            snippet=str(alter_raw),
        )
    alter_number = int(alter_raw)
    # Every row must carry an integer alter# equal to row 0; bare
    # `int(row[-1])` would silently truncate e.g. 1.5 → 1 and mask
    # a malformed .mt0.
    for i, row in enumerate(rows[1:], start=2):
        rv = row[-1]
        if not float(rv).is_integer():
            raise Mt0ParseError(
                f"alter# column is not an integer in row {i}",
                snippet=str(rv),
            )
        if int(rv) != alter_number:
            raise Mt0ParseError(
                f"alter# inconsistent across rows "
                f"(row {i}={int(rv)} vs row 1={alter_number})",
                snippet=str(rv),
            )

    return Mt0Result(
        header=dict(header),
        title=title,
        columns=tuple(columns),
        rows=tuple(tuple(r) for r in rows),
        alter_number=alter_number,
    )
