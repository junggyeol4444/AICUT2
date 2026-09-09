import './styles.css';
import { PIPELINE, PROJECTS, CANDIDATES, TIMELINE, EVENTS, KNOWLEDGE, LOGS, CALIBRATION, EPISODE } from './data.js';
import { api, withFallback } from './api.js';

const state = {
  view: 'workspace', selectedCandidate: '01', selectedProject: null, filter: '전체', modal: null,
  toastTimer: null, runtimeOnline: false, projects: null, candidates: null, episodes: [], logs: null,
  job: null, timeline: null, busy: false,
  references: null, knowledge: null,
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
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
})[character]);

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
    candidates: project.candidate_count ?? project.candidates ?? 0,
    episodes: project.episode_count ?? project.episodes ?? 0,
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
  return `<article class="project-card" data-project="${escapeHtml(project.id)}"><div class="project-thumb"><i>▶</i><span>${project.duration}</span></div><div class="project-body"><div class="project-title"><h3>${escapeHtml(project.name)}</h3><span class="status ${project.status.toLowerCase()}">${escapeHtml(label)}</span></div><p>${escapeHtml(project.file)} · ${project.size} · 오디오 ${project.tracks}트랙</p><div class="project-numbers"><span><b>${project.candidates}</b> 후보</span><span><b>${project.episodes}</b> 에피소드</span><small>${escapeHtml(project.updated)}</small></div><div class="mini-progress"><i style="width:${project.progress}%"></i></div></div></article>`;
}

function workspaceView() {
  const active = projects().find(project=>!['NO_CONTENT','PUBLISHED','REVIEW_PENDING'].includes(project.status));
  const activeSummary = active ? `<article class="active-project"><div class="active-top"><div><span class="pulse"></span><b>현재 처리 중</b></div><button data-project="${escapeHtml(active.id)}">상세 보기 →</button></div><h2>${escapeHtml(active.name)}</h2><p>${escapeHtml(PIPELINE.find(stage=>stage.key===active.status)?.description||active.status)}</p><div class="active-progress"><i style="width:${active.progress}%"></i></div><div class="active-meta"><span><b>${active.progress}%</b> 전체 진행률</span><span><b>${active.candidates}</b> 발견 후보</span><span><b>${active.episodes}</b> 에피소드</span></div></article>` : `<article class="active-project"><div class="active-top"><div><b>대기 중</b></div></div><h2>실행 중인 분석이 없습니다</h2><p>새 방송을 등록하면 실제 처리 상태가 여기에 표시됩니다.</p></article>`;
  return `<div class="page">${pageHeader('AUTONOMOUS CONTENT PRODUCER','좋은 방송을, 완성된 콘텐츠로.','긴 생방송에서 사건을 이해하고 독립적인 YouTube 콘텐츠를 발견합니다.',`<button class="accent-button large" data-action="new-project">${icons.plus} 방송 가져오기</button>`)}
    <section class="hero-grid">${activeSummary}
      <article class="quick-upload" data-action="new-project"><span>＋</span><h3>새 방송 분석</h3><p>로컬 MP4 또는 MKV 경로를 입력하세요</p><small>최대 10시간 · 멀티트랙 지원</small></article></section>
    <div class="section-heading"><div><h2>최근 프로젝트</h2><p>진행 중이거나 최근 완료된 방송입니다</p></div><button data-view="projects">전체 보기 →</button></div>
    <section class="project-grid">${projects().map(projectCard).join('')}</section>
    <section class="dashboard-grid"><article class="panel learning"><div class="panel-title"><div><span class="eyebrow">LEARNING LOOP</span><h3>제작 지식이 계속 개선되고 있어요</h3></div><span class="spark">✦</span></div><div class="loop-row"><div><b>${state.references?.length??146}</b><small>분석 레퍼런스</small></div><i>→</i><div><b>${state.knowledge?.length??23}</b><small>제작 패턴</small></div><i>→</i><div><b>${projects().reduce((sum,item)=>sum+item.episodes,0)}</b><small>성과 학습 대상</small></div></div></article>
    <article class="panel metric-summary"><div class="panel-title"><div><span class="eyebrow">CHANNEL QUALITY</span><h3>최근 캘리브레이션</h3></div><button data-view="calibration">열기</button></div><strong>87.3</strong><span>/ 100</span><div class="quality-bar"><i></i></div><small>지난 측정 대비 +3.8</small></article></section>
  </div>`;
}

