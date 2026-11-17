"""Flask health endpoint, served from a background thread that can be stopped cleanly.

``/`` keeps the plain "Bot is alive!" reply that existing keep-alive pingers
expect. ``/healthz`` reports real state and answers 503 until the bot is
connected and the database is reachable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from flask import Flask, jsonify
from werkzeug.serving import BaseWSGIServer, make_server

log = logging.getLogger(__name__)


@dataclass
class HealthState:
    started_at: float = field(default_factory=time.time)
    ready: bool = False  # set once the gateway session is up
    db_check: Callable[[], bool] = lambda: True


def create_health_app(state: HealthState) -> Flask:
    app = Flask("mathbot.health")

    @app.get("/")
    def root():
        return "Bot is alive!", 200

    @app.get("/healthz")
    def healthz():
        db_ok = state.db_check()
        healthy = state.ready and db_ok
        body = {
            "status": "ok" if healthy else "starting" if not state.ready else "degraded",
            "discord_ready": state.ready,
            "database": db_ok,
            "uptime_seconds": round(time.time() - state.started_at),
        }
        return jsonify(body), 200 if healthy else 503

    return app


class HealthServer:
    def __init__(self, state: HealthState, host: str, port: int) -> None:
        self.state = state
        self.host = host
        self.port = port
        self._server: BaseWSGIServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the port (raising immediately if it is taken) and serve in the background."""
        self._server = make_server(self.host, self.port, create_health_app(self.state), threaded=True)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, name="health", daemon=True)
        self._thread.start()
        log.info("health endpoint listening", extra={"host": self.host, "port": self.port})

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
