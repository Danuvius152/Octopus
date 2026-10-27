"""Line positions in unchanged text; a CRLF completes after its LF."""

import re
from bisect import bisect_right


class LineCoordinates:
    def __init__(self, text: str, first_line: int = 1, last_line: int | None = None):
        self.first_line = first_line
        self.break_ends = [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]
        # A delivered passage can stop between CR and LF. Its authoritative
        # endpoint distinguishes that CR from a complete lone-CR newline.
        if text.endswith("\r") and last_line == first_line + len(self.break_ends) - 1:
            self.break_ends.pop()

    def line(self, position: int) -> int:
        return self.first_line + bisect_right(self.break_ends, position)

    @classmethod
    def passage(cls, value):
        return cls(value["content"], value["line_start"], value["line_end"])
