"""The Recordings inbox: an Outline doc people paste Zoom links into.

People just add links, one per line or bullet, optionally with the series:

    - https://illinois.zoom.us/rec/share/...
    - https://illinois.zoom.us/rec/share/... Software stack meeting

adopt() moves each such line into the queue table, which the poller owns:

    | Recording | Series | Status |
    |---|---|---|
    | https://illinois.zoom.us/rec/share/... | Software stack meeting | |

Any table with a Status column counts as the queue; Recording is whichever
cell holds a Zoom link, and Series is optional. The poller fills Status in as
it goes. Typing `retry` into it asks for a re-run.

A pasted line may carry the recording's passcode as `passcode: <code>`
anywhere on it. adopt() takes it out, so it isn't copied into the table, and
returns it to the caller.
"""

import re
from dataclasses import dataclass

ZOOM = re.compile(r"https://[\w.-]*zoom\.us/rec/(?:share|play)/[^\s)\]|>]+")
# `passcode: X` or `password: X`, any case. The code is everything up to the next space.
PASSCODE = re.compile(r"\bpass(?:code|word)\s*[:=]\s*(\S+)", re.IGNORECASE)
WANTS_RUN = {"", "retry"}
QUEUE_HEADER = "| Recording | Series | Status |\n|---|---|---|\n"
# What's left of a pasted line once its links are gone: list markers and separators.
_LEFTOVER = re.compile(r"^\s*(?:[-*+]|\d+[.)]|\[[ xX]\])?\s*|^[\s:—–-]+|[\s:—–-]+$")


@dataclass(frozen=True)
class Row:
    url: str
    series: str
    status: str

    @property
    def wants_run(self) -> bool:
        return self.status.strip().lower() in WANTS_RUN


def _links(text: str) -> list[str]:
    """Zoom links in text, in order, once each. Outline may backslash-escape
    characters in link text; Zoom urls never contain backslashes."""
    return list(dict.fromkeys(m.group(0).replace("\\", "") for m in ZOOM.finditer(text)))


def _cells(line: str) -> list[str] | None:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in re.split(r"(?<!\\)\|", s[1:-1])]


def _is_rule(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def _tables(lines: list[str]):
    """Yields (header cells, [(line index, cells)], end) for each table with a Status
    column, where end is the index of the line after the table."""
    i = 0
    while i < len(lines):
        head = _cells(lines[i])
        rule = _cells(lines[i + 1]) if i + 1 < len(lines) else None
        if head and rule and _is_rule(rule) and "status" in [h.lower() for h in head]:
            rows = []
            j = i + 2
            while j < len(lines) and (cells := _cells(lines[j])) is not None:
                rows.append((j, cells))
                j += 1
            yield [h.lower() for h in head], rows, j
            i = j
        else:
            i += 1


def parse(text: str) -> list[Row]:
    out = []
    for head, rows, _ in _tables(text.splitlines()):
        status_i = head.index("status")
        series_i = head.index("series") if "series" in head else None
        for _, cells in rows:
            link = next((links[0] for c in cells if (links := _links(c))), None)
            if not link:
                continue
            out.append(
                Row(
                    url=link,
                    series=cells[series_i] if series_i is not None and series_i < len(cells) else "",
                    status=cells[status_i] if status_i < len(cells) else "",
                )
            )
    return out


def _series(line: str) -> str:
    # Only text outside links counts: a link's own title is usually a date or "recording".
    rest = re.sub(r"\[[^\]]*\]\([^)]*\)", "", line)
    rest = ZOOM.sub("", rest).replace("<>", "")
    prev = None
    while prev != rest:
        prev, rest = rest, _LEFTOVER.sub("", rest)
    return rest.replace("|", "\\|").strip()


def _passcode(raw: str) -> str:
    """Undoes Outline's markdown escaping (`a\\_b`) and drops quotes or backticks."""
    return re.sub(r"\\(.)", r"\1", raw).strip("`'\"")


def adopt(text: str) -> tuple[str, dict[str, str]]:
    """Moves lines with Zoom links outside the queue table into it.

    Returns the new text and {url: passcode} for every link taken from a pasted
    line (passcode "" if none). Links already queued are just removed from the
    list, but still returned, so a re-pasted link can bring its passcode. The
    table is appended to the doc if it's missing.
    """
    lines = text.splitlines(keepends=True)
    bare = [ln.rstrip("\r\n") for ln in lines]
    tables = list(_tables(bare))
    in_table = {j for _, rows, _ in tables for j, _ in rows}
    queued = {r.url for r in parse(text)}
    new_rows, drop, adopted = [], set(), {}
    fenced = False
    for j, line in enumerate(bare):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or j in in_table or _cells(line) is not None:
            continue
        urls = _links(line)
        if not urls:
            continue
        drop.add(j)
        passcode = _passcode(m.group(1)) if (m := PASSCODE.search(line)) else ""
        series = _series(PASSCODE.sub("", line))
        for url in urls:
            adopted[url] = passcode
            if url in queued:
                continue
            queued.add(url)
            new_rows.append(f"| {url} | {series} | |\n")
    if not drop:
        return text, {}
    nl = "\r\n" if "\r\n" in text else "\n"
    rows = "".join(new_rows).replace("\n", nl)
    out = [ln for j, ln in enumerate(lines) if j not in drop]
    if tables:
        end = tables[0][2]
        at = end - sum(1 for j in drop if j < end)
    else:
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += nl
        out.append(nl + QUEUE_HEADER.replace("\n", nl))
        at = len(out)
    if at > 0 and not out[at - 1].endswith(("\n", "\r")):
        out[at - 1] += nl
    out.insert(at, rows)
    return "".join(out), adopted


def set_status(text: str, url: str, status: str) -> str:
    """Returns text with the Status cell of url's row replaced. Unchanged if the row is gone."""
    lines = text.splitlines(keepends=True)
    bare = [ln.rstrip("\r\n") for ln in lines]
    cell = status.replace("|", "\\|").replace("\n", " ")
    for head, rows, _ in _tables(bare):
        status_i = head.index("status")
        for j, cells in rows:
            if not any(url in _links(c) for c in cells):
                continue
            cells = cells + [""] * (len(head) - len(cells))
            cells[status_i] = cell
            ending = lines[j][len(bare[j]) :]
            lines[j] = "| " + " | ".join(cells) + " |" + ending
            return "".join(lines)
    return text
