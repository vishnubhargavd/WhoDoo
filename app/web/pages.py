"""Server-rendered review pages. They read through the same services as the API;
every action on a page is a fetch() call to /api/v1, so there is one code path for writes."""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import (
    APPROVERS,
    CurrentUser,
    get_current_user,
    get_db,
    get_optional_user,
    require_approver,
    require_finance_admin,
    require_roles,
)
from app.models.ledger import ENTRY_TYPES, EXPENSE_CATEGORIES, PAYMENT_STATUSES, SOURCE_MODULES
from app.schemas.ledger import LedgerFilters
from app.core.errors import AppError
from app.services import (
    announcement_service,
    event_service,
    expense_service,
    ledger_service,
    merch_service,
    payment_service,
    shared_directory,
    task_service,
)
from app.web.templating import inr, render

router = APIRouter(include_in_schema=False)


# The twelve WhoDoo tools from the product design. `href` is set only for the ones
# that exist in this application; the rest are shown honestly as not here yet.
FEATURES = (
    ("membercore", "MemberCore", "Memberships", "Profiles, dues, roles, renewals and member discounts in one source of truth.", None),
    ("fairseat", "FairSeat", "Tickets", "Live ticket availability with member pricing and fair, automatic waitlists.", None),
    ("gatesync", "GateSync", "Attendance", "QR check-in at the door with built-in duplicate prevention.", None),
    ("crewpulse", "CrewPulse", "Volunteers", "Coordinate tasks and recognise every volunteer contribution.", "/my-tasks"),
    ("trustledger", "TrustLedger", "Finances", "Transparent budgets with clear, auditable approval flows.", "/treasurer"),
    ("snap-to-reimburse", "Snap-to-Reimburse", "Reimbursements", "Snap a receipt. OCR drafts the claim and routes it for approval.", "/expenses/new"),
    ("merchflow", "MerchFlow", "Merchandise", "Take merch orders and keep inventory honest across every drop.", "/store"),
    ("crewdoo-connect", "WhoDoo Connect", "Announcements", "Announcements and reminders that reach exactly the right people.", None),
    ("event-autopilot", "Event Autopilot", "Events", "Readiness monitoring that flags what an event still needs before doors open.", "/events"),
    ("syncsphere", "SyncSphere", "Calendar", "A conflict-aware schedule for every club event.", "/events"),
    ("certidoo", "CertiDoo", "Certificates", "Issue verifiable digital certificates for participation and service.", "/certificates"),
    ("legacyvault", "LegacyVault", "Handover", "Pass knowledge to next year's leaders without starting from zero.", None),
)


def _features(user: CurrentUser | None) -> list[dict]:
    features = [dict(zip(("id", "name", "category", "description", "href"), row)) for row in FEATURES]
    for feature in features:
        # TrustLedger's pages are for the treasurer and president; others get their own expenses.
        if feature["id"] == "trustledger" and user is not None and not user.is_finance_admin:
            feature["href"] = "/expenses"
        if feature["id"] == "crewdoo-connect" and user is not None:
            feature["href"] = "/announcements"
    return features


