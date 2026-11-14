'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const spec = require('../openapi.json');
const { parseZoneResponse } = require('../zone-api.js');

function example() {
  return structuredClone(spec.paths['/api/v1/zones'].get.responses['200'].content['application/json'].example);
}

test('adapts the documented response and converts longitude/latitude', () => {
  const [zone] = parseZoneResponse(example());
  assert.deepEqual(zone.polygons[0][0][0], [31.45, 35.38]);
  assert.equal(zone.level, 'elevated');
  assert.equal(zone.series[2].value, -4);
  assert.equal(zone.observations, 3);
});

test('accepts multipolygons, holes and unavailable values', () => {
  const payload = example();
  const zone = payload.zones[0];
  const outer = zone.geometry.coordinates[0];
  const hole = [[35.381, 31.451], [35.382, 31.451], [35.382, 31.452], [35.381, 31.451]];
  zone.geometry = { type: 'MultiPolygon', coordinates: [[outer, hole], [outer]] };
  zone.risk_level = null;
  zone.cumulative_series = null;
  Object.keys(zone.summary).forEach(key => { zone.summary[key] = null; });
  const [parsed] = parseZoneResponse(payload);
  assert.equal(parsed.polygons.length, 2);
  assert.equal(parsed.polygons[0].length, 2);
  assert.equal(parsed.level, 'unknown');
  assert.equal(parsed.series, null);
  assert.equal(parsed.losRate, null);
});

test('rejects invalid response, duplicate IDs and invalid geometry', () => {
  const payload = example();
  assert.throws(() => parseZoneResponse({ schema_version: '2.0', zones: [] }));
  payload.zones.push(structuredClone(payload.zones[0]));
  assert.throws(() => parseZoneResponse(payload), /duplicate/);
  payload.zones.pop();
  payload.zones[0].geometry.coordinates[0][0] = [181, 31];
  assert.throws(() => parseZoneResponse(payload), /coordinate/);
});

test('rejects invalid measurements and unordered history', () => {
  const payload = example();
  payload.zones[0].summary.latest_pair_mean_coherence = 1.1;
  assert.throws(() => parseZoneResponse(payload), /measurements/);
  payload.zones[0].summary.latest_pair_mean_coherence = 0.7;
  payload.zones[0].cumulative_series.reverse();
  assert.throws(() => parseZoneResponse(payload), /displacement point/);
});