function projectsView() {
  return `<div class="page">${pageHeader('PROJECT LIBRARY','프로젝트','모든 방송 분석 작업과 산출물을 관리합니다.',`<button class="accent-button" data-action="new-project">${icons.plus} 새 프로젝트</button>`)}
    <div class="toolbar"><div class="search-field">${icons.search}<input placeholder="프로젝트 검색" /></div><div class="segmented"><button class="active">전체 3</button><button>진행 중 1</button><button>검수 대기 1</button><button>완료 1</button></div><button class="sort">최근 수정순 ⇅</button></div>
    <section class="project-table"><div class="table-head"><span>프로젝트</span><span>상태</span><span>분석 결과</span><span>수정일</span><span></span></div>${projects().map(p=>`<article data-project="${p.id}"><div class="table-project"><span class="tiny-thumb">▶</span><span><b>${p.name}</b><small>${p.file} · ${p.duration}</small></span></div><span class="status ${p.status.toLowerCase()}">${PIPELINE.find(x=>x.key===p.status)?.label || '콘텐츠 없음'}</span><span class="result"><b>${p.candidates}</b> 후보 · <b>${p.episodes}</b> 에피소드</span><span>${p.updated}</span><button>•••</button></article>`).join('')}</section>
  </div>`;
}

function pipeline(project) {
  const index = PIPELINE.findIndex(stage => stage.key === project.status);
  const terminal = ['NO_CONTENT', 'PUBLISHED'].includes(project.status);
  const current = index >= 0 ? index : terminal ? PIPELINE.length : -1;
  const message = state.job?.steps?.at(-1)?.error_message
    || project.raw?.error_message
    || state.job?.steps?.at(-1)?.step
    || (project.status === 'NO_CONTENT' ? '제작 가치가 있는 콘텐츠가 없어 정상 종료되었습니다.' : `현재 상태 · ${project.status}`);
  return `<section class="pipeline panel"><div class="pipeline-row">${PIPELINE.map((s,i)=>`<div class="stage ${i<current||terminal?'done':i===current?'current':''}"><i>${i<current||terminal?'✓':i+1}</i><span>${s.label}</span></div>${i<PIPELINE.length-1?'<hr>':''}`).join('')}</div><div class="progress-copy"><span><b>${escapeHtml(message)}</b></span><span>전체 진행률 <b>${project.progress}%</b></span></div><div class="progress"><i style="width:${project.progress}%"></i></div>${state.job?.running?'<button class="secondary-button cancel-job" data-action="cancel-analysis">분석 취소</button>':''}</section>`;
}

function candidateCard(c) {
  return `<article class="candidate ${state.selectedCandidate===c.id?'selected':''}" data-candidate="${escapeHtml(c.id)}" tabindex="0"><div class="candidate-head"><span class="candidate-no">${escapeHtml(c.displayId || c.id)}</span><span class="decision" style="--tone:${c.color}"><i></i>${escapeHtml(c.decision)}</span><span class="score">독립성 <b>${c.score}</b></span></div><h3>${escapeHtml(c.title)}</h3><p>${escapeHtml(c.summary)}</p><div class="candidate-meta"><span>◷ ${escapeHtml(c.time)}</span><span>▱ 장면 ${c.scenes}개</span>${c.tags.map(t=>`<em>${escapeHtml(t)}</em>`).join('')}</div></article>`;
}

