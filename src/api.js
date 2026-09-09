const API_ROOT = '/api';

export class ApiError extends Error {
  constructor(message, status = 0, code = 'network_error') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      headers: { 'Content-Type': 'application/json', ...options.headers },
      ...options,
    });
  } catch (error) {
    throw new ApiError(`로컬 런타임에 연결할 수 없습니다: ${error.message}`);
  }
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json')
    ? await response.json()
    : { error: 'invalid_response', message: await response.text() || `HTTP ${response.status}` };
  if (!response.ok) {
    throw new ApiError(payload.message || payload.error || `HTTP ${response.status}`, response.status, payload.error);
  }
  return payload;
}

export const api = {
  health: () => request('/health'),
  projects: () => request('/projects'),
  project: projectId => request(`/projects/${projectId}`),
  createProject: payload => request('/projects', { method: 'POST', body: JSON.stringify(payload) }),
  runProject: (projectId, options = {}) => request(`/projects/${projectId}/run`, {
    method: 'POST', body: JSON.stringify(options),
  }),
  cancelProject: projectId => request(`/projects/${projectId}/cancel`, { method: 'POST', body: '{}' }),
  importAnalysis: (projectId, manifest) => request(`/projects/${projectId}/analysis`, {
    method: 'POST', body: JSON.stringify(manifest),
  }),
  job: projectId => request(`/projects/${projectId}/job`),
  candidates: projectId => request(`/projects/${projectId}/candidates`),
  episodes: projectId => request(`/projects/${projectId}/episodes`),
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
  calibrations: () => request('/calibrations'),
  references: () => request('/references'),
  knowledgePatterns: kind => request(`/knowledge/patterns${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`),
  calibrate: payload => request('/calibrations', { method: 'POST', body: JSON.stringify(payload) }),
  sourceOutputPairs: () => request('/learning/source-output'),
  analyzeSourceOutput: payload => request('/learning/source-output', {
    method: 'POST', body: JSON.stringify(payload),
  }),
  performance: episodeId => request(`/episodes/${episodeId}/performance`),
  collectPerformance: (episodeId, payload) => request(`/episodes/${episodeId}/performance`, {
    method: 'POST', body: JSON.stringify(payload),
  }),
  preprocess: (projectId, payload) => request(`/projects/${projectId}/preprocess`, {
    method: 'POST', body: JSON.stringify(payload),
  }),
  createScanPlan: (projectId, payload) => request(`/projects/${projectId}/scan-plan`, {
    method: 'POST', body: JSON.stringify(payload),
  }),
  importTranscript: (projectId, segments) => request(`/projects/${projectId}/transcript`, {
    method: 'POST', body: JSON.stringify({ segments }),
  }),
  transcribe: (projectId, payload) => request(`/projects/${projectId}/transcribe`, {
    method: 'POST', body: JSON.stringify(payload),
  }),
  logs: projectId => request(projectId ? `/projects/${projectId}/logs` : '/logs'),
};

export async function withFallback(operation, fallback) {
  try { return await operation(); }
  catch (error) {
    console.info('AICUT local runtime unavailable; using prototype fixtures.', error.message);
    return typeof fallback === 'function' ? fallback() : fallback;
  }
}
