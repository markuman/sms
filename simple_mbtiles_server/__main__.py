from gevent import (
    monkey,
)
monkey.patch_all()

from collections import OrderedDict
from contextlib import ExitStack, contextmanager
import heapq
import itertools
import json
import logging
import os
import math
import signal
import struct
import sys
import tarfile
import tempfile
import zlib
import sqlite3

import gevent
from gevent.pywsgi import (
    WSGIServer,
)
from flask import (
    Flask,
    Response,
    request,
    send_from_directory,
)
import httpx
from werkzeug.middleware.proxy_fix import (
    ProxyFix,
)

from .glyphs_pb2 import glyphs
from .gpx import GpxStore, to_gpx
from .mcp import MCPServer, ToolError


# ---------------------------------------------------------------------------
# POI radius search helpers
# ---------------------------------------------------------------------------

def _lat_lon_to_tile(lat, lon, zoom):
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    # clamp to valid range
    y = max(0, min(n - 1, y))
    x = max(0, min(n - 1, x))
    return x, y


def _lat_lon_to_tile_f(lat, lon, zoom):
    """Like _lat_lon_to_tile, but keeps the fractional part."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _tile_width_km(lat, zoom):
    """Width of one tile in km at the given latitude."""
    return 40075.017 * math.cos(math.radians(lat)) / (2 ** zoom)


def _point_segment_distance(px, py, ax, ay, bx, by):
    """Euclidean distance from point p to segment a-b (planar)."""
    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _tiles_along_line(lat1, lon1, lat2, lon2, zoom, buffer_tiles=None):
    """
    Tiles forming a corridor around the straight line from point 1 to point 2.

    On a diagonal route a bounding box wastes roughly half of the decoded
    tiles, so we only keep tiles whose centre lies within *buffer_tiles* of
    the line (straight in tile space, which is accurate enough at z14).

    buffer_tiles defaults to max(2, 10 % of the segment length in tiles),
    about 3.3 km at 48 deg N -- enough slack to walk around a lake.  For an
    almost axis-parallel route the default is additionally capped at the
    bounding box's own short side: the box is naturally narrow there, and a
    fixed two-tile buffer would load *more* tiles than a plain bbox.

    Returns (tiles, buffer_tiles).
    """
    ax, ay = _lat_lon_to_tile_f(lat1, lon1, zoom)
    bx, by = _lat_lon_to_tile_f(lat2, lon2, zoom)

    length_tiles = math.hypot(bx - ax, by - ay)
    if buffer_tiles is None:
        buffer_tiles = max(2.0, 0.1 * length_tiles)
        # never puff the corridor up beyond the bbox short side
        short_side = min(abs(bx - ax), abs(by - ay))
        buffer_tiles = max(1.0, min(buffer_tiles, short_side / 2.0 + 1.0))
    buffer_tiles = float(buffer_tiles)

    # tile centres sit at +0.5, so add half a tile to catch the edge tiles
    r = buffer_tiles + 0.5
    n = 2 ** zoom

    x_min = max(0, int(math.floor(min(ax, bx) - r)))
    x_max = min(n - 1, int(math.ceil(max(ax, bx) + r)))
    y_min = max(0, int(math.floor(min(ay, by) - r)))
    y_max = min(n - 1, int(math.ceil(max(ay, by) + r)))

    tiles = []
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            if _point_segment_distance(x + 0.5, y + 0.5, ax, ay, bx, by) <= r:
                tiles.append((zoom, x, y))

    return tiles, buffer_tiles


def _tiles_along_path(coords, zoom, buffer_tiles=1.0):
    """Corridor tiles around a multi-point path of [lon, lat] pairs."""
    tiles = set()
    for i in range(len(coords) - 1):
        a = coords[i]
        b = coords[i + 1]
        seg, _ = _tiles_along_line(a[1], a[0], b[1], b[0], zoom, buffer_tiles)
        tiles.update(seg)
    if not tiles and coords:
        x, y = _lat_lon_to_tile(coords[0][1], coords[0][0], zoom)
        tiles.add((zoom, x, y))
    return sorted(tiles)


class LruCache:
    """
    Minimal LRU cache for decoded tiles.

    Decoding MVT in pure Python is the expensive part of routing, and an agent
    planning a tour queries the same region over and over.  gevent schedules
    cooperatively, so plain dict operations need no locking here.
    """

    def __init__(self, max_entries=2000):
        self.max_entries = max(1, int(max_entries))
        self._data = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        try:
            value = self._data.pop(key)
        except KeyError:
            self.misses += 1
            return None
        self._data[key] = value
        self.hits += 1
        return value

    def put(self, key, value):
        if key in self._data:
            del self._data[key]
        self._data[key] = value
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def stats(self):
        return {
            'entries': len(self._data),
            'max_entries': self.max_entries,
            'hits': self.hits,
            'misses': self.misses,
        }


def _tiles_in_radius(lat, lon, radius_km, zoom=14, max_tiles=100):
    delta_lat = radius_km / 111.0
    delta_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
    x0, y1 = _lat_lon_to_tile(lat - delta_lat, lon - delta_lon, zoom)
    x1, y0 = _lat_lon_to_tile(lat + delta_lat, lon + delta_lon, zoom)
    tiles = [
        (zoom, x, y)
        for x in range(x0, x1 + 1)
        for y in range(y0, y1 + 1)
    ]
    if len(tiles) > max_tiles:
        cx, cy = _lat_lon_to_tile(lat, lon, zoom)
        tiles.sort(key=lambda t: (t[1] - cx) ** 2 + (t[2] - cy) ** 2)
        tiles = tiles[:max_tiles]
    return tiles


_POI_CATEGORY_FILTERS = {
    'supermarket': {
        'subclass': {'supermarket', 'convenience', 'grocery', 'food'},
        'class':    {'supermarket', 'convenience'},
    },
    'pharmacy': {
        'subclass': {'pharmacy'},
        'class':    {'pharmacy'},
    },
    'hospital': {
        'subclass': {'hospital', 'clinic', 'doctors', 'dentist'},
        'class':    {'hospital'},
    },
    'fuel': {
        'subclass': {'fuel'},
        'class':    {'fuel'},
    },
    'charging_station': {
        'subclass': {'charging_station'},
        'class':    {'charging_station'},
    },
    # Huts you can actually stay in. Deliberately excludes `shelter`:
    # OpenMapTiles lumps bus stop shelters, weather shelters and public
    # air-raid shelters under that subclass. Around Garmisch that is 113 of
    # 122 hits, mostly unnamed, which buries the four real huts far beyond
    # any sane result limit.
    'alpine_hut': {
        'subclass': {'alpine_hut', 'wilderness_hut', 'basic_hut'},
        'class': {'alpine_hut'},
    },
    # Everything roofed, including bus stop shelters and picnic huts.
    'shelter': {
        'subclass': {'shelter', 'lean_to', 'picnic_site'},
        'class': {'shelter'},
    },
    'camp_site': {
        'subclass': {'camp_site', 'caravan_site'},
        'class': {'campsite'},
    },
}


def _poi_name(props):
    """
    Best available name for a POI.

    OpenMapTiles splits names across `name`, `name_de`, `name:latin` and
    friends; plenty of features carry a localised name but no plain `name`.
    """
    for key in ('name', 'name_de', 'name:de', 'name:latin', 'name_en', 'name:en', 'name_int'):
        value = props.get(key)
        if value:
            return value
    return None


def _matches_poi_category(props, category):
    f = _POI_CATEGORY_FILTERS.get(category)
    if not f:
        return False
    return (props.get('subclass') in f['subclass'] or
            props.get('class') in f['class'])


# --- Minimal protobuf / MVT decoder (no extra dependencies) ----------------

def _varint(data, pos):
    result = shift = 0
    while True:
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _zigzag(n):
    return (n >> 1) ^ -(n & 1)


def _parse_mvt_value(data):
    """Decode a vector_tile.Tile.Value message → Python scalar."""
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            v, pos = _varint(data, pos)
            if field == 4:
                return v           # int_value
            if field == 5:
                return v           # uint_value
            if field == 6:
                return _zigzag(v)  # sint_value
            if field == 7:
                return bool(v)     # bool_value
        elif wire == 2:
            length, pos = _varint(data, pos)
            chunk = data[pos:pos + length]; pos += length
            if field == 1:
                return chunk.decode('utf-8', errors='replace')  # string_value
        elif wire == 5:
            v = struct.unpack_from('<f', data, pos)[0]; pos += 4
            return v               # float_value
        elif wire == 1:
            v = struct.unpack_from('<d', data, pos)[0]; pos += 8
            return v               # double_value
        else:
            break
    return None


def _parse_mvt_feature(data):
    """Decode a vector_tile.Tile.Feature → (tags, geom_type, geometry)."""
    pos = 0
    tags = []
    geom_type = 0
    geometry = []
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            v, pos = _varint(data, pos)
            if field == 3:
                geom_type = v
        elif wire == 2:
            length, pos = _varint(data, pos)
            chunk = data[pos:pos + length]; pos += length
            if field in (2, 4):  # tags or geometry (packed uint32)
                p2, vals = 0, []
                while p2 < len(chunk):
                    v, p2 = _varint(chunk, p2)
                    vals.append(v)
                if field == 2:
                    tags = vals
                else:
                    geometry = vals
    return tags, geom_type, geometry


def _parse_mvt_layer(data):
    """Decode a vector_tile.Tile.Layer → (name, keys, values, raw_features, extent)."""
    pos = 0
    name = ''
    keys = []
    values = []
    raw_features = []
    extent = 4096
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field = tag >> 3
        wire = tag & 7
        if wire == 2:
            length, pos = _varint(data, pos)
            chunk = data[pos:pos + length]; pos += length
            if field == 1:
                name = chunk.decode('utf-8', errors='replace')
            elif field == 2:
                raw_features.append(chunk)
            elif field == 3:
                keys.append(chunk.decode('utf-8', errors='replace'))
            elif field == 4:
                values.append(_parse_mvt_value(chunk))
        elif wire == 0:
            v, pos = _varint(data, pos)
            if field == 5:
                extent = v
    return name, keys, values, raw_features, extent


def _parse_mvt_poi(raw_tile, tile_x, tile_y, zoom, category=None):
    """
    Decompress and parse a raw MVT tile blob.

    Returns a list of GeoJSON Point features from the poi layer.  When
    *category* is None every POI is returned, which is what the tile cache
    stores -- filtering then happens per request.
    """
    try:
        data = zlib.decompress(raw_tile, wbits=32 + zlib.MAX_WBITS)
    except Exception:
        data = raw_tile  # tile may already be uncompressed

    features = []
    pos = 0
    n = 2 ** zoom

    while pos < len(data):
        try:
            tag, pos = _varint(data, pos)
        except IndexError:
            break
        field = tag >> 3
        wire = tag & 7
        if wire == 2:
            try:
                length, pos = _varint(data, pos)
            except IndexError:
                break
            chunk = data[pos:pos + length]; pos += length
            if field != 3:          # 3 = Layer in Tile message
                continue
            layer_name, keys, values, raw_features, extent = _parse_mvt_layer(chunk)
            if layer_name != 'poi':
                continue
            for rf in raw_features:
                tags, geom_type, geometry = _parse_mvt_feature(rf)
                if geom_type != 1:  # POINT only
                    continue
                # Decode properties from parallel key/value index arrays
                props = {}
                for i in range(0, len(tags) - 1, 2):
                    ki, vi = tags[i], tags[i + 1]
                    if ki < len(keys) and vi < len(values):
                        props[keys[ki]] = values[vi]
                if category is not None and not _matches_poi_category(props, category):
                    continue
                # Decode point geometry (MoveTo command + one dx/dy pair)
                # geometry[0] = command_integer, [1] = dx zigzag, [2] = dy zigzag
                if len(geometry) < 3:
                    continue
                px = _zigzag(geometry[1])
                py = _zigzag(geometry[2])
                lon = ((tile_x + px / extent) / n) * 360.0 - 180.0
                lat_rad = math.atan(
                    math.sinh(math.pi * (1.0 - 2.0 * (tile_y + py / extent) / n))
                )
                lat = math.degrees(lat_rad)
                features.append({
                    'type': 'Feature',
                    'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                    'properties': props,
                })
        elif wire == 0:
            try:
                _, pos = _varint(data, pos)
            except IndexError:
                break
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8

    return features


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

# OpenMapTiles transportation class weights per profile.
# Factor multiplied with haversine distance → weighted cost.
# None = not passable for this profile.
_ROUTING_PROFILES = {
    'foot': {
        'footway':       1.0,
        'path':          1.0,
        'pedestrian':    1.0,
        'steps':         1.2,
        'track':         1.1,
        'living_street': 1.2,
        'residential':   1.3,
        'service':       1.4,
        'minor':         1.5,
        'tertiary':      1.8,
        'secondary':     2.5,
        'cycleway':      1.3,
        'primary':       None,
        'trunk':         None,
        'motorway':      None,
        'rail':          None,
        'transit':       None,
        'aerialway':     None,
        'ferry':         None,
    },
    'bike': {
        'cycleway':      1.0,
        'path':          1.1,
        'track':         1.2,
        'living_street': 1.1,
        'residential':   1.1,
        'service':       1.3,
        'minor':         1.3,
        'tertiary':      1.4,
        'secondary':     1.6,
        'primary':       2.5,
        'pedestrian':    1.5,
        'trunk':         None,
        'motorway':      None,
        'footway':       None,
        'steps':         None,
        'rail':          None,
        'transit':       None,
        'aerialway':     None,
        'ferry':         None,
    },
}

# Average travel speeds used for duration estimate.
_PROFILE_SPEED_KMH = {'foot': 4.5, 'bike': 15.0}

# Every transportation class we know about, regardless of profile.  Tiles are
# decoded profile-agnostically (so one cache entry serves foot and bike), the
# profile weights are applied later while building the graph.
_KNOWN_ROAD_CLASSES = frozenset(
    cls
    for profile in _ROUTING_PROFILES.values()
    for cls, factor in profile.items()
    if factor is not None
)

# Graph nodes are integer tuples of degrees * 1e5 (about 1 m resolution).
# Integers snap tile borders together exactly and keep the cache compact.
_E5 = 100000.0


def _node_lonlat(node):
    return node[0] / _E5, node[1] / _E5


def _haversine(lon1, lat1, lon2, lat2):
    """Return great-circle distance in km."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _decode_mvt_geometry(geometry, tile_x, tile_y, zoom, extent):
    """
    Decode MVT geometry command sequence.
    Returns list of lines; each line is a list of [lon, lat] pairs.
    """
    n = 2 ** zoom
    cursor_x = cursor_y = 0
    lines = []
    current_line = []
    i = 0
    while i < len(geometry):
        cmd_int = geometry[i]; i += 1
        cmd_id = cmd_int & 7
        count = cmd_int >> 3
        if cmd_id == 1:  # MoveTo
            if current_line:
                lines.append(current_line)
                current_line = []
            for _ in range(count):
                if i + 1 >= len(geometry):
                    break
                cursor_x += _zigzag(geometry[i]); i += 1
                cursor_y += _zigzag(geometry[i]); i += 1
                lon = ((tile_x + cursor_x / extent) / n) * 360.0 - 180.0
                lat = math.degrees(math.atan(math.sinh(
                    math.pi * (1.0 - 2.0 * (tile_y + cursor_y / extent) / n)
                )))
                current_line.append([round(lon, 6), round(lat, 6)])
        elif cmd_id == 2:  # LineTo
            for _ in range(count):
                if i + 1 >= len(geometry):
                    break
                cursor_x += _zigzag(geometry[i]); i += 1
                cursor_y += _zigzag(geometry[i]); i += 1
                lon = ((tile_x + cursor_x / extent) / n) * 360.0 - 180.0
                lat = math.degrees(math.atan(math.sinh(
                    math.pi * (1.0 - 2.0 * (tile_y + cursor_y / extent) / n)
                )))
                current_line.append([round(lon, 6), round(lat, 6)])
        elif cmd_id == 7:  # ClosePath
            if current_line:
                current_line.append(current_line[0])
                lines.append(current_line)
                current_line = []
        else:
            break
    if current_line:
        lines.append(current_line)
    return lines


