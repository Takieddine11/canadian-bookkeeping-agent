"""Entry point: aiohttp server hosting the Bot Framework CloudAdapter.

Run locally:

    pip install -e ".[dev]"
    python -m src.bot.app

Expose the local port via ngrok (``ngrok http 3978``) and point the Azure Bot
resource's messaging endpoint at ``https://<ngrok-id>.ngrok-free.app/api/messages``.

Required environment variables (see ``.env.example``):

* ``MICROSOFT_APP_ID``        — Azure Bot app registration client ID
* ``MICROSOFT_APP_PASSWORD``  — client secret (or certificate for MSI)
* ``MICROSOFT_APP_TYPE``      — ``SingleTenant`` for this project's v1
* ``MICROSOFT_APP_TENANTID``  — Azure AD tenant ID for the firm
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from dotenv import load_dotenv

from src.bot.activity_handler import AuditBot
from src.store.engagement_db import DEFAULT_ROOT, EngagementStore

HOST = "0.0.0.0"
PORT = 3978
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s :: %(message)s"


def _build_auth_config() -> SimpleNamespace:
    """Shape env vars into the attribute names ConfigurationBotFrameworkAuthentication expects.

    The SDK's ``ConfigurationServiceClientCredentialFactory`` reads ``APP_ID``,
    ``APP_PASSWORD``, ``APP_TYPE``, ``APP_TENANTID`` directly via ``hasattr`` — no
    ``MICROSOFT_`` prefix. We keep the prefixed names in ``.env`` (Microsoft's own
    convention in Azure Bot docs) and map them here.
    """
    return SimpleNamespace(
        APP_ID=os.environ.get("MICROSOFT_APP_ID", ""),
        APP_PASSWORD=os.environ.get("MICROSOFT_APP_PASSWORD", ""),
        APP_TYPE=os.environ.get("MICROSOFT_APP_TYPE", "SingleTenant"),
        APP_TENANTID=os.environ.get("MICROSOFT_APP_TENANTID", ""),
    )


def create_app(uploads_root: Path | None = None) -> web.Application:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format=LOG_FORMAT,
    )

    root = Path(uploads_root) if uploads_root else DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)

    store = EngagementStore(root=root)
    bot = AuditBot(store=store, uploads_root=root)

    auth = ConfigurationBotFrameworkAuthentication(_build_auth_config())
    adapter = CloudAdapter(auth)

    async def on_error(context, error: Exception) -> None:  # noqa: ANN001 (SDK type)
        logging.getLogger("bot.adapter").exception("unhandled turn error: %s", error)
        await context.send_activity("Sorry — an internal error occurred. The issue has been logged.")

    adapter.on_turn_error = on_error

    async def messages(req: web.Request) -> web.Response:
        return await adapter.process(req, bot)

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application(middlewares=[aiohttp_error_middleware])
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    return app


def main() -> None:
    app = create_app()
    logging.getLogger(__name__).info("bot.listening host=%s port=%s", HOST, PORT)
    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
