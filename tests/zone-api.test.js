'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const spec = require('../openapi.json');
const {
  smoothZonePolygons, parseZoneResponse, adjustRiskLevel, regionalOsmData, assignZonePlaceNames, zoneWithinOsmCoverage,
  osmInfrastructureQuery, osmInfrastructureElements
} = require('../zone-api.js');

function example() {
  return structuredClone(spec.paths['/api/v1/zones'].get.responses['200'].content['application/json'].example);
}

test('smooths display corners without changing the source geometry, including holes and multipolygons', () => {
  const outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]];
  const hole = [[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]];
  const polygons = [[outer, hole], [outer]];
  const original = structuredClone(polygons);
  const result = smoothZonePolygons(polygons);
  assert.deepEqual(polygons, original);
  assert.deepEqual(result.map(rings => rings.length), [2, 1]);
  assert.equal(result[0][0].length, 20);
  assert.deepEqual(result[0][0][0], [4, 0]);
  assert.deepEqual(result[0][0][4], [0, 4]);
  assert.deepEqual(result[0][0][2], [1, 1]);
  assert.deepEqual(result[0][1][0], [2.8, 2]);
});

test('handles duplicate vertices and unclosed display rings without invalid coordinates', () => {
  const ring = [[0, 0], [0, 0], [0, 2], [2, 0]];
  const [smoothed] = smoothZonePolygons([[ring]])[0];
  assert.equal(smoothed.length, 15);
  assert.ok(smoothed.flat().every(Number.isFinite));
  assert.deepEqual(smoothZonePolygons([[[[0, 0], [0, 0]]]]), [[[[0, 0], [0, 0]]]]);
});

test('adapts the documented response and converts longitude/latitude', () => {
  const [zone] = parseZoneResponse(example());
  assert.deepEqual(zone.polygons[0][0][0], [31.45, 35.38]);
  assert.equal(zone.level, 'elevated');
  assert.equal(zone.baseLevel, 'elevated');
  assert.equal(zone.series[2].value, -4);
  assert.equal(zone.observations, 3);
});

test('adjusts backend risk one level for infrastructure and clamps at the extremes', () => {
  const cases = [
    ['low', 'watch', 'low'],
    ['watch', 'elevated', 'low'],
    ['elevated', 'high', 'watch'],
    ['high', 'high', 'elevated']
  ];
  for (const [base, withInfrastructure, withoutInfrastructure] of cases) {
    assert.deepEqual(adjustRiskLevel(base, true, true), { level: withInfrastructure, reason: 'infrastructure' });
    assert.deepEqual(adjustRiskLevel(base, false, true), { level: withoutInfrastructure, reason: 'none' });
  }
});

