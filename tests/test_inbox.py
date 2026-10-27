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
