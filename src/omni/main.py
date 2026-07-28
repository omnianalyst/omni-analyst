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

    from omni.api.coverage import build_router

    app.include_router(build_router(app))
    return app


app = create_app()
