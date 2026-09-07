import './styles.css';
import { PIPELINE, PROJECTS, CANDIDATES, TIMELINE, EVENTS, KNOWLEDGE, LOGS, CALIBRATION, EPISODE } from './data.js';
import { api, withFallback } from './api.js';

const state = {
  view: 'workspace', selectedCandidate: '01', selectedProject: null, filter: '전체', modal: null,
  toastTimer: null, runtimeOnline: false, projects: null, candidates: null, episodes: [], logs: null,
};
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icons = { workspace:'⌂', projects:'▣', knowledge:'◇', calibration:'◉', logs:'≡', search:'⌕', plus:'＋', check:'✓' };

function shell() {
  return `<aside class="sidebar">
    <button class="brand" data-view="workspace"><span class="logo">A<i>/</i></span><span><b>AICUT</b><small>STUDIO</small></span></button>
    <nav class="primary-nav">
      ${navItem('workspace','워크스페이스')}${navItem('projects','프로젝트','3')}${navItem('knowledge','콘텐츠 지식')}
    </nav>
    <p class="nav-label">SYSTEM</p><nav>${navItem('calibration','캘리브레이션')}${navItem('logs','처리 로그')}</nav>
    <section class="system-card"><div><span></span><b id="runtime-status">런타임 확인 중</b></div><small id="runtime-detail">Local API · Queue —</small></section>
    <div class="profile"><span class="avatar">JS</span><span><b>JUNE STUDIO</b><small>Creator plan</small></span><button>⌄</button></div>
  </aside><main><header><div class="breadcrumb"><span>AICUT Studio</span><i>/</i><b id="page-title">워크스페이스</b></div>
    <div class="header-actions"><button class="search-button">${icons.search}<span>검색</span><kbd>⌘ K</kbd></button><button class="icon-button">♢<span class="notification"></span></button><button class="accent-button" data-action="new-project">${icons.plus} 새 프로젝트</button></div>
  </header><div id="page"></div></main><div id="modal-root"></div><div id="toast"></div>`;
}

function navItem(key, label, count='') { return `<button data-view="${key}" class="nav-item ${state.view===key?'active':''}"><i>${icons[key]}</i><span>${label}</span>${count?`<em>${count}</em>`:''}</button>`; }

function pageHeader(eyebrow, title, copy, actions='') {
  return `<section class="page-heading"><div><span class="eyebrow">${eyebrow}</span><h1>${title}</h1><p>${copy}</p></div>${actions}</section>`;
}

const projects = () => state.projects || PROJECTS;
const candidates = () => state.candidates || CANDIDATES;

function formatDuration(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return [Math.floor(value / 3600), Math.floor(value % 3600 / 60), value % 60]
    .map(part => String(part).padStart(2, '0')).join(':');
}

function normalizeProject(project) {
  const media = JSON.parse(project.media_info_json || '{}');
  return {
    id: project.project_id, name: project.name, file: project.file_path.split(/[\\/]/).pop(),
    duration: formatDuration(project.duration_sec),
    size: media.size_bytes ? `${(media.size_bytes / 1073741824).toFixed(1)} GB` : '크기 확인 전',
    tracks: media.audio_tracks || 0, status: project.status, progress: project.progress,
    candidates: project.candidate_count || 0, episodes: project.episode_count || 0,
    updated: new Date(project.updated_at).toLocaleString('ko-KR'), raw: project,
  };
}

function normalizeCandidate(candidate, index) {
  const decisions = { MAKE: '제작', COMBINE: '결합 검토', HOLD: '보류', REJECT: '제작 안함' };
  const colors = { MAKE: '#d6ff4b', COMBINE: '#ffb44b', HOLD: '#a28bff', REJECT: '#6b7584' };
  return {
    id: candidate.candidate_id, displayId: String(index + 1).padStart(2, '0'),
    title: candidate.core_summary, summary: candidate.required_context || candidate.decision_reason,
    score: Math.round(candidate.independence_score * 100), decision: decisions[candidate.decision] || candidate.decision,
    color: colors[candidate.decision] || '#7c8797', tags: [`사건 ${candidate.related_event_ids?.length || 0}개 연결`],
    scenes: candidate.related_event_ids?.length || 0, people: ['분석 결과 참조'],
    context: candidate.required_context || '추가 맥락 없음', reason: candidate.decision_reason,
    time: '사건 언급 구간', raw: candidate,
  };
}

