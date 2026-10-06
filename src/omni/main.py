from contextlib import asynccontextmanager

from neutron import App, CORSMiddleware
from starlette.middleware import Middleware

from omni.auth import jwt_secret
from omni.auth.middleware import ActivePrincipalMiddleware
from omni.config import settings
from omni.db import connect, migrate
from omni.middleware import MaxBodySizeMiddleware


def create_app(database_url: str | None = None) -> App:
    url = database_url or settings.database_url

    @asynccontextmanager
    async def lifespan(neutron_app: App):
        # Configuration faults are fatal at startup, not silent downgrades.
        # If the signing key is missing or short, every authenticated request
        # would otherwise render as anonymous -- misleading 401s plus
        # shared-data responses wherever anonymous is valid. An infrastructure
        # failure must not impersonate an unauthenticated caller.
        jwt_secret()
        # Same rule for proxy trust: an unparsable OMNI_TRUSTED_PROXIES entry
        # would otherwise surface as a per-request exception on the login
        # path, or worse, quietly change which peer addresses are trusted.
        from omni.auth.forwarded import validate_trusted_proxies

        validate_trusted_proxies(settings.omni_trusted_proxies)
        client = await connect(url)
        await migrate(client)
        neutron_app.db = client
        neutron_app.state.db = client
        # Prime the dummy password hash and the hashing executor: the first
        # unknown-email login then verifies against a ready hash instead of
        # paying a one-time hash inside a request (and the executor's thread
        # is known-good before any credential depends on it).
        from omni.auth.users import prime_password_hash

        await prime_password_hash()
        try:
            yield
        finally:
            from omni.venue.manager import disconnect_all

            await disconnect_all()
            await client.close()
            neutron_app.db = None
            neutron_app.state.db = None

    app = App(
        title="Omni Analyst v2",
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
        middleware=[
            CORSMiddleware(
                allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                allow_headers=["Authorization", "Content-Type"],
            ),
            Middleware(MaxBodySizeMiddleware),
            Middleware(ActivePrincipalMiddleware),
        ],
    )

    from omni.api.alerts import build_router as alerts_router
    from omni.api.auth import build_router as auth_router
    from omni.api.autonomous import build_router as autonomous_router
    from omni.api.briefing import build_router as briefing_router
    from omni.api.bulletin import build_router as bulletin_router
    from omni.api.companies import build_router as companies_router
    from omni.api.coverage import build_router as coverage_router
    from omni.api.export import build_router as export_router
    from omni.api.exposure import build_router as exposure_router
    from omni.api.finance import build_router as finance_router
    from omni.api.holdings import build_router as holdings_router
    from omni.api.mcp import build_mount as mcp_mount
    from omni.api.objective import build_router as objective_router
    from omni.api.portfolio import build_router as portfolio_router
    from omni.api.profile import build_router as profile_router
    from omni.api.research import build_router as research_router
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
    app.include_router(holdings_router(app))
    app.include_router(exposure_router(app))
    app.include_router(export_router(app))
    app.include_router(scanner_router(app))
    app.include_router(risk_monitor_router(app))
    app.include_router(research_router(app))
    app.include_router(profile_router(app))
    app.include_router(companies_router(app))
    app.include_router(settings_router(app))
    app.include_router(finance_router(app))
    app.include_router(wallets_router(app))
    app.include_router(mcp_mount(app))
    return app


app = create_app()
