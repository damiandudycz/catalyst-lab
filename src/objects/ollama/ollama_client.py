"""Small synchronous client for a local Ollama server.

Call these methods from a worker thread when using them from the GTK interface.
"""

import json
import socket
import threading
from http.client import IncompleteRead
from contextlib import contextmanager
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


class OllamaError(RuntimeError):
    """An Ollama request failed or returned an invalid response."""


class OllamaCancelled(OllamaError):
    """An in-flight Ollama request was canceled by the user."""


def redact_server_url(value: str) -> tuple[str, bool]:
    """Return a display-safe HTTP(S) server URL and whether secret parts were removed."""
    value = str(value).strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        value = "http://" + value
    parsed = urlsplit(value)
    redacted = bool(parsed.username or parsed.password or parsed.query or parsed.fragment)
    return (urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")),
            redacted)


class OllamaClient:
    """Client for Ollama's local HTTP API (model discovery, chat, and generate)."""

    def __init__(self, base_url: str = "http://localhost:11434", timeout: float = 300,
                 model: str | None = None, cancel_event: threading.Event | None = None):
        base_url = str(base_url).strip().rstrip("/")
        if not base_url:
            raise ValueError("Ollama server URL can't be empty")
        if not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url
        self.base_url = base_url
        self.display_base_url, _ = redact_server_url(base_url)
        self.timeout = timeout
        self.model = model
        self.cancel_event = cancel_event or threading.Event()
        self._response_lock = threading.Lock()
        self._active_response = None
        self._thinking_capabilities: dict[str, bool | None] = {}

    def cancel(self) -> None:
        """Interrupt the active HTTP read and prevent further requests on this client."""
        self.cancel_event.set()
        with self._response_lock:
            response = self._active_response
        if response is not None:
            try:
                stream = getattr(response, "fp", None)
                raw = getattr(stream, "raw", None)
                connection = getattr(raw, "_sock", None)
                if connection is not None:
                    connection.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            try:
                response.close()
            except OSError:
                pass

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise OllamaCancelled("Stopped by user")

    def _set_active_response(self, response):
        with self._response_lock:
            self._active_response = response
            canceled = self.cancel_event.is_set()
        if canceled:
            response.close()

    def _clear_active_response(self, response):
        with self._response_lock:
            if self._active_response is response:
                self._active_response = None

    @contextmanager
    def _open(self, request):
        self._check_cancelled()
        with urlopen(request, timeout=self.timeout) as response:
            self._set_active_response(response)
            try:
                yield response
            finally:
                self._clear_active_response(response)

    @classmethod
    def from_settings(cls, settings=None, timeout: float = 300) -> "OllamaClient":
        """Create a client using the saved application URL."""
        if settings is None:
            from .repositories import Repository
            settings = Repository.Settings.value
        return cls(settings.ollama_url, timeout=timeout, model=settings.ollama_model or None)

    def list_models(self) -> list[dict]:
        """Return installed models as Ollama model metadata dictionaries."""
        response = self._request("GET", "/api/tags")
        models = response.get("models")
        if not isinstance(models, list):
            raise OllamaError("Ollama returned an invalid model list")
        return models

    def supports_thinking(self, model: str | None = None) -> bool | None:
        """Return Ollama's declared thinking capability, or None if unknown."""
        model = self._model(model)
        if model not in self._thinking_capabilities:
            try:
                result = self._request("POST", "/api/show", {"model": model})
            except OllamaError:
                # Older Ollama servers may not expose capabilities. Preserve
                # their existing behavior and let the chat endpoint decide.
                return None
            thinking = result.get("thinking")
            values = thinking.get("values") if isinstance(thinking, dict) else None
            if isinstance(values, list) and values:
                # Ollama documents [false] as the explicit no-thinking case;
                # named values and true indicate that the model has a control.
                self._thinking_capabilities[model] = values != [False]
            else:
                # Missing metadata does not imply that the model cannot think.
                self._thinking_capabilities[model] = None
        return self._thinking_capabilities[model]

    def chat(self, messages: list[dict], model: str | None = None, **options) -> dict:
        """Send chat messages and return Ollama's response object."""
        model = self._model(model)
        if options.get("think") is True and self.supports_thinking(model) is False:
            options.pop("think")
        payload = {"model": model, "messages": messages, "stream": False, **options}
        try:
            return self._request("POST", "/api/chat", payload)
        except OllamaError as error:
            if not self._retry_without_thinking(payload, error):
                raise
            return self._request("POST", "/api/chat", payload)

    def chat_stream(self, messages: list[dict], on_chunk: Callable[[dict], None],
                    model: str | None = None, **options) -> dict:
        """Stream Ollama chat NDJSON events, returning the final event."""
        model = self._model(model)
        thinking_supported = self.supports_thinking(model)
        if thinking_supported is False and options.get("think") is True:
            options.pop("think")
        payload = {"model": model, "messages": messages, "stream": True, **options}
        received_event = False

        def track_event(event, thinking_supported=True):
            nonlocal received_event
            received_event = True
            tagged_event = dict(event)
            tagged_event["_thinking_supported"] = thinking_supported
            on_chunk(tagged_event)

        try:
            return self._stream_request(
                "/api/chat", payload,
                # Only treat a model as a native-thinking model when Ollama
                # explicitly advertises that capability. Unknown/older
                # server metadata must not make regular answer text appear in
                # the Thinking block; explicit in-band think markers are still
                # parsed by the chat view.
                lambda event: track_event(event, thinking_supported is True))
        except OllamaError as error:
            if received_event or not self._retry_without_thinking(payload, error):
                raise
            return self._stream_request(
                "/api/chat", payload, lambda event: track_event(event, thinking_supported=False))

    @staticmethod
    def _retry_without_thinking(payload: dict, error: OllamaError) -> bool:
        """Remove unsupported `think` only when Ollama explicitly rejects it."""
        if payload.get("think") is not True:
            return False
        detail = str(error).lower()
        if "does not support thinking" not in detail and "does not support think" not in detail:
            return False
        payload.pop("think", None)
        return True

    def generate(self, prompt: str, model: str | None = None, **options) -> dict:
        """Generate a completion and return Ollama's response object."""
        model = self._model(model)
        return self._request("POST", "/api/generate", {
            "model": model, "prompt": prompt, "stream": False, **options,
        })

    def pull_model(self, model: str, progress: Callable[[dict], None] | None = None) -> None:
        """Download a model, calling progress with each Ollama NDJSON event."""
        model = str(model).strip()
        if not model:
            raise ValueError("Model name can't be empty")
        request = Request(self.base_url + "/api/pull", data=json.dumps({
            "model": model, "stream": True,
        }).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"})
        try:
            with self._open(request) as response:
                for line in response:
                    self._check_cancelled()
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise OllamaError("Ollama returned invalid pull progress data") from error
                    if not isinstance(event, dict):
                        raise OllamaError("Ollama returned invalid pull progress data")
                    if event.get("error"):
                        raise OllamaError(str(event["error"]))
                    if progress is not None:
                        progress(event)
                    if event.get("status") == "success":
                        return
                self._check_cancelled()
        except HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            raise OllamaError(f"Ollama returned HTTP {error.code}" + (f": {detail}" if detail else "")) from error
        except (URLError, TimeoutError, OSError) as error:
            if self.cancel_event.is_set():
                raise OllamaCancelled("Stopped by user") from error
            raise OllamaError(f"Can't download from Ollama at {self.display_base_url}: {error}") from error
        raise OllamaError("Ollama ended the model download before reporting success")

    def _model(self, model: str | None) -> str:
        if model:
            return model
        if not self.model:
            raise OllamaError("Select an Ollama model first")
        return self.model

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with self._open(request) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            raise OllamaError(f"Ollama returned HTTP {error.code}" + (f": {detail}" if detail else "")) from error
        except (URLError, TimeoutError, OSError) as error:
            if self.cancel_event.is_set():
                raise OllamaCancelled("Stopped by user") from error
            raise OllamaError(f"Can't connect to Ollama at {self.display_base_url}: {error}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OllamaError("Ollama returned invalid JSON") from error
        if not isinstance(result, dict):
            raise OllamaError("Ollama returned an invalid response")
        if result.get("error"):
            raise OllamaError(str(result["error"]))
        self._check_cancelled()
        return result

    def _stream_request(self, path: str, payload: dict, on_chunk: Callable[[dict], None]) -> dict:
        request = Request(self.base_url + path, data=json.dumps(payload).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/json"})
        final = None
        try:
            with self._open(request) as response:
                for line in response:
                    self._check_cancelled()
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise OllamaError("Ollama returned invalid streaming data") from error
                    if not isinstance(event, dict):
                        raise OllamaError("Ollama returned invalid streaming data")
                    if event.get("error"):
                        raise OllamaError(str(event["error"]))
                    final = event
                    on_chunk(event)
                self._check_cancelled()
        except HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            raise OllamaError(f"Ollama returned HTTP {error.code}" + (f": {detail}" if detail else "")) from error
        except IncompleteRead as error:
            if self.cancel_event.is_set():
                raise OllamaCancelled("Stopped by user") from error
            raise OllamaError("EOF") from error
        except (URLError, TimeoutError, OSError) as error:
            if self.cancel_event.is_set():
                raise OllamaCancelled("Stopped by user") from error
            raise OllamaError(f"Can't connect to Ollama at {self.display_base_url}: {error}") from error
        if final is None:
            raise OllamaError("Ollama ended the response before sending any data")
        return final