function projectCard(project) {
  const label = PIPELINE.find(x=>x.key===project.status)?.label || (project.status==='NO_CONTENT'?'콘텐츠 없음':project.status);
  return `<article class="project-card" data-project="${project.id}"><div class="project-thumb"><i>▶</i><span>${project.duration}</span></div><div class="project-body"><div class="project-title"><h3>${project.name}</h3><span class="status ${project.status.toLowerCase()}">${label}</span></div><p>${project.file} · ${project.size} · 오디오 ${project.tracks}트랙</p><div class="project-numbers"><span><b>${project.candidates}</b> 후보</span><span><b>${project.episodes}</b> 에피소드</span><small>${project.updated}</small></div><div class="mini-progress"><i style="width:${project.progress}%"></i></div></div></article>`;
}

function workspaceView() {
  return `<div class="page">${pageHeader('AUTONOMOUS CONTENT PRODUCER','좋은 방송을, 완성된 콘텐츠로.','긴 생방송에서 사건을 이해하고 독립적인 YouTube 콘텐츠를 발견합니다.',`<button class="accent-button large" data-action="new-project">${icons.plus} 방송 가져오기</button>`)}
    <section class="hero-grid"><article class="active-project"><div class="active-top"><div><span class="pulse"></span><b>현재 분석 중</b></div><button data-action="open-review">상세 보기 →</button></div><h2>2026-08-19 생방송</h2><p>가치 평가를 마치고 후보별 편집 구조와 장면을 계획하고 있습니다.</p><div class="active-progress"><i></i></div><div class="active-meta"><span><b>68%</b> 전체 진행률</span><span><b>04</b> 발견 후보</span><span><b>37</b> 정밀 분석 구간</span><span><b>01:18:22</b> 처리 시간</span></div></article>
      <article class="quick-upload" data-action="new-project"><span>＋</span><h3>새 방송 분석</h3><p>MP4 또는 MKV 파일을 놓으세요</p><small>최대 10시간 · 멀티트랙 지원</small></article></section>
    <div class="section-heading"><div><h2>최근 프로젝트</h2><p>진행 중이거나 최근 완료된 방송입니다</p></div><button data-view="projects">전체 보기 →</button></div>
    <section class="project-grid">${projects().map(projectCard).join('')}</section>
    <section class="dashboard-grid"><article class="panel learning"><div class="panel-title"><div><span class="eyebrow">LEARNING LOOP</span><h3>제작 지식이 계속 개선되고 있어요</h3></div><span class="spark">✦</span></div><div class="loop-row"><div><b>146</b><small>분석 레퍼런스</small></div><i>→</i><div><b>23</b><small>원본↔완성본</small></div><i>→</i><div><b>18</b><small>성과 학습 영상</small></div></div></article>
    <article class="panel metric-summary"><div class="panel-title"><div><span class="eyebrow">CHANNEL QUALITY</span><h3>최근 캘리브레이션</h3></div><button data-view="calibration">열기</button></div><strong>87.3</strong><span>/ 100</span><div class="quality-bar"><i></i></div><small>지난 측정 대비 +3.8</small></article></section>
  </div>`;
}

