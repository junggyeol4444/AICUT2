from __future__ import annotations

import json
from datetime import date
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .performance import PerformanceError, validate_metrics


class YouTubeAnalyticsClient:
    endpoint = "https://youtubeanalytics.googleapis.com/v2/reports"

    def __init__(self, access_token: Callable[[], str], *, opener: Callable = urlopen):
        self.access_token, self.opener = access_token, opener

    def collect_video_metrics(
        self, video_id: str, start_date: date, end_date: date, duration_sec: float,
    ) -> dict:
        if not video_id or end_date < start_date or duration_sec <= 0:
            raise PerformanceError("Analytics 영상 ID, 조회 기간 또는 영상 길이가 올바르지 않습니다.")
        summary = self._report({
            "ids": "channel==MINE", "startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
            "metrics": "views,likes,comments,shares,averageViewDuration,averageViewPercentage",
            "filters": f"video=={video_id}",
        })
        retention = self._report({
            "ids": "channel==MINE", "startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
            "dimensions": "elapsedVideoTimeRatio", "metrics": "audienceWatchRatio",
            "filters": f"video=={video_id}", "sort": "elapsedVideoTimeRatio",
        })
        row = summary[0] if summary else {}
        metrics = {
            "views": int(row.get("views", 0)), "likes": int(row.get("likes", 0)),
            "comments": int(row.get("comments", 0)), "shares": int(row.get("shares", 0)),
            "average_view_duration_sec": float(row.get("averageViewDuration", 0)),
            "average_view_percentage": float(row.get("averageViewPercentage", 0)) / 100,
            "click_through_rate": 0,
            "retention": [{
                "second": float(item["elapsedVideoTimeRatio"]) * float(duration_sec),
                "ratio": min(max(float(item["audienceWatchRatio"]), 0), 1),
            } for item in retention],
            "source": "YOUTUBE_ANALYTICS_API", "video_id": video_id,
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        }
        return validate_metrics(metrics)

    def _report(self, params: dict[str, str]) -> list[dict]:
        request = Request(f"{self.endpoint}?{urlencode(params)}", headers={
            "Authorization": f"Bearer {self.access_token()}", "Accept": "application/json",
        })
        try:
            payload = json.loads(self.opener(request).read().decode("utf-8"))
            headers = [item["name"] for item in payload.get("columnHeaders", [])]
            return [dict(zip(headers, row)) for row in payload.get("rows", [])]
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise PerformanceError(f"YouTube Analytics API 오류 ({error.code}): {detail[-2000:]}") from error
        except (URLError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise PerformanceError(f"YouTube Analytics 응답을 처리할 수 없습니다: {error}") from error
