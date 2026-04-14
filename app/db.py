import gridfs
from pymongo import MongoClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from .config import settings
# --------------------------
# MongoDB setup
# --------------------------
client = MongoClient(settings.MONGO_URI)

# Specify database and collections
db = client["BitcoinCultureHub"]
collection = db["users"]
explore = db["explore"]
waitlist = db["waitlist"]
fs = gridfs.GridFS(db, collection="images")
bookmark_collection = db["bookmarks"]

# --------------------------
# MySQL / SQLModel setup
# --------------------------


# --------------------------
# Optional / commented-out legacy code
# --------------------------
# engine = create_engine(settings.DATABASE_URL)
# SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
# Base = declarative_base()
#
# def get_db():
#     db = SessionLocal()
#     try:
#         yield db
#     finally:
#         db.close()


if not settings.DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured. Set DEPLOYED_DATABASE_URL or DATABASE_URL before starting the app."
    )

engine = create_async_engine(settings.DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,   
    expire_on_commit=False
)

async def get_session():
    async with AsyncSessionLocal() as session:
        yield session
