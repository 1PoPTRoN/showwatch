"""FakeShow: a plain, unbranded fake cinema site for ShowWatch's tests (SPEC §9).

This is a test harness, not a copy of any real site. The booking flow goes: movie page → listing
→ quantity dialog → seat map → order summary → payment page → confirmation. There are
interstitials (CAPTCHA, queue, login, a 403 block page), a control API under `/__control/` and a
request log. Control traffic is not logged, because it is not site traffic.
"""

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlencode

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tests.fakesite import pages
from tests.fakesite.state import INTERSTITIALS, ControlError, FakeShow, SiteState, now_utc

SESSION_COOKIE = "fakeshow_session"


def _state(request: Request) -> SiteState:
    state: SiteState = request.app.state.site
    return state


async def _form(request: Request) -> dict[str, str]:
    # Parsed by hand: Starlette's form parser needs python-multipart, which is not a dependency.
    body = (await request.body()).decode("utf-8", "replace")
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


def _seats(raw: str) -> list[str]:
    return [s for s in (part.strip() for part in raw.split(",")) if s]


def _qty(raw: str | None) -> int:
    try:
        return max(1, min(10, int(raw or "2")))
    except ValueError:
        return 2


class SiteMiddleware(BaseHTTPMiddleware):
    """Forced status codes, interstitial redirects, latency and the request log, for site paths."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path.startswith("/__control"):
            return await call_next(request)
        state = _state(request)
        if state.latency_ms:
            await asyncio.sleep(state.latency_ms / 1000)
        response: Response
        if state.status_code is not None:
            response = _forced(state.status_code)
        elif state.interstitial and path != f"/{state.interstitial}":
            response = RedirectResponse(f"/{state.interstitial}", status_code=302)
        else:
            response = await call_next(request)
        state.log.append(
            {
                "method": request.method,
                "path": path,
                "query": request.url.query,
                "status": response.status_code,
                "at": now_utc().isoformat(),
            }
        )
        if SESSION_COOKIE not in request.cookies:
            response.set_cookie(SESSION_COOKIE, now_utc().strftime("%Y%m%d%H%M%S%f"), httponly=True)
        return response


def _forced(status: int) -> Response:
    if status == 403:
        body = pages.message_page("blocked", "Access denied", "Your request was blocked.")
    elif status == 429:
        body = pages.message_page("error", "Too many requests", "Please slow down.")
    else:
        body = pages.message_page("error", "Something went wrong", f"Error {status}.")
    return HTMLResponse(body, status_code=status)


# --- site pages ----------------------------------------------------------------------------------


async def home(request: Request) -> Response:
    return RedirectResponse("/movie", status_code=302)


async def movie(request: Request) -> Response:
    return HTMLResponse(pages.movie_page(_state(request)))


async def listing(request: Request) -> Response:
    return HTMLResponse(pages.listing_page(_state(request)))


def _show_or_404(request: Request) -> tuple[SiteState, FakeShow] | Response:
    state = _state(request)
    show = state.show(request.path_params["show_id"])
    if show is None or not state.booking_open:
        return HTMLResponse(
            pages.message_page("error", "Not found", "No such show."), status_code=404
        )
    return state, show


async def quantity(request: Request) -> Response:
    found = _show_or_404(request)
    if isinstance(found, Response):
        return found
    state, show = found
    return HTMLResponse(pages.quantity_page(state, show))


async def seat_map(request: Request) -> Response:
    found = _show_or_404(request)
    if isinstance(found, Response):
        return found
    state, show = found
    unavailable = state.unavailable(show.show_id, now_utc())
    selected = [s for s in _seats(request.query_params.get("selected", "")) if s not in unavailable]
    return HTMLResponse(
        pages.seats_page(
            state,
            show,
            qty=_qty(request.query_params.get("qty")),
            selected=selected,
            unavailable=unavailable,
            notice=request.query_params.get("notice", ""),
        )
    )


def _back_to_map(show_id: str, qty: int, selected: list[str], notice: str = "") -> Response:
    query = {"qty": qty, "selected": ",".join(selected)}
    if notice:
        query["notice"] = notice
    return RedirectResponse(f"/show/{show_id}/seats?{urlencode(query)}", status_code=303)


async def select_seat(request: Request) -> Response:
    found = _show_or_404(request)
    if isinstance(found, Response):
        return found
    state, show = found
    form = await _form(request)
    qty, selected, seat = (
        _qty(form.get("qty")),
        _seats(form.get("selected", "")),
        form.get("seat", ""),
    )
    unavailable = state.unavailable(show.show_id, now_utc())
    if seat not in state.seat_labels():
        return _back_to_map(show.show_id, qty, selected, f"There is no seat {seat}.")
    if state.steal_seats_on_click > 0 and seat not in selected:
        state.steal_seats_on_click -= 1
        state.taken.add(seat)  # someone else got there first
        return _back_to_map(show.show_id, qty, selected, f"Sorry, seat {seat} was just taken.")
    if seat in unavailable:
        return _back_to_map(show.show_id, qty, selected, f"Seat {seat} is not available.")
    if seat in selected:
        selected.remove(seat)
    elif len(selected) >= qty:
        return _back_to_map(show.show_id, qty, selected, f"You have already chosen {qty} seats.")
    else:
        selected.append(seat)
    return _back_to_map(show.show_id, qty, selected)


async def hold_seats(request: Request) -> Response:
    found = _show_or_404(request)
    if isinstance(found, Response):
        return found
    state, show = found
    form = await _form(request)
    qty, selected = _qty(form.get("qty")), _seats(form.get("selected", ""))
    now = now_utc()
    if len(selected) != qty or set(selected) & state.unavailable(show.show_id, now):
        return _back_to_map(show.show_id, qty, [], "Those seats are no longer available.")
    hold = state.create_hold(show.show_id, tuple(selected), now)
    return RedirectResponse(f"/summary/{hold.hold_id}", status_code=303)


async def summary(request: Request) -> Response:
    state = _state(request)
    hold = state.holds.get(request.path_params["hold_id"])
    show = state.show(hold.show_id) if hold else None
    if hold is None or show is None:
        return HTMLResponse(
            pages.message_page("error", "Not found", "No such booking."), status_code=404
        )
    left = int((hold.expires_at - now_utc()).total_seconds())
    if left <= 0:
        return HTMLResponse(
            pages.message_page("hold-expired", "Hold expired", "Your seats were released.")
        )
    return HTMLResponse(pages.summary_page(state, show, hold.hold_id, hold.seats, left))


async def payment(request: Request) -> Response:
    state = _state(request)
    hold = state.holds.get(request.path_params["hold_id"])
    show = state.show(hold.show_id) if hold else None
    if hold is None or show is None:
        return HTMLResponse(
            pages.message_page("error", "Not found", "No such booking."), status_code=404
        )
    return HTMLResponse(pages.payment_page(state, show, hold.hold_id, hold.seats))


async def pay(request: Request) -> Response:
    state = _state(request)
    hold = state.holds.get((await _form(request)).get("hold_id", ""))
    if hold is None or hold.expires_at <= now_utc():
        return HTMLResponse(
            pages.message_page("error", "Nothing to pay", "That booking is not active.")
        )
    hold.paid = True
    return RedirectResponse(f"/confirmation/{hold.hold_id}", status_code=303)


async def confirmation(request: Request) -> Response:
    hold = _state(request).holds.get(request.path_params["hold_id"])
    if hold is None or not hold.paid:
        return HTMLResponse(
            pages.message_page("error", "Not found", "No such booking."), status_code=404
        )
    return HTMLResponse(pages.confirmation_page(hold.seats))


def _interstitial(kind: str) -> Callable[[Request], Awaitable[Response]]:
    async def endpoint(request: Request) -> Response:
        return HTMLResponse(pages.interstitial_page(kind))

    return endpoint


# --- control API ---------------------------------------------------------------------------------


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True})


async def set_state(request: Request) -> Response:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ControlError("expected a JSON object")
        _state(request).update(data)
    except (ControlError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


async def reset(request: Request) -> Response:
    request.app.state.site = SiteState()
    return JSONResponse({"ok": True})


async def request_log(request: Request) -> Response:
    return JSONResponse(_state(request).log)


async def shutdown(request: Request) -> Response:
    # A graceful, provable stop: uvicorn exits with code 0 (it re-raises SIGTERM instead).
    stop: Callable[[], None] | None = request.app.state.shutdown
    if stop is not None:
        stop()
    return JSONResponse({"ok": True})


async def external_ref(request: Request) -> Response:
    # For the browser lockdown test (T6.8): a page that tries to load from other hosts.
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>External</title>'
        '<link rel="stylesheet" href="https://assets.external.invalid/style.css"></head>'
        "<body><h1>External references</h1>"
        '<img src="http://203.0.113.10/pixel.png" alt="">'
        "</body></html>"
    )
    return HTMLResponse(html)


def create_app() -> Starlette:
    routes = [
        Route("/", home),
        Route("/movie", movie),
        Route("/movie/shows", listing),
        Route("/show/{show_id}", quantity),
        Route("/show/{show_id}/seats", seat_map),
        Route("/show/{show_id}/select", select_seat, methods=["POST"]),
        Route("/show/{show_id}/hold", hold_seats, methods=["POST"]),
        Route("/summary/{hold_id}", summary),
        Route("/payment/{hold_id}", payment),
        Route("/pay", pay, methods=["POST"]),
        Route("/confirmation/{hold_id}", confirmation),
        *(Route(f"/{kind}", _interstitial(kind)) for kind in INTERSTITIALS),
        Route("/__control/health", health),
        Route("/__control/state", set_state, methods=["POST"]),
        Route("/__control/reset", reset, methods=["POST"]),
        Route("/__control/log", request_log),
        Route("/__control/shutdown", shutdown, methods=["POST"]),
        Route("/__control/external-ref", external_ref),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(SiteMiddleware)])
    app.state.site = SiteState()
    app.state.shutdown = None  # set by __main__ once the server exists
    return app
