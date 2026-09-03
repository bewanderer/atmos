"""The public API.

Read only, and read only by construction: it connects as a role with no write
grants at all, so no request can alter a stored reading.

Run it with:

    uvicorn atmos.api.app:app --reload
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from atmos.api import db
from atmos.api.routes import router

# Windows defaults to the proactor loop, which psycopg refuses to run async on.
# Deployment is Linux, but development is not, and an API that only starts on
# one of them is not much use. Set before any loop is created.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DESCRIPTION = """
Open archive of air quality and weather measurements for Bosnia and Herzegovina.

**Values are served as published.** Nothing is rounded and nothing is corrected.
Where a source publishes 7.2 you get 7.2, and `raw_value` carries the original
string beside the harmonised number so unit conversion can always be undone.

**Numbers are strings.** Measurements are decimals, and turning them into
floating point would change them. Parse them yourself, in whatever precision you
need, rather than receiving something we have already damaged.

**Nothing is deleted.** A reading that looks wrong is flagged, never removed, and
a source that changes a published value gets a new revision while the first one
stays. Ask for `revisions=true` to see the whole chain.

**You choose the sources.** Every statistic recomputes against the sources you
select. Excluding one and watching the answer move is a supported thing to do,
not a workaround.

**One clock.** Timestamps come back at Bosnia and Herzegovina local time. Each
source also reports the timezone it publishes in, so the original is never lost.

Data is CC BY 4.0. Attribution requirements are per source and given in `/meta`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await db.open_pool()
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(
    title="Atmos API",
    version="0.1.0",
    description=DESCRIPTION,
    license_info={"name": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"},
    lifespan=lifespan,
)

# The site is served from a different origin to the API, so the browser needs
# this. Reads only, no credentials, so it costs nothing to allow widely: the
# data is public and meant to be used by anyone.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ATMOS_CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/v1")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness only. Says nothing about the data."""
    await db.fetch_one("select 1 as ok")
    return {"status": "ok"}
