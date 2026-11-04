"""Loading and validating targets.yaml (SPEC §8).

User input enters through `load_settings` (a file), `parse_settings_yaml` (text) or
`parse_settings` (parsed data). Beyond the model rules, they add two checks:

* no secrets in the file (C7): secret-looking keys are refused, with a pointer to `.env`;
* no target date in the past, judged on the calendar of the configured timezone using the
  injected Clock.

The past-date rule applies to user input only. A stored target whose dates have since passed must
still load after a restart, so the models themselves do not check it.

Error messages name the key and where it is, but never echo input values. YAML traps that would
otherwise corrupt values silently are refused: unquoted times (YAML reads 18:00 as the integer
1080), duplicate keys, and booleans standing in for numbers or labels.
"""

import asyncio
import difflib
import inspect
import re
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictInt,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import ErrorDetails, PydanticCustomError

from showwatch import limits
from showwatch.clock import Clock
from showwatch.errors import ConfigError

DEFAULT_TIMEZONE = "Asia/Kolkata"
MAX_CONFIG_BYTES = 64 * 1024

Loc = tuple[str | int, ...]


def normalize_label(text: str) -> str:
    """The form used to compare languages, formats and venues: case and spacing are ignored."""
    return " ".join(text.split()).casefold()


def _fail(code: str, message: str) -> PydanticCustomError:
    # The message travels as context, so braces in it are never treated as a template.
    return PydanticCustomError(code, "{message}", {"message": message})


# --- field types -------------------------------------------------------------------------------

_HHMM = re.compile(r"(\d{1,2}):(\d{2})")
_TIME_HELP = 'expected a time like "18:00" (HH:MM, 24-hour)'


def _parse_local_time(value: object) -> time:
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise _fail(
                "time_tz", "times are wall-clock times in the config timezone; drop the offset"
            )
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        hours, minutes = divmod(value, 60)
        if value >= 0 and hours < 24:
            raise _fail(
                "time_unquoted",
                f"unquoted time: YAML reads it as the number {value}; "
                f'quote it: "{hours:02d}:{minutes:02d}"',
            )
        raise _fail("time_unquoted", f"unquoted time read as the number {value}; {_TIME_HELP}")
    if isinstance(value, str):
        match = _HHMM.fullmatch(value.strip())
        if match:
            hours, minutes = int(match[1]), int(match[2])
            if hours < 24 and minutes < 60:
                return time(hours, minutes)
    raise _fail("time_format", _TIME_HELP)


def _parse_optional_local_time(value: object) -> time | None:
    return None if value is None else _parse_local_time(value)


def _format_time(value: time) -> str:
    return value.strftime("%H:%M")


LocalTime = Annotated[
    time, BeforeValidator(_parse_local_time), PlainSerializer(_format_time, return_type=str)
]


def _format_optional_time(value: time | None) -> str | None:
    return None if value is None else _format_time(value)


OptionalLocalTime = Annotated[
    time | None,
    BeforeValidator(_parse_optional_local_time),
    PlainSerializer(_format_optional_time, return_type=str | None),
]

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        raise _fail("date_format", "expected a date like 2026-12-18 (YYYY-MM-DD), not a date-time")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise _fail("date_format", "expected a date like 2026-12-18 (YYYY-MM-DD)")


ConfigDate = Annotated[date, BeforeValidator(_parse_date)]

_ROW = re.compile(r"[A-Z0-9]{1,3}")
_ROW_HELP = 'row labels are 1-3 letters or digits, like "H" or "AA"'


def _parse_row(value: object) -> str:
    if isinstance(value, bool):
        raise _fail("row_label", f'{_ROW_HELP}; quote labels such as "ON" that YAML reads as true')
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str):
        label = value.strip().upper()
        if _ROW.fullmatch(label):
            return label
    raise _fail("row_label", _ROW_HELP)


RowLabel = Annotated[str, BeforeValidator(_parse_row)]

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY_LOOKUP = {name.lower(): name[:3] for name in _WEEKDAYS} | {
    name[:3].lower(): name[:3] for name in _WEEKDAYS
}


def _parse_day(value: object) -> object:
    return _DAY_LOOKUP.get(value.strip().lower(), value) if isinstance(value, str) else value


DayName = Annotated[
    Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], BeforeValidator(_parse_day)
]
Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]

_TARGET_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def _require_unique[T](
    values: Iterable[T], what: str, key: Callable[[T], Hashable] = lambda v: v, note: str = ""
) -> None:
    seen: set[Hashable] = set()
    for value in values:
        k = key(value)
        if k in seen:
            shown = value.isoformat() if isinstance(value, date) else repr(value)
            raise _fail("duplicate", f"duplicate {what} {shown}{note}")
        seen.add(k)


