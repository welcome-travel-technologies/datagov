"""The ASGI app gunicorn loads in its worker: the hooks first, then luccy's own app, unchanged
apart from its /health route. Imported in the worker (no --preload), so the Qdrant client and
the live corpus are made after the fork."""
from luccy_qdrant import hooks

hooks.install()

from serving.api.app import app  # noqa: E402  - luccy's FastAPI app, imported after the hooks

hooks.install_health(app)

__all__ = ["app"]