test('preserves unknown or unconfirmed risk while applying known overlaps in partial coverage', () => {
  assert.deepEqual(adjustRiskLevel('unknown', true, true), { level: 'unknown', reason: 'unknown' });
  assert.deepEqual(adjustRiskLevel('watch', null, false), { level: 'watch', reason: 'unavailable' });
  assert.deepEqual(adjustRiskLevel('watch', false, false), { level: 'watch', reason: 'incomplete' });
  assert.deepEqual(adjustRiskLevel('watch', true, false), { level: 'elevated', reason: 'infrastructure' });
  assert.throws(() => adjustRiskLevel('watch', undefined, true), /Invalid/);
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

test('renders regional features and recomputes intersections for changed zones', () => {
  const [zone] = parseZoneResponse(example());
  const inside = { type: 'node', id: 1, lat: 31.451, lon: 35.381, tags: {} };
  const outsideZone = { type: 'node', id: 2, lat: 31.47, lon: 35.40, tags: {} };
  const cache = {
    schema_version: '3.0', elements: { 'node/1': inside, 'node/2': outsideZone },
    coverage: { type: 'Polygon', coordinates: [[[35.3, 31.4], [35.5, 31.4], [35.5, 31.6], [35.3, 31.6], [35.3, 31.4]]] }
  };
  const result = regionalOsmData(cache, [zone]);
  assert.deepEqual(result.elements, [inside, outsideZone]);
  assert.deepEqual(result.results[zone.id], [inside]);
  assert.deepEqual(result.outsideCoverage, []);
  const changedPayload = example();
  changedPayload.zones[0].geometry.coordinates = [[[35.39, 31.46], [35.41, 31.46], [35.41, 31.48], [35.39, 31.46]]];
  const [changed] = parseZoneResponse(changedPayload);
  assert.deepEqual(regionalOsmData(cache, [changed]).results[zone.id], [outsideZone]);
  assert.deepEqual(regionalOsmData(cache, []).elements, [inside, outsideZone]);
  cache.elements['node/1'] = { ...inside, lat: null };
  assert.throws(() => regionalOsmData(cache, [zone]), /Invalid infrastructure element/);
  cache.elements['node/1'] = { ...inside, tags: { name: null } };
  assert.throws(() => regionalOsmData(cache, [zone]), /Invalid infrastructure element/);
});

test('marks incomplete coverage even where a segment crosses a concave boundary', () => {
  const ring = [[0, 0], [0, 4], [4, 4], [4, 3], [1, 3], [1, 1], [4, 1], [4, 0], [0, 0]];
  const zone = { id: 'partial', polygons: [[[[2, 0.5], [2, 3.5], [3, 3.5], [2, 0.5]]]] };
  assert.equal(zoneWithinOsmCoverage(zone, ring), false);
  const cache = {
    schema_version: '3.0', elements: { 'node/1': { type: 'node', id: 1, lat: 2, lon: 0.5, tags: {} } },
    coverage: { type: 'Polygon', coordinates: [ring.map(([lat, lon]) => [lon, lat])] }
  };
  const result = regionalOsmData(cache, [zone]);
  assert.deepEqual(result.outsideCoverage, ['partial']);
  assert.equal(result.results.partial.length, 1);
});

test('the checked-in infrastructure cache matches the study area', () => {
  const zones = parseZoneResponse(require('../data/api/zones.json'));
  const cache = require('../data/api/osm-cache.json');
  const area = JSON.parse(require('node:fs').readFileSync('data/api/osm-study-area.geojson', 'utf8'));
  assert.ok(zones.length > 0);
  assert.deepEqual(cache.coverage, area.features[0].geometry);
  const result = regionalOsmData(cache, zones);
  assert.ok(result.elements.length > 0);
  assert.equal(Object.keys(result.results).length, zones.length);
});

test('names zones from nearby places, with stable distinct labels', () => {
  const [base] = parseZoneResponse(example());
  const first = { ...base, id: 'a' }, second = { ...base, id: 'b' };
  const center = base.polygons[0][0][0];
  const dataset = {
    schema_version: '1.0', bounds: [30, 34, 33, 37],
    places: [
      { name: 'Far away', lat: center[0] + 0.2, lon: center[1] },
      { name: 'Ein Gedi', lat: center[0], lon: center[1] }
    ]
  };
  assert.deepEqual(assignZonePlaceNames([second, first], dataset), { outsideCoverage: false });
  assert.equal(first.name, 'Ein Gedi · 1');
  assert.equal(second.name, 'Ein Gedi · 2');
  assignZonePlaceNames([first], dataset);
  assert.equal(first.name, 'Ein Gedi');
  dataset.places.shift();
  dataset.places[0].lat += 0.2;
  assignZonePlaceNames([first], dataset);
  assert.equal(first.name, 'Zone a');
});

test('keeps IDs for incomplete coverage and rejects invalid datasets', () => {
  const [zone] = parseZoneResponse(example());
  const dataset = {
    schema_version: '1.0', bounds: [31.44, 35.37, 31.46, 35.39],
    places: [{ name: 'Example', lat: 31.45, lon: 35.38 }]
  };
  assert.equal(assignZonePlaceNames([zone], dataset).outsideCoverage, true);
  assert.equal(zone.name, `Zone ${zone.id}`);
  dataset.places[0].lat = null;
  assert.throws(() => assignZonePlaceNames([zone], dataset), /Invalid/);
});

test('the checked-in place dataset names current zones without duplicate labels', () => {
  const zones = parseZoneResponse(require('../data/api/zones.json'));
  const result = assignZonePlaceNames(zones, require('../data/api/osm-places.json'));
  assert.equal(result.outsideCoverage, false);
  assert.ok(zones.some(zone => zone.name !== `Zone ${zone.id}`));
  assert.equal(new Set(zones.map(zone => zone.name)).size, zones.length);
});

test('live infrastructure requests include new layers and reject incomplete data', () => {
  const query = osmInfrastructureQuery({ south: 31, west: 35, north: 32, east: 36 });
  assert.match(query, /31,35,32,36/);
  assert.match(query, /waterway.*dam/);
  assert.match(query, /natural.*sinkhole/);
  const way = { type: 'way', id: 1, tags: { highway: 'primary' }, geometry: [
    { lat: 31.4, lon: 35.4 }, { lat: 31.5, lon: 35.5 }
  ] };
  assert.deepEqual(osmInfrastructureElements({ elements: [way] }), [way]);
  assert.throws(() => osmInfrastructureElements({ remark: 'timeout', elements: [] }), /Incomplete/);
  assert.throws(() => osmInfrastructureElements({ elements: [{ ...way, geometry: [] }] }), /Invalid/);
});
