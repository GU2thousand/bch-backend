from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.model import User, Levels, Organization
from fastapi import HTTPException
from datetime import datetime, date, timedelta

DAILY_LOGIN_XP = 10
CLAIM_APPLICATION_XP = 10
ORG_CREATION_XP_CAP = 3

# Determines user's level based off their total xp
async def compute_level(xp: int, session: AsyncSession) -> int:
    result = await session.exec(
        select(Levels)
        .where(Levels.min_cumulative_xp <= xp)
        .order_by(Levels.id.desc())
    )

    level = result.first()
    return level.id if level else 1

# Check the level after xp is added and decided to increase level or not
async def check_and_update_level(user: User, session: AsyncSession) -> bool:
    new_level = await compute_level(user.current_xp, session)
    if new_level != user.current_level:
        user.current_level = new_level
        return True
    else:
        return False

# Fetches user from database and appends an amount of xp
async def grant_xp(user_id: str, amount: int, session: AsyncSession) -> User:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.current_xp += amount

    await check_and_update_level(user, session)
    await session.commit()

    return user

async def grant_daily_login_xp(user: User, session: AsyncSession) -> bool:
    STREAK_2_3 = 1.25
    STREAK_4_6 = 1.5
    STREAK_7_13 = 2.0
    STREAK_14_29 = 2.5
    STREAK_30 = 3.0
    today = date.today()

    if user.last_daily_reward_at is not None and user.last_daily_reward_at.date() >= today:
        return False

    yesterday = today - timedelta(days=1)
    last_login_date = user.last_daily_reward_at.date() if user.last_daily_reward_at else None

    if last_login_date == yesterday:
        user.current_login_streak += 1
    else:
        user.current_login_streak = 1

    user.last_daily_reward_at = datetime.utcnow()

    # Multiplier xp for login streaks
    if user.current_login_streak <= 1:
        await grant_xp(user.id, DAILY_LOGIN_XP, session)
    elif user.current_login_streak >= 2 and user.current_login_streak <= 3:
        await grant_xp(user.id, int(STREAK_2_3 * DAILY_LOGIN_XP), session)
    elif user.current_login_streak >= 4 and user.current_login_streak <= 6:
        await grant_xp(user.id, int(STREAK_4_6 * DAILY_LOGIN_XP), session)
    elif user.current_login_streak >= 7 and user.current_login_streak <= 13:
        await grant_xp(user.id, int(STREAK_7_13 * DAILY_LOGIN_XP), session)
    elif user.current_login_streak >= 14 and user.current_login_streak <= 29:
        await grant_xp(user.id, int(STREAK_14_29 * DAILY_LOGIN_XP), session)
    elif user.current_login_streak >= 30:
        await grant_xp(user.id, int(STREAK_30 * DAILY_LOGIN_XP), session)

    return True


# TODO: Add a max cap of 3 applications per day
async def grant_application_xp(user_id: str, session: AsyncSession) -> None:
    await grant_xp(user_id, 10, session)

async def grant_org_creation_xp(user_id: str, session: AsyncSession) -> bool:
    result = await session.exec(
        select(func.count(Organization.id)).where(
            Organization.owner_id == user_id,
            Organization.status == "approved",
            Organization.deleted_at.is_(None),
        )
    )
    approved_count = result.one()

    if approved_count > ORG_CREATION_XP_CAP:
        return False

    await grant_xp(user_id, 35, session)
    return True


async def grant_proof_of_competence_xp(user_id: str, session: AsyncSession) -> None:
    await grant_xp(user_id, CLAIM_APPLICATION_XP, session)