# --- models --------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, coerce_numbers_to_str=True
    )


class Mode(StrEnum):
    NOTIFY_ONLY = "notify-only"
    DRY_RUN = "dry-run"
    ASSIST = "assist"


class ShowtimeWindow(_Model):
    """Show start times to accept: wall-clock times in the config timezone, both ends included."""

    start: LocalTime
    end: LocalTime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise _fail(
                "window_order",
                f"start ({_format_time(self.start)}) must be before end "
                f"({_format_time(self.end)}); windows cannot cross midnight",
            )
        return self


class HotWindow(_Model):
    """When listings tend to open; polled at the hot interval. Wall-clock, config timezone."""

    days: tuple[DayName, ...]
    start: LocalTime
    end: LocalTime

    @field_validator("days")
    @classmethod
    def _days(cls, days: tuple[str, ...]) -> tuple[str, ...]:
        if not days:
            raise _fail("days_empty", "at least one day is required")
        _require_unique(days, "day")
        return days

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise _fail(
                "window_order",
                f"start ({_format_time(self.start)}) must be before end "
                f"({_format_time(self.end)}); windows cannot cross midnight",
            )
        return self


class Polling(_Model):
    base_interval_s: StrictInt = 900
    hot_interval_s: StrictInt = 60
    jitter_pct: StrictInt = 20
    hot_windows: tuple[HotWindow, ...] = ()

    @field_validator("base_interval_s", "hot_interval_s")
    @classmethod
    def _floor(cls, value: int, info: ValidationInfo) -> int:
        if value < limits.MIN_TARGET_INTERVAL_S:
            raise _fail(
                "interval_floor",
                f"{info.field_name} must be at least {limits.MIN_TARGET_INTERVAL_S} seconds "
                f"(hard floor, C4); got {value}",
            )
        return value

    @field_validator("jitter_pct")
    @classmethod
    def _jitter(cls, value: int) -> int:
        if not 0 <= value <= limits.MAX_JITTER_PCT:
            raise _fail(
                "jitter_range",
                f"jitter_pct must be between 0 and {limits.MAX_JITTER_PCT}; got {value}",
            )
        return value

    @model_validator(mode="after")
    def _hot_not_slower(self) -> Self:
        if self.hot_interval_s > self.base_interval_s:
            raise _fail(
                "interval_order",
                f"hot_interval_s ({self.hot_interval_s}) may not exceed base_interval_s "
                f"({self.base_interval_s})",
            )
        return self


class Target(_Model):
    """One movie to watch (SPEC §8). Construct it from user input via `parse_settings`."""

    id: str
    mode: Mode = Mode.NOTIFY_ONLY
    movie: Text
    city: Text
    language: Text
    formats: tuple[Text, ...]  # preference order
    venues: tuple[Text, ...] = ()  # ranked; empty = any venue
    dates: tuple[ConfigDate, ...]
    showtime_window: ShowtimeWindow
    seats: StrictInt
    preferred_rows: tuple[RowLabel, ...] = ()
    avoid_rows: tuple[RowLabel, ...] = ()
    max_price_per_seat: StrictInt  # rupees

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not _TARGET_ID.fullmatch(value):
            raise _fail(
                "target_id",
                "id must be 1-40 lowercase letters, digits or hyphens, starting with a letter or "
                "digit (e.g. movie-fri)",
            )
        return value

    @field_validator("formats")
    @classmethod
    def _formats(cls, formats: tuple[str, ...]) -> tuple[str, ...]:
        if not formats:
            raise _fail("formats_empty", "at least one format is required, in order of preference")
        _require_unique(formats, "format", normalize_label, " (compared ignoring case and spacing)")
        return formats

    @field_validator("venues")
    @classmethod
    def _venues(cls, venues: tuple[str, ...]) -> tuple[str, ...]:
        _require_unique(venues, "venue", normalize_label, " (compared ignoring case and spacing)")
        return venues

    @field_validator("dates")
    @classmethod
    def _dates(cls, dates: tuple[date, ...]) -> tuple[date, ...]:
        if not dates:
            raise _fail("dates_empty", "at least one date is required")
        _require_unique(dates, "date")
        return tuple(sorted(dates))

    @field_validator("seats")
    @classmethod
    def _seats(cls, value: int) -> int:
        if not 1 <= value <= limits.MAX_SEATS_PER_TARGET:
            raise _fail(
                "seats_range",
                f"seats must be between 1 and {limits.MAX_SEATS_PER_TARGET} "
                f"(personal-scale limit, C5); got {value}",
            )
        return value

    @field_validator("preferred_rows", "avoid_rows")
    @classmethod
    def _rows(cls, rows: tuple[str, ...]) -> tuple[str, ...]:
        _require_unique(rows, "row")
        return rows

    @field_validator("max_price_per_seat")
    @classmethod
    def _price(cls, value: int) -> int:
        if value <= 0:
            raise _fail(
                "price", f"max_price_per_seat must be a positive amount in rupees; got {value}"
            )
        return value

    @model_validator(mode="after")
    def _rows_disjoint(self) -> Self:
        both = sorted(set(self.preferred_rows) & set(self.avoid_rows))
        if both:
            raise _fail("rows_overlap", f"row {both[0]!r} is in both preferred_rows and avoid_rows")
        return self


