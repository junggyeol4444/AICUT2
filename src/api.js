const API_ROOT = '/api';

async function request(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
  return payload;
}

export const api = {
  health: () => request('/health'),
  projects: () => request('/projects'),
  createProject: payload => request('/projects', { method: 'POST', body: JSON.stringify(payload) }),
  runProject: (projectId, manifestPath) => request(`/projects/${projectId}/run`, {
    method: 'POST', body: JSON.stringify({ manifest_path: manifestPath || null }),
  }),
  importAnalysis: (projectId, manifest) => request(`/projects/${projectId}/analysis`, {
    method: 'POST', body: JSON.stringify(manifest),
  }),
  job: projectId => request(`/projects/${projectId}/job`),
  candidates: projectId => request(`/projects/${projectId}/candidates`),
  reviewCandidate: (candidateId, decision, feedback = '') => request(`/candidates/${candidateId}/review`, {
    method: 'POST', body: JSON.stringify({ decision, feedback }),
  }),
  timeline: episodeId => request(`/episodes/${episodeId}/timeline`),
  renderEpisode: (episodeId, options = {}) => request(`/episodes/${episodeId}/render`, {
    method: 'POST', body: JSON.stringify(options),
  }),
  packageEpisode: (episodeId, options) => request(`/episodes/${episodeId}/package`, {
    method: 'POST', body: JSON.stringify(options),
  }),
  reviewEpisode: (episodeId, approved) => request(`/episodes/${episodeId}/review`, {
    method: 'POST', body: JSON.stringify({ approved }),
  }),
  publishEpisode: (episodeId, privacyStatus = 'PRIVATE') => request(`/episodes/${episodeId}/publish`, {
    method: 'POST', body: JSON.stringify({ privacy_status: privacyStatus }),
  }),
  uploads: () => request('/uploads'),
  runUpload: uploadId => request(`/uploads/${uploadId}/run`, { method: 'POST', body: '{}' }),
  logs: projectId => request(projectId ? `/projects/${projectId}/logs` : '/logs'),
};

export async function withFallback(operation, fallback) {
  try { return await operation(); }
  catch (error) {
    console.info('AICUT local runtime unavailable; using prototype fixtures.', error.message);
    return typeof fallback === 'function' ? fallback() : fallback;
  }
}