def _extract_road_segments(raw_tile, tile_x, tile_y, zoom):
    """
    Parse a raw MVT blob and extract every usable transportation segment.
    Returns a list of (node_a, node_b, dist_km, road_class).

    Nodes are (lon_e5, lat_e5) integer tuples (about 1 m resolution).  The
    routing profile is deliberately *not* applied here, so a cached tile can
    serve any profile.
    """
    try:
        data = zlib.decompress(raw_tile, wbits=32 + zlib.MAX_WBITS)
    except Exception:
        data = raw_tile

    segments = []
    pos = 0

    while pos < len(data):
        try:
            tag, pos = _varint(data, pos)
        except IndexError:
            break
        field = tag >> 3
        wire = tag & 7
        if wire == 2:
            try:
                length, pos = _varint(data, pos)
            except IndexError:
                break
            chunk = data[pos:pos + length]; pos += length
            if field != 3:  # not a Tile.Layer
                continue
            layer_name, keys, values, raw_features, extent = _parse_mvt_layer(chunk)
            if layer_name != 'transportation':
                continue
            for rf in raw_features:
                tags, geom_type, geometry = _parse_mvt_feature(rf)
                if geom_type != 2:  # LineString only
                    continue
                props = {}
                for ki in range(0, len(tags) - 1, 2):
                    k, v = tags[ki], tags[ki + 1]
                    if k < len(keys) and v < len(values):
                        props[keys[k]] = values[v]
                road_class = props.get('class', '')
                if road_class not in _KNOWN_ROAD_CLASSES:
                    continue
                for coords in _decode_mvt_geometry(geometry, tile_x, tile_y, zoom, extent):
                    for j in range(len(coords) - 1):
                        a, b = coords[j], coords[j + 1]
                        node_a = (int(round(a[0] * _E5)), int(round(a[1] * _E5)))
                        node_b = (int(round(b[0] * _E5)), int(round(b[1] * _E5)))
                        if node_a == node_b:
                            continue
                        dist = _haversine(a[0], a[1], b[0], b[1])
                        segments.append((node_a, node_b, dist, road_class))
        elif wire == 0:
            try:
                _, pos = _varint(data, pos)
            except IndexError:
                break
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8

    return segments


def _extract_contour_lines(raw_tile, tile_x, tile_y, zoom):
    """
    Parse a raw MVT blob, extract contour layer LineStrings with their elevation.
    Returns list of (ele: int, coords: [[lon, lat], ...]) for every contour segment.
    """
    try:
        data = zlib.decompress(raw_tile, wbits=32 + zlib.MAX_WBITS)
    except Exception:
        data = raw_tile

    results = []
    pos = 0

    while pos < len(data):
        try:
            tag, pos = _varint(data, pos)
        except IndexError:
            break
        field = tag >> 3
        wire = tag & 7
        if wire == 2:
            try:
                length, pos = _varint(data, pos)
            except IndexError:
                break
            chunk = data[pos:pos + length]; pos += length
            if field != 3:  # not a Tile.Layer
                continue
            layer_name, keys, values, raw_features, extent = _parse_mvt_layer(chunk)
            if layer_name != 'contours':
                continue
            for rf in raw_features:
                tags, geom_type, geometry = _parse_mvt_feature(rf)
                if geom_type != 2:  # LineString only
                    continue
                props = {}
                for ki in range(0, len(tags) - 1, 2):
                    k, v = tags[ki], tags[ki + 1]
                    if k < len(keys) and v < len(values):
                        props[keys[k]] = values[v]
                ele = props.get('ele')
                if ele is None:
                    continue
                try:
                    ele = int(ele)
                except (ValueError, TypeError):
                    continue
                for coords in _decode_mvt_geometry(geometry, tile_x, tile_y, zoom, extent):
                    if len(coords) >= 2:
                        results.append((ele, coords))
        elif wire == 0:
            try:
                _, pos = _varint(data, pos)
            except IndexError:
                break
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8

    return results


def _seg_intersect(p1, p2, p3, p4):
    """
    Test whether segment p1→p2 intersects segment p3→p4.
    Returns the interpolation parameter t along p1→p2 (0..1) if intersecting, else None.
    Uses 2-D planar arithmetic (valid for short segments in lon/lat space).
    """
    d1x = p2[0] - p1[0]; d1y = p2[1] - p1[1]
    d2x = p4[0] - p3[0]; d2y = p4[1] - p3[1]
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-12:
        return None  # parallel / collinear
    dx = p3[0] - p1[0]; dy = p3[1] - p1[1]
    t = (dx * d2y - dy * d2x) / denom
    u = (dx * d1y - dy * d1x) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return t
    return None


_CONTOUR_CELL = 0.005  # ~500 m grid for the spatial index


