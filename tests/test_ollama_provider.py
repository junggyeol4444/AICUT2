"""The local reasoning provider, against a server that speaks Ollama's protocol.

18장 draws the line between program and AI; it does not say which machine the
AI runs on, and 20.1's stack names no model service at all. So a local model is
in-spec, and it is the only option for an operator who will not send hours of
their own broadcast to somebody else's server.

These run a real HTTP server on localhost and assert what actually crosses the
wire — above all that the frames arrive, byte for byte. 5.2 has the passes read
화면 and 소리 together, and a provider that quietly drops the images turns the
whole system back into the speech-only editor 1.2 rejects, with nothing failing
to say so.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aicut.errors import ProviderError
from aicut.llm import PRODUCERS, get_producer
from aicut.llm.ollama_provider import OllamaProducer, looks_like_a_vision_model


class _Handler(BaseHTTPRequestHandler):
    """Enough of Ollama's API to exercise the provider. Records what it receives."""

    def log_message(self, *args):        # keep the test output readable
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": m} for m in self.server.models]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        body = json.loads(raw)
        self.server.requests.append(body)
        status, reply = self.server.reply
        if status != 200:
            self._send(reply, status)
            return
        self._send(reply)

    def _send(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class OllamaTestCase(unittest.TestCase):
    models = ["qwen2.5vl:7b", "llama3:8b"]
    reply = (200, {"message": {"role": "assistant", "content": '{"summary": "ok"}'}})

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.models = list(self.models)
        self.server.reply = self.reply
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def producer(self, **kwargs) -> OllamaProducer:
        kwargs.setdefault("model", "qwen2.5vl:7b")
        return OllamaProducer(host=self.host, **kwargs)

    def frames(self, count=3) -> list[str]:
        made = []
        for i in range(count):
            path = Path(self.tmp.name) / f"f{i}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]) * (64 + i))
            made.append(str(path))
        return made


class RegistrationTests(unittest.TestCase):
    def test_ollama_is_a_provider_the_cli_can_name(self):
        self.assertIn("ollama", PRODUCERS)

    def test_an_unknown_provider_names_the_ones_that_exist(self):
        with self.assertRaises(ProviderError) as caught:
            get_producer("gpt")
        for name in PRODUCERS:
            self.assertIn(name, str(caught.exception))


class VisionModelTests(unittest.TestCase):
    """5.2: a text-only model turns this back into a speech-only editor."""

    def test_models_that_take_images_are_recognised(self):
        for model in ("llava:13b", "qwen2.5vl:7b", "gemma3:4b", "llama3.2-vision",
                      "minicpm-v", "moondream"):
            self.assertTrue(looks_like_a_vision_model(model), model)

    def test_text_only_models_are_not(self):
        for model in ("llama3:8b", "qwen2.5:7b", "mistral", "phi4", "deepseek-r1"):
            self.assertFalse(looks_like_a_vision_model(model), model)

    def test_a_text_only_model_is_warned_about_not_refused(self):
        """It is the operator's machine and their call; silence is what is wrong."""
        with self.assertLogs("aicut.llm.ollama_provider", level="WARNING") as logged:
            OllamaProducer(model="llama3:8b", host="http://127.0.0.1:1")
        self.assertIn("5.2", "".join(logged.output))


class FramesReachTheModelTests(OllamaTestCase):
    def test_the_exact_frame_bytes_arrive(self):
        frames = self.frames()
        self.producer().summarize_window({"window": {"start_sec": 0}}, images=frames)
        sent = self.server.requests[0]["messages"][-1]["images"]
        self.assertEqual(len(sent), len(frames))
        for path, encoded in zip(frames, sent):
            self.assertEqual(
                hashlib.sha256(base64.b64decode(encoded)).hexdigest(),
                hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            )

    def test_the_frames_stay_in_the_order_they_were_given(self):
        frames = self.frames(4)
        self.producer().summarize_window({"window": {}}, images=frames)
        sent = self.server.requests[0]["messages"][-1]["images"]
        self.assertEqual([len(base64.b64decode(s)) for s in sent],
                         [len(Path(f).read_bytes()) for f in frames])

    def test_a_call_with_no_frames_sends_no_images_key(self):
        self.producer().summarize_window({"window": {}})
        self.assertNotIn("images", self.server.requests[0]["messages"][-1])

    def test_an_unreadable_frame_is_skipped_not_fatal(self):
        frames = self.frames(2) + [str(Path(self.tmp.name) / "gone.png")]
        self.producer().summarize_window({"window": {}}, images=frames)
        self.assertEqual(len(self.server.requests[0]["messages"][-1]["images"]), 2)

    def test_the_task_prompt_travels_as_the_system_message(self):
        self.producer().summarize_window({"window": {}})
        messages = self.server.requests[0]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("5.2", messages[0]["content"])

    def test_json_is_demanded_of_the_model(self):
        self.producer().summarize_window({"window": {}})
        self.assertEqual(self.server.requests[0]["format"], "json")

    def test_the_context_size_is_passed_when_set(self):
        self.producer(num_ctx=16384).summarize_window({"window": {}})
        self.assertEqual(self.server.requests[0]["options"]["num_ctx"], 16384)


class CheckTests(OllamaTestCase):
    def test_a_loaded_model_reports_what_it_is(self):
        state = self.producer().check()
        self.assertEqual(state["model"], "qwen2.5vl:7b")
        self.assertTrue(state["takes_images"])

    def test_a_model_that_was_never_pulled_says_how_to_pull_it(self):
        with self.assertRaises(ProviderError) as caught:
            self.producer(model="llava:34b").check()
        self.assertIn("ollama pull llava:34b", str(caught.exception))

    def test_a_tag_difference_is_not_treated_as_a_missing_model(self):
        self.producer(model="qwen2.5vl").check()

    def test_no_server_says_how_to_start_one(self):
        producer = OllamaProducer(model="qwen2.5vl:7b", host="http://127.0.0.1:1")
        with self.assertRaises(ProviderError) as caught:
            producer.check()
        self.assertIn("ollama serve", str(caught.exception))


class FailureTests(OllamaTestCase):
    def test_a_rejected_request_is_not_retried(self):
        self.server.reply = (400, {"error": "bad request"})
        producer = self.producer(max_retries=3)
        with self.assertRaises(ProviderError):
            producer.summarize_window({"window": {}})
        self.assertEqual(len(self.server.requests), 1, "a 400 was retried")

    def test_a_missing_model_at_call_time_says_how_to_pull_it(self):
        self.server.reply = (404, {"error": "model not found"})
        with self.assertRaises(ProviderError) as caught:
            self.producer().summarize_window({"window": {}})
        self.assertIn("ollama pull", str(caught.exception))

    def test_an_empty_reply_says_what_usually_causes_it(self):
        self.server.reply = (200, {"message": {"content": "   "}})
        with self.assertRaises(ProviderError) as caught:
            self.producer(max_retries=1).summarize_window({"window": {}})
        self.assertIn("--ollama-num-ctx", str(caught.exception))

    def test_the_exchange_can_be_kept_for_the_15_4_reviewer(self):
        log_dir = Path(self.tmp.name) / "log"
        self.producer(transcript_dir=log_dir).summarize_window({"window": {"start_sec": 7}})
        files = list(log_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        kept = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(kept["model"], "qwen2.5vl:7b")
        self.assertIn("start_sec", kept["request"])


class HostTests(unittest.TestCase):
    def test_a_bare_host_and_port_is_accepted(self):
        """OLLAMA_HOST is conventionally written without a scheme."""
        self.assertEqual(
            OllamaProducer(model="llava", host="192.168.1.9:11434").host,
            "http://192.168.1.9:11434",
        )

    def test_a_trailing_slash_does_not_double_up(self):
        self.assertEqual(
            OllamaProducer(model="llava", host="http://localhost:11434/").host,
            "http://localhost:11434",
        )


if __name__ == "__main__":
    unittest.main()
