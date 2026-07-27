from pathlib import Path

from neutron.nucleus import NucleusClient
from neutron.nucleus.migrate import Migrator

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


async def connect(url: str) -> NucleusClient:
    return await NucleusClient.connect(url)


async def migrate(client: NucleusClient) -> list[str]:
    return await Migrator(client.pool).migrate(str(MIGRATIONS_DIR))