function candidateDetail(c) {
  return `<div class="detail-top"><div><span class="eyebrow">SELECTED CANDIDATE · ${escapeHtml(c.id)}</span><h2>${escapeHtml(c.title)}</h2></div><button>•••</button></div><div class="preview"><div class="preview-glow"></div><button class="play">▶</button><div class="preview-caption">분석된 원본 장면 프리뷰</div><div class="timecode">${escapeHtml(c.time.slice(0,8))}</div></div>
  <div class="section-title"><span>사건 타임라인</span><b>${c.scenes} SCENES</b></div><div class="eventline">${EVENTS.map((e,i)=>`<i style="left:${7+i*21}%;--c:${i===0?'#d6ff4b':i===4?'#a28bff':'#7c8797'}"><small>${e.type}</small></i>`).join('')}</div>
  <div class="fact-row"><span>주요 인물</span><b>${escapeHtml(c.people.join(' · '))}</b></div><div class="fact-row"><span>필요 맥락</span><b>${escapeHtml(c.context)}</b></div><div class="insight"><span>✦</span><div><b>AI 판단 근거</b><p>${escapeHtml(c.reason)}</p></div></div><div class="detail-actions"><button class="secondary" data-action="edit-plan">편집 계획 보기</button><button class="accent-button" data-action="approve">후보 승인 <span>→</span></button></div>`;
}

function reviewView() {
  const project=projects().find(x=>x.id===state.selectedProject) || projects()[0];
  const visibleCandidates = candidates();
  const c=visibleCandidates.find(x=>x.id===state.selectedCandidate) || visibleCandidates[0];
  if (!project) return `<div class="page">${pageHeader('BROADCAST ANALYSIS','프로젝트 없음','먼저 방송 프로젝트를 등록하세요.')}</div>`;
  if (!c) return `<div class="page">${pageHeader('BROADCAST ANALYSIS','콘텐츠 후보 검토','아직 발견된 콘텐츠 후보가 없습니다. 분석 완료 후 다시 확인하세요.')}${pipeline(project)}</div>`;
  return `<div class="page review-page">${pageHeader('BROADCAST ANALYSIS','콘텐츠 후보 검토','AI가 방송 전체에서 발견한 사건을 검토하고 제작 여부를 결정하세요.',`<div class="source"><span>▶</span><div><b>${escapeHtml(project.file)}</b><small>${project.duration} · ${project.size} · ${project.tracks} audio tracks</small></div><button>•••</button></div>`)}${pipeline(project)}
    <section class="workspace"><div class="list-pane panel"><div class="list-header"><div><h2>발견된 후보 <span>${visibleCandidates.length}</span></h2><p>화면이 아니라 사건 단위로 분류했습니다</p></div><div class="filters">${['전체','제작','결합 검토','보류','제작 안함'].map(x=>`<button class="${state.filter===x?'active':''}" data-filter="${x}">${x}</button>`).join('')}</div></div><div id="candidate-list">${visibleCandidates.filter(c=>state.filter==='전체'||c.decision===state.filter).map(candidateCard).join('')}</div></div><aside class="candidate-detail panel">${candidateDetail(c)}</aside></section>
  </div>`;
}