function projectsView() {
  return `<div class="page">${pageHeader('PROJECT LIBRARY','프로젝트','모든 방송 분석 작업과 산출물을 관리합니다.',`<button class="accent-button" data-action="new-project">${icons.plus} 새 프로젝트</button>`)}
    <div class="toolbar"><div class="search-field">${icons.search}<input placeholder="프로젝트 검색" /></div><div class="segmented"><button class="active">전체 3</button><button>진행 중 1</button><button>검수 대기 1</button><button>완료 1</button></div><button class="sort">최근 수정순 ⇅</button></div>
    <section class="project-table"><div class="table-head"><span>프로젝트</span><span>상태</span><span>분석 결과</span><span>수정일</span><span></span></div>${projects().map(p=>`<article data-project="${p.id}"><div class="table-project"><span class="tiny-thumb">▶</span><span><b>${p.name}</b><small>${p.file} · ${p.duration}</small></span></div><span class="status ${p.status.toLowerCase()}">${PIPELINE.find(x=>x.key===p.status)?.label || '콘텐츠 없음'}</span><span class="result"><b>${p.candidates}</b> 후보 · <b>${p.episodes}</b> 에피소드</span><span>${p.updated}</span><button>•••</button></article>`).join('')}</section>
  </div>`;
}

function pipeline() {
  return `<section class="pipeline panel"><div class="pipeline-row">${PIPELINE.map((s,i)=>`<div class="stage ${i<4?'done':i===4?'current':''}"><i>${i<4?'✓':i+1}</i><span>${s.label}</span></div>${i<PIPELINE.length-1?'<hr>':''}`).join('')}</div><div class="progress-copy"><span><b>가치 평가 완료</b> · 4개의 후보를 찾았습니다</span><span>전체 진행률 <b>68%</b></span></div><div class="progress"><i></i></div></section>`;
}

function candidateCard(c) {
  return `<article class="candidate ${state.selectedCandidate===c.id?'selected':''}" data-candidate="${c.id}" tabindex="0"><div class="candidate-head"><span class="candidate-no">${c.displayId || c.id}</span><span class="decision" style="--tone:${c.color}"><i></i>${c.decision}</span><span class="score">독립성 <b>${c.score}</b></span></div><h3>${c.title}</h3><p>${c.summary}</p><div class="candidate-meta"><span>◷ ${c.time}</span><span>▱ 장면 ${c.scenes}개</span>${c.tags.map(t=>`<em>${t}</em>`).join('')}</div></article>`;
}

function candidateDetail(c) {
  return `<div class="detail-top"><div><span class="eyebrow">SELECTED CANDIDATE · ${c.id}</span><h2>${c.title}</h2></div><button>•••</button></div><div class="preview"><div class="preview-glow"></div><button class="play">▶</button><div class="preview-caption">“잠깐만, 이게 왜 여기 있어?”</div><div class="timecode">${c.time.slice(0,8)}</div></div>
  <div class="section-title"><span>사건 타임라인</span><b>${c.scenes} SCENES</b></div><div class="eventline">${EVENTS.map((e,i)=>`<i style="left:${7+i*21}%;--c:${i===0?'#d6ff4b':i===4?'#a28bff':'#7c8797'}"><small>${e.type}</small></i>`).join('')}</div>
  <div class="fact-row"><span>주요 인물</span><b>${c.people.join(' · ')}</b></div><div class="fact-row"><span>필요 맥락</span><b>${c.context}</b></div><div class="insight"><span>✦</span><div><b>AI 판단 근거</b><p>${c.reason}</p></div></div><div class="detail-actions"><button class="secondary" data-action="edit-plan">편집 계획 보기</button><button class="accent-button" data-action="approve">후보 승인 <span>→</span></button></div>`;
}

