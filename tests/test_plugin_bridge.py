import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.plugin_bridge import InEditorBridge
from backend.pipeline import PipelineManager


class PluginBridgeTest(unittest.TestCase):
    def test_embedded_bridge_analyzes_manifest_and_exports_editor_timelines(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "selected.mp4"
            source.write_bytes(b"video")
            bridge = InEditorBridge(
                Path(directory) / "workspace",
                pipeline_factory=lambda database: PipelineManager(
                    database, probe=lambda _path: SimpleNamespace(to_dict=lambda: {
                        "duration_sec": 7200, "width": 1920, "height": 1080, "audio_tracks": 1,
                    }),
                ),
            )
            result = bridge.analyze_selected_media(
                str(source), manifest_path=str(Path(__file__).parent / "fixtures" / "analysis-manifest.json"),
            )
        self.assertEqual(result["project"]["status"], "PLANNING")
        self.assertTrue(result["pipeline"]["done"])
        self.assertTrue(result["editor_exports"])
        self.assertTrue(Path(result["editor_exports"][0]["fcpxml"]).name.endswith(".fcpxml"))

    def test_host_registry_is_explicit_about_native_and_interchange_support(self):
        root = Path(__file__).parents[1]
        registry = json.loads((root / "editor_plugins" / "hosts.json").read_text())
        hosts = {item["id"]: item for item in registry["hosts"]}
        self.assertEqual(hosts["davinci_resolve"]["mode"], "embedded_python")
        self.assertEqual(hosts["adobe_premiere_pro"]["mode"], "cep_panel")
        self.assertEqual(hosts["capcut"]["mode"], "render_and_caption")
        for host in registry["hosts"]:
            if host.get("adapter"):
                self.assertTrue((root / "editor_plugins" / host["adapter"]).is_file())


if __name__ == "__main__":
    unittest.main()