function knowledgeView() {
  const patterns = state.knowledge === null ? KNOWLEDGE : state.knowledge.map(pattern=>({
    title:pattern.title, type:pattern.kind, confidence:Math.round(pattern.confidence*100),
    references:Math.max(1,pattern.evidence?.length||0), trend:'실측', description:pattern.description,
  }));
  return `<div class="page">${pageHeader('YOUTUBE CONTENT INTELLIGENCE','콘텐츠 지식','레퍼런스에서 추출한 제작 패턴입니다. 고정 규칙이 아니라 새 콘텐츠의 판단 근거로 사용됩니다.',`<button class="secondary-button">레퍼런스 가져오기</button>`)}
    <section class="stat-grid"><article><span>분석 영상</span><b>${state.references?.length??146}</b><small>${state.runtimeOnline?'저장된 공개 레퍼런스':'데모 데이터'}</small></article><article><span>유효 제작 패턴</span><b>${patterns.filter(pattern=>pattern.confidence>=75).length}</b><small>신뢰도 75% 이상</small></article><article><span>전체 패턴</span><b>${patterns.length}</b><small>근거와 함께 저장</small></article><article><span>미디어 보관</span><b>${state.references?.filter(item=>!item.media_discarded).length??0}</b><small>분석 후 폐기 원칙</small></article></section>
    <div class="toolbar"><div class="search-field">${icons.search}<input placeholder="패턴 검색" /></div><div class="segmented"><button class="active">전체</button><button>스토리텔링</button><button>편집</button><button>자막</button><button>패키징</button></div></div>
    <section class="knowledge-grid">${patterns.map(k=>`<article class="knowledge-card"><div><span class="knowledge-type">${escapeHtml(k.type)}</span><span class="trend">${escapeHtml(k.trend)}</span></div><h3>${escapeHtml(k.title)}</h3><p>${escapeHtml(k.description)}</p><div class="confidence"><span>신뢰도</span><b>${k.confidence}%</b><i><em style="width:${k.confidence}%"></em></i></div><footer><span>근거 ${k.references}개</span><button>근거 보기 →</button></footer></article>`).join('')||'<p class="empty-state">아직 분석된 제작 패턴이 없습니다.</p>'}</section>
  </div>`;
}

function calibrationView() {
  return `<div class="page">${pageHeader('CHANNEL CALIBRATION','캘리브레이션','근거 없는 고정 임계값 대신 실제 원본과 사람 편집본으로 채널별 판단 기준을 측정합니다.',`<button class="accent-button" data-action="calibrate">새로 측정</button>`)}
  <section class="calibration-summary panel"><div><span class="eyebrow">ACTIVE PROFILE</span><h2>${CALIBRATION.name}</h2><p>원본↔완성본 ${CALIBRATION.samples}쌍 · 마지막 측정 ${CALIBRATION.measured}</p></div><div class="overall-score"><b>87.3</b><span>/ 100</span><small>운영 가능</small></div></section>
  <section class="calibration-grid"><article class="panel"><div class="panel-title"><div><h3>평가 지표</h3><p>사람 편집본과 자동 판단의 일치도</p></div></div><div class="metric-list">${CALIBRATION.metrics.map(m=>`<div><span>${m.label}</span><b>${m.value}%</b><i><em style="width:${m.value}%"></em></i></div>`).join('')}</div></article><article class="panel"><div class="panel-title"><div><h3>측정 파라미터</h3><p>코드가 아닌 채널 프로파일에 저장됩니다</p></div><button>JSON 내보내기</button></div><div class="param-list">${CALIBRATION.params.map(p=>`<div><span>${p[0]}</span><b>${p[1]}</b><em>${p[2]}</em></div>`).join('')}</div></article></section>
  <section class="calibration-note"><span>i</span><div><b>환경이 달라졌나요?</b><p>마이크, 게임 또는 합방 구성이 변경되었다면 다시 측정하세요. 임시값은 운영 프로파일로 승격되지 않습니다.</p></div><button data-action="calibrate">데이터셋 추가</button></section></div>`;
}

