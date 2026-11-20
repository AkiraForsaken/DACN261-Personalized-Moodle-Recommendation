"""Run the engine service: ``python -m api`` (or ``python engine-api.py``).

Needs the MariaDB driver (collector) and FastAPI/uvicorn: run it in .venv.
The model stages still run in .venv-kt, as with the worker.
"""

import argparse


def main(argv=None):
    from .settings import Settings

    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="python engine-api.py", description=__doc__)
    parser.add_argument("--host", default=settings.host,
                        help="bind address (default $ENGINE_API_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args(argv)

    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
