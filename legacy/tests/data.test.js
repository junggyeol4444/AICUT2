import test from 'node:test';
import assert from 'node:assert/strict';
import { PIPELINE, PROJECTS, CANDIDATES, TIMELINE, EVENTS, KNOWLEDGE, CALIBRATION, EPISODE } from '../src/data.js';

test('pipeline includes the mandatory human review gate', () => {
  assert.deepEqual(PIPELINE.map(stage => stage.key), [
    'PARSING', 'UNDERSTANDING', 'DISCOVERING', 'EVALUATING',
    'PLANNING', 'RENDERING', 'REVIEW_PENDING'
  ]);
});

test('projects support active, review and no-content outcomes', () => {
  assert.equal(PROJECTS.length, 3);
  assert.ok(PROJECTS.some(project => project.status === 'NO_CONTENT'));
  assert.ok(PROJECTS.some(project => project.status === 'REVIEW_PENDING'));
  assert.ok(PROJECTS.every(project => project.file && project.duration && Number.isFinite(project.progress)));
});

test('content candidates contain explainable event-level decisions', () => {
  assert.ok(CANDIDATES.length > 0);
  assert.ok(CANDIDATES.every(candidate => candidate.reason && candidate.context && candidate.people.length));
  assert.deepEqual(new Set(CANDIDATES.map(candidate => candidate.decision)), new Set(['제작', '결합 검토', '제작 안함']));
});

test('edit timeline permits non-chronological source reconstruction', () => {
  assert.ok(TIMELINE.every((cut, index) => cut.order === index + 1));
  assert.notDeepEqual(TIMELINE.map(cut => cut.source), [...TIMELINE].sort((a, b) => a.source.localeCompare(b.source)).map(cut => cut.source));
  assert.ok(TIMELINE.every(cut => ['KEEP', 'TRIM', 'CUT'].includes(cut.pacing)));
});

test('event graph, learned knowledge, calibration and package data are available', () => {
  assert.ok(EVENTS.length >= 5);
  assert.ok(KNOWLEDGE.every(pattern => pattern.references > 0 && pattern.confidence > 0));
  assert.ok(CALIBRATION.metrics.every(metric => metric.value >= 0 && metric.value <= 100));
  assert.equal(EPISODE.titleOptions.length, 3);
});
