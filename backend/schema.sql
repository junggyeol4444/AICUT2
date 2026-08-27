PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  file_path TEXT NOT NULL,
  duration_sec REAL NOT NULL DEFAULT 0 CHECK (duration_sec >= 0),
  status TEXT NOT NULL DEFAULT 'QUEUED',
  progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  target_duration_hint TEXT,
  channel_ref TEXT,
  calibration_profile_id TEXT,
  media_info_json TEXT NOT NULL DEFAULT '{}',
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  summary TEXT NOT NULL,
  people_json TEXT NOT NULL DEFAULT '[]',
  relations_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS event_mentions (
  mention_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  source_start_sec REAL NOT NULL,
  source_end_sec REAL NOT NULL,
  role TEXT NOT NULL,
  CHECK (source_end_sec > source_start_sec)
);

CREATE TABLE IF NOT EXISTS content_candidates (
  candidate_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  core_summary TEXT NOT NULL,
  related_event_ids_json TEXT NOT NULL DEFAULT '[]',
  required_context TEXT NOT NULL DEFAULT '',
  independence_score REAL NOT NULL CHECK (independence_score BETWEEN 0 AND 1),
  decision TEXT NOT NULL CHECK (decision IN ('MAKE','COMBINE','HOLD','REJECT')),
  decision_reason TEXT NOT NULL DEFAULT '',
  user_feedback TEXT,
  reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  candidate_ids_json TEXT NOT NULL DEFAULT '[]',
  planned_structure_json TEXT NOT NULL DEFAULT '{}',
  target_type TEXT,
  planned_duration_sec REAL,
  output_mp4_path TEXT,
  thumbnail_path TEXT,
  render_status TEXT NOT NULL DEFAULT 'PENDING',
  review_status TEXT NOT NULL DEFAULT 'PENDING',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edit_timeline (
  cut_id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
  sequence_order INTEGER NOT NULL,
  source_start_sec REAL NOT NULL,
  source_end_sec REAL NOT NULL,
  speaker_tag TEXT NOT NULL DEFAULT 'UNKNOWN',
  scene_role TEXT NOT NULL,
  pacing_mode TEXT NOT NULL CHECK (pacing_mode IN ('KEEP','TRIM','CUT')),
  visual_effect_json TEXT NOT NULL DEFAULT '{}',
  subtitle_ref TEXT,
  UNIQUE (episode_id, sequence_order),
  CHECK (source_end_sec > source_start_sec)
);

CREATE TABLE IF NOT EXISTS calibration_profiles (
  profile_id TEXT PRIMARY KEY,
  channel_ref TEXT NOT NULL,
  name TEXT NOT NULL,
  params_json TEXT NOT NULL,
  measured_at TEXT NOT NULL,
  eval_score REAL NOT NULL CHECK (eval_score BETWEEN 0 AND 100)
);

CREATE TABLE IF NOT EXISTS job_logs (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'INFO',
  message TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_artifacts (
  artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, kind, path)
);

CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
CREATE INDEX IF NOT EXISTS idx_candidates_project ON content_candidates(project_id);
CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id);
CREATE INDEX IF NOT EXISTS idx_logs_project ON job_logs(project_id, log_id DESC);