function reviewView() {
  const project=projects().find(x=>x.id===state.selectedProject) || projects()[0];
  const visibleCandidates = candidates();
  const c=visibleCandidates.find(x=>x.id===state.selectedCandidate) || visibleCandidates[0];
  if (!project) return `<div class="page">${pageHeader('BROADCAST ANALYSIS','프로젝트 없음','먼저 방송 프로젝트를 등록하세요.')}</div>`;
  if (!c) return `<div class="page">${pageHeader('BROADCAST ANALYSIS','콘텐츠 후보 검토','아직 발견된 콘텐츠 후보가 없습니다. 분석 완료 후 다시 확인하세요.')}${pipeline()}</div>`;
  return `<div class="page review-page">${pageHeader('BROADCAST ANALYSIS','콘텐츠 후보 검토','AI가 방송 전체에서 발견한 사건을 검토하고 제작 여부를 결정하세요.',`<div class="source"><span>▶</span><div><b>${project.file}</b><small>${project.duration} · ${project.size} · ${project.tracks} audio tracks</small></div><button>•••</button></div>`)}${pipeline()}
    <section class="workspace"><div class="list-pane panel"><div class="list-header"><div><h2>발견된 후보 <span>${visibleCandidates.length}</span></h2><p>화면이 아니라 사건 단위로 분류했습니다</p></div><div class="filters">${['전체','제작','결합 검토','보류','제작 안함'].map(x=>`<button class="${state.filter===x?'active':''}" data-filter="${x}">${x}</button>`).join('')}</div></div><div id="candidate-list">${visibleCandidates.filter(c=>state.filter==='전체'||c.decision===state.filter).map(candidateCard).join('')}</div></div><aside class="candidate-detail panel">${candidateDetail(c)}</aside></section>
  </div>`;
}

function knowledgeView() {
  return `<div class="page">${pageHeader('YOUTUBE CONTENT INTELLIGENCE','콘텐츠 지식','레퍼런스에서 추출한 제작 패턴입니다. 고정 규칙이 아니라 새 콘텐츠의 판단 근거로 사용됩니다.',`<button class="secondary-button">레퍼런스 가져오기</button>`)}
    <section class="stat-grid"><article><span>분석 영상</span><b>146</b><small>이번 달 +21</small></article><article><span>유효 제작 패턴</span><b>38</b><small>신뢰도 75% 이상</small></article><article><span>최근 갱신</span><b>2h</b><small>4개 패턴 업데이트</small></article><article><span>미디어 보관</span><b>0</b><small>분석 후 자동 폐기</small></article></section>
    <div class="toolbar"><div class="search-field">${icons.search}<input placeholder="패턴 검색" /></div><div class="segmented"><button class="active">전체</button><button>스토리텔링</button><button>편집</button><button>자막</button><button>패키징</button></div></div>
    <section class="knowledge-grid">${KNOWLEDGE.map(k=>`<article class="knowledge-card"><div><span class="knowledge-type">${k.type}</span><span class="trend">${k.trend}</span></div><h3>${k.title}</h3><p>${k.description}</p><div class="confidence"><span>신뢰도</span><b>${k.confidence}%</b><i><em style="width:${k.confidence}%"></em></i></div><footer><span>레퍼런스 ${k.references}개</span><button>근거 보기 →</button></footer></article>`).join('')}</section>
  </div>`;
}

function calibrationView() {
  return `<div class="page">${pageHeader('CHANNEL CALIBRATION','캘리브레이션','근거 없는 고정 임계값 대신 실제 원본과 사람 편집본으로 채널별 판단 기준을 측정합니다.',`<button class="accent-button" data-action="calibrate">새로 측정</button>`)}
  <section class="calibration-summary panel"><div><span class="eyebrow">ACTIVE PROFILE</span><h2>${CALIBRATION.name}</h2><p>원본↔완성본 ${CALIBRATION.samples}쌍 · 마지막 측정 ${CALIBRATION.measured}</p></div><div class="overall-score"><b>87.3</b><span>/ 100</span><small>운영 가능</small></div></section>
  <section class="calibration-grid"><article class="panel"><div class="panel-title"><div><h3>평가 지표</h3><p>사람 편집본과 자동 판단의 일치도</p></div></div><div class="metric-list">${CALIBRATION.metrics.map(m=>`<div><span>${m.label}</span><b>${m.value}%</b><i><em style="width:${m.value}%"></em></i></div>`).join('')}</div></article><article class="panel"><div class="panel-title"><div><h3>측정 파라미터</h3><p>코드가 아닌 채널 프로파일에 저장됩니다</p></div><button>JSON 내보내기</button></div><div class="param-list">${CALIBRATION.params.map(p=>`<div><span>${p[0]}</span><b>${p[1]}</b><em>${p[2]}</em></div>`).join('')}</div></article></section>
  <section class="calibration-note"><span>i</span><div><b>환경이 달라졌나요?</b><p>마이크, 게임 또는 합방 구성이 변경되었다면 다시 측정하세요. 임시값은 운영 프로파일로 승격되지 않습니다.</p></div><button data-action="calibrate">데이터셋 추가</button></section></div>`;
}

