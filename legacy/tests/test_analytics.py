import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend.analytics import AnalyticsCollectionManager, YouTubeAnalyticsClient
from backend.database import Database
from backend.performance import PerformanceError


class Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


class AnalyticsTest(unittest.TestCase):
    def test_collects_summary_and_source_timeline_retention(self):
        requests = []

        def opener(request):
            requests.append(request)
            query = parse_qs(urlparse(request.full_url).query)
            if "dimensions" not in query:
                return Response({
                    "columnHeaders": [{"name": name} for name in (
                        "views", "likes", "comments", "shares", "averageViewDuration", "averageViewPercentage",
                    )],
                    "rows": [[1200, 80, 12, 4, 92.5, 61.2]],
                })
            return Response({
                "columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
                "rows": [[0, 1], [.25, .72], [.5, .55], [1, .31]],
            })

        client = YouTubeAnalyticsClient(lambda: "fresh-access", opener=opener)
        metrics = client.collect_video_metrics("video-1", date(2026, 8, 1), date(2026, 8, 30), 200)
        self.assertEqual(metrics["views"], 1200)
        self.assertAlmostEqual(metrics["average_view_percentage"], .612)
        self.assertEqual([point["second"] for point in metrics["retention"]], [0, 50, 100, 200])
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer fresh-access")
        self.assertEqual(parse_qs(urlparse(requests[0].full_url).query)["filters"], ["video==video-1"])

    def test_rejects_invalid_collection_range(self):
        client = YouTubeAnalyticsClient(lambda: "token")
        with self.assertRaises(PerformanceError):
            client.collect_video_metrics("video", date(2026, 8, 2), date(2026, 8, 1), 100)

    def test_durable_24h_7d_30d_snapshots_run_when_due(self):
        origin = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)

        class Client:
            def collect_video_metrics(self, video_id, start, end, duration):
                return {"views": 10, "likes": 1, "comments": 0, "shares": 0,
                        "click_through_rate": 0, "average_view_percentage": .5, "retention": [],
                        "video_id": video_id, "duration": duration, "period": [str(start), str(end)]}

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "analytics.db")
            project = database.create_project({"file_path": "/media/live.mkv"})
            manifest = json.loads((Path(__file__).parent / "fixtures" / "analysis-manifest.json").read_text())
            database.import_analysis(project["project_id"], manifest)
            jobs = database.schedule_analytics_snapshots("episode-operation", "video-1", origin)
            self.assertEqual([job["snapshot_label"] for job in jobs], ["24H", "7D", "30D"])
            result = AnalyticsCollectionManager(database, Client()).run_due(
                datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
            )
            snapshots = database.list_performance("episode-operation")
            remaining = database.due_analytics_collections(datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
        self.assertEqual(result, {"completed": 2, "failed": 0})
        self.assertEqual({item["metrics"]["snapshot_label"] for item in snapshots}, {"24H", "7D"})
        self.assertEqual([item["snapshot_label"] for item in remaining], ["30D"])


if __name__ == "__main__":
    unittest.main()
