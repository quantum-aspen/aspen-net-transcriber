"""The two Outline API calls the poller needs. The agent talks to Outline through its MCP server."""

import json
import urllib.error
import urllib.request

from pydantic import SecretStr


class OutlineError(RuntimeError):
    pass


class Outline:
    def __init__(self, base_url: str, api_key: SecretStr):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _post(self, method: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/api/{method}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError as e:
            raise OutlineError(f"{method}: HTTP {e.code} {e.read()[:300]!r}") from e
        except (OSError, ValueError, KeyError) as e:  # network down, timeout, or a garbled reply
            raise OutlineError(f"{method}: {type(e).__name__}: {e}") from e

    def document(self, doc_id: str) -> dict:
        """Accepts a document UUID or the url id at the end of a doc link."""
        return self._post("documents.info", {"id": doc_id})

    def update_text(self, doc_id: str, text: str) -> dict:
        return self._post("documents.update", {"id": doc_id, "text": text})
