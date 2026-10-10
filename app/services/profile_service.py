import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, bad_request, not_found
from app.models.payment_profile import UserPaymentProfile
from app.models.renewal_reminder import MemberRenewalReminder
from app.models.shared import Club, User
from app.schemas.member_profile import MemberProfileOut, MemberProfileUpdate, RenewalReminderOut
from app.services.upi_service import mask_upi_id


async def get_member_profile(db: AsyncSession, user_id: uuid.UUID) -> MemberProfileOut:
    """Retrieve public and member-editable profile data along with active renewal reminders."""
    user = await db.get(User, user_id)
    if not user:
        raise not_found("User")

    # Fetch masked UPI if configured
    upi_profile = await db.get(UserPaymentProfile, user_id)
    upi_masked = mask_upi_id(upi_profile.upi_id) if (upi_profile and upi_profile.upi_id) else None

    # Fetch active in-app renewal reminders
    rem_stmt = (
        select(MemberRenewalReminder)
        .where(
            MemberRenewalReminder.user_id == user_id,
            MemberRenewalReminder.is_dismissed.is_(False),
        )
        .order_by(MemberRenewalReminder.expires_at.asc())
    )
    rem_res = await db.execute(rem_stmt)
    reminders = [RenewalReminderOut.model_validate(r) for r in rem_res.scalars().all()]

    return MemberProfileOut(
        id=user.id,
        club_id=user.club_id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active_member=user.is_active_member,
        phone=user.phone,
        department=user.department,
        academic_year=user.academic_year,
        profile_image_url=user.profile_image_url,
        upi_id_masked=upi_masked,
        active_reminders=reminders,
    )


async def update_member_profile(
    db: AsyncSession, user_id: uuid.UUID, update_data: MemberProfileUpdate
) -> MemberProfileOut:
    """Update permitted profile details. Disallows modifying role, identity, or club privileges."""
    user = await db.get(User, user_id)
    if not user:
        raise not_found("User")

    if update_data.full_name is not None:
        trimmed = update_data.full_name.strip()
        if not trimmed:
            raise bad_request("invalid_name", "Display name cannot be empty")
        user.full_name = trimmed

    if update_data.phone is not None:
        user.phone = update_data.phone.strip() or None

    if update_data.department is not None:
        user.department = update_data.department.strip() or None

    if update_data.academic_year is not None:
        user.academic_year = update_data.academic_year.strip() or None

    if update_data.profile_image_url is not None:
        url = update_data.profile_image_url.strip()
        if url and not (url.startswith("http://") or url.startswith("https://") or url.startswith("/")):
            raise bad_request("invalid_url", "Profile image must be a valid URL")
        user.profile_image_url = url or None

    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)

    return await get_member_profile(db, user_id)


async def dismiss_renewal_reminder(
    db: AsyncSession, reminder_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Dismiss a specific renewal reminder for the user."""
    reminder = await db.get(MemberRenewalReminder, reminder_id)
    if not reminder or reminder.user_id != user_id:
        raise not_found("Renewal Reminder")

    reminder.is_dismissed = True
    reminder.dismissed_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def generate_renewal_reminders_for_club(
    db: AsyncSession, club_id: uuid.UUID, as_of: datetime | None = None
) -> int:
    """Generate in-app renewal reminders at 30d, 14d, 7d, 1d, and on expiry from genuine membership dates.

    Guarantees idempotency and prevents duplicates per membership and reminder stage.
    """
    now = as_of or datetime.now(timezone.utc)

    # Check if public.memberships exists
    query = text("""
        SELECT m.id, m.user_id, m.club_id, m.expires_at, p.name AS plan_name
        FROM public.memberships m
        JOIN public.membership_plans p ON p.id = m.plan_id AND p.club_id = m.club_id
        WHERE m.club_id = :club_id AND m.status IN ('active', 'expired')
    """)

    try:
        res = await db.execute(query, {"club_id": club_id})
        memberships = res.fetchall()
    except DBAPIError:
        # Schema not ready or absent
        return 0

    count_created = 0

    for row in memberships:
        m_id = row.id
        u_id = row.user_id
        expires_at = row.expires_at
        plan_name = row.plan_name

        days_remaining = (expires_at - now).total_seconds() / 86400.0

        # Stages to evaluate
        stages_to_trigger: list[str] = []
        if days_remaining <= 0:
            stages_to_trigger.append("on_expiry")
        elif days_remaining <= 1:
            stages_to_trigger.append("1_day")
        elif days_remaining <= 7:
            stages_to_trigger.append("7_days")
        elif days_remaining <= 14:
            stages_to_trigger.append("14_days")
        elif days_remaining <= 30:
            stages_to_trigger.append("30_days")

        for stage in stages_to_trigger:
            # Check if this stage reminder already exists
            existing_stmt = select(MemberRenewalReminder).where(
                MemberRenewalReminder.membership_id == m_id,
                MemberRenewalReminder.reminder_stage == stage,
            )
            existing = (await db.execute(existing_stmt)).scalar_one_or_none()
            if existing:
                continue

            expiry_str = expires_at.strftime("%d %b %Y")
            if stage == "on_expiry":
                msg = f"Your {plan_name} membership expired on {expiry_str}. Renew now to keep your club perks!"
            elif stage == "1_day":
                msg = f"Your {plan_name} membership expires tomorrow ({expiry_str}). Renew today!"
            elif stage == "7_days":
                msg = f"Your {plan_name} membership expires in 7 days on {expiry_str}. Renew now to stay active."
            elif stage == "14_days":
                msg = f"Your {plan_name} membership expires in 2 weeks on {expiry_str}."
            else:
                msg = f"Your {plan_name} membership is up for renewal on {expiry_str}."

            reminder = MemberRenewalReminder(
                id=uuid.uuid4(),
                club_id=club_id,
                user_id=u_id,
                membership_id=m_id,
                plan_name=plan_name,
                reminder_stage=stage,
                expires_at=expires_at,
                message=msg,
                action_url=f"/member/renew?plan={plan_name}",
                is_dismissed=False,
                created_at=datetime.now(timezone.utc),
            )
            db.add(reminder)
            count_created += 1

        # Reconcile: If membership has renewed and expires > 30 days ahead, dismiss obsolete reminders
        if days_remaining > 30:
            dismiss_stmt = select(MemberRenewalReminder).where(
                MemberRenewalReminder.membership_id == m_id,
                MemberRenewalReminder.is_dismissed.is_(False),
            )
            obs_res = await db.execute(dismiss_stmt)
            for obs in obs_res.scalars().all():
                obs.is_dismissed = True
                obs.dismissed_at = datetime.now(timezone.utc)

    if count_created > 0:
        await db.commit()

    return count_created
