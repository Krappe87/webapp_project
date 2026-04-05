from fastapi import FastAPI
from app.database import engine, Base
from app.routers import views, webhooks, api

# Ensure tables are created on boot
Base.metadata.create_all(bind=engine)

app = FastAPI(title="RMM Alerts Application")

# Connect all the isolated routing modules
app.include_router(views.router)
app.include_router(webhooks.router)
app.include_router(api.router)