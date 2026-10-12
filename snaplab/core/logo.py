"""The SNAP logo: two network plugs meeting with a spark between them.

ASCII art, so the boot menu, the setup wizard and the host's text screens all
draw the same thing. SPARK holds the (row, column) of each character drawn in
blue; everything else is white.

Standard library only (the host's text screens use it).
"""

from __future__ import annotations

ART = [
    "                    .-------.                    ",
    "                .--'         '--.                ",
    "              .'   \\    |    /   '.              ",
    "             /      \\   |   /      \\             ",
    "    .------.-+------.       .------+-.------.    ",
    "====|||||||| |[|||||]       [|||||]| ||||||||====",
    "====|||||||| |[|||||]       [|||||]| ||||||||====",
    "    '------'-+------'       '------+-'------'    ",
    "             \\      /   |   \\      /             ",
    "              '.   /    |    \\   .'              ",
    "                '--.         .--'                ",
    "                    '-------'                    ",
]
WIDTH = len(ART[0])
SPARK_ROWS = (2, 3, 8, 9)


def _spark() -> frozenset[tuple[int, int]]:
    """The spark's strokes: in the rows it crosses, everything between the arc's two ends."""
    cells = set()
    for row in SPARK_ROWS:
        line = ART[row]
        marks = [col for col, ch in enumerate(line) if ch != " "]
        # The arc is the first and last group of characters on the line.
        first_end = next(c for c in marks if line[c + 1] == " ")
        last_start = max(c for c in marks if line[c - 1] == " ")
        cells |= {(row, col) for col in marks if first_end < col < last_start}
    return frozenset(cells)


SPARK = _spark()


def runs(row: int) -> list[tuple[str, bool]]:
    """The row as (text, is_blue) runs, for drawing in two colours."""
    out: list[tuple[str, bool]] = []
    for col, ch in enumerate(ART[row]):
        blue = (row, col) in SPARK
        if out and out[-1][1] == blue:
            out[-1] = (out[-1][0] + ch, blue)
        else:
            out.append((ch, blue))
    return out


def html(blue_class: str = "spark") -> str:
    """The logo as preformatted HTML text, the spark wrapped in <span class=blue_class>."""
    rows = []
    for row in range(len(ART)):
        parts = []
        for text, blue in runs(row):
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f'<span class="{blue_class}">{text}</span>' if blue else text)
        rows.append("".join(parts))
    return "\n".join(rows)


def white_and_spark() -> tuple[list[str], list[str]]:
    """Two layers of the same size: the white art with the spark blanked, and the spark alone."""
    white, spark = [], []
    for row, line in enumerate(ART):
        white.append("".join(" " if (row, col) in SPARK else ch for col, ch in enumerate(line)))
        spark.append("".join(ch if (row, col) in SPARK else " " for col, ch in enumerate(line)))
    return white, spark
