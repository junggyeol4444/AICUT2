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
  pacing_reason TEXT NOT NULL DEFAULT '',
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
  publication_status TEXT NOT NULL DEFAULT 'PRIVATE' CHECK (publication_status IN ('PRIVATE','UNLISTED','PUBLIC','SCHEDULED')),
  scheduled_publish_at TEXT,
  thumbnail_uploaded_at TEXT,
  upload_session_url TEXT,
  uploaded_bytes INTEGER NOT NULL DEFAULT 0 CHECK (uploaded_bytes >= 0),
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

CREATE TABLE IF NOT EXISTS analytics_collection_jobs (
  collection_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
  youtube_video_id TEXT NOT NULL,
  snapshot_label TEXT NOT NULL CHECK (snapshot_label IN ('24H','7D','30D')),
  due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','RUNNING','COMPLETE','FAILED')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (episode_id, snapshot_label)
);

CREATE TABLE IF NOT EXISTS strategy_versions (
  strategy_version_id TEXT PRIMARY KEY,
  channel_ref TEXT NOT NULL,
  version_number INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('DRAFT','ACTIVE','ROLLED_BACK')),
  strategy_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (channel_ref, version_number)
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
  input_hash TEXT,
  checkpoint_version INTEGER NOT NULL DEFAULT 1,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  started_at TEXT,
  completed_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (project_id, step)
);

CREATE TABLE IF NOT EXISTS analysis_observations (
  observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  modality TEXT NOT NULL CHECK (modality IN ('AUDIO','VISION')),
  kind TEXT NOT NULL,
  track_index INTEGER,
  start_sec REAL NOT NULL,
  end_sec REAL NOT NULL,
  confidence REAL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  CHECK (start_sec >= 0 AND end_sec > start_sec),
  CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS understanding_windows (
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  sequence_order INTEGER NOT NULL,
  start_sec REAL NOT NULL,
  end_sec REAL NOT NULL,
  summary TEXT NOT NULL,
  memory_json TEXT NOT NULL,
  precision_ranges_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(project_id, sequence_order),
  CHECK(start_sec >= 0 AND end_sec > start_sec)
);

CREATE TABLE IF NOT EXISTS scene_retrieval_results (
  retrieval_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  candidate_id TEXT NOT NULL REFERENCES content_candidates(candidate_id) ON DELETE CASCADE,
  query TEXT NOT NULL DEFAULT '',
  start_sec REAL NOT NULL,
  end_sec REAL NOT NULL,
  score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),
  scene_role TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  CHECK(start_sec >= 0 AND end_sec > start_sec)
);

CREATE TABLE IF NOT EXISTS planning_versions (
  planning_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  version_number INTEGER NOT NULL,
  manifest_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, version_number)
);

CREATE TABLE IF NOT EXISTS runtime_scheduler_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  completed_at TEXT NOT NULL,
  results_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
CREATE INDEX IF NOT EXISTS idx_candidates_project ON content_candidates(project_id);
CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id);
CREATE INDEX IF NOT EXISTS idx_logs_project ON job_logs(project_id, log_id DESC);
CREATE INDEX IF NOT EXISTS idx_upload_jobs_status ON upload_jobs(status, retry_at);
CREATE INDEX IF NOT EXISTS idx_source_output_project ON source_output_pairs(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_performance_episode ON performance_snapshots(episode_id, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_analytics_collection_due ON analytics_collection_jobs(status, due_at);
CREATE INDEX IF NOT EXISTS idx_transcript_project_time ON transcript_segments(project_id, start_sec);
CREATE INDEX IF NOT EXISTS idx_scan_windows_project ON scan_windows(project_id, pass_kind, start_sec);
CREATE INDEX IF NOT EXISTS idx_pipeline_steps_project ON pipeline_steps(project_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_observations_project_time ON analysis_observations(project_id, start_sec, modality);
CREATE INDEX IF NOT EXISTS idx_understanding_project_time ON understanding_windows(project_id, start_sec);
CREATE INDEX IF NOT EXISTS idx_retrieval_candidate_score ON scene_retrieval_results(candidate_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_planning_versions_project ON planning_versions(project_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_runtime_scheduler_runs_time ON runtime_scheduler_runs(completed_at DESC);
