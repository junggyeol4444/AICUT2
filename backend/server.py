from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .database import Database
from .pipeline import PipelineManager
from .render import RenderError, RenderPlan, export_plan, render

ROOT = Path(__file__).resolve().parents[1]
DB = Database(os.environ.get("AICUT_DB", ROOT / "aicut.db"))
PIPELINE = PipelineManager(DB)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AICUT/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                self.json({"status": "ok", "service": "aicut-local-runtime"})
            elif path == "/api/projects":
                self.json(DB.list_projects())
            elif path.startswith("/api/projects/") and path.endswith("/candidates"):
                self.json(DB.list_candidates(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/logs"):
                self.json(DB.logs(path.split("/")[3]))
            elif path.startswith("/api/projects/") and path.endswith("/job"):
                self.json(PIPELINE.state(path.split("/")[3]))
            elif path.startswith("/api/projects/"):
                self.json(DB.get_project(path.split("/")[3]))
            elif path.startswith("/api/episodes/") and path.endswith("/timeline"):
                self.json(DB.get_timeline(path.split("/")[3]))
            elif path == "/api/logs":
                self.json(DB.logs())
            else:
                self.serve_static(path)
        except KeyError as error:
            self.json({"error": "not_found", "id": str(error.args[0])}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.json({"error": "internal_error", "message": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.body()
            if path == "/api/projects":
                if not payload.get("file_path"):
                    return self.json({"error": "file_path_required"}, HTTPStatus.BAD_REQUEST)
                self.json(DB.create_project(payload), HTTPStatus.CREATED)
            elif path.startswith("/api/projects/") and path.endswith("/run"):
                project_id = path.split("/")[3]
                DB.get_project(project_id)
                accepted = PIPELINE.submit(project_id, payload.get("manifest_path"))
                self.json({"accepted": accepted, "project_id": project_id}, HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            elif path.startswith("/api/projects/") and path.endswith("/analysis"):
                project_id = path.split("/")[3]
                self.json(DB.import_analysis(project_id, payload))
            elif path.startswith("/api/candidates/") and path.endswith("/review"):
                self.json(DB.review_candidate(path.split("/")[3], payload.get("decision", ""), payload.get("feedback", "")))
            elif path.startswith("/api/episodes/") and path.endswith("/review"):
                self.json(DB.review_episode(path.split("/")[3], bool(payload.get("approved"))))
            elif path.startswith("/api/episodes/") and path.endswith("/render"):
                episode_id = path.split("/")[3]
                episode = DB.get_episode(episode_id)
                output_path = payload.get("output_path") or str(ROOT / "outputs" / f"{episode_id}.mp4")
                plan = RenderPlan(
                    input_path=episode["file_path"], output_path=output_path,
                    cuts=tuple(DB.get_timeline(episode_id)),
                    width=int(payload.get("width", 1920)), height=int(payload.get("height", 1080)),
                )
                if not payload.get("execute", False):
                    self.json({"episode_id": episode_id, "dry_run": True, "plan": json.loads(export_plan(plan))})
                else:
                    DB.set_render_status(episode_id, "RENDERING")
                    try:
                        result = render(plan)
                    except Exception:
                        DB.set_render_status(episode_id, "FAILED")
                        raise
                    DB.set_render_status(episode_id, "COMPLETE", result["output_path"])
                    self.json({"episode_id": episode_id, **result})
            else:
                self.json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except ValueError as error:
            self.json({"error": "invalid_request", "message": str(error)}, HTTPStatus.BAD_REQUEST)
        except RenderError as error:
            self.json({"error": "render_error", "message": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except KeyError as error:
            self.json({"error": "not_found", "id": str(error.args[0])}, HTTPStatus.NOT_FOUND)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)

    def serve_static(self, request_path: str) -> None:
        root = ROOT / ("dist" if (ROOT / "dist").exists() else "")
        relative = request_path.lstrip("/") or "index.html"
        target = (root / relative).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            return self.json({"error": "invalid_path"}, HTTPStatus.BAD_REQUEST)
        if not target.is_file():
            target = root / "index.html"
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[AICUT] {self.address_string()} {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AICUT local API and web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    print(f"AICUT local runtime: http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), ApiHandler).serve_forever()


if __name__ == "__main__":
    main()