class LlmSettings(_Model):
    """Optional help from Claude (Phase 9). Off the hot path, and never used for payment."""

    model: Text | None = None  # SHOWWATCH_LLM_MODEL overrides; default claude-opus-5-5
    # Off by default: healing sends a screenshot and an accessibility snapshot of the site page
    # (never the payment page) to Anthropic's API.
    healer: bool = False


class Settings(_Model):
    version: StrictInt
    timezone: str = DEFAULT_TIMEZONE
    notifier: Literal["telegram", "console"] = "console"
    polling: Polling = Field(default_factory=Polling)
    heartbeat_at: OptionalLocalTime = time(9, 0)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    targets: tuple[Target, ...]

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != 1:
            raise _fail(
                "version", f"version must be 1 (the only config format so far); got {value}"
            )
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        if _zone(value) is None:
            raise _fail("timezone", "unknown timezone; use an IANA name such as Asia/Kolkata")
        return value

    @field_validator("targets")
    @classmethod
    def _targets(cls, targets: tuple[Target, ...]) -> tuple[Target, ...]:
        if not targets:
            raise _fail("targets_empty", "at least one target is required")
        if len(targets) > limits.MAX_TARGETS:
            raise _fail(
                "targets_max",
                f"at most {limits.MAX_TARGETS} targets allowed (personal-scale limit, C5); "
                f"got {len(targets)}",
            )
        _require_unique((t.id for t in targets), "target id")
        return targets

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def local_datetime(self, on: date, at: time) -> datetime:
        """The tz-aware instant of wall-clock time `at` on `on` in the config timezone."""
        return datetime.combine(on, at, tzinfo=self.tz)


def _zone(name: object) -> ZoneInfo | None:
    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


# --- entry points ----------------------------------------------------------------------------


