"""Looks up a course's officially registered duration from the government
CRICOS course registry. Verified live against the real site: a course code
can be looked up with a plain GET to CourseDetails.aspx?CourseCode=<code> --
no need to drive the search form's ASP.NET postback at all. An unknown code
just redirects back to the empty search page instead of a details page, so
"not found" is detected by the duration field simply not being there.
"""

import re
import httpx

CRICOS_DETAILS_URL = "https://cricos.education.gov.au/Course/CourseDetails.aspx"

# Keyed off the page's actual element id rather than its human-readable
# label text, so a copy change on the page ("Duration (Weeks):" -> something
# else) doesn't silently break this.
DURATION_PATTERN = re.compile(r'id="ctl00_cphDefaultPage_courseDetail_lblDuration">(\d+)<')


class CricosLookupError(RuntimeError):
    pass


def lookup_duration_weeks(cricos_code: str) -> int | None:
    """Returns the registered duration in weeks, or None if the code isn't
    found on the registry. Raises CricosLookupError only for an actual
    network/request failure, not for a not-found code.
    """
    try:
        response = httpx.get(
            CRICOS_DETAILS_URL,
            params={"CourseCode": cricos_code},
            timeout=20.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CricosLookupError(f"CRICOS registry request failed: {exc}") from exc

    match = DURATION_PATTERN.search(response.text)
    return int(match.group(1)) if match else None
