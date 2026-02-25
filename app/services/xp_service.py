from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.model import User, Levels
from fastapi import HTTPException
from datetime import datetime, date

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

# gives 10xp to users for their first login of the day
async def grant_daily_login_xp(user: User, session: AsyncSession) -> bool:
    current_day = date.today()
    
    # Sign-up scneario and then login same day and checks for previous valid login date
    if user.last_daily_reward_at == None or user.last_daily_reward_at.date() < current_day: 
        user.last_daily_reward_at = datetime.utcnow()
        await grant_xp(user.id, 10, session)
        return True

    return False


    
    
    
    