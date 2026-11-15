'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const spec = require('../openapi.json');
const { parseZoneResponse, cachedOsmFeatures } = require('../zone-api.js');

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

test('only uses complete cache entries for the exact current geometry', () => {
  const [zone] = parseZoneResponse(example());
  const element = { type: 'node', id: 1, lat: 31.45, lon: 35.38, tags: {} };
  const cache = {
    schema_version: '2.0',
    elements: { 'node/1': element },
    zones: { [zone.id]: { geometry: structuredClone(zone.geometry), elements: ['node/1'] } }
  };
  assert.deepEqual(cachedOsmFeatures(cache, zone), [element]);
  assert.deepEqual(cachedOsmFeatures(cache, { ...zone, name: 'Updated name' }), [element]);
  const changed = structuredClone(zone);
  changed.geometry.coordinates[0][0][0] += 0.001;
  assert.throws(() => cachedOsmFeatures(cache, changed), /Outdated/);
  assert.throws(() => cachedOsmFeatures(cache, { ...zone, id: 'new-zone' }), /Missing/);
  cache.zones.removed = structuredClone(cache.zones[zone.id]);
  assert.deepEqual(cachedOsmFeatures(cache, zone), [element]);
  delete cache.elements['node/1'];
  assert.throws(() => cachedOsmFeatures(cache, zone), /Missing infrastructure element/);
  cache.elements['node/1'] = { ...element, lat: null };
  assert.throws(() => cachedOsmFeatures(cache, zone), /Invalid infrastructure element/);
  cache.zones[zone.id].elements = [];
  assert.deepEqual(cachedOsmFeatures(cache, zone), []);
  cache.schema_version = '1.0';
  assert.throws(() => cachedOsmFeatures(cache, zone), /Unsupported/);
});

test('invalidates changes inside multipolygons and holes', () => {
  const payload = example();
  const outer = payload.zones[0].geometry.coordinates[0];
  const hole = [[35.381, 31.451], [35.382, 31.451], [35.382, 31.452], [35.381, 31.451]];
  payload.zones[0].geometry = { type: 'MultiPolygon', coordinates: [[outer, hole], [outer]] };
  const [zone] = parseZoneResponse(payload);
  const cache = {
    schema_version: '2.0', elements: {},
    zones: { [zone.id]: { geometry: structuredClone(zone.geometry), elements: [] } }
  };
  assert.deepEqual(cachedOsmFeatures(cache, zone), []);
  cache.zones[zone.id].geometry.coordinates[0][1][0][0] += 0.001;
  assert.throws(() => cachedOsmFeatures(cache, zone), /Outdated/);
});

test('the checked-in infrastructure cache covers the current zone export', () => {
  const zones = parseZoneResponse(require('../data/api/zones.json'));
  const cache = require('../data/api/osm-cache.json');
  assert.ok(zones.length > 0);
  for (const zone of zones) assert.ok(Array.isArray(cachedOsmFeatures(cache, zone)));
});
