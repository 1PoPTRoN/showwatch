"""ARIA roles and accessible names, computed from markup (pure: no I/O).

A simplified version of the browser's rules (WAI-ARIA implicit roles, accname), enough to read a
listing page from its HTML and to suggest locators while recording. Where it differs from a
browser, the browser is right: the browser tier and `showwatch canary` use Playwright's own roles.

Precedence for a name: aria-labelledby, aria-label, a form control's <label>, a button input's
value, an image's alt, the element's text (for roles named by their content), then the title.
"""

from collections.abc import Mapping

ARIA_ROLES = frozenset(
    {
        "alert", "alertdialog", "application", "article", "banner", "blockquote", "button",
        "caption", "cell", "checkbox", "code", "columnheader", "combobox", "complementary",
        "contentinfo", "definition", "deletion", "dialog", "directory", "document", "emphasis",
        "feed", "figure", "form", "generic", "grid", "gridcell", "group", "heading", "img",
        "insertion", "link", "list", "listbox", "listitem", "log", "main", "marquee", "math",
        "meter", "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio", "navigation",
        "none", "note", "option", "paragraph", "presentation", "progressbar", "radio",
        "radiogroup", "region", "row", "rowgroup", "rowheader", "scrollbar", "search",
        "searchbox", "separator", "slider", "spinbutton", "status", "strong", "subscript",
        "superscript", "switch", "tab", "table", "tablist", "tabpanel", "term", "textbox", "time",
        "timer", "toolbar", "tooltip", "tree", "treegrid", "treeitem",
    }
)  # fmt: skip

# Roles whose accessible name comes from their content when nothing else names them.
NAMED_BY_CONTENT = frozenset(
    {
        "button", "cell", "checkbox", "columnheader", "gridcell", "heading", "link", "menuitem",
        "menuitemcheckbox", "menuitemradio", "option", "radio", "row", "rowheader", "switch",
        "tab", "tooltip", "treeitem",
    }
)  # fmt: skip

_SIMPLE = {
    "button": "button",
    "dialog": "dialog",
    "h1": "heading",
    "h2": "heading",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "li": "listitem",
    "main": "main",
    "nav": "navigation",
    "ol": "list",
    "option": "option",
    "table": "table",
    "td": "cell",
    "textarea": "textbox",
    "th": "columnheader",
    "tr": "row",
    "ul": "list",
}
_INPUT = {
    "button": "button",
    "checkbox": "checkbox",
    "image": "button",
    "number": "spinbutton",
    "radio": "radio",
    "range": "slider",
    "reset": "button",
    "search": "searchbox",
    "submit": "button",
}
_TEXT_INPUTS = frozenset({"", "email", "tel", "text", "url"})


def normalise(text: str) -> str:
    return " ".join(text.split())


def role_of(tag: str, attrs: Mapping[str, str]) -> str | None:
    """The element's ARIA role: its first known explicit role, else the tag's implicit one."""
    for token in attrs.get("role", "").split():
        if token in ARIA_ROLES:
            return token
    tag = tag.lower()
    if tag in ("a", "area"):
        return "link" if "href" in attrs else None
    if tag == "select":
        multiple = "multiple" in attrs or _int(attrs.get("size")) > 1
        return "listbox" if multiple else "combobox"
    if tag == "input":
        kind = attrs.get("type", "").lower()
        if kind in _INPUT:
            return _INPUT[kind]
        if kind in _TEXT_INPUTS:
            return "combobox" if "list" in attrs else "textbox"
        return None
    if tag == "img":
        return "img" if attrs.get("alt", "x") != "" else None
    if tag == "section":
        return "region" if attrs.get("aria-label") or attrs.get("aria-labelledby") else None
    return _SIMPLE.get(tag)


def heading_level(tag: str, attrs: Mapping[str, str]) -> int | None:
    level = _int(attrs.get("aria-level"))
    if level > 0:
        return level
    tag = tag.lower()
    if len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
        return int(tag[1])
    return None


def name_from(
    role: str | None,
    tag: str,
    attrs: Mapping[str, str],
    *,
    content: str,
    label: str = "",
    labelledby: str = "",
) -> str:
    """The accessible name, from the pieces a page gives (see the module docstring)."""
    tag = tag.lower()
    for candidate in (labelledby, attrs.get("aria-label", "")):
        if normalise(candidate):
            return normalise(candidate)
    if tag in ("input", "select", "textarea", "meter", "progress") and normalise(label):
        return normalise(label)
    if tag == "input":
        kind = attrs.get("type", "").lower()
        if kind in ("button", "submit", "reset") and normalise(attrs.get("value", "")):
            return normalise(attrs["value"])
        if kind == "submit":
            return "Submit"
        if kind == "image" and normalise(attrs.get("alt", "")):
            return normalise(attrs["alt"])
    if tag == "img" and normalise(attrs.get("alt", "")):
        return normalise(attrs["alt"])
    if role in NAMED_BY_CONTENT and normalise(content):
        return normalise(content)
    return normalise(attrs.get("title", ""))


def _int(value: str | None) -> int:
    try:
        return int(value or "")
    except ValueError:
        return 0