function logsView() {
  return `<div class="page">${pageHeader('PROCESS OBSERVABILITY','처리 로그','멀티모달 분석과 콘텐츠 제작 파이프라인의 실행 기록입니다.',`<div class="live-indicator"><i></i>LIVE</div>`)}<div class="toolbar"><div class="search-field wide">${icons.search}<input placeholder="메시지 또는 상태 검색" /></div><div class="segmented"><button class="active">전체</button><button>정보</button><button>경고</button><button>오류</button></div><button class="secondary-button">로그 내보내기</button></div><section class="log-console"><header><span>TIME</span><span>STAGE</span><span>MESSAGE</span></header>${LOGS.map((l,i)=>`<div><time>${l[0]}</time><b>${l[1]}</b><p>${l[2]}</p><span class="log-level ${i===2?'warn':''}">${i===2?'WARN':'INFO'}</span></div>`).join('')}<footer><i></i> 새 이벤트를 기다리는 중...</footer></section></div>`;
}

function editPlanModal() {
  return `<div class="modal-backdrop"><section class="modal editor-modal"><header><div><span class="eyebrow">EDIT PLAN · EPISODE 01</span><h2>비선형 편집 계획</h2><p>원본 시점과 관계없이 완성본 순서대로 구성됩니다.</p></div><button class="close" data-close>×</button></header><div class="editor-layout"><aside><h3>사건 구조</h3>${EVENTS.map(e=>`<div class="event-item"><time>${e.time}</time><span><b>${e.type}</b><small>${e.text}</small></span></div>`).join('')}</aside><div class="timeline-editor"><div class="timeline-head"><span>순서</span><span>원본 구간</span><span>역할 / 대사</span><span>호흡</span><span>연출</span></div>${TIMELINE.map(t=>`<article draggable="true"><i>⠿</i><b>${String(t.order).padStart(2,'0')}</b><time>${t.source}<small>${t.end}</small></time><span><b>${t.role}</b><small>${t.text}</small></span><em class="pacing ${t.pacing.toLowerCase()}">${t.pacing}</em><span class="effect">${t.effect}</span><button>•••</button></article>`).join('')}</div></div><footer><div><span>예상 길이</span><b>08:42</b><span>사용 장면</span><b>6 / 12</b></div><button class="secondary-button" data-action="export-json">JSON 내보내기</button><button class="accent-button" data-action="package">렌더링 시뮬레이션 →</button></footer></section></div>`;
}

function packageModal() {
  return `<div class="modal-backdrop"><section class="modal package-modal"><header><div><span class="eyebrow">REVIEW GATE</span><h2>에피소드 검수 및 퍼블리싱</h2><p>사람이 승인하기 전에는 공개되지 않습니다.</p></div><button class="close" data-close>×</button></header><div class="package-grid"><div><div class="video-output"><span>RENDER COMPLETE</span><button>▶</button><strong>08:42</strong></div><div class="render-spec"><span>H.264 · ${EPISODE.resolution}</span><span>AAC · ${EPISODE.loudness}</span><span>2-pass loudnorm</span></div></div><div class="metadata-form"><label>제목 후보</label>${EPISODE.titleOptions.map((t,i)=>`<button class="title-option ${i===0?'selected':''}"><i>${i===0?'✓':''}</i>${t}<small>${t.length}/100</small></button>`).join('')}<label>설명 및 챕터</label><textarea>${EPISODE.description}</textarea><label>태그</label><div class="tag-list">${EPISODE.tags.map(t=>`<span>#${t}</span>`).join('')}</div></div></div><footer><button class="secondary-button">수정 요청</button><div class="privacy"><span>업로드 공개 범위</span><select><option>비공개</option><option>일부공개</option></select></div><button class="accent-button" data-action="publish">검수 승인 및 업로드 →</button></footer></section></div>`;
}

