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

function osmInfrastructureQuery(bounds) {
  const bbox = [bounds.south, bounds.west, bounds.north, bounds.east].join(',');
  const selectors = [
    'way["highway"]', 'way["building"]', 'way["bridge"]["bridge"!="no"]',
    'nw["man_made"~"^(pumping_station|pipeline|water_well|water_works|water_tower|wastewater_plant|dyke|embankment|works|storage_tank|mast|tower)$"]',
    'nw["power"~"^(line|minor_line|substation|plant|generator)$"]',
    'nw["water"="reservoir"]', 'way["landuse"~"^(reservoir|industrial|salt_pond|quarry)$"]',
    'nw["tourism"~"^(hotel|guest_house)$"]',
    'nw["amenity"~"^(school|kindergarten|hospital|clinic|fire_station|police|fuel)$"]',
    'nw["waterway"="dam"]', 'nw["aeroway"="helipad"]',
    'nw["natural"="sinkhole"]', 'nw["hazard"]'
  ];
  return `[out:json][timeout:30];(${selectors.map(selector => `${selector}(${bbox});`).join('')});out body geom;`;
}

function overpassElements(payload) {
  if (!payload || payload.remark || !Array.isArray(payload.elements)) {
    throw new Error('Incomplete or invalid Overpass response');
  }
  return payload.elements;
}

function osmInfrastructureElements(payload) {
  return overpassElements(payload).map(element => {
    if (!element || !element.tags || typeof element.tags !== 'object' || Array.isArray(element.tags) ||
        !Number.isInteger(element.id) ||
        (element.type === 'node' && (!Number.isFinite(element.lat) || !Number.isFinite(element.lon))) ||
        (element.type === 'way' && (!Array.isArray(element.geometry) || element.geometry.length < 2 ||
          !element.geometry.every(point => point && Number.isFinite(point.lat) && Number.isFinite(point.lon)))) ||
        !['node', 'way'].includes(element.type)) {
      throw new Error('Invalid Overpass infrastructure element');
    }
    return element;
  });
}

function assignZonePlaceNames(zones, dataset) {
  if (!dataset || dataset.schema_version !== '1.0' || !Array.isArray(dataset.bounds) ||
      dataset.bounds.length !== 4 ||       !dataset.bounds.every(Number.isFinite) ||
      !Array.isArray(dataset.places) || !dataset.places.length) {
    throw new Error('Invalid OSM place-name dataset');
  }
  const [south, west, north, east] = dataset.bounds;
  if (south < -90 || north > 90 || west < -180 || east > 180 || south >= north || west >= east ||
      !dataset.places.every(place => place && typeof place.name === 'string' && place.name.trim() &&
        Number.isFinite(place.lat) && Number.isFinite(place.lon) && place.lat >= south && place.lat <= north &&
        place.lon >= west && place.lon <= east)) {
    throw new Error('Invalid OSM place-name bounds or places');
  }
  const maxDistanceKm = 10;
  const groups = new Map();
  let outsideCoverage = false;
  for (const zone of zones) {
    zone.name = `Zone ${zone.id}`;
    const points = zone.polygons.flat(2);
    const bounds = points.reduce((result, [lat, lon]) => [
      Math.min(result[0], lat), Math.min(result[1], lon), Math.max(result[2], lat), Math.max(result[3], lon)
    ], [Infinity, Infinity, -Infinity, -Infinity]);
    const lat = (bounds[0] + bounds[2]) / 2;
    const lon = (bounds[1] + bounds[3]) / 2;
    const latRadius = maxDistanceKm / 111;
    const lonRadius = maxDistanceKm / (111 * Math.cos(lat * Math.PI / 180));
    if (lat - latRadius < south || lat + latRadius > north ||
        lon - lonRadius < west || lon + lonRadius > east) {
      outsideCoverage = true;
      continue;
    }
    let nearest = null, nearestDistance = maxDistanceKm;
    for (const place of dataset.places) {
      const dLat = (place.lat - lat) * Math.PI / 180;
      const dLon = (place.lon - lon) * Math.PI / 180;
      const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat * Math.PI / 180) *
        Math.cos(place.lat * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
      const distance = 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(Math.max(0, 1 - a)));
      if (distance < nearestDistance) {
        nearest = place.name.trim();
        nearestDistance = distance;
      }
    }
    if (nearest) {
      if (!groups.has(nearest)) groups.set(nearest, []);
      groups.get(nearest).push(zone);
    }
  }
  for (const [name, group] of groups) {
    group.sort((a, b) => a.id.localeCompare(b.id));
    group.forEach((zone, index) => {
      zone.name = `Near ${name}${group.length > 1 ? ` · ${index + 1}` : ''}`;
    });
  }
  return { outsideCoverage };
}

if (typeof module !== 'undefined') module.exports = {
  parseZoneResponse, cachedOsmFeatures, assignZonePlaceNames,
  osmInfrastructureQuery, osmInfrastructureElements
};
