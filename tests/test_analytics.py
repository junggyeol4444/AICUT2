import json
import unittest
from datetime import date
from urllib.parse import parse_qs, urlparse

from backend.analytics import YouTubeAnalyticsClient
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


if __name__ == "__main__":
    unittest.main()
