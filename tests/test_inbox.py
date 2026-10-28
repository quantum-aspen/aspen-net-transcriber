import pytest

from transcriber import inbox

DOC = """Paste a Zoom share link into a new row. Leave Status empty.

| Recording | Series | Status |
|---|---|---|
| https://illinois.zoom.us/rec/share/AAA.bbb | Software stack meeting |  |
| [10-16](https://illinois.zoom.us/rec/share/CCC.ddd) | Software stack meeting | done |
| https://illinois.zoom.us/rec/share/EEE.fff |  | retry |
| not a link | x | |

Other table, no Status column:

| Recording | Notes |
|---|---|
| https://illinois.zoom.us/rec/share/ZZZ.zzz | ignored |
"""


def test_parse_finds_rows_in_status_tables_only():
    rows = inbox.parse(DOC)
    assert [r.url for r in rows] == [
        "https://illinois.zoom.us/rec/share/AAA.bbb",
        "https://illinois.zoom.us/rec/share/CCC.ddd",
        "https://illinois.zoom.us/rec/share/EEE.fff",
    ]
    assert rows[0].series == "Software stack meeting"
    assert [r.wants_run for r in rows] == [True, False, True]


def test_set_status_rewrites_only_that_row():
    out = inbox.set_status(DOC, "https://illinois.zoom.us/rec/share/AAA.bbb", "working")
    assert "| https://illinois.zoom.us/rec/share/AAA.bbb | Software stack meeting | working |\n" in out
    assert out.count("\n") == DOC.count("\n")
    assert inbox.parse(out)[1:] == inbox.parse(DOC)[1:]


def test_set_status_escapes_pipes_and_keeps_links():
    url = "https://illinois.zoom.us/rec/share/EEE.fff"
    out = inbox.set_status(DOC, url, "failed: a | b")
    row = inbox.parse(out)[2]
    assert row.status == "failed: a \\| b"
    assert row.url == url


def test_set_status_on_missing_row_is_a_no_op():
    assert inbox.set_status(DOC, "https://illinois.zoom.us/rec/share/NOPE", "x") == DOC


def test_short_row_gets_padded():
    doc = "| Recording | Series | Status |\n|---|---|---|\n| https://x.zoom.us/rec/share/Q |\n"
    out = inbox.set_status(doc, "https://x.zoom.us/rec/share/Q", "queued")
    assert inbox.parse(out)[0].status == "queued"


Z1 = "https://illinois.zoom.us/rec/share/AAA.bbb"
Z2 = "https://illinois.zoom.us/rec/share/X_y.z"


def test_adopt_creates_table_from_a_plain_list():
    doc = f"Paste Zoom links below.\n\n- {Z1}\n- [{Z2}]({Z2}) Software stack meeting\n- a note\n"
    out, adopted = inbox.adopt(doc)
    assert adopted == [Z1, Z2]
    assert out == (
        "Paste Zoom links below.\n\n- a note\n\n"
        "| Recording | Series | Status |\n|---|---|---|\n"
        f"| {Z1} |  | |\n| {Z2} | Software stack meeting | |\n"
    )
    assert [(r.url, r.series, r.wants_run) for r in inbox.parse(out)] == [
        (Z1, "", True), (Z2, "Software stack meeting", True),
    ]  # fmt: skip
    assert inbox.adopt(out) == (out, [])


def test_adopt_appends_to_existing_table_and_drops_duplicates():
    out, adopted = inbox.adopt(DOC + f"\n* Software stack meeting — <{Z2}>\n{Z1}\n")
    assert adopted == [Z2]
    rows = inbox.parse(out)
    assert rows[3].url == Z2 and rows[3].series == "Software stack meeting"
    assert out.count(Z1) == 1
    # the new row lands inside the queue table, not the later one
    assert out.index(Z2) < out.index("Other table")


@pytest.mark.parametrize(
    "line,series",
    [
        (f"1. {Z1} - Weekly sync", "Weekly sync"),
        (f"- [ ] Weekly sync: {Z1}", "Weekly sync"),
        (f"[10-23 recording]({Z1})", ""),
        (f"{Z1.replace('_', '')}", ""),
        (f"- a | b {Z1}", "a \\| b"),
    ],
)
def test_adopt_series_from_the_rest_of_the_line(line, series):
    out, _ = inbox.adopt(line + "\n")
    assert inbox.parse(out)[0].series == series


def test_adopt_unescapes_outline_escapes():
    out, adopted = inbox.adopt(f"- [{Z2.replace('_', chr(92) + '_')}]({Z2})\n")
    assert adopted == [Z2]
    assert inbox.set_status(out, Z2, "queued") != out


def test_adopt_skips_code_blocks():
    doc = f"```\n{Z1}\n```\n"
    assert inbox.adopt(doc) == (doc, [])
