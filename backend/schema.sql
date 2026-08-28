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

CREATE TABLE IF NOT EXISTS upload_jobs (
  upload_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
  privacy_status TEXT NOT NULL CHECK (privacy_status IN ('PRIVATE','UNLISTED')),
  status TEXT NOT NULL CHECK (status IN ('QUEUED','UPLOADING','RETRY_QUEUED','COMPLETE','FAILED')),
  youtube_video_id TEXT,
  retry_at TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_output_pairs (
  pair_id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
  source_ref TEXT NOT NULL,
  output_ref TEXT NOT NULL,
  selection_analysis_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance_snapshots (
  performance_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
  metrics_json TEXT NOT NULL,
  collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_segments (
  segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  track_index INTEGER NOT NULL,
  start_sec REAL NOT NULL,
  end_sec REAL NOT NULL,
  speaker_tag TEXT NOT NULL DEFAULT 'UNKNOWN',
  text TEXT NOT NULL,
  confidence REAL,
  words_json TEXT NOT NULL DEFAULT '[]',
  CHECK (start_sec >= 0 AND end_sec > start_sec),
  CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS scan_windows (
  window_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  pass_kind TEXT NOT NULL CHECK (pass_kind IN ('COARSE','PRECISION')),
  start_sec REAL NOT NULL,
  end_sec REAL NOT NULL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  CHECK (start_sec >= 0 AND end_sec > start_sec)
);

CREATE TABLE IF NOT EXISTS pipeline_steps (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  step TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','COMPLETE','FAILED','CANCELLED')),
  progress INTEGER NOT NULL CHECK (progress BETWEEN 0 AND 100),
  output_json TEXT NOT NULL DEFAULT '{}',
  error_message TEXT,
  started_at TEXT,
  completed_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (project_id, step)
);

CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
CREATE INDEX IF NOT EXISTS idx_candidates_project ON content_candidates(project_id);
CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id);
CREATE INDEX IF NOT EXISTS idx_logs_project ON job_logs(project_id, log_id DESC);
CREATE INDEX IF NOT EXISTS idx_upload_jobs_status ON upload_jobs(status, retry_at);
CREATE INDEX IF NOT EXISTS idx_source_output_project ON source_output_pairs(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_performance_episode ON performance_snapshots(episode_id, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_transcript_project_time ON transcript_segments(project_id, start_sec);
CREATE INDEX IF NOT EXISTS idx_scan_windows_project ON scan_windows(project_id, pass_kind, start_sec);
CREATE INDEX IF NOT EXISTS idx_pipeline_steps_project ON pipeline_steps(project_id, updated_at);
