"""The Recordings inbox: a markdown table in an Outline doc.

    | Recording | Series | Status |
    |---|---|---|
    | https://illinois.zoom.us/rec/share/... | Software stack meeting | |

Any table with a Status column counts; Recording is whichever cell holds a
Zoom link, and Series is optional. People add rows with an empty Status. The
poller fills Status in as it goes. Typing `retry` into it asks for a re-run.
"""

import re
from dataclasses import dataclass

ZOOM = re.compile(r"https://[\w.-]*zoom\.us/rec/(?:share|play)/[^\s)\]|>]+")
WANTS_RUN = {"", "retry"}


@dataclass(frozen=True)
class Row:
    url: str
    series: str
    status: str

    @property
    def wants_run(self) -> bool:
        return self.status.strip().lower() in WANTS_RUN


def _cells(line: str) -> list[str] | None:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in re.split(r"(?<!\\)\|", s[1:-1])]


def _is_rule(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def _tables(lines: list[str]):
    """Yields (header cells, [(line index, cells)]) for each table with a Status column."""
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
            yield [h.lower() for h in head], rows
            i = j
        else:
            i += 1


def parse(text: str) -> list[Row]:
    out = []
    for head, rows in _tables(text.splitlines()):
        status_i = head.index("status")
        series_i = head.index("series") if "series" in head else None
        for _, cells in rows:
            link = next((m.group(0) for c in cells if (m := ZOOM.search(c))), None)
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


def set_status(text: str, url: str, status: str) -> str:
    """Returns text with the Status cell of url's row replaced. Unchanged if the row is gone."""
    lines = text.splitlines(keepends=True)
    bare = [ln.rstrip("\r\n") for ln in lines]
    cell = status.replace("|", "\\|").replace("\n", " ")
    for head, rows in _tables(bare):
        status_i = head.index("status")
        for j, cells in rows:
            if not any(url in c for c in cells):
                continue
            cells = cells + [""] * (len(head) - len(cells))
            cells[status_i] = cell
            ending = lines[j][len(bare[j]) :]
            lines[j] = "| " + " | ".join(cells) + " |" + ending
            return "".join(lines)
    return text
