"""Environment-driven settings of the engine service. No I/O besides os.environ."""

import os
from dataclasses import dataclass

DEFAULT_PORT = 8010
MIN_TOKEN_LENGTH = 16


@dataclass(frozen=True)
class Settings:
    api_token: str
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    site: str | None = None
    # How long a request with ?wait=1 blocks before answering 202 with the job.
    wait_seconds: float = 120.0

    @classmethod
    def from_env(cls):
        token = os.getenv("API_TOKEN", "").strip()
        if len(token) < MIN_TOKEN_LENGTH:
            raise RuntimeError(
                f"API_TOKEN must be a secret of at least {MIN_TOKEN_LENGTH} characters, e.g.\n"
                "  export API_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')\n"
                "Moodle sends the same value (local_microlearning | enginetoken)."
            )
        return cls(
            api_token=token,
            host=os.getenv("ENGINE_API_HOST", "127.0.0.1"),
            port=int(os.getenv("ENGINE_API_PORT", DEFAULT_PORT)),
            site=os.getenv("MICROLEARNING_SITE") or None,
            wait_seconds=float(os.getenv("ENGINE_API_WAIT_SECONDS", "120")),
        )
