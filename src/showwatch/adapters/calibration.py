"""The site calibration file (Phase 10): everything BmsAdapter knows about a booking site.

The user records it once with `scripts/calibrate.py` (a headed browser; the user clicks, the
recorder only listens), checks the draft, and saves it as `calibration/bms-locators.yaml`.
tests/support/fakeshow-calibration.yaml is a complete, commented example.

What a file must say, and why:

* `steps`: how to find each allow-listed booking step (executor.guards.Step). There is no payment
  step, and an unknown step is refused.
* `forbidden`: at least one locator that must never be clicked (C1), such as the button that
  leads from the order summary to payment.
* `pages.payment`: how to recognise the payment page, so ShowWatch stops before it. Any page off
  the site's own hosts counts as payment too (BmsAdapter).
* `listing`: how to read shows from a date's listing page, by role and accessible name.
* `seat_map` and `hold_timer`: how to read seats and the hold countdown.
* `interstitials`: optional extra patterns; built-in title checks always apply (bms.py).

Problems are reported all at once, by location, without echoing values.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from showwatch.adapters.aria import ARIA_ROLES
from showwatch.adapters.base import LocatorSet, LocatorSpec
from showwatch.config import _describe, _fail, _StrictLoader, _yaml_problem
from showwatch.errors import CalibrationError

STEP_NAMES = ("choose_quantity", "select_seats", "seat", "proceed_to_summary")
SHOW_GROUPS = frozenset({"time", "format", "language", "price"})
MAX_FILE_BYTES = 64 * 1024
DEFAULT_PATH = Path("calibration") / "bms-locators.yaml"

_ATTRIBUTE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*")
_KEY = re.compile(r"[a-z][a-z0-9_]*")
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


def _regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise _fail("regex", f"not a valid regular expression ({exc.msg})") from None
    return value


def _attribute(value: str) -> str:
    if not _ATTRIBUTE.fullmatch(value):
        raise _fail("attribute", "not an HTML attribute name (like data-row)")
    return value


Pattern = Annotated[str, AfterValidator(_regex)]
Attribute = Annotated[str, AfterValidator(_attribute)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Locator(_Model):
    """One way to find an element (SPEC §5): a role (with a name), a test id, or exact text."""

    role: str | None = None
    name: str | None = None
    name_pattern: Pattern | None = None
    test_id: str | None = None
    text: str | None = None
    navigates: bool = False  # clicking it loads a new page

    @field_validator("role")
    @classmethod
    def _role(cls, value: str | None) -> str | None:
        if value is not None and value not in ARIA_ROLES:
            raise _fail("role", "not an ARIA role (use one get_by_role accepts, such as button)")
        return value

    @model_validator(mode="after")
    def _one_way(self) -> Self:
        ways = [w for w in (self.role, self.test_id, self.text) if w is not None]
        if len(ways) != 1:
            raise _fail("locator", "give exactly one of role, test_id or text")
        if self.role is None and (self.name is not None or self.name_pattern is not None):
            raise _fail("locator", "name and name_pattern go with a role")
        if self.name is not None and self.name_pattern is not None:
            raise _fail("locator", "give a name or a name_pattern, not both")
        return self

    def spec(self) -> LocatorSpec:
        return LocatorSpec(
            role=self.role,
            name=self.name,
            name_pattern=self.name_pattern,
            test_id=self.test_id,
            text=self.text,
            navigates=self.navigates,
        )


class Site(_Model):
    base_url: str
    login_path: str = "/"
    hosts: tuple[str, ...] = ()  # other hosts that belong to the booking site (not payment)
    timezone: str = "Asia/Kolkata"  # the timezone show times are listed in

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.query:
            raise _fail("base_url", "base_url must be the site's address, like https://example.com")
        return value.rstrip("/")

    @field_validator("login_path")
    @classmethod
    def _login_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise _fail("login_path", "login_path must be a path on the site, starting with /")
        return value

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(h.lower() for h in hosts)
        if not all(_HOST.fullmatch(h) for h in cleaned):
            raise _fail("hosts", "hosts are plain host names, like www.example.com")
        return cleaned

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise _fail(
                "timezone", "unknown timezone; use an IANA name such as Asia/Kolkata"
            ) from None
        return value

    @property
    def all_hosts(self) -> frozenset[str]:
        return frozenset({(urlsplit(self.base_url).hostname or "").lower(), *self.hosts})


class Steps(_Model):
    choose_quantity: Locator
    select_seats: Locator
    seat: Locator  # filled in with {row} and {col}, as the seat map gives them
    proceed_to_summary: Locator


class ShowLink(_Model):
    role: str = "link"
    # Applied to each link's accessible name. `time` is required; `format`, `language` and
    # `price` are used when present (see Calibration.assumptions()).
    name_pattern: Pattern
    id_pattern: Pattern | None = None  # applied to the href; `id` names the show

    @field_validator("name_pattern")
    @classmethod
    def _groups(cls, value: str) -> str:
        groups = set(re.compile(value).groupindex)
        if "time" not in groups:
            raise _fail("show_time", "name_pattern must capture the show time as (?P<time>...)")
        unknown = sorted(groups - SHOW_GROUPS)
        if unknown:
            raise _fail("show_groups", f"name_pattern groups can be {sorted(SHOW_GROUPS)} only")
        return value

    @field_validator("id_pattern")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        if value is not None and "id" not in re.compile(value).groupindex:
            raise _fail("show_id", "id_pattern must capture the show id as (?P<id>...)")
        return value


class Venue(_Model):
    role: str = "heading"
    level: int | None = Field(default=None, ge=1, le=6)
    name_pattern: Pattern | None = None  # only elements whose name matches are venues


class Listing(_Model):
    ready_pattern: Pattern  # text that is on the listing page once it has rendered
    tier: Literal["auto", "http", "browser"] = "auto"
    show: ShowLink
    venue: Venue = Field(default_factory=Venue)


class SeatMap(_Model):
    container: Locator
    row_attribute: Attribute
    col_attribute: Attribute  # the column index, counting gaps, as the seat locator wants it
    gap_attribute: Attribute | None = None  # a cell with this attribute = "true" is an aisle
    taken_attributes: tuple[Attribute, ...]  # present (and not "false") = not available

    @field_validator("taken_attributes")
    @classmethod
    def _taken(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if not names:
            raise _fail("taken", "list at least one attribute that marks a taken seat")
        return tuple(sorted(set(names)))


class PageRule(_Model):
    """A page is recognised when any pattern matches its path, title or visible text."""

    url_patterns: tuple[Pattern, ...] = ()  # matched against the URL's path
    title_patterns: tuple[Pattern, ...] = ()
    text_patterns: tuple[Pattern, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.url_patterns or self.title_patterns or self.text_patterns)

    def matches_url_or_title(self, *, path: str, title: str) -> bool:
        return any(re.search(p, path) for p in self.url_patterns) or any(
            re.search(p, title) for p in self.title_patterns
        )

    def matches_text(self, text: str) -> bool:
        return any(re.search(p, text) for p in self.text_patterns)


class Pages(_Model):
    payment: PageRule
    summary: PageRule | None = None  # recorded for reference

    @field_validator("payment")
    @classmethod
    def _payment(cls, rule: PageRule) -> PageRule:
        if rule.is_empty:
            raise _fail(
                "payment",
                "the payment page needs at least one url, title or text pattern: ShowWatch must "
                "recognise it to stop before it (C1)",
            )
        return rule


class Interstitials(_Model):
    captcha: PageRule = Field(default_factory=PageRule)
    queue: PageRule = Field(default_factory=PageRule)
    login: PageRule = Field(default_factory=PageRule)
    forbidden: PageRule = Field(default_factory=PageRule)


class Calibration(_Model):
    version: Literal[1]
    site: Site
    steps: Steps
    forbidden: dict[str, Locator]
    listing: Listing
    seat_map: SeatMap
    hold_timer: Locator
    pages: Pages
    interstitials: Interstitials = Field(default_factory=Interstitials)

    @field_validator("forbidden")
    @classmethod
    def _forbidden(cls, entries: dict[str, Locator]) -> dict[str, Locator]:
        if not entries:
            raise _fail(
                "forbidden",
                "list at least one locator that must never be clicked, such as the button that "
                "leads to payment (C1)",
            )
        if not all(_KEY.fullmatch(name) for name in entries):
            raise _fail("forbidden_name", "names are lowercase words joined by _, like pay_now")
        return entries

    def locators(self) -> LocatorSet:
        return {name: getattr(self.steps, name).spec() for name in STEP_NAMES}

    def forbidden_locators(self) -> LocatorSet:
        return {name: locator.spec() for name, locator in self.forbidden.items()}

    def assumptions(self) -> list[str]:
        """What ShowWatch will assume because the file does not say (shown by calibrate.py)."""
        groups = set(re.compile(self.listing.show.name_pattern).groupindex)
        notes = []
        if "format" not in groups:
            notes.append("show links do not name a format: each target's first format is used")
        if "language" not in groups:
            notes.append("show links do not name a language: each target's language is used")
        if "price" not in groups:
            notes.append(
                "show links carry no price: max_price_per_seat cannot be checked before the "
                "seat map (you still see the price before paying)"
            )
        if self.interstitials.captcha.is_empty or self.interstitials.queue.is_empty:
            notes.append(
                "no CAPTCHA or queue page described: only the built-in title checks spot them"
            )
        return notes


# --- entry points ----------------------------------------------------------------------------


def load_calibration(path: Path) -> Calibration:
    """Read and validate a calibration file. Every problem is reported in one CalibrationError."""
    source = str(path)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise CalibrationError([f"cannot read the file: {exc.strerror or exc}"], source) from None
    if len(data) > MAX_FILE_BYTES:
        raise CalibrationError([f"file is over {MAX_FILE_BYTES // 1024} KB"], source)
    try:
        raw = yaml.load(data.decode("utf-8"), Loader=_StrictLoader)  # noqa: S506 (a SafeLoader)
    except UnicodeDecodeError:
        raise CalibrationError(["file is not valid UTF-8 text"], source) from None
    except yaml.YAMLError as exc:
        raise CalibrationError([f"YAML syntax error: {_yaml_problem(exc)}"], source) from None
    return parse_calibration(raw, source=source)


def parse_calibration(raw: object, *, source: str | None = None) -> Calibration:
    if not isinstance(raw, Mapping):
        raise CalibrationError(["the top level must be a mapping (key: value lines)"], source)
    try:
        return Calibration.model_validate(raw)
    except ValidationError as exc:
        raise CalibrationError(_problems(exc, raw), source) from None


def _problems(exc: ValidationError, raw: Mapping[Any, Any]) -> list[str]:
    sections: list[str] = []
    steps: list[str] = []
    others: list[str] = []
    for error in exc.errors(include_input=False):
        loc = tuple(error["loc"])
        if error["type"] == "missing" and len(loc) == 1:
            sections.append(str(loc[0]))
        elif error["type"] == "missing" and len(loc) == 2 and loc[0] == "steps":
            steps.append(str(loc[1]))
        else:
            others.append(_describe(error, raw, Calibration))
    problems = []
    if sections:
        problems.append(f"missing sections: {', '.join(sorted(sections))}")
    if steps:
        problems.append(
            f"missing steps: {', '.join(sorted(steps))} (every one of {', '.join(STEP_NAMES)} "
            "is needed)"
        )
    return problems + others
