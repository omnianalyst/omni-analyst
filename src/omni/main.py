from contextlib import asynccontextmanager

from neutron import App

from omni.config import settings
from omni.db import connect, migrate


def create_app(database_url: str | None = None) -> App:
    url = database_url or settings.database_url

    @asynccontextmanager
    async def lifespan(neutron_app: App):
        client = await connect(url)
        await migrate(client)
        neutron_app.db = client
        try:
            yield
        finally:
            await client.close()
            neutron_app.db = None

    app = App(
        title="Omni Analyst v2",
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )

    from omni.api.alerts import build_router as alerts_router
    from omni.api.auth import build_router as auth_router
    from omni.api.autonomous import build_router as autonomous_router
    from omni.api.briefing import build_router as briefing_router
    from omni.api.bulletin import build_router as bulletin_router
    from omni.api.coverage import build_router as coverage_router
    from omni.api.exposure import build_router as exposure_router
    from omni.api.objective import build_router as objective_router
    from omni.api.portfolio import build_router as portfolio_router
    from omni.api.risk_monitor import build_router as risk_monitor_router
    from omni.api.scanner import build_router as scanner_router
    from omni.api.settings import build_router as settings_router
    from omni.api.system import build_router as system_router
    from omni.api.trading import build_router as trading_router
    from omni.api.wallets import build_router as wallets_router
    from omni.api.watchlist import build_router as watchlist_router

    app.include_router(coverage_router(app))
    app.include_router(objective_router(app))
    app.include_router(briefing_router(app))
    app.include_router(bulletin_router(app))
    app.include_router(autonomous_router(app))
    app.include_router(auth_router(app))
    app.include_router(watchlist_router(app))
    app.include_router(alerts_router(app))
    app.include_router(system_router(app))
    app.include_router(trading_router(app))
    app.include_router(portfolio_router(app))
    app.include_router(exposure_router(app))
    app.include_router(scanner_router(app))
    app.include_router(risk_monitor_router(app))
    app.include_router(settings_router(app))
    app.include_router(wallets_router(app))
    return app


app = create_app()
