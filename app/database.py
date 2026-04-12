import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Grabs the database URL from .env, defaults to localhost if missing
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rmmuser:rmmpassword@localhost:5432/rmmdb")

_pool_size = int(os.getenv("SQLALCHEMY_POOL_SIZE", "10"))
_max_overflow = int(os.getenv("SQLALCHEMY_MAX_OVERFLOW", "20"))

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Dependency to get the database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()