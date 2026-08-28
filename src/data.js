export const PIPELINE = [
  { key: 'PARSING', label: '파싱', description: '멀티트랙 음성·영상 추출' },
  { key: 'UNDERSTANDING', label: '전체 이해', description: '사건·인물·관계 구조화' },
  { key: 'DISCOVERING', label: '콘텐츠 발견', description: '사건 기반 후보 탐색' },
  { key: 'EVALUATING', label: '가치 평가', description: '독립성·완결성 평가' },
  { key: 'PLANNING', label: '편집 기획', description: '장면 검색·호흡 설계' },
  { key: 'RENDERING', label: '렌더링', description: '편집 계획 실행' },
  { key: 'REVIEW_PENDING', label: '사람 검수', description: '공개 전 필수 승인' }
];

export const PROJECTS = [
  { id: 'live-0819', name: '2026-08-19 생방송', file: '2026_08_19_live.mkv', duration: '06:14:32', size: '48.2 GB', tracks: 4, status: 'PLANNING', progress: 68, candidates: 4, episodes: 2, updated: '방금 전' },
  { id: 'live-0816', name: '2026-08-16 합방', file: 'collab_0816.mkv', duration: '04:52:17', size: '37.9 GB', tracks: 3, status: 'REVIEW_PENDING', progress: 100, candidates: 6, episodes: 3, updated: '3일 전' },
  { id: 'live-0812', name: '2026-08-12 생방송', file: 'rank_game_0812.mp4', duration: '08:04:09', size: '61.3 GB', tracks: 2, status: 'NO_CONTENT', progress: 100, candidates: 0, episodes: 0, updated: '7일 전' }
];

export const CANDIDATES = [
  { id: '01', time: '00:42:18 — 01:07:34', title: '완벽한 잠입 작전이 한순간에 무너진 이유', summary: '초반의 자신감 넘치는 계획이 팀원의 우발 행동으로 틀어지고 예상 밖의 역전으로 이어지는 하나의 완결된 사건입니다.', score: 94, decision: '제작', color: '#d6ff4b', tags: ['게임 플레이', '4개 시점 연결'], scenes: 12, people: ['JUNE', 'MISO', 'NOAH'], context: '작전의 목표와 세 사람의 역할을 38초 안에 설명할 수 있습니다.', reason: '명확한 목표·갈등·반전·결과가 존재하고 서로 떨어진 네 시점의 인과관계가 확인됩니다.' },
  { id: '02', time: '02:13:09 — 03:02:41', title: '3시간 전 농담이 현실이 된 순간', summary: '방송 초반의 복선과 후반 반응이 시간적으로 떨어져 있으나 인과관계가 명확합니다. 배경 설명을 포함해 독립 구성이 가능합니다.', score: 87, decision: '제작', color: '#a28bff', tags: ['토크 + 게임', '3개 시점 연결'], scenes: 8, people: ['JUNE', 'MISO'], context: '초반 농담과 당시 게임 규칙을 함께 제시해야 합니다.', reason: '초반 발언이 결과 장면의 의미를 바꾸며 시청자가 사전 방송을 몰라도 이해할 수 있습니다.' },
  { id: '03', time: '04:21:55 — 04:34:12', title: '디스코드에 갑자기 들어온 의문의 손님', summary: '대화 반응은 강하지만 사건의 결말이 없습니다. 후보 05의 후속 대화와 결합하면 완결성이 개선됩니다.', score: 72, decision: '결합 검토', color: '#ffb44b', tags: ['다인원', '후속 사건 연결'], scenes: 6, people: ['JUNE', 'UNKNOWN'], context: '의문의 참가자가 누구인지 설명할 후속 장면이 필요합니다.', reason: '도입과 반응은 강하지만 현재 선택 구간만으로는 인물의 정체와 사건 결과가 해소되지 않습니다.' },
  { id: '04', time: '05:48:02 — 05:56:30', title: '랭크 마지막 판의 아쉬운 패배', summary: '단일 사건이지만 유사 장면이 반복되고 맥락 대비 정보 밀도가 낮아 독립 콘텐츠로 권장하지 않습니다.', score: 38, decision: '제작 안함', color: '#6b7584', tags: ['게임 플레이', '밀도 부족'], scenes: 3, people: ['JUNE'], context: '이전 랭크 기록을 길게 설명해야 합니다.', reason: '반복 플레이 비중이 높고 결말이 기존 게임 영상과 차별화되지 않아 제작 비용 대비 가치가 낮습니다.' }
];

