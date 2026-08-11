from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routers import portfolio, health, dashboard, pipeline
from config.settings import settings
from core.observability import setup_logging

# Initialize structured logging on module load (before app creation)
setup_logging()


app = FastAPI(
    title="Project VYUHA API",
    description="High-Conviction Equity Accumulation Engine (Multi-Agent, NSE/BSE)",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, restrict to dashboard domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(portfolio.router)
app.include_router(pipeline.router)
app.include_router(dashboard.router)