async def _home_tiles(db: AsyncSession, user: CurrentUser) -> list[dict]:
    """Live figures for the signed-in user's home page. Every number comes from the services."""
    def plural(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"

    tiles = []
    upcoming = await event_service.list_events(db, user, "upcoming")
    tiles.append({"module": "Events", "tag": "Event Autopilot", "href": "/events", "figure": len(upcoming),
                  "title": "upcoming event" + ("" if len(upcoming) == 1 else "s"),
                  "detail": "Schedules, owners and readiness for everything being planned."})

    tasks = [task for task in await task_service.list_mine(db, user) if task.status != "done"]
    tiles.append({"module": "Tasks", "tag": "CrewPulse", "href": "/my-tasks", "figure": len(tasks),
                  "title": "open task" + ("" if len(tasks) == 1 else "s") + " for you",
                  "detail": "What you have been asked to do, and what is blocked."})

    claims = await expense_service.list_my_claims(db, user)
    in_progress = [claim for claim in claims if claim.status in ("pending_approval", "approved", "change_requested")]
    waiting_on_me = sum(claim.status == "change_requested" for claim in claims)
    tiles.append({"module": "Expenses", "tag": "Snap-to-Reimburse", "href": "/expenses", "figure": len(in_progress),
                  "title": "claim" + ("" if len(in_progress) == 1 else "s") + " in progress",
                  "detail": (plural(waiting_on_me, "claim") + " waiting for your changes." if waiting_on_me
                             else "Snap a receipt and track it through to payment.")})

    if user.role in APPROVERS:
        queue = await expense_service.get_queue(db, user)
        detail = "Claims waiting for your decision."
        if user.is_finance_admin:
            detail = f"{plural(len(queue.to_pay), 'approved claim')} waiting to be paid."
        tiles.append({"module": "Approvals", "tag": "Two-person rule", "href": "/expenses/approvals",
                      "figure": len(queue.to_approve), "title": "to approve", "detail": detail})

    if user.is_finance_admin:
        summary = await ledger_service.get_summary(db, user.club_id)
        tiles.append({"module": "Finance", "tag": "TrustLedger", "href": "/treasurer",
                      "figure": inr(summary.current_balance).split(".")[0], "title": "club balance",
                      "detail": f"{inr(summary.pending_reimbursements)} owed in approved reimbursements."})

    products = await merch_service.list_products(db, user)
    orders = await merch_service.list_orders(db, user)
    unpaid = sum(order.status == "created" for order in orders)
    tiles.append({"module": "Store", "tag": "MerchFlow", "href": "/store", "figure": len(products),
                  "title": "product" + ("" if len(products) == 1 else "s") + " on sale",
                  "detail": (plural(unpaid, "order") + " of yours waiting for payment." if unpaid
                             else f"You have placed {plural(len(orders), 'order')}.")})
    return tiles


@router.get("/")
async def landing(request: Request, user: CurrentUser | None = Depends(get_optional_user)):
    """The product page: the first thing anyone sees, signed in or not."""
    return render(request, "landing.html", user, features=_features(user))


@router.get("/welcome")
async def welcome():
    """Old address of the product page."""
    return RedirectResponse("/", status_code=308)


@router.get("/home")
async def home(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """A signed-in person's own page: live figures from every module they can use."""
    club = await shared_directory.get_club(db, user.club_id)
    return render(request, "home.html", user, tiles=await _home_tiles(db, user), club_name=club.name if club else None)


@router.get("/whoami")
async def whoami(user: CurrentUser | None = Depends(get_optional_user)):
    """Who this browser is signed in as. Pages use it to notice a switch of user in another tab."""
    return {"id": str(user.id), "name": user.full_name} if user else {"id": None, "name": None}


@router.get("/login")
async def login(next: str = "/home"):
    """Where "Enter WhoDoo" leads. Today that is the demo sign-in; real sign-in will take this route over."""
    if get_settings().demo_auth:
        target = next if next.startswith("/") and not next.startswith("//") and next != "/" else "/home"
        return RedirectResponse(f"/dev/login?next={quote(target)}", status_code=303)
    raise AppError(503, "sign_in_unavailable", "Sign-in is not connected in this build yet")


@router.get("/treasurer")
async def treasurer_dashboard(
    request: Request, user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
):
    summary = await ledger_service.get_summary(db, user.club_id)
    events = await shared_directory.list_events(db, user.club_id)
    event_results = [
        await ledger_service.get_event_profit_loss(db, user.club_id, event.id, event.name) for event in events
    ]
    recent, _ = await ledger_service.list_entries(db, user.club_id, LedgerFilters(limit=8))
    peak = max([max(point.income, point.expenses) for point in summary.monthly_trend] or [0]) or 1
    return render(
        request,
        "treasurer_dashboard.html",
        user,
        summary=summary,
        event_results=event_results,
        recent=recent,
        trend_peak=peak,
    )


@router.get("/treasurer/ledger")
async def ledger_page(
    request: Request, user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
):
    try:
        filters = LedgerFilters.model_validate(dict(request.query_params))
        filter_error = None
    except ValidationError as exc:
        filters = LedgerFilters()
        filter_error = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in exc.errors())
    entries, total = await ledger_service.list_entries(db, user.club_id, filters)
    events = await shared_directory.list_events(db, user.club_id)
    return render(
        request,
        "ledger.html",
        user,
        entries=entries,
        total=total,
        filters=filters,
        filter_error=filter_error,
        events=events,
        event_names={event.id: event.name for event in events},
        entry_types=ENTRY_TYPES,
        source_modules=SOURCE_MODULES,
        payment_statuses=PAYMENT_STATUSES,
        categories=EXPENSE_CATEGORIES,
    )


# --- Snap-to-Reimburse ---------------------------------------------------------


@router.get("/expenses")
async def my_expenses(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    claims = await expense_service.list_my_claims(db, user)
    return render(request, "expenses.html", user, claims=claims)


@router.get("/expenses/new")
async def new_expense(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    events = await shared_directory.list_events(db, user.club_id)
    return render(
        request,
        "expense_new.html",
        user,
        events=events,
        categories=EXPENSE_CATEGORIES,
        approval_limit=get_settings().expense_auto_approval_limit,
    )


@router.get("/expenses/approvals")
async def expense_approvals(
    request: Request, user: CurrentUser = Depends(require_approver), db: AsyncSession = Depends(get_db)
):
    queue = await expense_service.get_queue(db, user)
    return render(request, "expense_approvals.html", user, queue=queue)


@router.get("/expenses/{expense_id}")
async def expense_detail(
    expense_id: uuid.UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    claim = await expense_service.get_claim_detail(db, user, expense_id)
    events = await shared_directory.list_events(db, user.club_id)
    return render(
        request,
        "expense_detail.html",
        user,
        claim=claim,
        events=events,
        categories=EXPENSE_CATEGORIES,
        approval_limit=get_settings().expense_auto_approval_limit,
    )


# --- MerchFlow -------------------------------------------------------------------


@router.get("/store")
async def store(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    products = await merch_service.list_products(db, user)
    return render(request, "store.html", user, products=products)


@router.get("/store/{product_id}")
async def product_detail(
    product_id: uuid.UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    product = await merch_service.get_product(db, user, product_id)
    events = await shared_directory.list_events(db, user.club_id)
    return render(request, "product_detail.html", user, product=product, events=events)


@router.get("/orders")
async def my_orders(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    orders = await merch_service.list_orders(db, user)
    return render(request, "orders.html", user, orders=orders)


@router.get("/orders/{order_id}")
async def order_detail(
    order_id: uuid.UUID,
    request: Request,
    session_id: str | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Order confirmation. Stripe sends the buyer back here with `session_id`; the payment is
    checked with the provider before the page is drawn, so a refresh simply checks again."""
    order = await merch_service.get_order(db, user, order_id)
    problem = None
    waiting = order.payment_status in ("pending", "failed") and order.status == "created"
    if session_id and waiting and order.user_id == user.id and session_id == order.stripe_checkout_session_id:
        try:
            outcome = await payment_service.verify_payment(db, user, session_id)
            await db.commit()
            problem = outcome.error
        except AppError as exc:
            await db.rollback()
            problem = exc
        order = await merch_service.get_order(db, user, order_id)
    return render(
        request, "order_detail.html", user, order=order, problem=problem,
        is_owner=order.user_id == user.id, just_paid=bool(session_id) and problem is None and order.payment_status == "paid",
    )


@router.get("/checkout/{order_id}")
async def checkout(
    order_id: uuid.UUID,
    request: Request,
    cancelled: bool = False,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = await merch_service.get_order(db, user, order_id)
    provider = payment_service.get_provider()
    return render(
        request, "checkout.html", user, order=order, provider=provider, is_owner=order.user_id == user.id,
        cancelled=cancelled,
    )


@router.get("/admin/merch")
async def admin_merch(
    request: Request, user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
):
    products = await merch_service.list_products(db, user, include_inactive=True)
    return render(request, "admin_merch.html", user, products=products)


@router.get("/admin/merch/orders")
async def admin_orders(
    request: Request, user: CurrentUser = Depends(require_finance_admin), db: AsyncSession = Depends(get_db)
):
    orders = await merch_service.list_orders(db, user, everyone=True)
    return render(request, "admin_orders.html", user, orders=orders)


@router.get("/announcements")
async def announcements_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = await announcement_service.list_announcements(db, user.club_id)
    can_post = user.role in ("president", "treasurer", "event_lead", "admin")
    return render(
        request, "announcements.html", user,
        announcements=items, can_post=can_post
    )


@router.post("/announcements")
async def post_announcement_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.schemas.announcement import AnnouncementCreate
    form = await request.form()
    title = str(form.get("title", "")).strip()
    content = str(form.get("content", "")).strip()
    audience = str(form.get("target_audience", "all")).strip()
    is_pinned = bool(form.get("is_pinned"))

    if not title or not content:
        items = await announcement_service.list_announcements(db, user.club_id)
        return render(
            request, "announcements.html", user,
            announcements=items, can_post=True, error="Title and content are required."
        )

    create_data = AnnouncementCreate(
        title=title, content=content, target_audience=audience, is_pinned=is_pinned
    )
    await announcement_service.create_announcement(db, user, create_data)
    await db.commit()
    return RedirectResponse(url="/announcements", status_code=303)


@router.get("/certificates")
async def certificates_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import certidoo_service
    can_issue = user.role in ("president", "treasurer", "event_lead")
    my_certs = await certidoo_service.list_user_certificates(db, user.id)
    club_events = []
    club_members = []
    all_certs = []
    if can_issue:
        club_events = await shared_directory.list_events(db, user.club_id)
        club_members = await shared_directory.list_users(db, user.club_id)
        all_certs = await certidoo_service.list_club_certificates(db, user.club_id)

    return render(
        request,
        "certificates.html",
        user,
        my_certificates=my_certs,
        can_issue=can_issue,
        club_events=club_events,
        club_members=club_members,
        all_issued_certificates=all_certs,
    )


@router.post("/certificates/issue")
async def issue_certificate_page(
    request: Request,
    user: CurrentUser = Depends(require_roles("president", "treasurer", "event_lead")),
    db: AsyncSession = Depends(get_db),
):
    from app.schemas.certidoo import CertificateIssueRequest
    from app.services import certidoo_service
    form = await request.form()
    event_id_raw = form.get("event_id")
    recipient_id_raw = form.get("recipient_user_id")
    cert_type = str(form.get("certificate_type", "participation"))
    title = str(form.get("title", "")).strip()

    if not event_id_raw or not recipient_id_raw or not title:
        return RedirectResponse(url="/certificates", status_code=303)

    try:
        req = CertificateIssueRequest(
            event_id=uuid.UUID(str(event_id_raw)),
            recipient_user_id=uuid.UUID(str(recipient_id_raw)),
            certificate_type=cert_type,  # type: ignore
            title=title,
        )
        await certidoo_service.issue_certificate(db, user.club_id, user.id, req)
        return RedirectResponse(url="/certificates", status_code=303)
    except AppError as exc:
        my_certs = await certidoo_service.list_user_certificates(db, user.id)
        club_events = await shared_directory.list_events(db, user.club_id)
        club_members = await shared_directory.list_users(db, user.club_id)
        all_certs = await certidoo_service.list_club_certificates(db, user.club_id)
        return render(
            request,
            "certificates.html",
            user,
            my_certificates=my_certs,
            can_issue=True,
            club_events=club_events,
            club_members=club_members,
            all_issued_certificates=all_certs,
            error=exc.message,
        )


@router.get("/certificates/verify")
async def verify_certificate_lookup_page(
    request: Request,
    code: str | None = None,
    user: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import certidoo_service
    if code:
        code = code.strip().upper()
        try:
            cert_out = await certidoo_service.verify_certificate_by_code(db, code)
            return render(
                request,
                "certificate_verify.html",
                user,
                cert=cert_out,
                query_code=code,
            )
        except AppError as exc:
            return render(
                request,
                "certificate_verify.html",
                user,
                cert=None,
                query_code=code,
                error=exc.message,
            )
    return render(
        request,
        "certificate_verify.html",
        user,
        cert=None,
        query_code=None,
    )


@router.get("/certificates/verify/{code}")
async def verify_certificate_direct_page(
    request: Request,
    code: str,
    user: CurrentUser | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import certidoo_service
    code = code.strip().upper()
    try:
        cert_out = await certidoo_service.verify_certificate_by_code(db, code)
        return render(
            request,
            "certificate_verify.html",
            user,
            cert=cert_out,
            query_code=code,
        )
    except AppError as exc:
        return render(
            request,
            "certificate_verify.html",
            user,
            cert=None,
            query_code=code,
            error=exc.message,
        )