function logsView() {
  const rows = state.logs === null ? LOGS.map(([time,stage,message])=>({time,stage,message,level:'INFO'})) : state.logs.map(log=>({time:new Date(log.created_at).toLocaleTimeString('ko-KR'),stage:log.stage,message:log.message,level:log.level||'INFO'}));
  return `<div class="page">${pageHeader('PROCESS OBSERVABILITY','처리 로그','멀티모달 분석과 콘텐츠 제작 파이프라인의 실행 기록입니다.',`<div class="live-indicator"><i></i>${state.runtimeOnline?'LIVE':'DEMO'}</div>`)}<div class="toolbar"><div class="search-field wide">${icons.search}<input placeholder="메시지 또는 상태 검색" /></div><div class="segmented"><button class="active">전체</button><button>정보</button><button>경고</button><button>오류</button></div><button class="secondary-button">로그 내보내기</button></div><section class="log-console"><header><span>TIME</span><span>STAGE</span><span>MESSAGE</span></header>${rows.map(log=>`<div><time>${escapeHtml(log.time)}</time><b>${escapeHtml(log.stage)}</b><p>${escapeHtml(log.message)}</p><span class="log-level ${log.level==='ERROR'?'error':log.level==='WARN'?'warn':''}">${escapeHtml(log.level)}</span></div>`).join('')}${rows.length?'':'<div class="empty-log">아직 기록된 작업 로그가 없습니다.</div>'}<footer><i></i> ${state.runtimeOnline?'새 이벤트를 기다리는 중...':'데모 로그를 표시하고 있습니다.'}</footer></section></div>`;
}

function editPlanModal() {
  const source = state.runtimeOnline ? (state.timeline || []) : TIMELINE;
  const timeline = source.map((cut,index)=>state.runtimeOnline?{
    order:cut.sequence_order, source:formatDuration(cut.source_start_sec), end:formatDuration(cut.source_end_sec),
    role:cut.scene_role, pacing:cut.pacing_mode, text:cut.pacing_reason||cut.speaker_tag,
    effect:cut.visual_effect?.type||'none', duration:cut.source_end_sec-cut.source_start_sec,
  }:cut);
  const duration = timeline.reduce((total,cut)=>cut.pacing==='CUT'?total:total+(cut.duration||0),0);
  return `<div class="modal-backdrop"><section class="modal editor-modal"><header><div><span class="eyebrow">EDIT PLAN · ${escapeHtml(currentEpisodeId())}</span><h2>비선형 편집 계획</h2><p>원본 시점과 관계없이 완성본 순서대로 구성됩니다.</p></div><button class="close" data-close>×</button></header><div class="editor-layout"><aside><h3>사건 구조</h3>${EVENTS.map(e=>`<div class="event-item"><time>${e.time}</time><span><b>${e.type}</b><small>${e.text}</small></span></div>`).join('')}</aside><div class="timeline-editor"><div class="timeline-head"><span>순서</span><span>원본 구간</span><span>역할 / 대사</span><span>호흡</span><span>연출</span></div>${timeline.map(t=>`<article><i>⠿</i><b>${String(t.order).padStart(2,'0')}</b><time>${t.source}<small>${t.end}</small></time><span><b>${escapeHtml(t.role)}</b><small>${escapeHtml(t.text)}</small></span><em class="pacing ${t.pacing.toLowerCase()}">${t.pacing}</em><span class="effect">${escapeHtml(t.effect)}</span><button>•••</button></article>`).join('')||'<p class="empty-state">생성된 편집 계획이 없습니다.</p>'}</div></div><footer><div><span>예상 길이</span><b>${state.runtimeOnline?formatDuration(duration):EPISODE.duration}</b><span>사용 장면</span><b>${timeline.filter(c=>c.pacing!=='CUT').length} / ${timeline.length}</b></div><button class="secondary-button" data-action="export-json">JSON 내보내기</button><button class="accent-button" data-action="package">렌더링 시뮬레이션 →</button></footer></section></div>`;
}