function newProjectModal() {
  return `<div class="modal-backdrop"><section class="modal new-project-modal"><header><div><span class="eyebrow">NEW BROADCAST</span><h2>새 방송 분석</h2><p>원본은 로컬에서 처리되며 편집 계획과 분석 결과가 보존됩니다.</p></div><button class="close" data-close>×</button></header><div class="dropzone"><span>＋</span><h3>생방송 파일을 선택하거나 놓으세요</h3><p>MP4, MKV · 최대 10시간 · 멀티 오디오 트랙 지원</p><button class="secondary-button">파일 선택</button><input type="file" accept="video/mp4,video/x-matroska"></div><div class="form-grid"><label>목표 길이 힌트<small>강제값이 아닙니다</small><select><option>AI가 결정</option><option>10분 내외</option><option>20분 내외</option><option>Shorts</option></select></label><label>채널 프로파일<select><option>JUNE Studio</option></select></label><label>캘리브레이션<select><option>게임/합방 · 2026-08-17</option></select></label><label class="toggle-label">완료 후 알림<span class="toggle on"><i></i></span></label></div><footer><button class="secondary-button" data-close>취소</button><button class="accent-button" data-action="start-analysis">전체 방송 분석 시작 →</button></footer></section></div>`;
}

function showModal(type) { state.modal=type; $('#modal-root').innerHTML=type==='edit'?editPlanModal():type==='package'?packageModal():newProjectModal(); bindCommon(); }
function toast(message) { const el=$('#toast'); el.textContent=message; el.classList.add('show'); clearTimeout(state.toastTimer); state.toastTimer=setTimeout(()=>el.classList.remove('show'),2800); }

async function openProject(projectId) {
  state.selectedProject = projectId;
  const [candidateRows, episodeRows, logRows] = await Promise.all([
    withFallback(() => api.candidates(projectId), []), withFallback(() => api.episodes(projectId), []),
    withFallback(() => api.logs(projectId), []),
  ]);
  state.candidates = candidateRows.map(normalizeCandidate);
  state.episodes = episodeRows;
  state.logs = logRows;
  state.selectedCandidate = state.candidates[0]?.id || null;
  setView('review');
}

const currentEpisodeId = () => state.episodes[0]?.episode_id || 'episode-operation';

