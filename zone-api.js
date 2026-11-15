'use strict';

function smoothZonePolygons(polygons) {
  return polygons.map(rings => rings.map(ring => {
    const points = ring.filter((point, index) => index === 0 ||
      point[0] !== ring[index - 1][0] || point[1] !== ring[index - 1][1]);
    if (points.length > 1 && points[0][0] === points[points.length - 1][0] &&
        points[0][1] === points[points.length - 1][1]) points.pop();
    if (points.length < 3) return ring;

    return points.flatMap((point, index) => {
      const previous = points[(index + points.length - 1) % points.length];
      const next = points[(index + 1) % points.length];
      const previousLength = Math.hypot(previous[0] - point[0], previous[1] - point[1]);
      const nextLength = Math.hypot(next[0] - point[0], next[1] - point[1]);
      const radius = Math.min(previousLength, nextLength) * 0.4;
      const start = point.map((value, axis) => value + (previous[axis] - value) * radius / previousLength);
      const end = point.map((value, axis) => value + (next[axis] - value) * radius / nextLength);
      return Array.from({ length: 5 }, (_, step) => {
        const t = step / 4;
        return point.map((value, axis) =>
          (1 - t) ** 2 * start[axis] + 2 * (1 - t) * t * value + t ** 2 * end[axis]);
      });
    });
  }));
}

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
      id: zone.id, name: `Zone ${zone.id}`, baseLevel: level, level,
      label: level === 'unknown' ? 'Unavailable' : level[0].toUpperCase() + level.slice(1),
      riskAdjustment: level === 'unknown' ? 'unknown' : 'unavailable',
      polygons, geometry,
      losRate: summary.los_velocity_mm_per_year, cumulative: summary.cumulative_shift_mm,
      acceleration: summary.acceleration_mm_per_year2, coherence: summary.latest_pair_mean_coherence,
      pairStart: summary.latest_pair_reference_time, pairEnd: summary.latest_pair_secondary_time,
      observations: series === null ? null : series.length, series
    };
  });
}

function adjustRiskLevel(baseLevel, hasInfrastructure, completeCoverage) {
  const levels = ['low', 'watch', 'elevated', 'high'];
  if (![true, false, null].includes(hasInfrastructure) || typeof completeCoverage !== 'boolean') {
    throw new Error('Invalid risk adjustment input');
  }
  if (baseLevel === 'unknown') return { level: baseLevel, reason: 'unknown' };
  const index = levels.indexOf(baseLevel);
  if (index < 0) throw new Error('Invalid risk adjustment input');
  if (hasInfrastructure === null) return { level: baseLevel, reason: 'unavailable' };
  if (hasInfrastructure) return { level: levels[Math.min(index + 1, levels.length - 1)], reason: 'infrastructure' };
  if (!completeCoverage) return { level: baseLevel, reason: 'incomplete' };
  return { level: levels[Math.max(index - 1, 0)], reason: 'none' };
}

function pointOnSegment(point, a, b) {
  const cross = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0]);
  return Math.abs(cross) < 1e-10 &&
    point[0] >= Math.min(a[0], b[0]) - 1e-10 && point[0] <= Math.max(a[0], b[0]) + 1e-10 &&
    point[1] >= Math.min(a[1], b[1]) - 1e-10 && point[1] <= Math.max(a[1], b[1]) + 1e-10;
}

function pointInPolygon(point, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[j], b = polygon[i];
    if (pointOnSegment(point, a, b)) return true;
    if ((a[0] > point[0]) !== (b[0] > point[0]) &&
        point[1] < (b[1] - a[1]) * (point[0] - a[0]) / (b[0] - a[0]) + a[1]) inside = !inside;
  }
  return inside;
}

