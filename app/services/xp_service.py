from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.model import User, Levels, Organization
from fastapi import HTTPException
from datetime import datetime, date, timedelta

ORG_CREATION_XP_CAP = 3

# determines user's level based off their total xp
async def compute_level(xp: int, session: AsyncSession) -> int:
    result = await session.exec(
        select(Levels)
        .where(Levels.min_cumulative_xp <= xp)
        .order_by(Levels.id.desc())
    )

    level = result.first()
    return level.id if level else 1

# check the level after xp is added and decided to increase level or not
async def check_and_update_level(user: User, session: AsyncSession) -> bool:
    new_level = await compute_level(user.current_xp, session)
    if new_level != user.current_level:
        user.current_level = new_level
        return True
    else:
        return False

# fetches user from database and appends an amount of xp
async def grant_xp(user_id: str, amount: int, session: AsyncSession) -> User:
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.current_xp += amount

    await check_and_update_level(user, session)
    await session.commit()

    return user

async def grant_login_streak_xp(user: User, session: AsyncSession) -> bool:
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
    await grant_xp(user.id, 5, session)
    return True

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

# TODO: Add guradrail/cap for max of 
async def grant_proof_of_competence_xp(user_id: str, session: AsyncSession) -> None:
    await grant_xp(user_id, 10, session)