export const TIMELINE = [
  { order: 1, source: '00:42:18', end: '00:42:37', role: '결과 예고', pacing: 'TRIM', speaker: 'JUNE', effect: 'zoom', text: '이때까지만 해도 완벽한 줄 알았다' },
  { order: 2, source: '00:17:02', end: '00:17:41', role: '배경', pacing: 'KEEP', speaker: 'MISO', effect: 'none', text: '오늘 작전은 딱 하나야' },
  { order: 3, source: '00:31:12', end: '00:32:08', role: '계획', pacing: 'TRIM', speaker: 'JUNE', effect: 'crop', text: '들키지만 않으면 돼' },
  { order: 4, source: '00:43:01', end: '00:44:22', role: '갈등', pacing: 'KEEP', speaker: 'NOAH', effect: 'subtitle', text: '잠깐, 문이 왜 열려 있어?' },
  { order: 5, source: '01:03:47', end: '01:04:38', role: '반전', pacing: 'KEEP', speaker: 'JUNE', effect: 'replay', text: '우리가 아니라 저쪽이 걸렸어' },
  { order: 6, source: '01:07:02', end: '01:07:34', role: '결과', pacing: 'TRIM', speaker: 'MISO', effect: 'zoom', text: '이게 성공한다고?' }
];

export const EVENTS = [
  { time: '00:17:02', type: '최초 언급', text: 'MISO가 잠입 작전의 목표를 제안', strength: 62 },
  { time: '00:31:12', type: '관련 대화', text: '역할 분담과 실패 조건을 합의', strength: 71 },
  { time: '00:43:01', type: '갈등', text: 'NOAH의 우발 행동으로 계획 노출', strength: 96 },
  { time: '01:03:47', type: '반전', text: '상대 팀이 먼저 발각된 사실 확인', strength: 91 },
  { time: '01:07:34', type: '결과', text: '작전 성공과 인물 반응으로 종료', strength: 84 }
];

export const KNOWLEDGE = [
  { title: '결과 선공개 후 원인 회수', type: '스토리텔링', confidence: 91, references: 28, trend: '+12%', description: '결과의 일부를 먼저 보여준 뒤 과거 시점으로 돌아가 원인을 설명하는 패턴' },
  { title: '반응 직후 1.2배 리플레이', type: '편집', confidence: 84, references: 19, trend: '+7%', description: '핵심 사건 직후 인물 반응을 보존하고 사건 장면을 짧게 반복하는 패턴' },
  { title: '상황 자막 최소화', type: '자막', confidence: 78, references: 34, trend: '-3%', description: '게임 UI 정보가 충분한 구간에서는 대사 자막만 유지하는 패턴' },
  { title: '두 인물 대비 썸네일', type: '패키징', confidence: 88, references: 22, trend: '+18%', description: '사건 전후의 상반된 표정을 좌우로 배치하고 핵심 사물만 강조하는 패턴' }
];

export const LOGS = [
  ['14:32:48', 'PLANNING', '후보 01의 장면 검색 완료 · 18개 중 12개 선택'],
  ['14:32:41', 'PLANNING', '시간적으로 분리된 사건 언급 4개 연결'],
  ['14:31:09', 'EVALUATING', '후보 04 제작 제외 · 독립성 0.38'],
  ['14:29:55', 'EVALUATING', '콘텐츠 후보 4개 가치 평가 완료'],
  ['14:26:12', 'DISCOVERING', '사건 그래프에서 후보 4개 생성'],
  ['13:58:33', 'UNDERSTANDING', '전체 방송 1차 통과 완료 · 정밀 구간 37개'],
  ['12:17:04', 'PARSING', '오디오 트랙 4개 및 프레임 인덱스 생성 완료']
];

export const CALIBRATION = {
  name: 'JUNE Studio · 게임/합방', measured: '2026-08-17', samples: 14,
  metrics: [
    { label: '호흡 보존 재현율', value: 91 }, { label: '불필요 정적 정밀도', value: 86 },
    { label: '콘텐츠 발견 일치도', value: 83 }, { label: '오탐 억제율', value: 89 }
  ],
  params: [
    ['무음 레벨', '-38.6 dB', '실측'], ['최소 무음 지속', '0.46 s', '실측'],
    ['1차 통과 간격', '12 s', '실측'], ['정밀 재검토 점수', '0.74', '튜닝'],
    ['화제 전환 유사도', '0.41', '튜닝'], ['썸네일 표정 가중치', '0.52', '튜닝']
  ]
};

export const EPISODE = {
  titleOptions: ['완벽한 잠입 작전이 10초 만에 망한 이유', '팀원이 문 하나 잘못 열었더니 생긴 일', '이 작전이 성공한 게 더 이상합니다'],
  description: '완벽하게 준비한 잠입 작전. 그런데 문 하나가 열리면서 모든 계획이 꼬이기 시작했습니다.\n\n00:00 결과 미리보기\n00:19 작전의 시작\n01:54 예상 밖의 변수\n04:12 마지막 반전',
  tags: ['잠입게임', '게임하이라이트', '스트리머', '합방'], duration: '08:42', resolution: '1080p', loudness: '-14 LUFS'
};