function setView(view) {
  state.view=view; state.modal=null; $('#modal-root').innerHTML='';
  const pages={workspace:workspaceView,projects:projectsView,review:reviewView,knowledge:knowledgeView,calibration:calibrationView,logs:logsView};
  $('#page').innerHTML=(pages[view]||workspaceView)(); $('#page-title').textContent={workspace:'워크스페이스',projects:'프로젝트',review:'콘텐츠 후보 검토',knowledge:'콘텐츠 지식',calibration:'캘리브레이션',logs:'처리 로그'}[view];
  $$('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.view===view)); bindCommon(); window.scrollTo(0,0);
}

function bindCommon() {
  $$('[data-view]').forEach(el=>el.onclick=()=>setView(el.dataset.view));
  $$('[data-project]').forEach(el=>el.onclick=()=>state.runtimeOnline?openProject(el.dataset.project):setView('review'));
  $$('[data-action="new-project"]').forEach(el=>el.onclick=()=>showModal('new'));
  $$('[data-action="open-review"]').forEach(el=>el.onclick=()=>setView('review'));
  $$('[data-candidate]').forEach(el=>{ const select=()=>{state.selectedCandidate=el.dataset.candidate;setView('review')}; el.onclick=select;el.onkeydown=e=>e.key==='Enter'&&select(); });
  $$('[data-filter]').forEach(el=>el.onclick=()=>{state.filter=el.dataset.filter;setView('review')});
  $$('[data-close]').forEach(el=>el.onclick=()=>{state.modal=null;$('#modal-root').innerHTML=''});
  $$('[data-action="edit-plan"]').forEach(el=>el.onclick=()=>showModal('edit'));
  $$('[data-action="approve"]').forEach(el=>el.onclick=async()=>{await withFallback(()=>api.reviewCandidate(state.selectedCandidate,'MAKE'),null);toast('후보를 승인하고 편집 기획 큐에 추가했습니다.');showModal('edit')});
  $$('[data-action="package"]').forEach(el=>el.onclick=async()=>{const episodeId=currentEpisodeId();await withFallback(async()=>{await api.renderEpisode(episodeId,{execute:false});return api.packageEpisode(episodeId,{execute:false,metadata:{title_options:EPISODE.titleOptions,description:EPISODE.description,tags:EPISODE.tags,chapters:[]},thumbnail_timestamps:[12,94,252]})},null);showModal('package')});
  $$('[data-action="export-json"]').forEach(el=>el.onclick=()=>toast('편집 계획 JSON을 내보냈습니다.'));
  $$('[data-action="publish"]').forEach(el=>el.onclick=async()=>{const episodeId=currentEpisodeId();await withFallback(async()=>{await api.reviewEpisode(episodeId,true);return api.publishEpisode(episodeId,'PRIVATE')},null);state.modal=null;$('#modal-root').innerHTML='';toast('검수 승인 완료 · 비공개 업로드 큐에 등록했습니다.')});
  $$('[data-action="start-analysis"]').forEach(el=>el.onclick=async()=>{const input=$('.dropzone input');const file=input?.files?.[0];const project=await withFallback(()=>api.createProject({file_path:file?.path||file?.name||'selected_broadcast.mkv',name:file?.name?.replace(/\.[^.]+$/,'')||'새 생방송',target_duration_hint:'AI',channel_ref:'JUNE Studio'}),null);if(project)await withFallback(()=>api.runProject(project.project_id),null);state.modal=null;$('#modal-root').innerHTML='';toast('프로젝트를 만들고 미디어 파싱을 시작했습니다.')});
  $$('[data-action="calibrate"]').forEach(el=>el.onclick=()=>toast('원본↔완성본 데이터셋 선택 창을 준비했습니다.'));
  const drop=$('.dropzone'); if(drop){const input=$('input',drop);drop.onclick=e=>{if(e.target.tagName!=='INPUT')input.click()};input.onchange=()=>{if(input.files[0]){$('h3',drop).textContent=input.files[0].name;$('p',drop).textContent=`${(input.files[0].size/1073741824).toFixed(2)} GB · 분석 준비 완료`;drop.classList.add('ready')}};}
}

document.querySelector('#app').innerHTML=shell();
setView('workspace');

withFallback(() => api.health(), null).then(async health => {
  state.runtimeOnline = Boolean(health);
  const status = $('#runtime-status'), detail = $('#runtime-detail');
  if (status) status.textContent = health ? '로컬 런타임 온라인' : '데모 데이터 모드';
  if (detail) detail.textContent = health ? 'SQLite API · Ready' : 'API 미실행 · Fixtures';
  if (health) {
    const rows = await withFallback(() => api.projects(), []);
    state.projects = rows.map(normalizeProject);
    state.selectedProject = state.projects[0]?.id || null;
    setView(state.view);
  }
});

document.addEventListener('keydown', e=>{if(e.key==='Escape'&&state.modal){state.modal=null;$('#modal-root').innerHTML=''}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();toast('프로젝트·후보·사건 통합 검색')}});