function packageModal() {
  return `<div class="modal-backdrop"><section class="modal package-modal"><header><div><span class="eyebrow">REVIEW GATE</span><h2>에피소드 검수 및 퍼블리싱</h2><p>사람이 승인하기 전에는 공개되지 않습니다.</p></div><button class="close" data-close>×</button></header><div class="package-grid"><div><div class="video-output"><span>RENDER COMPLETE</span><button>▶</button><strong>08:42</strong></div><div class="render-spec"><span>H.264 · ${EPISODE.resolution}</span><span>AAC · ${EPISODE.loudness}</span><span>2-pass loudnorm</span></div></div><div class="metadata-form"><label>제목 후보</label>${EPISODE.titleOptions.map((t,i)=>`<button class="title-option ${i===0?'selected':''}"><i>${i===0?'✓':''}</i>${t}<small>${t.length}/100</small></button>`).join('')}<label>설명 및 챕터</label><textarea>${EPISODE.description}</textarea><label>태그</label><div class="tag-list">${EPISODE.tags.map(t=>`<span>#${t}</span>`).join('')}</div></div></div><footer><button class="secondary-button">수정 요청</button><div class="privacy"><span>업로드 공개 범위</span><select><option>비공개</option><option>일부공개</option></select></div><button class="accent-button" data-action="publish">검수 승인 및 업로드 →</button></footer></section></div>`;
}

function newProjectModal() {
  return `<div class="modal-backdrop"><section class="modal new-project-modal"><header><div><span class="eyebrow">NEW BROADCAST</span><h2>새 방송 분석</h2><p>원본은 로컬에서 처리되며 편집 계획과 분석 결과가 보존됩니다.</p></div><button class="close" data-close>×</button></header><div class="source-path-panel"><label>로컬 원본 절대 경로<small>API 서버가 접근할 수 있는 경로를 입력하세요</small><input class="source-path" type="text" placeholder="/media/recordings/2026-08-19-live.mkv" autocomplete="off"></label><p>일반 브라우저는 보안상 선택한 파일의 실제 경로를 전달하지 않습니다. 편집기 플러그인에서는 선택 클립의 경로가 자동으로 입력됩니다.</p></div><div class="form-grid"><label>목표 길이 힌트<small>강제값이 아닙니다</small><select><option>AI가 결정</option><option>10분 내외</option><option>20분 내외</option><option>Shorts</option></select></label><label>채널 프로파일<select><option>JUNE Studio</option></select></label><label>캘리브레이션<select><option>게임/합방 · 2026-08-17</option></select></label><label class="toggle-label">완료 후 알림<span class="toggle on"><i></i></span></label></div><div class="modal-error" role="alert"></div><footer><button class="secondary-button" data-close>취소</button><button class="accent-button" data-action="start-analysis">전체 방송 분석 시작 →</button></footer></section></div>`;
}

function showModal(type) { state.modal=type; $('#modal-root').innerHTML=type==='edit'?editPlanModal():type==='package'?packageModal():newProjectModal(); bindCommon(); }
function toast(message) { const el=$('#toast'); el.textContent=message; el.classList.add('show'); clearTimeout(state.toastTimer); state.toastTimer=setTimeout(()=>el.classList.remove('show'),2800); }
function showActionError(error) {
  const target = $('.modal-error');
  if (target) { target.textContent = error.message; target.classList.add('show'); }
  else toast(`오류 · ${error.message}`);
}

async function mutate(action, success) {
  if (state.busy) return null;
  state.busy = true;
  try {
    const result = await action();
    if (success) toast(success);
    return result;
  } catch (error) {
    showActionError(error);
    return null;
  } finally {
    state.busy = false;
  }
}

