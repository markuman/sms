from gevent import (
    monkey,
)
monkey.patch_all()

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
    'alpine_hut': {
        'subclass': {
            'alpine_hut', 'wilderness_hut', 'shelter',
            'lean_to', 'basic_hut', 'camp_site',
        },
        'class': {'shelter', 'accommodation', 'campsite'},
    },
}


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


def _parse_mvt_poi(raw_tile, tile_x, tile_y, zoom, category):
    """
    Decompress and parse a raw MVT tile blob.
    Returns a list of GeoJSON Point features matching *category*.
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
                if not _matches_poi_category(props, category):
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


def _extract_road_segments(raw_tile, tile_x, tile_y, zoom, profile):
    """
    Parse a raw MVT blob, extract transportation layer segments passable for
    the given profile. Returns list of (node_a, node_b, weighted_dist, [c_a, c_b]).
    node_a/node_b are (lon, lat) tuples rounded to 5 decimal places (~1 m).
    """
    weights = _ROUTING_PROFILES.get(profile, _ROUTING_PROFILES['foot'])

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
                factor = weights.get(props.get('class', ''))
                if factor is None:
                    continue
                for coords in _decode_mvt_geometry(geometry, tile_x, tile_y, zoom, extent):
                    for j in range(len(coords) - 1):
                        a, b = coords[j], coords[j + 1]
                        node_a = (round(a[0], 5), round(a[1], 5))
                        node_b = (round(b[0], 5), round(b[1], 5))
                        if node_a == node_b:
                            continue
                        dist = _haversine(a[0], a[1], b[0], b[1]) * factor
                        segments.append((node_a, node_b, dist))
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


def _elevation_profile(path_coords, contour_lines):
    """
    Estimate ascent and descent along path_coords by intersecting with contour lines.

    For each path segment we collect every contour crossing (ele value + position t
    along the segment).  We then sort all crossings by their cumulative position and
    sum up the signed ele-differences → ascent (m) and descent (m, positive value).
    """
    # gather all crossings: (cumulative_distance, ele)
    crossings = []
    cum = 0.0

    for si in range(len(path_coords) - 1):
        a = path_coords[si]
        b = path_coords[si + 1]
        seg_len = _haversine(a[0], a[1], b[0], b[1])

        seg_crossings = []
        for ele, contour_coords in contour_lines:
            for ci in range(len(contour_coords) - 1):
                c = contour_coords[ci]
                d = contour_coords[ci + 1]
                t = _seg_intersect(a, b, c, d)
                if t is not None:
                    seg_crossings.append((t, ele))

        # deduplicate very close crossings (same ele within tiny t-distance)
        seg_crossings.sort()
        deduped = []
        for t, ele in seg_crossings:
            if deduped and abs(t - deduped[-1][0]) < 1e-6 and ele == deduped[-1][1]:
                continue
            deduped.append((t, ele))

        for t, ele in deduped:
            crossings.append((cum + t * seg_len, ele))

        cum += seg_len

    if len(crossings) < 2:
        return 0, 0

    crossings.sort()
    ascent = 0
    descent = 0
    for i in range(1, len(crossings)):
        diff = crossings[i][1] - crossings[i - 1][1]
        if diff > 0:
            ascent += diff
        else:
            descent += -diff

    return int(ascent), int(descent)


def _build_graph(all_segments):
    """Build bidirectional adjacency graph: node → [(neighbor, weighted_dist)]."""
    graph = {}
    for node_a, node_b, dist in all_segments:
        graph.setdefault(node_a, []).append((node_b, dist))
        graph.setdefault(node_b, []).append((node_a, dist))
    return graph


def _nearest_node(graph, lon, lat):
    """Linear scan for the closest graph node to (lon, lat). Returns (key, dist_km)."""
    best_key = None
    best_dist = float('inf')
    for key in graph:
        d = _haversine(lon, lat, key[0], key[1])
        if d < best_dist:
            best_dist = d
            best_key = key
    return best_key, best_dist


def _astar_route(graph, start, end):
    """
    A* shortest path. Returns (coords, weighted_cost) or (None, None).
    coords is a list of [lon, lat] pairs forming the route.
    """
    def h(node):
        return _haversine(node[0], node[1], end[0], end[1])

    open_set = [(h(start), 0.0, start)]
    came_from = {}
    g_score = {start: 0.0}

    while open_set:
        _, g, current = heapq.heappop(open_set)

        if g > g_score.get(current, float('inf')):
            continue

        if current == end:
            path = []
            node = end
            while node in came_from:
                path.append(node)
                node = came_from[node]
            path.append(start)
            path.reverse()
            return [[n[0], n[1]] for n in path], g_score[end]

        for neighbor, dist in graph.get(current, []):
            new_g = g + dist
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

            # Linienbreite: 50hm-Linien dicker als 10hm-Linien, je nach Zoom interpoliert
            is_50m = ['==', ['%', ['to-number', ['get', 'ele']], 50], 0]
            line_width = [
                'interpolate', ['linear'], ['zoom'],
                10, ['case', is_50m, 1.2, 0.5],
                14, ['case', is_50m, 2.0, 0.9],
            ]

            contour_line_layer = {
                'id': 'contour-line',
                'type': 'line',
                'source': 'contours',
                'source-layer': 'contours',
                'minzoom': 10,
                'layout': {
                    'line-join': 'round',
                    'visibility': 'none',
                },
                'paint': {
                    'line-color': '#8b5a2b',
                    'line-width': line_width,
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

            style_dict['layers'].extend([contour_line_layer, contour_label_layer])

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

    def get_route(identifier, version):
        try:
            from_str = request.args['from']
            to_str   = request.args['to']
            profile  = request.args.get('profile', 'foot')
            from_lat, from_lon = map(float, from_str.split(','))
            to_lat,   to_lon   = map(float, to_str.split(','))
        except (KeyError, ValueError):
            return Response(status=400, content_type='application/json',
                            response=json.dumps({'error': 'invalid parameters; expected from=lat,lon&to=lat,lon&profile=foot|bike'}))

        if profile not in _ROUTING_PROFILES:
            return Response(status=400, content_type='application/json',
                            response=json.dumps({'error': f'unknown profile "{profile}"; use foot or bike'}))

        try:
            db_connection = mbtiles_dict[(identifier, version)]['db_connection']
        except KeyError:
            return Response(status=404)

        zoom = 14

        # Bounding box of start+end with 30% buffer (min ~1 km each side).
        min_lat = min(from_lat, to_lat)
        max_lat = max(from_lat, to_lat)
        min_lon = min(from_lon, to_lon)
        max_lon = max(from_lon, to_lon)

        buf_lat = max((max_lat - min_lat) * 0.3, 0.009)   # ~1 km
        buf_lon = max((max_lon - min_lon) * 0.3, 0.013)

        x0, y1 = _lat_lon_to_tile(min_lat - buf_lat, min_lon - buf_lon, zoom)
        x1, y0 = _lat_lon_to_tile(max_lat + buf_lat, max_lon + buf_lon, zoom)

        tile_count = (x1 - x0 + 1) * (y1 - y0 + 1)
        if tile_count > 500:
            return Response(status=400, content_type='application/json',
                            response=json.dumps({'error': f'bounding box too large ({tile_count} tiles); keep routes under ~30 km'}))

        # Parse transportation segments from every tile in the bbox.
        cursor = db_connection.cursor()
        all_segments = []
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                y_tms = (2 ** zoom - 1) - ty
                cursor.execute(sql, (zoom, tx, y_tms))
                row = cursor.fetchone()
                if row:
                    all_segments.extend(_extract_road_segments(row[0], tx, ty, zoom, profile))
        cursor.close()

        if not all_segments:
            return Response(status=404, content_type='application/json',
                            response=json.dumps({'error': 'no road network found in area'}))

        graph = _build_graph(all_segments)

        start_node, start_snap_km = _nearest_node(graph, from_lon, from_lat)
        end_node,   end_snap_km   = _nearest_node(graph, to_lon,   to_lat)

        if start_snap_km > 0.5:
            return Response(status=404, content_type='application/json',
                            response=json.dumps({'error': f'no road within 500 m of start point (closest: {start_snap_km*1000:.0f} m)'}))
        if end_snap_km > 0.5:
            return Response(status=404, content_type='application/json',
                            response=json.dumps({'error': f'no road within 500 m of end point (closest: {end_snap_km*1000:.0f} m)'}))

        if start_node == end_node:
            return Response(status=400, content_type='application/json',
                            response=json.dumps({'error': 'start and end snap to the same node'}))

        path_coords, _ = _astar_route(graph, start_node, end_node)

        if path_coords is None:
            return Response(status=404, content_type='application/json',
                            response=json.dumps({'error': 'no route found between the two points'}))

        dist_km = sum(
            _haversine(path_coords[i][0], path_coords[i][1],
                       path_coords[i + 1][0], path_coords[i + 1][1])
            for i in range(len(path_coords) - 1)
        )
        speed = _PROFILE_SPEED_KMH.get(profile, 4.5)

        result = {
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': path_coords},
            'properties': {
                'distance_km':  round(dist_km, 3),
                'duration_min': round(dist_km / speed * 60, 1),
                'profile':      profile,
                'tiles_loaded': tile_count,
                'nodes':        len(graph),
            },
        }
        return Response(status=200, content_type='application/json', response=json.dumps(result))

    def get_elevation(identifier, version):
        """
        POST /v1/elevation/<id>@<ver>
        Body: {"coordinates": [[lon, lat], ...]}
        Returns: {"ascent_m": <int>, "descent_m": <int>}

        Computes ascent/descent by intersecting the given LineString with
        contour lines from contours.mbtiles (zoom 14).  Only available when
        contours.mbtiles is loaded.
        """
        if not contours_dict:
            return Response(status=404, content_type='application/json',
                            response=json.dumps({'error': 'no contours available'}))

        try:
            body = request.get_json(force=True, silent=True) or {}
            coords = body.get('coordinates', [])
            if len(coords) < 2:
                raise ValueError('need at least 2 coordinates')
            # validate
            coords = [[float(c[0]), float(c[1])] for c in coords]
        except Exception as exc:
            return Response(status=400, content_type='application/json',
                            response=json.dumps({'error': f'invalid body: {exc}'}))

        zoom = 14

        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        min_lon, max_lon = min(lons), max(lons)
        min_lat, max_lat = min(lats), max(lats)

        buf_lat = max((max_lat - min_lat) * 0.05, 0.002)
        buf_lon = max((max_lon - min_lon) * 0.05, 0.003)

        x0, y1 = _lat_lon_to_tile(min_lat - buf_lat, min_lon - buf_lon, zoom)
        x1, y0 = _lat_lon_to_tile(max_lat + buf_lat, max_lon + buf_lon, zoom)

        tile_count = (x1 - x0 + 1) * (y1 - y0 + 1)
        if tile_count > 500:
            return Response(status=400, content_type='application/json',
                            response=json.dumps({'error': f'area too large ({tile_count} tiles)'}))

        cursor = contours_dict['db_connection'].cursor()
        sql = 'SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?'
        all_contours = []
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                y_tms = (2 ** zoom - 1) - ty
                cursor.execute(sql, (zoom, tx, y_tms))
                row = cursor.fetchone()
                if row:
                    all_contours.extend(_extract_contour_lines(row[0], tx, ty, zoom))
        cursor.close()

        if not all_contours:
            return Response(status=200, content_type='application/json',
                            response=json.dumps({'ascent_m': 0, 'descent_m': 0}))

        ascent, descent = _elevation_profile(coords, all_contours)
        return Response(status=200, content_type='application/json',
                        response=json.dumps({'ascent_m': ascent, 'descent_m': descent}))

    def get_index():
        return send_from_directory(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'vendor'), 'index.html')

    def get_capabilities():
        return Response(status=200, content_type='application/json', response=json.dumps({
            'contours': bool(contours_dict),
            'routing':  True,
        }))

    def get_poi(identifier, version):
        try:
            lat      = float(request.args['lat'])
            lon      = float(request.args['lon'])
            category = request.args['category']
        except (KeyError, ValueError):
            return Response(status=400)

        if category not in _POI_CATEGORY_FILTERS:
            return Response(status=400)

        try:
            db_connection = mbtiles_dict[(identifier, version)]['db_connection']
        except KeyError:
            return Response(status=404)

        radius = min(float(request.args.get('radius', 15)), 50.0)  # cap at 50 km

        tiles = _tiles_in_radius(lat, lon, radius, zoom=14, max_tiles=100)

        seen     = set()
        features = []

        cursor = db_connection.cursor()
        for (z, x, y) in tiles:
            y_tms = (2 ** z - 1) - y
            cursor.execute(sql, (z, x, y_tms))
            row = cursor.fetchone()
            if not row:
                continue
            raw_tile = row[0]
            for f in _parse_mvt_poi(raw_tile, x, y, z, category):
                coords = f['geometry']['coordinates']
                key = (round(coords[0], 6), round(coords[1], 6))
                if key not in seen:
                    seen.add(key)
                    features.append(f)
        cursor.close()

        return Response(
            status=200,
            content_type='application/json',
            response=json.dumps({'type': 'FeatureCollection', 'features': features}),
        )

    @app.after_request
    def _add_headers(resp):
        if http_access_control_allow_origin:
            resp.headers['access-control-allow-origin'] = http_access_control_allow_origin
        return resp

    app.add_url_rule('/', view_func=get_index)
    app.add_url_rule('/v1/capabilities', view_func=get_capabilities)
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

    with ExitStack() as exit_stack:
        start, stop = simple_mbtiles_server(
            logger,
            exit_stack,
            int(os.environ['PORT']),
            env['MBTILES'],
            env.get('HTTP_ACCESS_CONTROL_ALLOW_ORIGIN'),
        )

        gevent.signal_handler(signal.SIGTERM, stop)
        start()
        gevent.get_hub().join()

    logger.info('Shut down gracefully')


if __name__ == '__main__':
    main()
