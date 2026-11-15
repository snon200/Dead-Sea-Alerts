'use strict';

function parseZoneResponse(payload) {
  if (!payload || payload.schema_version !== '1.0' || !Array.isArray(payload.zones)) {
    throw new Error('Unsupported or invalid zone response');
  }
  const ids = new Set();
  const nullableNumber = value => value === null || (typeof value === 'number' && Number.isFinite(value));
  const isTimestamp = value => typeof value === 'string' &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    Number.isFinite(Date.parse(value));
  return payload.zones.map(zone => {
    if (!zone || typeof zone.id !== 'string' || !zone.id.trim() || ids.has(zone.id)) {
      throw new Error('Invalid or duplicate zone identifier');
    }
    ids.add(zone.id);
    const geometry = zone.geometry;
    if (!geometry || !['Polygon', 'MultiPolygon'].includes(geometry.type) || !Array.isArray(geometry.coordinates)) {
      throw new Error(`Invalid geometry for zone ${zone.id}`);
    }
    const rawPolygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
    if (!rawPolygons.length) throw new Error(`Empty geometry for zone ${zone.id}`);
    const polygons = rawPolygons.map(rings => {
      if (!Array.isArray(rings) || !rings.length) throw new Error(`Invalid polygon for zone ${zone.id}`);
      return rings.map(ring => {
        if (!Array.isArray(ring) || ring.length < 4) throw new Error(`Invalid ring for zone ${zone.id}`);
        const points = ring.map(point => {
          if (!Array.isArray(point) || point.length !== 2 ||
              !point.every(value => typeof value === 'number' && Number.isFinite(value)) ||
              Math.abs(point[0]) > 180 || Math.abs(point[1]) > 90) {
            throw new Error(`Invalid coordinate for zone ${zone.id}`);
          }
          return [point[1], point[0]];
        });
        const first = points[0], last = points[points.length - 1];
        if (first[0] !== last[0] || first[1] !== last[1]) throw new Error(`Unclosed ring for zone ${zone.id}`);
        return points;
      });
    });
    if (zone.risk_level !== null && !['high', 'elevated', 'watch', 'low'].includes(zone.risk_level)) {
      throw new Error(`Invalid risk level for zone ${zone.id}`);
    }
    const summary = zone.summary;
    const numbers = ['los_velocity_mm_per_year', 'cumulative_shift_mm', 'acceleration_mm_per_year2', 'latest_pair_mean_coherence'];
    const dates = ['latest_pair_reference_time', 'latest_pair_secondary_time'];
    if (!summary || !numbers.every(key => nullableNumber(summary[key])) ||
        !dates.every(key => summary[key] === null || isTimestamp(summary[key])) ||
        (summary.latest_pair_mean_coherence !== null &&
          (summary.latest_pair_mean_coherence < 0 || summary.latest_pair_mean_coherence > 1))) {
      throw new Error(`Invalid measurements for zone ${zone.id}`);
    }
    if (zone.cumulative_series !== null && !Array.isArray(zone.cumulative_series)) {
      throw new Error(`Invalid displacement series for zone ${zone.id}`);
    }
    let previousTime = -Infinity;
    const series = zone.cumulative_series === null ? null : zone.cumulative_series.map(point => {
      if (!point || !isTimestamp(point.time) || typeof point.displacement_mm !== 'number' ||
          !Number.isFinite(point.displacement_mm) || Date.parse(point.time) <= previousTime) {
        throw new Error(`Invalid displacement point for zone ${zone.id}`);
      }
      previousTime = Date.parse(point.time);
      return { time: point.time, value: point.displacement_mm };
    });
    const level = zone.risk_level || 'unknown';
    return {
      id: zone.id, name: `Zone ${zone.id}`, level,
      label: level === 'unknown' ? 'Unavailable' : level[0].toUpperCase() + level.slice(1),
      polygons, geometry,
      losRate: summary.los_velocity_mm_per_year, cumulative: summary.cumulative_shift_mm,
      acceleration: summary.acceleration_mm_per_year2, coherence: summary.latest_pair_mean_coherence,
      pairStart: summary.latest_pair_reference_time, pairEnd: summary.latest_pair_secondary_time,
      observations: series === null ? null : series.length, series
    };
  });
}

function cachedOsmFeatures(cache, zone) {
  if (!cache || cache.schema_version !== '2.0' || !cache.elements || typeof cache.elements !== 'object' ||
      Array.isArray(cache.elements) || !cache.zones || typeof cache.zones !== 'object' || Array.isArray(cache.zones)) {
    throw new Error('Unsupported or invalid infrastructure cache');
  }
  if (!Object.hasOwn(cache.zones, zone.id)) throw new Error(`Missing infrastructure cache for zone ${zone.id}`);
  const entry = cache.zones[zone.id];
  if (!entry || !entry.geometry || entry.geometry.type !== zone.geometry.type ||
      JSON.stringify(entry.geometry.coordinates) !== JSON.stringify(zone.geometry.coordinates)) {
    throw new Error(`Outdated infrastructure cache for zone ${zone.id}`);
  }
  if (!Array.isArray(entry.elements)) throw new Error(`Invalid infrastructure cache for zone ${zone.id}`);
  return entry.elements.map(key => {
    if (typeof key !== 'string' || !Object.hasOwn(cache.elements, key)) {
      throw new Error(`Missing infrastructure element for zone ${zone.id}`);
    }
    const element = cache.elements[key];
    if (!element || !element.tags || typeof element.tags !== 'object' || Array.isArray(element.tags) ||
        key !== `${element.type}/${element.id}` ||
        (element.type === 'node' && (!Number.isFinite(element.lat) || !Number.isFinite(element.lon))) ||
        (element.type === 'way' && (!Array.isArray(element.geometry) || element.geometry.length < 2 ||
          !element.geometry.every(point => point && Number.isFinite(point.lat) && Number.isFinite(point.lon)))) ||
        !['node', 'way'].includes(element.type)) {
      throw new Error(`Invalid infrastructure element ${key}`);
    }
    return element;
  });
}

if (typeof module !== 'undefined') module.exports = { parseZoneResponse, cachedOsmFeatures };