def _build_contour_index(contour_lines, cell=_CONTOUR_CELL):
    """
    Bucket contour segments into a coarse lon/lat grid.

    Without an index, crossing detection is O(path_points * contour_segments),
    which is fine for a 5 km route and hopeless for a 50 km one.  Each contour
    segment is registered in every grid cell its bounding box touches.
    """
    index = {}
    for ele, coords in contour_lines:
        for i in range(len(coords) - 1):
            c = coords[i]
            d = coords[i + 1]
            entry = (ele, c, d)
            x0 = int(math.floor(min(c[0], d[0]) / cell))
            x1 = int(math.floor(max(c[0], d[0]) / cell))
            y0 = int(math.floor(min(c[1], d[1]) / cell))
            y1 = int(math.floor(max(c[1], d[1]) / cell))
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    index.setdefault((x, y), []).append(entry)
    return index


def _contour_crossings(path_coords, contour_index, cell=_CONTOUR_CELL):
    """
    Collect every contour crossing along path_coords.

    Returns (crossings, cumulative_distances) where crossings is a sorted list
    of (distance_km_from_start, ele_m) and cumulative_distances holds the
    along-path distance of every input coordinate.
    """
    crossings = []
    cumulative = [0.0]
    cum = 0.0

    for si in range(len(path_coords) - 1):
        a = path_coords[si]
        b = path_coords[si + 1]
        seg_len = _haversine(a[0], a[1], b[0], b[1])

        x0 = int(math.floor(min(a[0], b[0]) / cell))
        x1 = int(math.floor(max(a[0], b[0]) / cell))
        y0 = int(math.floor(min(a[1], b[1]) / cell))
        y1 = int(math.floor(max(a[1], b[1]) / cell))

        seen = set()
        seg_crossings = []
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for entry in contour_index.get((x, y), ()):
                    key = id(entry)
                    if key in seen:
                        continue
                    seen.add(key)
                    ele, c, d = entry
                    t = _seg_intersect(a, b, c, d)
                    if t is not None:
                        seg_crossings.append((t, ele))

        # deduplicate crossings that land on the same spot with the same ele
        seg_crossings.sort()
        deduped = []
        for t, ele in seg_crossings:
            if deduped and abs(t - deduped[-1][0]) < 1e-6 and ele == deduped[-1][1]:
                continue
            deduped.append((t, ele))

        for t, ele in deduped:
            crossings.append((cum + t * seg_len, ele))

        cum += seg_len
        cumulative.append(cum)

    crossings.sort()
    return crossings, cumulative


def _ascent_descent(crossings):
    """Sum signed ele differences between consecutive crossings."""
    if len(crossings) < 2:
        return 0, 0
    ascent = 0.0
    descent = 0.0
    for i in range(1, len(crossings)):
        diff = crossings[i][1] - crossings[i - 1][1]
        if diff > 0:
            ascent += diff
        else:
            descent -= diff
    return int(ascent), int(descent)


def _interpolate_elevations(cumulative, crossings):
    """
    Assign an elevation to every path point by linear interpolation between
    the surrounding contour crossings.

    Accuracy is roughly +/- half the contour interval (so about 5-15 m), which
    is plenty for a GPX elevation profile but not a survey.  Returns None when
    there is nothing to interpolate from.
    """
    if len(crossings) < 2:
        return None

    elevations = []
    idx = 0
    last = len(crossings) - 1
    for dist in cumulative:
        while idx < last and crossings[idx + 1][0] < dist:
            idx += 1
        d0, e0 = crossings[idx]
        if idx >= last:
            elevations.append(float(e0))
            continue
        d1, e1 = crossings[idx + 1]
        if dist <= d0:
            elevations.append(float(e0))
        elif d1 - d0 <= 0:
            elevations.append(float(e1))
        else:
            f = (dist - d0) / (d1 - d0)
            elevations.append(e0 + (e1 - e0) * f)
    return elevations


def _elevation_profile(path_coords, contour_lines):
    """Backwards compatible wrapper: returns (ascent_m, descent_m)."""
    index = _build_contour_index(contour_lines)
    crossings, _ = _contour_crossings(path_coords, index)
    return _ascent_descent(crossings)


