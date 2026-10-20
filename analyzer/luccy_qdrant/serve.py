"""`python -m luccy_qdrant.serve`: luccy's own serving command, on the hooked app.

The same gunicorn invocation luccy's image runs (serving/deploy/Dockerfile CMD): one uvicorn
worker (the job table and the loaded corpus live in one process), --timeout 900,
--graceful-timeout 300, PORT (default 8787), logs to the container log, no control socket;
GUNICORN_CMD_ARGS still adds flags. Only the app differs: `luccy_qdrant.app:app`.

The container's settings are checked here first (hooks.verify): one that cannot serve the
live corpus exits before gunicorn starts. The pins were checked when the image was built; a
difference here is only a loud warning. When a sync moves the `luccy_kw` alias, luccy's own
bundle pickup drains the worker and SIGTERMs it; gunicorn starts a fresh worker, which loads
the new bundle.
"""
import os
import sys


def argv(port: str | None = None) -> list[str]:
    return ["gunicorn", "luccy_qdrant.app:app",
            "--worker-class", "uvicorn_worker.UvicornWorker", "--workers", "1",
            "--timeout", "900", "--graceful-timeout", "300",
            "--bind", f"0.0.0.0:{port or os.environ.get('PORT', '8787')}",
            "--access-logfile", "-", "--error-logfile", "-", "--no-control-socket"]


def main() -> None:
    from luccy_qdrant import hooks
    hooks.verify(strict=False)
    print("[luccy_qdrant] starting luccy on the live DataGov corpus", file=sys.stderr, flush=True)
    args = argv()
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