function segmentsIntersect(a, b, c, d) {
  const direction = (p, q, r) => (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
  const abC = direction(a, b, c), abD = direction(a, b, d);
  const cdA = direction(c, d, a), cdB = direction(c, d, b);
  if (pointOnSegment(c, a, b) || pointOnSegment(d, a, b) ||
      pointOnSegment(a, c, d) || pointOnSegment(b, c, d)) return true;
  return ((abC > 0 && abD < 0) || (abC < 0 && abD > 0)) &&
    ((cdA > 0 && cdB < 0) || (cdA < 0 && cdB > 0));
}

function pointInZone(point, zone) {
  return zone.polygons.some(rings => {
    if (!pointInPolygon(point, rings[0])) return false;
    return !rings.slice(1).some(ring => {
      if (ring.some((vertex, index) => pointOnSegment(point, vertex, ring[(index + 1) % ring.length]))) return false;
      return pointInPolygon(point, ring);
    });
  });
}

function featureIntersectsZone(element, zone) {
  if (element.type === 'node') return pointInZone([element.lat, element.lon], zone);
  const points = element.geometry.map(point => [point.lat, point.lon]);
  if (points.some(point => pointInZone(point, zone))) return true;
  for (let i = 1; i < points.length; i++) {
    for (const ring of zone.polygons.flat()) {
      for (let j = 0; j < ring.length; j++) {
        if (segmentsIntersect(points[i - 1], points[i], ring[j], ring[(j + 1) % ring.length])) return true;
      }
    }
  }
  const first = points[0], last = points[points.length - 1];
  return points.length >= 4 && first[0] === last[0] && first[1] === last[1] &&
    zone.polygons.some(rings => pointInPolygon(rings[0][0], points));
}

function zoneWithinOsmCoverage(zone, ring) {
  for (const rings of zone.polygons) {
    const outer = rings[0];
    for (let i = 1; i < outer.length; i++) {
      const a = outer[i - 1], b = outer[i];
      if (!pointInPolygon(a, ring) || !pointInPolygon(b, ring)) return false;
      const cuts = [0, 1];
      const dx = b[0] - a[0], dy = b[1] - a[1];
      for (let j = 1; j < ring.length; j++) {
        const c = ring[j - 1], d = ring[j];
        const ex = d[0] - c[0], ey = d[1] - c[1];
        const denominator = dx * ey - dy * ex;
        if (Math.abs(denominator) < 1e-15) {
          for (const p of [c, d]) {
            if (pointOnSegment(p, a, b) && (dx || dy)) {
              cuts.push(Math.abs(dx) >= Math.abs(dy) ? (p[0] - a[0]) / dx : (p[1] - a[1]) / dy);
            }
          }
        } else {
          const t = ((c[0] - a[0]) * ey - (c[1] - a[1]) * ex) / denominator;
          const u = ((c[0] - a[0]) * dy - (c[1] - a[1]) * dx) / denominator;
          if (t > 0 && t < 1 && u >= 0 && u <= 1) cuts.push(t);
        }
      }
      cuts.sort((x, y) => x - y);
      for (let j = 1; j < cuts.length; j++) {
        const t = (cuts[j - 1] + cuts[j]) / 2;
        if (!pointInPolygon([a[0] + t * dx, a[1] + t * dy], ring)) return false;
      }
    }
  }
  return true;
}

function regionalOsmData(cache, zones) {
  const coverage = cache && cache.coverage;
  if (!cache || cache.schema_version !== '3.0' || !cache.elements || typeof cache.elements !== 'object' ||
      Array.isArray(cache.elements) || !coverage || coverage.type !== 'Polygon' ||
      !Array.isArray(coverage.coordinates) || coverage.coordinates.length !== 1) {
    throw new Error('Unsupported or invalid infrastructure cache');
  }
  const rawRing = coverage.coordinates[0];
  if (!Array.isArray(rawRing) || rawRing.length < 4 ||
      !rawRing.every(point => Array.isArray(point) && point.length === 2 &&
        point.every(Number.isFinite) && Math.abs(point[0]) <= 180 && Math.abs(point[1]) <= 90) ||
      rawRing[0][0] !== rawRing[rawRing.length - 1][0] || rawRing[0][1] !== rawRing[rawRing.length - 1][1]) {
    throw new Error('Invalid infrastructure coverage');
  }
  const ring = rawRing.map(([lon, lat]) => [lat, lon]);
  const elements = Object.entries(cache.elements).map(([key, element]) => {
    if (!element || !element.tags || typeof element.tags !== 'object' || Array.isArray(element.tags) ||
        !Object.values(element.tags).every(value => typeof value === 'string') ||
        !Number.isSafeInteger(element.id) || element.id <= 0 || key !== `${element.type}/${element.id}` ||
        (element.type === 'node' && (!Number.isFinite(element.lat) || !Number.isFinite(element.lon))) ||
        (element.type === 'way' && (!Array.isArray(element.geometry) || element.geometry.length < 2 ||
          !element.geometry.every(point => point && Number.isFinite(point.lat) && Number.isFinite(point.lon)))) ||
        !['node', 'way'].includes(element.type)) {
      throw new Error(`Invalid infrastructure element ${key}`);
    }
    return element;
  });
  const bounds = points => points.reduce((box, [lat, lon]) => [
    Math.min(box[0], lat), Math.min(box[1], lon), Math.max(box[2], lat), Math.max(box[3], lon)
  ], [Infinity, Infinity, -Infinity, -Infinity]);
  const indexed = elements.map(element => ({
    element,
    bounds: element.type === 'node' ? [element.lat, element.lon, element.lat, element.lon] :
      bounds(element.geometry.map(point => [point.lat, point.lon]))
  }));
  const results = Object.create(null), outsideCoverage = [];
  for (const zone of zones) {
    if (!zoneWithinOsmCoverage(zone, ring)) outsideCoverage.push(zone.id);
    const box = bounds(zone.polygons.flat(2));
    results[zone.id] = indexed.filter(({ element, bounds: other }) =>
      other[2] >= box[0] && other[0] <= box[2] && other[3] >= box[1] && other[1] <= box[3] &&
      featureIntersectsZone(element, zone)).map(({ element }) => element);
  }
  return { elements, results, outsideCoverage };
}

function osmInfrastructureQuery(bounds) {
  const bbox = [bounds.south, bounds.west, bounds.north, bounds.east].join(',');
  const selectors = [
    'way["highway"]', 'way["building"]', 'node["building"~"^(hotel|school|hospital)$"]', 'way["bridge"]["bridge"!="no"]',
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
      !dataset.places.every(place => {
        if (!place || typeof place.name !== 'string' || !place.name.trim()) return false;
        if (place.geometry !== undefined) {
          return (place.area === undefined || typeof place.area === 'boolean') &&
            Array.isArray(place.geometry) && place.geometry.length >= 2 &&
            place.geometry.every(point => Array.isArray(point) && point.length === 2 &&
              point.every(Number.isFinite) && Math.abs(point[0]) <= 90 && Math.abs(point[1]) <= 180);
        }
        return Number.isFinite(place.lat) && Number.isFinite(place.lon) && place.lat >= south &&
          place.lat <= north && place.lon >= west && place.lon <= east;
      })) {
    throw new Error('Invalid OSM place-name bounds or places');
  }
  const distanceKm = (lat, lon, targetLat, targetLon) => {
    const dLat = (targetLat - lat) * Math.PI / 180;
    const dLon = (targetLon - lon) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat * Math.PI / 180) *
      Math.cos(targetLat * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
    return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(Math.max(0, 1 - a)));
  };
  const placeDistance = (lat, lon, place) => {
    if (!place.geometry) return distanceKm(lat, lon, place.lat, place.lon);
    const geometry = place.geometry;
    const first = geometry[0], last = geometry[geometry.length - 1];
    if (place.area && geometry.length >= 4 && first[0] === last[0] && first[1] === last[1] &&
        pointInPolygon([lat, lon], geometry)) return 0;
    const lonScale = Math.cos(lat * Math.PI / 180);
    let nearestSquared = Infinity, nearestLat, nearestLon;
    for (let i = 1; i < geometry.length; i++) {
      const start = geometry[i - 1], end = geometry[i];
      const x = (start[1] - lon) * lonScale, y = start[0] - lat;
      const dx = (end[1] - start[1]) * lonScale, dy = end[0] - start[0];
      const lengthSquared = dx * dx + dy * dy;
      const t = lengthSquared ? Math.max(0, Math.min(1, -(x * dx + y * dy) / lengthSquared)) : 0;
      const squared = (x + t * dx) ** 2 + (y + t * dy) ** 2;
      if (squared < nearestSquared) {
        nearestSquared = squared;
        nearestLat = start[0] + t * (end[0] - start[0]);
        nearestLon = start[1] + t * (end[1] - start[1]);
      }
    }
    return distanceKm(lat, lon, nearestLat, nearestLon);
  };
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
    let nearest = null, nearestDistance = Infinity;
    for (const place of dataset.places) {
      const distance = placeDistance(lat, lon, place);
      if (distance < nearestDistance) {
        nearest = place.name.trim();
        nearestDistance = distance;
      }
    }
    const latRadius = nearestDistance / 111;
    const lonRadius = nearestDistance / (111 * Math.cos(lat * Math.PI / 180));
    if (lat - latRadius < south || lat + latRadius > north ||
        lon - lonRadius < west || lon + lonRadius > east) outsideCoverage = true;
    if (nearest) {
      if (!groups.has(nearest)) groups.set(nearest, []);
      groups.get(nearest).push(zone);
    }
  }
  for (const [name, group] of groups) {
    group.sort((a, b) => a.id.localeCompare(b.id));
    group.forEach((zone, index) => {
      zone.name = `${name}${group.length > 1 ? ` · ${index + 1}` : ''}`;
    });
  }
  return { outsideCoverage };
}

if (typeof module !== 'undefined') module.exports = {
  smoothZonePolygons, parseZoneResponse, adjustRiskLevel, regionalOsmData, assignZonePlaceNames,
  osmInfrastructureQuery, osmInfrastructureElements, featureIntersectsZone, zoneWithinOsmCoverage
};