class RoutingError(Exception):
    """Domain error with an HTTP-ish status attached."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _duration_min(profile, dist_km, ascent_m=None, descent_m=None):
    """
    Estimated travel time in minutes.

    Without elevation data this is a flat speed model.  With ascent/descent
    from contours.mbtiles we use DIN 33466 / SAC for walking: 300 m of ascent
    or 500 m of descent per hour, then take the larger of the horizontal and
    vertical partial times plus half of the smaller one.
    """
    speed = _PROFILE_SPEED_KMH.get(profile, 4.5)
    flat = dist_km / speed * 60.0
    if profile != 'foot' or ascent_m is None:
        return flat
    vertical = (ascent_m / 300.0 + (descent_m or 0) / 500.0) * 60.0
    if vertical <= 0:
        return flat
    return max(flat, vertical) + min(flat, vertical) / 2.0


def _build_graph(all_segments, profile):
    """
    Build a bidirectional adjacency graph for *profile*.

    all_segments is the profile-agnostic output of _extract_road_segments;
    the per-class weight factor is applied here.  Result maps
    node -> [(neighbour, weighted_km, real_km), ...].
    """
    weights = _ROUTING_PROFILES.get(profile, _ROUTING_PROFILES['foot'])
    graph = {}
    for node_a, node_b, dist, road_class in all_segments:
        factor = weights.get(road_class)
        if factor is None:
            continue
        weighted = dist * factor
        graph.setdefault(node_a, []).append((node_b, weighted, dist))
        graph.setdefault(node_b, []).append((node_a, weighted, dist))
    return graph


def _nearest_node(graph, lon, lat, candidates=None):
    """
    Linear scan for the graph node closest to (lon, lat).
    Returns (key, dist_km).  If *candidates* is given, only those nodes are
    considered.
    """
    best_key = None
    best_dist = float('inf')
    for key in (graph if candidates is None else candidates):
        d = _haversine(lon, lat, key[0] / _E5, key[1] / _E5)
        if d < best_dist:
            best_dist = d
            best_key = key
    return best_key, best_dist


def _connected_components(graph):
    """
    Partition the graph into connected components, largest first.

    Vector tiles are clipped at tile borders, so a real road network decodes
    into one big component plus hundreds of small fragments (dead ends of
    ways that continue in a tile we did not load).  Snapping start or
    destination onto such a fragment yields "no route found" even though the
    points sit right next to a perfectly routable street.
    """
    seen = set()
    components = []
    for node in graph:
        if node in seen:
            continue
        seen.add(node)
        stack = [node]
        component = [node]
        while stack:
            current = stack.pop()
            for neighbour, _weighted, _real in graph.get(current, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
                    component.append(neighbour)
        components.append(component)
    components.sort(key=len, reverse=True)
    return components


def _snap_pair(graph, from_lon, from_lat, to_lon, to_lat, max_snap_km=0.5):
    """
    Snap both endpoints onto the same connected component.

    Picks the component with the smallest combined snap distance, provided
    both endpoints stay within *max_snap_km*.  Returns
    (start_node, start_km, end_node, end_km).

    Two things keep this cheap on a 30k-node graph: a single pass over all
    nodes bucketed by component id (instead of one scan per component -- a
    25-tile graph has well over a thousand of them), and a squared planar
    distance for the comparison.  Ranking by squared equirectangular distance
    gives the same nearest node as haversine at these scales, and haversine
    is only evaluated for the handful of winners, which removes ~60k calls.
    """
    components = _connected_components(graph)
    if not components:
        return None, float('inf'), None, float('inf')

    component_of = {}
    for index, component in enumerate(components):
        for node in component:
            component_of[node] = index

    # Equirectangular scale factors around the midpoint; only used for
    # ranking, so the tiny distortion over a few hundred metres is irrelevant.
    mid_lat = (from_lat + to_lat) / 2.0
    kx = 111.19 * math.cos(math.radians(mid_lat)) / _E5
    ky = 111.19 / _E5
    from_x = from_lon * _E5 * kx
    from_y = from_lat * _E5 * ky
    to_x = to_lon * _E5 * kx
    to_y = to_lat * _E5 * ky
    max_sq = max_snap_km * max_snap_km

    best_start = {}
    best_end = {}
    overall_start = (float('inf'), None)
    overall_end = (float('inf'), None)

    for node, index in component_of.items():
        node_x = node[0] * kx
        node_y = node[1] * ky

        dx = node_x - from_x
        dy = node_y - from_y
        d_start = dx * dx + dy * dy
        current = best_start.get(index)
        if current is None or d_start < current[0]:
            best_start[index] = (d_start, node)
        if d_start < overall_start[0]:
            overall_start = (d_start, node)

        dx = node_x - to_x
        dy = node_y - to_y
        d_end = dx * dx + dy * dy
        current = best_end.get(index)
        if current is None or d_end < current[0]:
            best_end[index] = (d_end, node)
        if d_end < overall_end[0]:
            overall_end = (d_end, node)

    best = None
    for index, (start_sq, start_node) in best_start.items():
        if start_sq > max_sq:
            continue
        end = best_end.get(index)
        if end is None or end[0] > max_sq:
            continue
        total = start_sq + end[0]
        if best is None or total < best[0]:
            best = (total, start_node, end[1])

    def exact(node, lon, lat):
        return _haversine(lon, lat, node[0] / _E5, node[1] / _E5)

    if best is None:
        # Nothing routable in range -- report the plain nearest nodes so the
        # caller can produce a meaningful "x m away" error message.
        start_node = overall_start[1]
        end_node = overall_end[1]
        return (start_node, exact(start_node, from_lon, from_lat),
                end_node, exact(end_node, to_lon, to_lat))

    return (best[1], exact(best[1], from_lon, from_lat),
            best[2], exact(best[2], to_lon, to_lat))


def _astar_route(graph, start, end, max_expansions=2000000):
    """
    A* shortest path. Returns (coords, real_distance_km) or (None, None),
    coords being a list of [lon, lat] pairs.

    The heuristic is a straight-line distance and never overestimates the
    weighted cost (every profile weight factor is >= 1.0), so the search stays
    admissible and the result is exactly Dijkstra's optimum.

    Do not expect a dramatic speedup though: because tiles are loaded as a
    corridor along the straight line, the graph is already restricted to
    roughly the shape of the answer, and measurements on grid-like networks
    show only about 5 % fewer expansions for an end-to-end route. A* pays off
    when the route is short compared to the loaded graph (a cache-warm 10 km
    query inside a 50 km corridor came out ~2.7x faster than Dijkstra), and
    costs at most ~1.5x on a full traversal. Absolute times are single-digit
    milliseconds either way.

    Two implementation details keep the constant factor low: the heuristic
    uses an equirectangular approximation rather than haversine (six trig
    calls per push added up to more than the saved expansions), and it is
    memoised per node since nodes usually get pushed several times.
    """
    end_lat = end[1] / _E5

    # Local equirectangular scale factors: at the given latitude one degree of
    # longitude is km_per_deg_lon wide, one degree of latitude 111.19 km.
    # 0.999 keeps the estimate below the true great-circle distance so the
    # heuristic can never overestimate.
    km_per_deg_lat = 111.19
    km_per_deg_lon = km_per_deg_lat * math.cos(math.radians(end_lat))
    kx = km_per_deg_lon / _E5 * 0.999
    ky = km_per_deg_lat / _E5 * 0.999

    h_cache = {}

    def h(node):
        cached = h_cache.get(node)
        if cached is None:
            dx = (node[0] - end[0]) * kx
            dy = (node[1] - end[1]) * ky
            cached = math.sqrt(dx * dx + dy * dy)
            h_cache[node] = cached
        return cached

    open_set = [(h(start), 0.0, start)]
    came_from = {}
    g_score = {start: 0.0}
    expansions = 0

    while open_set:
        _, g, current = heapq.heappop(open_set)

        if g > g_score.get(current, float('inf')):
            continue

        if current == end:
            path = [end]
            node = end
            while node in came_from:
                node = came_from[node]
                path.append(node)
            path.reverse()
            coords = [list(_node_lonlat(n)) for n in path]
            real_km = sum(
                _haversine(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
                for i in range(len(coords) - 1)
            )
            return coords, real_km

        expansions += 1
        if expansions > max_expansions:
            return None, None

        for neighbor, weighted, _real in graph.get(current, []):
            new_g = g + weighted
            if new_g < g_score.get(neighbor, float('inf')):
                g_score[neighbor] = new_g
                came_from[neighbor] = current
                heapq.heappush(open_set, (new_g + h(neighbor), new_g, neighbor))

    return None, None


# ---------------------------------------------------------------------------


def simple_mbtiles_server(
        logger,
        exit_stack,
        port,
        mbtiles,
        http_access_control_allow_origin,
        photon_server=None,
        tile_cache_size=2000,
        gpx_dir=None,
        gpx_max_files=200,
        gpx_ttl_seconds=86400,
        route_max_tiles=1200,
        route_max_crow_km=50.0,
):
    server = None

    http_client = exit_stack.enter_context(httpx.Client(limits=httpx.Limits(max_connections=500)))

    # So we can share a single http client (i.e. a single pool of connections) for
    # all instances of sqlite_s3_query
    @contextmanager
    def get_http_client():
        yield http_client

    def read(path):
        real_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), path)
        with open(real_path, 'rb') as f:
            return f.read()

    def extract(path):
        tempdir = exit_stack.enter_context(tempfile.TemporaryDirectory())
        real_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), path)
        with tarfile.open(real_path, 'r:gz') as f:
            f.extractall(tempdir)
        return tempdir

    mbtiles_dict = {
        (mbtile['IDENTIFIER'], mbtile['VERSION']): {
            'db_connection': sqlite3.connect(mbtile['URL']),
            'min_zoom': int(mbtile['MIN_ZOOM']),
            'max_zoom': int(mbtile['MAX_ZOOM']),
        }
        for mbtile in mbtiles
    }

    contours_dict = {}
    if mbtiles:
        first_mbtiles_url = mbtiles[0].get('URL')
        if first_mbtiles_url:
            contours_path = os.path.join(os.path.dirname(first_mbtiles_url), 'contours.mbtiles')
            if os.path.exists(contours_path):
                contours_dict = {
                    'db_connection': sqlite3.connect(contours_path),
                    'url': contours_path,
                }

    styles_dict = {
        (style_id, style_version): {
            file_name: read(f'vendor/{style_id}@{style_version}/{file_name}')
            for file_name in file_names
        }
        for (style_id, style_version, file_names) in (
            ('dark-matter-gl-style', '1.0.0', ('style.json', 'sprite.json',
             'sprite.png', 'sprite@2x.json', 'sprite@2x.png',)),
            ('fiord-color-gl-style', '1.0.0', ('style.json', 'sprite.json',
             'sprite.png', 'sprite@2x.json', 'sprite@2x.png',)),
            ('maptiler-3d-gl-style', '1.0.0', ('style.json',)),
            ('maptiler-terrain-gl-style', '1.0.0', ('style.json',)),
            ('maptiler-basic-gl-style', '1.0.0', ('style.json',)),
            ('maptiler-terrain-gl-style', '1.0.0', ('style.json',)),
            ('maptiler-toner-gl-style', '1.0.0', ('style.json', 'sprite.json',
             'sprite.png', 'sprite@2x.json', 'sprite@2x.png',)),
            ('osm-bright-gl-style', '1.0.0', ('style.json', 'sprite.json',
             'sprite.png', 'sprite@2x.json', 'sprite@2x.png',)),

            ('positron-gl-style', '1.0.0', ('style.json', 'sprite.json',
             'sprite.png', 'sprite@2x.json', 'sprite@2x.png',)),
            ('osuv-style', '1.0.0', ('style.json',)),
        )
    }

    statics_dict = {
        ('maplibre-gl', '5.19.0', 'maplibre-gl.css'): {
            'bytes': read('vendor/maplibre-gl@5.19.0/maplibre-gl.css'),
            'mime': 'text/css',
        },
        ('maplibre-gl', '5.19.0', 'maplibre-gl.js'): {
            'bytes': read('vendor/maplibre-gl@5.19.0/maplibre-gl.js'),
            'mime': 'application/javascript',
        },
    }

    fonts_dict = {
        ('fonts-gl', '1.0.0'):
        extract('vendor/fonts-gl@1.0.0/fonts.tar.gz')
    }

    # Decoded-tile caches. Routing and elevation both re-read the same tiles
    # over and over while an agent iterates on a tour, and MVT decoding in
    # pure Python is by far the most expensive part of a request.
    road_cache = LruCache(tile_cache_size)
    contour_cache = LruCache(max(1, tile_cache_size // 4))
    poi_cache = LruCache(max(1, tile_cache_size // 4))

    gpx_store = GpxStore(
        gpx_dir or os.path.join(tempfile.gettempdir(), 'sms-gpx'),
        max_files=gpx_max_files,
        ttl_seconds=gpx_ttl_seconds,
    )
    logger.info('GPX exports are written to %s', gpx_store.directory)

    def start():
        server.serve_forever()

    def stop():
        server.stop()

    app = Flask('app')
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_for=0, x_host=0, x_port=0, x_prefix=0)

    sql = '''
        SELECT
            tile_data
        FROM
            tiles
        WHERE
            zoom_level=? AND
            tile_column=? AND
            tile_row=?
        LIMIT 1
    '''

    def get_tile(identifier, version, z, x, y):
        if identifier == 'contours' and version == '1.0.0' and contours_dict:
            db_connection = contours_dict['db_connection']
        else:
            try:
                db_connection = mbtiles_dict[(identifier, version)]['db_connection']
            except KeyError:
                return Response(status=404)

        tile_data = None
        y_tms = (2**z - 1) - y

        cursor = db_connection.cursor()
        cursor.execute(sql, (z, x, y_tms))
        rows = cursor.fetchall()
        cursor.close()

        if not rows and identifier == 'contours' and version == '1.0.0' and z == 14:
            z_fb = 13
            x_fb = x // 2
            y_fb = y // 2
            y_tms_fb = (2**z_fb - 1) - y_fb
            cursor2 = db_connection.cursor()
            cursor2.execute(sql, (z_fb, x_fb, y_tms_fb))
            rows = cursor2.fetchall()
            cursor2.close()

        if rows:
            tile_data = rows[0][0]  # Get the tile data from the first row


        allows_gzip = 'gzip' in request.headers.get(
            'accept-encoding', '').replace(' ', '').split(',')

        def ungzip(data):
            return zlib.decompress(data, wbits=32 + zlib.MAX_WBITS)

        return \
            Response(status=200, response=ungzip(tile_data), headers={
                'content-type': 'application/vnd.mapbox-vector-tile',
            }) if tile_data is not None and not allows_gzip else \
            Response(status=200, response=tile_data, headers={
                'content-encoding': 'gzip',
                'content-type': 'application/vnd.mapbox-vector-tile',
            }) if tile_data is not None else \
            Response(status=404)

    def get_styles(identifier, version):
        try:
            style_bytes = styles_dict[(identifier, version)]['style.json']
        except KeyError:
            return Response(status=404)

        try:
            tiles_identifier_with_version = request.args['tiles']
        except KeyError:
            return Response(status=400)

        try:
            tiles_identifier, tiles_version = tiles_identifier_with_version.split('@')
        except ValueError:
            return Response(status=400)

        try:
            tiles = mbtiles_dict[(tiles_identifier, tiles_version)]
        except KeyError:
            return Response(status=404)

        try:
            fonts_identifier_with_version = request.args['fonts']
        except KeyError:
            return Response(status=400)

        try:
            fonts_identifier, fonts_version = fonts_identifier_with_version.split('@')
        except ValueError:
            return Response(status=400)

        try:
            fonts_dict[(fonts_identifier, fonts_version)]
        except KeyError:
            return Response(status=404)

        style_dict = json.loads(style_bytes)
        style_dict['sources']['openmaptiles'] = {
            'type': 'vector',
            'tiles': [
                request.url_root + 'v1/tiles/' + tiles_identifier_with_version + '/{z}/{x}/{y}.mvt'
            ],
            'minzoom': tiles['min_zoom'],
            'maxzoom': tiles['max_zoom'],
        }
        style_dict['glyphs'] = request.url_root + 'v1/fonts/' + \
            fonts_identifier_with_version + '/{fontstack}/{range}.pbf'

        if contours_dict:
            style_dict['sources']['contours'] = {
                'type': 'vector',
                'tiles': [
                    request.url_root + 'v1/tiles/contours@1.0.0/{z}/{x}/{y}.mvt'
                ],
                'minzoom': 0,
                'maxzoom': 14,
            }

            is_50m = ['==', ['%', ['to-number', ['get', 'ele']], 50], 0]
            is_minor = ['!=', ['%', ['to-number', ['get', 'ele']], 50], 0]

            # Zwischenlinien (10hm): heller, transparenter
            contour_line_minor = {
                'id': 'contour-line-minor',
                'type': 'line',
                'source': 'contours',
                'source-layer': 'contours',
                'minzoom': 10,
                'filter': is_minor,
                'layout': {
                    'line-join': 'round',
                    'visibility': 'none',
                },
                'paint': {
                    'line-color': '#b89070',
                    'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.5, 14, 0.9],
                    'line-opacity': ['interpolate', ['linear'], ['zoom'], 10, 0.25, 14, 0.4],
                },
            }

            # Index-Linien (50hm): kräftig, gut sichtbar
            contour_line_major = {
                'id': 'contour-line-major',
                'type': 'line',
                'source': 'contours',
                'source-layer': 'contours',
                'minzoom': 10,
                'filter': is_50m,
                'layout': {
                    'line-join': 'round',
                    'visibility': 'none',
                },
                'paint': {
                    'line-color': '#8b5a2b',
                    'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1.2, 14, 2.0],
                    'line-opacity': ['interpolate', ['linear'], ['zoom'], 10, 0.5, 14, 1.0],
                },
            }

            # Beschriftung nur auf 50hm-Linien
            contour_label_layer = {
                'id': 'contour-label',
                'type': 'symbol',
                'source': 'contours',
                'source-layer': 'contours',
                'minzoom': 12,
                'filter': is_50m,
                'layout': {
                    'symbol-placement': 'line',
                    'symbol-spacing': 400,
                    'text-field': ['concat', ['to-string', ['get', 'ele']], ' m'],
                    'text-font': ['Noto Sans Regular'],
                    'text-size': ['interpolate', ['linear'], ['zoom'], 12, 9, 14, 11],
                    'visibility': 'none',
                },
                'paint': {
                    'text-color': '#6b5a4b',
                    'text-halo-color': '#fff',
                    'text-halo-width': 1,
                },
            }

            style_dict['layers'].extend([contour_line_minor, contour_line_major, contour_label_layer])

        if 'sprite' in style_dict:
            style_dict['sprite'] = request.url_root + 'v1/styles/' + \
                identifier + '@' + version + '/sprite'

        return Response(status=200, content_type='application/json',
                        response=json.dumps(style_dict))

    def get_sprite_file(identifier, version, file, content_type):
        try:
            sprite_bytes = styles_dict[(identifier, version)][file]
        except KeyError:
            return Response(status=404)

        return Response(status=200, content_type=content_type, response=sprite_bytes)

    def get_sprite_1x_json(identifier, version):
        return get_sprite_file(identifier, version, 'sprite.json', 'application/json')

    def get_sprite_2x_json(identifier, version):
        return get_sprite_file(identifier, version, 'sprite@2x.json', 'application/json')

    def get_sprite_1x_png(identifier, version):
        return get_sprite_file(identifier, version, 'sprite.png', 'image/png')

    def get_sprite_2x_png(identifier, version):
        return get_sprite_file(identifier, version, 'sprite@2x.png', 'image/png')

    def get_fonts(identifier, version, stack, range):
        # Combines all the fonts in the requested stack, but only include one glyph for each id
        # Although the format does seem to assume a file can itself have multiple "stacks", we
        # only look at the first from each. This is wrong if a file has multiple stacks

        def read(path):
            with open(path, 'rb') as f:
                return f.read()

        def parse_pbf(buffer):
            g = glyphs()
            g.ParseFromString(buffer)
            return g

        try:
            font_path = fonts_dict[(identifier, version)]
        except KeyError:
            return Response(status=404)

        if '.' in stack or '.' in range:
            return Response(status=404)

        fonts = stack.split(',')

        # So a request can't use too much CPU. So far haven't seen a stack that
        # that has more than 2, so this should be plenty for real uses
        if len(fonts) >= 5:
            return Response(status=400)

        try:
            pbfs = tuple((
                parse_pbf(zlib.decompress(
                    read(os.path.join(font_path, font, range + '.pbf.gz')),
                    wbits=32 + zlib.MAX_WBITS
                ))
                for font in fonts
            ))
        except FileNotFoundError:
            return Response(status=404)

        pdf_combined_glyphs_ids = set()
        pdf_combined_glyphs = []
        pbf_combined = glyphs()
        pbf_combined_stack = pbf_combined.stacks.add()
        pbf_combined_stack.name = stack
        pbf_combined_stack.range = pbfs[0].stacks[0].range

        for pbf in pbfs:
            for glyph in pbf.stacks[0].glyphs:
                if glyph.id not in pdf_combined_glyphs_ids:
                    pdf_combined_glyphs_ids.add(glyph.id)
                    pdf_combined_glyphs.append(glyph)

        for glyph in sorted(pdf_combined_glyphs, key=lambda g: g.id):
            pbf_combined_stack.glyphs.append(glyph)

        serialized = pbf_combined.SerializeToString()
        allows_gzip = 'gzip' in request.headers.get(
            'accept-encoding', '').replace(' ', '').split(',')

        def gzip(data):
            compress_obj = zlib.compressobj(wbits=31)
            return compress_obj.compress(serialized) + compress_obj.flush()

        return \
            Response(status=200, headers={
                'content-encoding': 'gzip',
                'content-type': 'application/vnd.google.protobuf',
            }, response=gzip(serialized)) if allows_gzip else \
            Response(status=200, headers={
                'content-type': 'application/vnd.google.protobuf',
            }, response=serialized)

    def get_static(identifier, version, file):
        try:
            static_dict = statics_dict[(identifier, version, file)]
        except KeyError:
            return Response(status=404)

        return Response(status=200, content_type=static_dict['mime'],
                        response=static_dict['bytes'])

    # ------------------------------------------------------------------
    # Tile loading with cache
    # ------------------------------------------------------------------

    def _load_tile_blob(db_connection, z, x, y):
        y_tms = (2 ** z - 1) - y
        cursor = db_connection.cursor()
        cursor.execute(sql, (z, x, y_tms))
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else None

    def _road_segments_for_tiles(identifier, version, tiles):
        """
        Decoded transportation segments for a list of (z, x, y) tiles.
        Returns (segments, cache_hits, cache_misses).
        """
        db_connection = mbtiles_dict[(identifier, version)]['db_connection']
        segments = []
        hits = misses = 0
        for (z, x, y) in tiles:
            key = (identifier, version, z, x, y)
            cached = road_cache.get(key)
            if cached is None:
                misses += 1
                blob = _load_tile_blob(db_connection, z, x, y)
                cached = _extract_road_segments(blob, x, y, z) if blob else []
                road_cache.put(key, cached)
            else:
                hits += 1
            segments.extend(cached)
        return segments, hits, misses

    def _contour_lines_for_tiles(tiles):
        """Decoded contour lines for a list of (z, x, y) tiles."""
        db_connection = contours_dict['db_connection']
        lines = []
        hits = misses = 0
        for (z, x, y) in tiles:
            key = (z, x, y)
            cached = contour_cache.get(key)
            if cached is None:
                misses += 1
                blob = _load_tile_blob(db_connection, z, x, y)
                if blob is None and z == 14:
                    # contours.mbtiles may only carry z13 for this area
                    blob = _load_tile_blob(db_connection, 13, x // 2, y // 2)
                    cached = _extract_contour_lines(blob, x // 2, y // 2, 13) if blob else []
                else:
                    cached = _extract_contour_lines(blob, x, y, z) if blob else []
                contour_cache.put(key, cached)
            else:
                hits += 1
            lines.extend(cached)
        return lines, hits, misses

    # ------------------------------------------------------------------
    # Routing core (shared by the REST endpoint and the MCP tools)
    # ------------------------------------------------------------------

    def compute_route(identifier, version, from_lat, from_lon, to_lat, to_lon,
                      profile='foot', buffer_km=None, with_elevation=False):
        """
        Route one segment from (from_lat, from_lon) to (to_lat, to_lon).

        Tiles are loaded as a corridor along the straight line instead of a
        bounding box: on a diagonal route a bbox wastes about 70 % of the
        decoded tiles.  Raises RoutingError on any expected failure.
        """
        if profile not in _ROUTING_PROFILES:
            raise RoutingError('unknown profile "%s"; use foot or bike' % profile, 400)

        if (identifier, version) not in mbtiles_dict:
            raise RoutingError('unknown tileset %s@%s' % (identifier, version), 404)

        zoom = 14
        crow_km = _haversine(from_lon, from_lat, to_lon, to_lat)
        if crow_km > route_max_crow_km:
            raise RoutingError(
                'straight-line distance %.1f km exceeds the %.0f km limit per segment; '
                'split the tour with additional waypoints'
                % (crow_km, route_max_crow_km), 400)

        buffer_tiles = None
        if buffer_km is not None:
            tile_km = _tile_width_km((from_lat + to_lat) / 2.0, zoom)
            buffer_tiles = max(1.0, float(buffer_km) / max(tile_km, 0.001))

        tiles, buffer_used = _tiles_along_line(
            from_lat, from_lon, to_lat, to_lon, zoom, buffer_tiles)

        if len(tiles) > route_max_tiles:
            raise RoutingError(
                'corridor too large (%d tiles, limit %d); reduce the buffer or '
                'insert a waypoint' % (len(tiles), route_max_tiles), 400)

        all_segments, hits, misses = _road_segments_for_tiles(identifier, version, tiles)

        if not all_segments:
            raise RoutingError('no road network found in area', 404)

        graph = _build_graph(all_segments, profile)
        if not graph:
            raise RoutingError('no way passable for profile "%s" in area' % profile, 404)

        # Snap both ends onto the *same* connected component. Tiles are
        # clipped at their borders, so the decoded network contains hundreds
        # of small fragments; the geometrically nearest node is regularly a
        # dead end that is not connected to anything.
        start_node, start_snap_km, end_node, end_snap_km = _snap_pair(
            graph, from_lon, from_lat, to_lon, to_lat)

        if start_snap_km > 0.5:
            raise RoutingError(
                'no routable way within 500 m of start %.5f,%.5f (closest: %.0f m)'
                % (from_lat, from_lon, start_snap_km * 1000), 404)
        if end_snap_km > 0.5:
            raise RoutingError(
                'no routable way within 500 m of destination %.5f,%.5f (closest: %.0f m)'
                % (to_lat, to_lon, end_snap_km * 1000), 404)
        if start_node == end_node:
            raise RoutingError('start and destination snap to the same node', 400)

        path_coords, dist_km = _astar_route(graph, start_node, end_node)

        if path_coords is None:
            raise RoutingError(
                'no route found between %.5f,%.5f and %.5f,%.5f -- start and '
                'destination are on separate parts of the network; try a '
                'waypoint or a larger buffer_km'
                % (from_lat, from_lon, to_lat, to_lon), 404)

        properties = {
            'distance_km': round(dist_km, 3),
            'profile': profile,
            'tiles_loaded': len(tiles),
            'buffer_tiles': round(buffer_used, 2),
            'nodes': len(graph),
            'cache_hits': hits,
            'cache_misses': misses,
            'snap_start_m': round(start_snap_km * 1000, 1),
            'snap_end_m': round(end_snap_km * 1000, 1),
        }

        ascent = descent = None
        if with_elevation and contours_dict:
            ascent, descent, _ = compute_elevation(path_coords)
            properties['ascent_m'] = ascent
            properties['descent_m'] = descent

        properties['duration_min'] = round(
            _duration_min(profile, dist_km, ascent, descent), 1)

        return {
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': path_coords},
            'properties': properties,
        }

    def compute_elevation(coords):
        """
        Ascent/descent along a [lon, lat] path using contours.mbtiles.
        Returns (ascent_m, descent_m, per_point_elevations_or_None).
        """
        if not contours_dict:
            raise RoutingError('no contours available', 404)

        zoom = 14
        tiles = _tiles_along_path(coords, zoom, buffer_tiles=1.0)
        if len(tiles) > route_max_tiles:
            raise RoutingError(
                'area too large (%d tiles, limit %d)' % (len(tiles), route_max_tiles), 400)

        contour_lines, _hits, _misses = _contour_lines_for_tiles(tiles)
        if not contour_lines:
            return 0, 0, None

        index = _build_contour_index(contour_lines)
        crossings, cumulative = _contour_crossings(coords, index)
        ascent, descent = _ascent_descent(crossings)
        elevations = _interpolate_elevations(cumulative, crossings)
        return ascent, descent, elevations

    def search_poi(identifier, version, lat, lon, category, radius_km=15.0, limit=None):
        """POIs of *category* around (lat, lon) as a list of GeoJSON features."""
        if category not in _POI_CATEGORY_FILTERS:
            raise RoutingError(
                'unknown category "%s"; available: %s'
                % (category, ', '.join(sorted(_POI_CATEGORY_FILTERS))), 400)

        if (identifier, version) not in mbtiles_dict:
            raise RoutingError('unknown tileset %s@%s' % (identifier, version), 404)

        db_connection = mbtiles_dict[(identifier, version)]['db_connection']
        radius_km = min(max(float(radius_km), 0.1), 50.0)
        tiles = _tiles_in_radius(lat, lon, radius_km, zoom=14, max_tiles=100)

        seen = set()
        features = []
        for (z, x, y) in tiles:
            key = ('poi', identifier, version, z, x, y)
            cached = poi_cache.get(key)
            if cached is None:
                blob = _load_tile_blob(db_connection, z, x, y)
                cached = _parse_mvt_poi(blob, x, y, z, None) if blob else []
                poi_cache.put(key, cached)
            for f in cached:
                if not _matches_poi_category(f['properties'], category):
                    continue
                coords = f['geometry']['coordinates']
                dedupe_key = (round(coords[0], 6), round(coords[1], 6))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                dist = _haversine(lon, lat, coords[0], coords[1])
                if dist > radius_km:
                    continue
                enriched = dict(f)
                props = dict(f['properties'])
                props['distance_km'] = round(dist, 3)
                enriched['properties'] = props
                features.append(enriched)

        # Sort by distance, but prefer named POIs inside the same rough
        # distance band: an unnamed hut 200 m closer is much less useful to
        # the caller than a named one they can look up.
        features.sort(key=lambda f: (
            round(f['properties']['distance_km'] * 2) / 2,
            0 if _poi_name(f['properties']) else 1,
            f['properties']['distance_km'],
        ))
        if limit:
            features = features[:int(limit)]
        return features

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    def _json(payload, status=200):
        return Response(status=status, content_type='application/json',
                        response=json.dumps(payload))

    def get_route(identifier, version):
        try:
            from_lat, from_lon = map(float, request.args['from'].split(','))
            to_lat, to_lon = map(float, request.args['to'].split(','))
        except (KeyError, ValueError):
            return _json({'error': 'invalid parameters; expected '
                                   'from=lat,lon&to=lat,lon&profile=foot|bike'}, 400)

        profile = request.args.get('profile', 'foot')
        with_elevation = request.args.get('elevation', '').lower() in ('1', 'true', 'yes')

        try:
            buffer_km = request.args.get('buffer_km')
            buffer_km = float(buffer_km) if buffer_km else None
        except ValueError:
            return _json({'error': 'buffer_km must be a number'}, 400)

        try:
            feature = compute_route(identifier, version, from_lat, from_lon,
                                    to_lat, to_lon, profile, buffer_km, with_elevation)
        except RoutingError as exc:
            return _json({'error': exc.message}, exc.status)

        return _json(feature)

    def get_elevation(identifier, version):
        """
        POST /v1/elevation/<id>@<ver>
        Body: {"coordinates": [[lon, lat], ...]}
        Returns: {"ascent_m": int, "descent_m": int}

        Ascent/descent come from intersecting the LineString with contour
        lines out of contours.mbtiles, so they are only available when that
        file is present.
        """
        try:
            body = request.get_json(force=True, silent=True) or {}
            coords = body.get('coordinates', [])
            if len(coords) < 2:
                raise ValueError('need at least 2 coordinates')
            coords = [[float(c[0]), float(c[1])] for c in coords]
        except Exception as exc:
            return _json({'error': 'invalid body: %s' % exc}, 400)

        try:
            ascent, descent, _elevations = compute_elevation(coords)
        except RoutingError as exc:
            return _json({'error': exc.message}, exc.status)

        return _json({'ascent_m': ascent, 'descent_m': descent})

    def get_poi(identifier, version):
        try:
            lat = float(request.args['lat'])
            lon = float(request.args['lon'])
            category = request.args['category']
        except (KeyError, ValueError):
            return Response(status=400)

        try:
            radius = float(request.args.get('radius', 15))
        except ValueError:
            return Response(status=400)

        try:
            features = search_poi(identifier, version, lat, lon, category, radius)
        except RoutingError as exc:
            return _json({'error': exc.message}, exc.status)

        return _json({'type': 'FeatureCollection', 'features': features})

    def get_gpx(gpx_id):
        xml = gpx_store.read(gpx_id)
        if xml is None:
            return _json({'error': 'unknown or expired gpx id'}, 404)
        return Response(status=200, response=xml, headers={
            'content-type': 'application/gpx+xml',
            'content-disposition': 'attachment; filename="%s.gpx"' % gpx_id,
        })

    def get_index():
        return send_from_directory(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'vendor'), 'index.html')

    def get_capabilities():
        return Response(status=200, content_type='application/json', response=json.dumps({
            'contours': bool(contours_dict),
            'routing':  True,
            'mcp':      True,
            'gpx':      True,
            'geocoding': bool(photon_server),
            'mcp_tools': mcp_server.tool_names(),
            'routing_limits': {
                'max_tiles': route_max_tiles,
                'max_crow_km': route_max_crow_km,
                'zoom': 14,
            },
            'tile_cache': {
                'roads': road_cache.stats(),
                'contours': contour_cache.stats(),
                'poi': poi_cache.stats(),
            },
        }))

    # ------------------------------------------------------------------
    # MCP tools
    # ------------------------------------------------------------------

    default_tiles = (mbtiles[0]['IDENTIFIER'], mbtiles[0]['VERSION']) if mbtiles else (None, None)

    _SAFETY_NOTE = (
        'Data source is OpenMapTiles vector tiles, which do NOT contain '
        'sac_scale, trail_visibility, via_ferrata_scale, ele or surface. '
        'Difficulty of a path is therefore UNKNOWN -- a T1 stroll and a T5 '
        'scramble look identical here. Never present a route as safe or '
        'beginner friendly, and tell the user to cross-check the track '
        'against a topographic map before walking it.'
    )

    def _tiles_from_args(args):
        identifier = args.get('tileset')
        if not identifier:
            if default_tiles[0] is None:
                raise ToolError('no tileset configured on this server')
            return default_tiles
        if '@' in identifier:
            ident, ver = identifier.split('@', 1)
        else:
            ident, ver = identifier, '1.0.0'
        if (ident, ver) not in mbtiles_dict:
            raise ToolError('unknown tileset "%s"; available: %s' % (
                identifier, ', '.join('%s@%s' % k for k in mbtiles_dict)))
        return ident, ver

    def _latlon(value, label):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ToolError('%s must be [lat, lon]' % label)
        try:
            lat, lon = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            raise ToolError('%s must contain two numbers' % label)
        if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
            raise ToolError('%s out of range: %s (expected [lat, lon])' % (label, value))
        return lat, lon

    def _external_url(path):
        try:
            return request.url_root.rstrip('/') + path
        except RuntimeError:
            return path

    def _photon_get(path, params):
        if not photon_server:
            raise ToolError('geocoding is not configured (PHOTONSERVER is unset)')
        url = photon_server.rstrip('/') + path
        try:
            resp = http_client.get(url, params=params, timeout=15.0)
        except httpx.ConnectError as exc:
            # Classic container pitfall: PHOTONSERVER resolves to a LAN
            # address that the container network cannot route to. Say so,
            # instead of leaking a bare "Connection refused".
            raise ToolError(
                'cannot reach the Photon server at %s (%s). If sms runs in a '
                'container and PHOTONSERVER points into your LAN, the '
                'container network has no route there -- try --network=host '
                'or a Photon address reachable from inside the container.'
                % (photon_server, exc))
        except httpx.TimeoutException as exc:
            raise ToolError('Photon server at %s timed out (%s)' % (photon_server, exc))
        except httpx.HTTPError as exc:
            raise ToolError('photon request to %s failed: %s' % (photon_server, exc))
        if resp.status_code != 200:
            raise ToolError('Photon server at %s returned HTTP %d'
                            % (photon_server, resp.status_code))
        try:
            return resp.json()
        except ValueError:
            raise ToolError('Photon server at %s returned a non-JSON response'
                            % photon_server)

    def _photon_features(payload, limit):
        results = []
        for feature in (payload.get('features') or [])[:limit]:
            props = feature.get('properties') or {}
            coords = (feature.get('geometry') or {}).get('coordinates') or [None, None]
            results.append({
                'name': props.get('name'),
                'lat': coords[1],
                'lon': coords[0],
                'type': props.get('osm_value') or props.get('type'),
                'street': props.get('street'),
                'housenumber': props.get('housenumber'),
                'postcode': props.get('postcode'),
                'city': props.get('city') or props.get('district'),
                'state': props.get('state'),
                'country': props.get('country'),
            })
        return results

    def tool_geocode(args):
        query = args.get('query')
        if not isinstance(query, str) or not query.strip():
            raise ToolError('query is required')
        limit = int(args.get('limit') or 5)
        params = {'q': query, 'limit': max(1, min(limit, 20))}
        if args.get('near'):
            lat, lon = _latlon(args['near'], 'near')
            params['lat'] = lat
            params['lon'] = lon
        payload = _photon_get('/api/', params)
        return {'query': query, 'results': _photon_features(payload, params['limit'])}

    def tool_reverse_geocode(args):
        lat, lon = _latlon(args.get('coordinate'), 'coordinate')
        limit = max(1, min(int(args.get('limit') or 1), 10))
        payload = _photon_get('/reverse', {'lat': lat, 'lon': lon, 'limit': limit})
        return {'coordinate': [lat, lon], 'results': _photon_features(payload, limit)}

    def tool_search_poi(args):
        lat, lon = _latlon(args.get('center'), 'center')
        category = args.get('category')
        if not isinstance(category, str):
            raise ToolError('category is required; available: %s'
                            % ', '.join(sorted(_POI_CATEGORY_FILTERS)))
        radius_km = float(args.get('radius_km') or 15)
        limit = max(1, min(int(args.get('limit') or 20), 100))
        identifier, version = _tiles_from_args(args)

        try:
            features = search_poi(identifier, version, lat, lon, category, radius_km, limit)
        except RoutingError as exc:
            raise ToolError(exc.message)

        results = []
        for f in features:
            props = f['properties']
            coords = f['geometry']['coordinates']
            results.append({
                'name': _poi_name(props),
                'lat': round(coords[1], 6),
                'lon': round(coords[0], 6),
                'category': props.get('subclass') or props.get('class'),
                'distance_km': props['distance_km'],
            })
        return {
            'center': [lat, lon],
            'category': category,
            'radius_km': radius_km,
            'count': len(results),
            'results': results,
        }

    def tool_plan_route(args):
        raw_waypoints = args.get('waypoints')
        if not isinstance(raw_waypoints, (list, tuple)) or len(raw_waypoints) < 2:
            raise ToolError('waypoints must be a list of at least two [lat, lon] pairs')
        if len(raw_waypoints) > 20:
            raise ToolError('at most 20 waypoints are supported')

        waypoints = [
            _latlon(wp, 'waypoints[%d]' % i)
            for i, wp in enumerate(raw_waypoints)
        ]

        profile = args.get('profile') or 'foot'
        if profile not in _ROUTING_PROFILES:
            raise ToolError('unknown profile "%s"; use foot or bike' % profile)

        buffer_km = args.get('buffer_km')
        buffer_km = float(buffer_km) if buffer_km else None
        identifier, version = _tiles_from_args(args)
        want_elevation = bool(args.get('elevation', True)) and bool(contours_dict)
        want_gpx = bool(args.get('export_gpx', True))
        name = args.get('name') or 'sms tour'

        coords = []
        segments = []
        errors = []
        total_km = 0.0
        cache_hits = cache_misses = 0
        tiles_loaded = 0

        for i in range(len(waypoints) - 1):
            (from_lat, from_lon) = waypoints[i]
            (to_lat, to_lon) = waypoints[i + 1]
            try:
                feature = compute_route(identifier, version, from_lat, from_lon,
                                        to_lat, to_lon, profile, buffer_km, False)
            except RoutingError as exc:
                errors.append('segment %d (%.5f,%.5f -> %.5f,%.5f): %s'
                              % (i + 1, from_lat, from_lon, to_lat, to_lon, exc.message))
                # Partial results are more useful to an agent than nothing, so
                # keep going with the remaining segments.
                segments.append({
                    'segment': i + 1,
                    'from': [from_lat, from_lon],
                    'to': [to_lat, to_lon],
                    'error': exc.message,
                })
                continue

            props = feature['properties']
            seg_coords = feature['geometry']['coordinates']
            total_km += props['distance_km']
            tiles_loaded += props['tiles_loaded']
            cache_hits += props['cache_hits']
            cache_misses += props['cache_misses']
            segments.append({
                'segment': i + 1,
                'from': [from_lat, from_lon],
                'to': [to_lat, to_lon],
                'distance_km': props['distance_km'],
                'points': len(seg_coords),
            })
            if coords and seg_coords and coords[-1] == seg_coords[0]:
                coords.extend(seg_coords[1:])
            else:
                coords.extend(seg_coords)

        if not coords:
            raise ToolError('no segment could be routed. ' + ' | '.join(errors))

        ascent = descent = None
        elevations = None
        if want_elevation:
            try:
                ascent, descent, elevations = compute_elevation(coords)
            except RoutingError as exc:
                errors.append('elevation: %s' % exc.message)

        duration_min = _duration_min(profile, total_km, ascent, descent)

        result = {
            'name': name,
            'profile': profile,
            'distance_km': round(total_km, 2),
            'duration_min': round(duration_min, 0),
            'points': len(coords),
            'segments': segments,
            'waypoints': [[lat, lon] for lat, lon in waypoints],
            'tiles_loaded': tiles_loaded,
            'cache_hits': cache_hits,
            'cache_misses': cache_misses,
            'warnings': [_SAFETY_NOTE],
        }
        if ascent is not None:
            result['ascent_m'] = ascent
            result['descent_m'] = descent
            result['duration_note'] = 'DIN 33466 estimate (300 m ascent / 500 m descent per hour)'
        else:
            result['duration_note'] = 'flat estimate at %.1f km/h, no elevation surcharge' \
                % _PROFILE_SPEED_KMH.get(profile, 4.5)
        if errors:
            result['errors'] = errors

        if want_gpx:
            wpts = [
                {'lat': lat, 'lon': lon, 'name': 'WP%d' % (i + 1)}
                for i, (lat, lon) in enumerate(waypoints)
            ]
            xml = to_gpx(coords, name=name, wpts=wpts, elevations=elevations)
            gpx_id = gpx_store.write(xml)
            result['gpx_id'] = gpx_id
            result['gpx_url'] = _external_url('/v1/gpx/%s.gpx' % gpx_id)

        # The track geometry itself is deliberately NOT returned: a 3000 point
        # LineString as tool text blows up any context window. The agent gets
        # metadata plus a URL, the human clicks the URL.
        return result

    def tool_export_gpx(args):
        raw_coords = args.get('coordinates')
        if not isinstance(raw_coords, (list, tuple)) or len(raw_coords) < 2:
            raise ToolError('coordinates must be a list of at least two [lon, lat] pairs')
        if len(raw_coords) > 100000:
            raise ToolError('too many coordinates (limit 100000)')

        coords = []
        for i, c in enumerate(raw_coords):
            if not isinstance(c, (list, tuple)) or len(c) < 2:
                raise ToolError('coordinates[%d] must be [lon, lat]' % i)
            try:
                coords.append([float(c[0]), float(c[1])])
            except (TypeError, ValueError):
                raise ToolError('coordinates[%d] must contain two numbers' % i)

        wpts = []
        for i, wp in enumerate(args.get('waypoints') or []):
            if isinstance(wp, dict):
                lat = wp.get('lat')
                lon = wp.get('lon')
                if lat is None or lon is None:
                    raise ToolError('waypoints[%d] needs lat and lon' % i)
                wpts.append({'lat': float(lat), 'lon': float(lon),
                             'name': wp.get('name') or 'WP%d' % (i + 1)})
            else:
                lat, lon = _latlon(wp, 'waypoints[%d]' % i)
                wpts.append({'lat': lat, 'lon': lon, 'name': 'WP%d' % (i + 1)})

        name = args.get('name') or 'sms track'
        elevations = None
        ascent = descent = None
        if args.get('elevation') and contours_dict:
            try:
                ascent, descent, elevations = compute_elevation(coords)
            except RoutingError:
                pass

        dist_km = sum(
            _haversine(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
            for i in range(len(coords) - 1)
        )
        profile = args.get('profile') or 'foot'
        gpx_id = gpx_store.write(to_gpx(coords, name=name, wpts=wpts, elevations=elevations))

        result = {
            'name': name,
            'gpx_id': gpx_id,
            'gpx_url': _external_url('/v1/gpx/%s.gpx' % gpx_id),
            'distance_km': round(dist_km, 2),
            'duration_min': round(_duration_min(profile, dist_km, ascent, descent), 0),
            'points': len(coords),
        }
        if ascent is not None:
            result['ascent_m'] = ascent
            result['descent_m'] = descent
        return result

    mcp_server = MCPServer(
        server_name='sms',
        server_version='4.0.0',
        instructions=(
            'sms serves OpenStreetMap vector tiles from a local MBTiles file and '
            'can plan hiking and trekking routes on them. ' + _SAFETY_NOTE
        ),
    )

    _LATLON_SCHEMA = {
        'type': 'array',
        'items': {'type': 'number'},
        'minItems': 2,
        'maxItems': 2,
        'description': 'coordinate as [latitude, longitude] in WGS84 degrees',
    }

    if photon_server:
        mcp_server.tool(
            'geocode',
            'Look up coordinates for a place name or address via the configured '
            'Photon server. Returns up to `limit` candidates with lat/lon. Use this '
            'first to turn a user request like "from Garmisch to the Zugspitze" into '
            'coordinates, then feed those into plan_route.',
            {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'place name or address'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 5},
                    'near': dict(_LATLON_SCHEMA,
                                 description='optional bias location as [lat, lon]'),
                },
                'required': ['query'],
            },
            tool_geocode,
        )

        mcp_server.tool(
            'reverse_geocode',
            'Resolve a coordinate to the nearest address or place name via the '
            'configured Photon server. Useful to label waypoints of a planned tour.',
            {
                'type': 'object',
                'properties': {
                    'coordinate': _LATLON_SCHEMA,
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 1},
                },
                'required': ['coordinate'],
            },
            tool_reverse_geocode,
        )

    mcp_server.tool(
        'search_poi',
        'Find points of interest around a coordinate, sorted by distance, '
        'named POIs first within the same distance band. '
        'Categories: ' + ', '.join(sorted(_POI_CATEGORY_FILTERS)) + '.\n'
        '- alpine_hut: staffed and unstaffed mountain huts for an overnight '
        'stop.\n'
        '- camp_site: campsites and caravan sites.\n'
        '- shelter: roofed spots WITHOUT accommodation -- this also contains '
        'bus stop shelters and public air-raid shelters and is mostly '
        'unnamed, so do not offer these as a place to sleep.\n'
        '- supermarket, pharmacy, hospital, fuel, charging_station: resupply '
        'and services.\n'
        'Only POIs present in the local vector tiles at zoom 14 are found. '
        'Opening hours, phone numbers, capacity and whether a hut is actually '
        'staffed or open are NOT available -- tell the user to call ahead '
        'before relying on a hut.',
        {
            'type': 'object',
            'properties': {
                'center': _LATLON_SCHEMA,
                'category': {
                    'type': 'string',
                    'enum': sorted(_POI_CATEGORY_FILTERS),
                    'description': 'POI category to search for',
                },
                'radius_km': {'type': 'number', 'minimum': 0.1, 'maximum': 50, 'default': 15},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20},
                'tileset': {'type': 'string',
                            'description': 'optional tileset as identifier@version'},
            },
            'required': ['center', 'category'],
        },
        tool_search_poi,
    )

    mcp_server.tool(
        'plan_route',
        'Plan a walking or cycling route through a list of waypoints and write it '
        'to a GPX file. Segments between consecutive waypoints are routed one by '
        'one, so a long tour can be chained: each individual segment must stay '
        'under %.0f km straight-line distance, the total tour is unlimited. '
        'Returns metadata plus a GPX download URL -- the track geometry is NOT '
        'returned, on purpose, because a few thousand track points would flood '
        'the context.\n\n'
        'IMPORTANT LIMITATIONS:\n'
        '- %s\n'
        '- Every waypoint must sit within 500 m of a routable way, otherwise that '
        'segment fails.\n'
        '- Turn restrictions and access tags are not in vector tiles, so one-way '
        'rules and private/closed paths are ignored.\n'
        '- Duration for foot is a flat 4.5 km/h estimate; only when '
        'contours.mbtiles is available is a DIN 33466 ascent surcharge applied.\n'
        '- If a segment fails, the other segments are still returned and the '
        'failure is listed in `errors` -- fix it by inserting an intermediate '
        'waypoint instead of retrying blindly.'
        % (route_max_crow_km, _SAFETY_NOTE),
        {
            'type': 'object',
            'properties': {
                'waypoints': {
                    'type': 'array',
                    'items': _LATLON_SCHEMA,
                    'minItems': 2,
                    'maxItems': 20,
                    'description': 'ordered list of [lat, lon] pairs; start, '
                                   'intermediate stops, destination',
                },
                'profile': {'type': 'string', 'enum': ['foot', 'bike'], 'default': 'foot'},
                'name': {'type': 'string', 'description': 'track name used in the GPX file'},
                'buffer_km': {
                    'type': 'number',
                    'minimum': 0.5,
                    'maximum': 20,
                    'description': 'corridor half-width around the straight line. '
                                   'Defaults to 10 %% of the segment length (min ~3 km). '
                                   'Increase when a detour around a lake or a closed '
                                   'area is needed.',
                },
                'elevation': {
                    'type': 'boolean',
                    'default': True,
                    'description': 'estimate ascent/descent from contour lines '
                                   '(only if contours.mbtiles is loaded)',
                },
                'export_gpx': {'type': 'boolean', 'default': True},
                'tileset': {'type': 'string',
                            'description': 'optional tileset as identifier@version'},
            },
            'required': ['waypoints'],
        },
        tool_plan_route,
    )

    mcp_server.tool(
        'export_gpx',
        'Write an arbitrary list of coordinates to a GPX file and return its '
        'download URL. Use this when you already have a track (for example a '
        'manually stitched one) instead of a plan_route result. Coordinates are '
        '[longitude, latitude] pairs, GeoJSON order.',
        {
            'type': 'object',
            'properties': {
                'coordinates': {
                    'type': 'array',
                    'items': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'minItems': 2,
                        'maxItems': 2,
                    },
                    'minItems': 2,
                    'description': 'track points as [lon, lat] pairs (GeoJSON order)',
                },
                'name': {'type': 'string', 'default': 'sms track'},
                'waypoints': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'lat': {'type': 'number'},
                            'lon': {'type': 'number'},
                            'name': {'type': 'string'},
                        },
                        'required': ['lat', 'lon'],
                    },
                    'description': 'optional named waypoints stored as <wpt> entries',
                },
                'elevation': {
                    'type': 'boolean',
                    'default': False,
                    'description': 'add interpolated <ele> values from contour lines',
                },
                'profile': {'type': 'string', 'enum': ['foot', 'bike'], 'default': 'foot'},
            },
            'required': ['coordinates'],
        },
        tool_export_gpx,
    )

    def post_mcp():
        payload, status = mcp_server.handle_raw(request.get_data())
        if payload is None:
            return Response(status=202)
        return Response(status=status, content_type='application/json',
                        response=json.dumps(payload))

    def get_mcp():
        # No SSE stream: this server is stateless streamable-HTTP, POST only.
        return Response(status=405, headers={'allow': 'POST'})

    @app.after_request
    def _add_headers(resp):
        if http_access_control_allow_origin:
            resp.headers['access-control-allow-origin'] = http_access_control_allow_origin
        return resp

    app.add_url_rule('/', view_func=get_index)
    app.add_url_rule('/v1/capabilities', view_func=get_capabilities)
    app.add_url_rule('/mcp', view_func=post_mcp, methods=['POST'])
    app.add_url_rule('/mcp', view_func=get_mcp, methods=['GET'], endpoint='get_mcp')
    app.add_url_rule('/v1/gpx/<string:gpx_id>.gpx', view_func=get_gpx)
    app.add_url_rule(
        '/v1/poi/<string:identifier>@<string:version>',
        view_func=get_poi)
    app.add_url_rule(
        '/v1/route/<string:identifier>@<string:version>',
        view_func=get_route)
    app.add_url_rule(
        '/v1/elevation/<string:identifier>@<string:version>',
        view_func=get_elevation,
        methods=['POST'])

    app.add_url_rule(
        '/v1/tiles/<string:identifier>@<string:version>/<int:z>/<int:x>/<int:y>.mvt',
        view_func=get_tile)
    app.add_url_rule(
        '/v1/styles/<string:identifier>@<string:version>/style.json',
        view_func=get_styles)
    app.add_url_rule(
        '/v1/styles/<string:identifier>@<string:version>/sprite.json',
        view_func=get_sprite_1x_json)
    app.add_url_rule(
        '/v1/styles/<string:identifier>@<string:version>/sprite@2x.json',
        view_func=get_sprite_2x_json)
    app.add_url_rule(
        '/v1/styles/<string:identifier>@<string:version>/sprite.png',
        view_func=get_sprite_1x_png)
    app.add_url_rule(
        '/v1/styles/<string:identifier>@<string:version>/sprite@2x.png',
        view_func=get_sprite_2x_png)
    app.add_url_rule(
        '/v1/fonts/<string:identifier>@<string:version>/<string:stack>/<string:range>.pbf',
        view_func=get_fonts)
    app.add_url_rule(
        '/v1/static/<string:identifier>@<string:version>/<string:file>', view_func=get_static)
    server = WSGIServer(('0.0.0.0', port), app, log=app.logger)

    return start, stop


def normalise_environment(key_values):
    # Separator is chosen to
    # - show the structure of variables fairly easily;
    # - avoid problems, since underscores are usual in environment variables
    separator = '__'

    def get_first_component(key):
        return key.split(separator)[0]

    def get_later_components(key):
        return separator.join(key.split(separator)[1:])

    without_more_components = {
        key: value
        for key, value in key_values.items()
        if not get_later_components(key)
    }

    with_more_components = {
        key: value
        for key, value in key_values.items()
        if get_later_components(key)
    }

    def grouped_by_first_component(items):
        def by_first_component(item):
            return get_first_component(item[0])

        # groupby requires the items to be sorted by the grouping key
        return itertools.groupby(
            sorted(items, key=by_first_component),
            by_first_component,
        )

    def items_with_first_component(items, first_component):
        return {
            get_later_components(key): value
            for key, value in items
            if get_first_component(key) == first_component
        }

    nested_structured_dict = {
        **without_more_components, **{
            first_component: normalise_environment(
                items_with_first_component(items, first_component))
            for first_component, items in grouped_by_first_component(with_more_components.items())
        }}

    def all_keys_are_ints():
        def is_int(string):
            try:
                int(string)
                return True
            except ValueError:
                return False

        return all([is_int(key) for key, value in nested_structured_dict.items()])

    def list_sorted_by_int_key():
        return [
            value
            for key, value in sorted(
                nested_structured_dict.items(),
                key=lambda key_value: int(key_value[0])
            )
        ]

    return \
        list_sorted_by_int_key() if all_keys_are_ints() else \
        nested_structured_dict


def main():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    logger.addHandler(handler)

    env = normalise_environment(os.environ)

    def env_int(key, default):
        try:
            return int(env.get(key, default))
        except (TypeError, ValueError):
            logger.warning('%s is not an integer, falling back to %s', key, default)
            return default

    def env_float(key, default):
        try:
            return float(env.get(key, default))
        except (TypeError, ValueError):
            logger.warning('%s is not a number, falling back to %s', key, default)
            return default

    with ExitStack() as exit_stack:
        start, stop = simple_mbtiles_server(
            logger,
            exit_stack,
            int(os.environ['PORT']),
            env['MBTILES'],
            env.get('HTTP_ACCESS_CONTROL_ALLOW_ORIGIN'),
            photon_server=env.get('PHOTONSERVER'),
            tile_cache_size=env_int('TILE_CACHE_SIZE', 2000),
            gpx_dir=env.get('GPX_DIR'),
            gpx_max_files=env_int('GPX_MAX_FILES', 200),
            gpx_ttl_seconds=env_int('GPX_TTL_SECONDS', 86400),
            route_max_tiles=env_int('ROUTE_MAX_TILES', 1200),
            route_max_crow_km=env_float('ROUTE_MAX_CROW_KM', 50.0),
        )

        gevent.signal_handler(signal.SIGTERM, stop)
        start()
        gevent.get_hub().join()

    logger.info('Shut down gracefully')


if __name__ == '__main__':
    main()