async def load_settings(path: Path, *, clock: Clock) -> Settings:
    """Read and validate a targets YAML file."""
    try:
        data = await asyncio.to_thread(_read_capped, path)
    except OSError as exc:
        raise ConfigError([f"cannot read the file: {exc.strerror or exc}"], str(path)) from None
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError(
            [f"file is too large (over {MAX_CONFIG_BYTES // 1024} KB); is this the right file?"],
            str(path),
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(["file is not valid UTF-8 text"], str(path)) from None
    return parse_settings_yaml(text, clock=clock, source=str(path))


def _read_capped(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(MAX_CONFIG_BYTES + 1)


def parse_settings_yaml(text: str, *, clock: Clock, source: str | None = None) -> Settings:
    try:
        raw = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 (_StrictLoader is a SafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError([f"YAML syntax error: {_yaml_problem(exc)}"], source) from None
    return parse_settings(raw, clock=clock, source=source)


def parse_settings(raw: object, *, clock: Clock, source: str | None = None) -> Settings:
    """Validate already-parsed config data. Every problem is reported in one ConfigError."""
    if not isinstance(raw, Mapping):
        raise ConfigError(
            ["the top level must be a mapping of settings (key: value lines)"], source
        )
    secrets = list(_secret_problems(raw, raw, ()))
    if secrets:
        raise ConfigError(secrets, source)

    problems: list[str] = []
    settings: Settings | None = None
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        problems.extend(_describe(error, raw) for error in exc.errors(include_input=False))
    problems.extend(_past_date_problems(raw, clock))
    if problems or settings is None:
        raise ConfigError(problems, source) from None
    return settings


# --- helpers: YAML -----------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys instead of silently keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:  # unhashable key: SafeLoader reports it properly below
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
        return super().construct_mapping(node, deep=deep)


def _yaml_problem(exc: yaml.YAMLError) -> str:
    # Built from the parts, without the source excerpt PyYAML would print (it could hold a secret).
    if not isinstance(exc, yaml.MarkedYAMLError):
        return "the file could not be parsed"
    text = exc.problem or "invalid YAML"
    if exc.problem_mark is not None:
        text += f" at line {exc.problem_mark.line + 1}, column {exc.problem_mark.column + 1}"
    if exc.context and exc.context_mark is not None:
        text += f" ({exc.context} at line {exc.context_mark.line + 1})"
    return text


# --- helpers: messages -----------------------------------------------------------------------


def _label(loc: Loc, raw: Mapping[Any, Any]) -> str:
    text = ""
    for position, item in enumerate(loc):
        if isinstance(item, int):
            text += f"[{item}]"
            if position == 1 and loc[0] == "targets":
                target_id = _raw_target_id(raw, item)
                if target_id:
                    text += f" ({target_id})"
        else:
            text += f".{item}" if text else str(item)
    return text or "config"


def _raw_target_id(raw: Mapping[Any, Any], index: int) -> str | None:
    targets = raw.get("targets")
    if isinstance(targets, Sequence) and not isinstance(targets, str) and index < len(targets):
        entry = targets[index]
        if isinstance(entry, Mapping):
            value = entry.get("id")
            if isinstance(value, str) and 0 < len(value) <= 40:
                return value
    return None


def _describe(error: ErrorDetails, raw: Mapping[Any, Any]) -> str:
    loc: Loc = tuple(error["loc"])
    where = _label(loc, raw)
    if error["type"] == "extra_forbidden":
        hint = _suggest(loc[:-1], str(loc[-1]))
        return f"{where}: unknown key" + (f" (did you mean {hint!r}?)" if hint else "")
    if error["type"] == "missing":
        return f"{where}: required key is missing"
    return f"{where}: {error['msg']}"


def _suggest(parent: Loc, key: str) -> str | None:
    model = _model_at(parent)
    if model is None:
        return None
    matches = difflib.get_close_matches(key, list(model.model_fields), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _model_at(loc: Loc) -> type[BaseModel] | None:
    model: type[BaseModel] = Settings
    for item in loc:
        if isinstance(item, int):
            continue
        field = model.model_fields.get(item)
        inner = _model_in(field.annotation) if field else None
        if inner is None:
            return None
        model = inner
    return model


def _model_in(annotation: object) -> type[BaseModel] | None:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        found = _model_in(arg)
        if found is not None:
            return found
    return None


# --- helpers: the user-input-only checks ---------------------------------------------------------

_SECRET_KEY = re.compile(
    r"token|secret|passw|api[_-]?key|otp|cvv|cookie|credential|chat[_-]?id", re.I
)


def _env_var_for(key: str) -> str | None:
    lowered = key.lower()
    if "chat" in lowered:
        return "TELEGRAM_CHAT_ID"
    if "anthropic" in lowered or re.search(r"api[_-]?key", lowered):
        return "ANTHROPIC_API_KEY"
    if "telegram" in lowered or "bot" in lowered:
        return "TELEGRAM_BOT_TOKEN"
    return None


def _secret_problems(node: object, raw: Mapping[Any, Any], loc: Loc) -> Iterator[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str) and _SECRET_KEY.search(key):
                var = _env_var_for(key)
                yield (
                    f"{_label((*loc, key), raw)}: '{key}' looks like a secret, and secrets are "
                    f"never read from the config file. Put it in .env"
                    f"{f' as {var}' if var else ''} (see .env.example) and delete it here."
                )
            elif isinstance(key, str | int):
                yield from _secret_problems(value, raw, (*loc, key))
    elif isinstance(node, Sequence) and not isinstance(node, str):
        for index, item in enumerate(node):
            yield from _secret_problems(item, raw, (*loc, index))


def _past_date_problems(raw: Mapping[Any, Any], clock: Clock) -> Iterator[str]:
    zone = _zone(raw.get("timezone", DEFAULT_TIMEZONE)) or ZoneInfo(DEFAULT_TIMEZONE)
    today = clock.now().astimezone(zone).date()
    targets = raw.get("targets")
    if not isinstance(targets, Sequence) or isinstance(targets, str):
        return
    for index, entry in enumerate(targets):
        dates = entry.get("dates") if isinstance(entry, Mapping) else None
        if not isinstance(dates, Sequence) or isinstance(dates, str):
            continue
        past: set[date] = set()
        for value in dates:
            try:
                parsed = _parse_date(value)
            except PydanticCustomError:
                continue  # reported by structural validation
            if parsed < today:
                past.add(parsed)
        if past:
            listed = ", ".join(d.isoformat() for d in sorted(past))
            verb = "is" if len(past) == 1 else "are"
            yield (
                f"{_label(('targets', index, 'dates'), raw)}: {listed} {verb} in the past "
                f"(today is {today.isoformat()} in {zone.key})"
            )
