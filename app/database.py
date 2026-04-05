import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Grabs the database URL from .env, defaults to localhost if missing
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rmmuser:rmmpassword@localhost:5432/rmmdb")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Dependency to get the database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()