async function openProject(projectId) {
  state.selectedProject = projectId;
  const [projectRow, candidateRows, episodeRows, logRows, job] = await Promise.all([
    withFallback(() => api.project(projectId), null),
    withFallback(() => api.candidates(projectId), []), withFallback(() => api.episodes(projectId), []),
    withFallback(() => api.logs(projectId), []), withFallback(() => api.job(projectId), null),
  ]);
  if (projectRow) {
    const normalized = normalizeProject(projectRow);
    const existing = projects().find(project => project.id === projectId);
    normalized.candidates = existing?.candidates ?? normalized.candidates;
    normalized.episodes = existing?.episodes ?? normalized.episodes;
    state.projects = projects().map(project => project.id === projectId ? normalized : project);
  }
  state.candidates = candidateRows.map(normalizeCandidate);
  state.episodes = episodeRows;
  state.logs = logRows;
  state.job = job;
  state.timeline = episodeRows[0] ? await withFallback(() => api.timeline(episodeRows[0].episode_id), []) : [];
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
  $$('[data-action="approve"]').forEach(el=>el.onclick=async()=>{const result=await mutate(()=>api.reviewCandidate(state.selectedCandidate,'MAKE'),'후보를 승인하고 편집 기획 큐에 추가했습니다.');if(result)showModal('edit')});
  $$('[data-action="package"]').forEach(el=>el.onclick=async()=>{const episodeId=currentEpisodeId();const result=await mutate(async()=>{await api.renderEpisode(episodeId,{execute:false});return api.packageEpisode(episodeId,{execute:false,metadata:{title_options:EPISODE.titleOptions,description:EPISODE.description,tags:EPISODE.tags,chapters:[]},thumbnail_timestamps:[12,94,252]})});if(result)showModal('package')});
  $$('[data-action="export-json"]').forEach(el=>el.onclick=()=>toast('편집 계획 JSON을 내보냈습니다.'));
  $$('[data-action="publish"]').forEach(el=>el.onclick=async()=>{const episodeId=currentEpisodeId();const result=await mutate(async()=>{await api.reviewEpisode(episodeId,true);return api.publishEpisode(episodeId,'PRIVATE')},'검수 승인 완료 · 비공개 업로드 큐에 등록했습니다.');if(result){state.modal=null;$('#modal-root').innerHTML=''}});
  $$('[data-action="cancel-analysis"]').forEach(el=>el.onclick=async()=>{const result=await mutate(()=>api.cancelProject(state.selectedProject),'분석 취소 요청을 전달했습니다.');if(result)await openProject(state.selectedProject)});
  $$('[data-action="start-analysis"]').forEach(el=>el.onclick=async()=>{const path=$('.source-path')?.value.trim();if(!path){showActionError(new Error('API 서버가 접근할 수 있는 원본의 절대 경로를 입력하세요.'));return}const name=path.split(/[\\/]/).pop()?.replace(/\.[^.]+$/,'')||'새 생방송';const project=await mutate(()=>api.createProject({file_path:path,name,target_duration_hint:'AI',channel_ref:'JUNE Studio'}));if(!project)return;const run=await mutate(()=>api.runProject(project.project_id));if(!run)return;state.modal=null;$('#modal-root').innerHTML='';toast('프로젝트를 만들고 미디어 파싱을 시작했습니다.');const rows=await api.projects();state.projects=rows.map(normalizeProject);await openProject(project.project_id)});
  $$('[data-action="calibrate"]').forEach(el=>el.onclick=()=>toast('원본↔완성본 데이터셋 선택 창을 준비했습니다.'));
}

document.querySelector('#app').innerHTML=shell();
setView('workspace');

withFallback(() => api.health(), null).then(async health => {
  state.runtimeOnline = Boolean(health);
  const status = $('#runtime-status'), detail = $('#runtime-detail');
  if (status) status.textContent = health ? '로컬 런타임 온라인' : '데모 데이터 모드';
  if (detail) detail.textContent = health ? 'SQLite API · Ready' : 'API 미실행 · Fixtures';
  if (health) {
    const [rows,references,knowledge] = await Promise.all([
      withFallback(() => api.projects(), []), withFallback(() => api.references(), []),
      withFallback(() => api.knowledgePatterns(), []),
    ]);
    state.projects = rows.map(normalizeProject);
    state.references = references;
    state.knowledge = knowledge;
    state.selectedProject = state.projects[0]?.id || null;
    setView(state.view);
  }
});

document.addEventListener('keydown', e=>{if(e.key==='Escape'&&state.modal){state.modal=null;$('#modal-root').innerHTML=''}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();toast('프로젝트·후보·사건 통합 검색')}});
