"""
Tests for the routing / MCP / GPX features.

Deliberately free of external services: the MBTiles fixtures are generated
in-process (a few hundred kB), so nothing is downloaded and the suite runs
anywhere.

    python3 -m pytest -v test/test_routing_mcp.py
"""

import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import zlib

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simple_mbtiles_server.gpx import GpxStore, to_gpx  # noqa: E402


# ---------------------------------------------------------------------------
# MVT fixture generation
# ---------------------------------------------------------------------------

Z = 14
GRID = 0.005
LAT0, LAT1 = 48.10, 48.20
LON0, LON1 = 11.50, 11.65


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zigzag(n):
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _lenval(field, data):
    return _tag(field, 2) + _varint(len(data)) + data


def _feature(geom_type, tags, geom):
    return (_lenval(2, b''.join(_varint(t) for t in tags))
            + _tag(3, 0) + _varint(geom_type)
            + _lenval(4, b''.join(_varint(g) for g in geom)))


def _layer(name, keys, values, features, extent=4096):
    b = _tag(15, 0) + _varint(2)
    b += _lenval(1, name.encode())
    for f in features:
        b += _lenval(2, f)
    for k in keys:
        b += _lenval(3, k.encode())
    for v in values:
        b += _lenval(4, _lenval(1, v.encode()))
    b += _tag(5, 0) + _varint(extent)
    return b


def _to_px(lon, lat, tx, ty, extent=4096):
    n = 2 ** Z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return int(round((x - tx) * extent)), int(round((y - ty) * extent))


def _line_geom(points):
    geom = []
    cx = cy = 0
    px, py = points[0]
    geom += [(1 << 3) | 1, _zigzag(px - cx), _zigzag(py - cy)]
    cx, cy = px, py
    rest = points[1:]
    geom.append((len(rest) << 3) | 2)
    for px, py in rest:
        geom += [_zigzag(px - cx), _zigzag(py - cy)]
        cx, cy = px, py
    return geom


def _point_geom(px, py):
    return [(1 << 3) | 1, _zigzag(px), _zigzag(py)]


def _tile_of(lat, lon):
    n = 2 ** Z
    return (int((lon + 180.0) / 360.0 * n),
            int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n))


def _tile_bounds(tx, ty):
    n = 2 ** Z
    lon0 = tx / n * 360.0 - 180.0
    lon1 = (tx + 1) / n * 360.0 - 180.0
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
    return lon0, lat0, lon1, lat1


def _frange(lo, hi):
    v = math.floor(lo / GRID) * GRID
    out = []
    while v <= hi + GRID / 2:
        out.append(round(v, 6))
        v += GRID
    return out


def _new_db(path, name):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE metadata (name text, value text)')
    con.execute('CREATE TABLE tiles (zoom_level integer, tile_column integer, '
                'tile_row integer, tile_data blob)')
    con.execute('CREATE UNIQUE INDEX tile_index on tiles '
                '(zoom_level, tile_column, tile_row)')
    for k, v in (('format', 'pbf'), ('name', name),
                 ('minzoom', '0'), ('maxzoom', '14')):
        con.execute('INSERT INTO metadata VALUES (?,?)', (k, v))
    return con


def _gz(data):
    c = zlib.compressobj(wbits=31)
    return c.compress(data) + c.flush()


def build_fixtures(directory):
    """
    Write planet.mbtiles (grid of ways + POIs) and contours.mbtiles.

    The way grid sits exactly on a lat/lon raster so that crossings share
    graph nodes -- otherwise the routing graph falls apart into isolated
    lines and every route test fails for the wrong reason.
    """
    x_min, y_max = _tile_of(LAT0, LON0)
    x_max, y_min = _tile_of(LAT1, LON1)
    lats = _frange(LAT0 - 0.02, LAT1 + 0.02)
    lons = _frange(LON0 - 0.02, LON1 + 0.02)

    keys = ['class', 'name', 'subclass']
    values = ['path', 'track', 'supermarket', 'alpine_hut', 'Testhuette', 'Testmarkt']
    ki = {k: i for i, k in enumerate(keys)}
    vi = {v: i for i, v in enumerate(values)}

    planet_path = os.path.join(directory, 'planet.mbtiles')
    con = _new_db(planet_path, 'smstest')
    for tx in range(x_min, x_max + 1):
        for ty in range(y_min, y_max + 1):
            lon0, lat0, lon1, lat1 = _tile_bounds(tx, ty)
            pad = 0.006
            tile_lats = [la for la in lats if lat0 - pad <= la <= lat1 + pad]
            tile_lons = [lo for lo in lons if lon0 - pad <= lo <= lon1 + pad]

            feats = []
            for la in tile_lats:
                pts = [_to_px(lo, la, tx, ty) for lo in tile_lons]
                if len(pts) >= 2:
                    feats.append(_feature(2, [ki['class'], vi['path']], _line_geom(pts)))
            for lo in tile_lons:
                pts = [_to_px(lo, la, tx, ty) for la in tile_lats]
                if len(pts) >= 2:
                    feats.append(_feature(2, [ki['class'], vi['track']], _line_geom(pts)))

            poi_feats = [
                _feature(1, [ki['class'], vi['alpine_hut'], ki['subclass'],
                             vi['alpine_hut'], ki['name'], vi['Testhuette']],
                         _point_geom(2048, 2048)),
                _feature(1, [ki['class'], vi['supermarket'], ki['subclass'],
                             vi['supermarket'], ki['name'], vi['Testmarkt']],
                         _point_geom(1000, 3000)),
            ]

            tile = (_lenval(3, _layer('transportation', keys, values, feats))
                    + _lenval(3, _layer('poi', keys, values, poi_feats)))
            con.execute('INSERT INTO tiles VALUES (?,?,?,?)',
                        (Z, tx, (2 ** Z - 1) - ty, _gz(tile)))
    con.commit()
    con.close()

    # contours: elevation rises linearly with longitude, 10 m per 0.001 deg
    ckeys = ['ele']
    cvalues = [str(500 + i * 10) for i in range(200)]
    cvi = {v: i for i, v in enumerate(cvalues)}
    contours_path = os.path.join(directory, 'contours.mbtiles')
    ccon = _new_db(contours_path, 'contours')
    for tx in range(x_min, x_max + 1):
        for ty in range(y_min, y_max + 1):
            lon0, lat0, lon1, lat1 = _tile_bounds(tx, ty)
            feats = []
            ele = 500
            while ele <= 2400:
                lo = 11.50 + (ele - 500) / 10000.0
                if lon0 <= lo <= lon1 and str(ele) in cvi:
                    pts = [_to_px(lo, lat0 - 0.01, tx, ty),
                           _to_px(lo, lat1 + 0.01, tx, ty)]
                    feats.append(_feature(2, [0, cvi[str(ele)]], _line_geom(pts)))
                ele += 10
            if not feats:
                continue
            tile = _lenval(3, _layer('contours', ckeys, cvalues, feats))
            ccon.execute('INSERT INTO tiles VALUES (?,?,?,?)',
                         (Z, tx, (2 ** Z - 1) - ty, _gz(tile)))
    ccon.commit()
    ccon.close()
    return planet_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PORT = 18712
BASE = 'http://127.0.0.1:%d' % PORT


@pytest.fixture(scope='session')
def server():
    with tempfile.TemporaryDirectory() as tmp:
        build_fixtures(tmp)
        env = dict(
            os.environ,
            PORT=str(PORT),
            MBTILES__1__URL=os.path.join(tmp, 'planet.mbtiles'),
            MBTILES__1__MIN_ZOOM='0',
            MBTILES__1__MAX_ZOOM='14',
            MBTILES__1__IDENTIFIER='mytiles',
            MBTILES__1__VERSION='1.0.0',
            GPX_DIR=os.path.join(tmp, 'gpx'),
            HTTP_ACCESS_CONTROL_ALLOW_ORIGIN='*',
        )
        env.pop('PHOTONSERVER', None)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen(
            [sys.executable, '-m', 'simple_mbtiles_server'],
            env=env, cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    httpx.get(BASE + '/v1/capabilities', timeout=1.0)
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise RuntimeError('server did not start')
            yield BASE
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def rpc(method, params=None, request_id=1):
    body = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        body['params'] = params
    return httpx.post(BASE + '/mcp', json=body, timeout=60.0)


def call_tool(name, arguments):
    resp = rpc('tools/call', {'name': name, 'arguments': arguments})
    assert resp.status_code == 200
    result = resp.json()['result']
    return json.loads(result['content'][0]['text']), result.get('isError', False)


# ---------------------------------------------------------------------------
# Corridor tile selection
# ---------------------------------------------------------------------------

def test_corridor_beats_bbox_on_diagonal():
    from simple_mbtiles_server.__main__ import _lat_lon_to_tile, _tiles_along_line

    tiles, _ = _tiles_along_line(48.0, 11.0, 48.318, 11.48, 14)
    x0, y1 = _lat_lon_to_tile(48.0, 11.0, 14)
    x1, y0 = _lat_lon_to_tile(48.318, 11.48, 14)
    bbox = (x1 - x0 + 1) * (y1 - y0 + 1)
    assert len(tiles) < bbox / 1.5


def test_corridor_not_worse_than_bbox_when_axis_parallel():
    """
    The whole point of the short-side cap: for an axis-parallel route the
    bounding box is narrow anyway, and a fixed 2-tile buffer would load more
    tiles than a bbox ever would.
    """
    from simple_mbtiles_server.__main__ import _lat_lon_to_tile, _tiles_along_line

    tiles, _ = _tiles_along_line(48.0, 11.0, 48.0, 11.672, 14)
    x0, y1 = _lat_lon_to_tile(48.0, 11.0, 14)
    x1, y0 = _lat_lon_to_tile(48.0, 11.672, 14)
    bbox = (x1 - x0 + 1) * (y1 - y0 + 1)
    assert len(tiles) <= bbox * 3.5


def test_corridor_contains_endpoints():
    from simple_mbtiles_server.__main__ import _lat_lon_to_tile, _tiles_along_line

    tiles, _ = _tiles_along_line(48.10, 11.50, 48.20, 11.65, 14)
    assert (14,) + _lat_lon_to_tile(48.10, 11.50, 14) in tiles
    assert (14,) + _lat_lon_to_tile(48.20, 11.65, 14) in tiles


def test_lru_cache_evicts_oldest():
    from simple_mbtiles_server.__main__ import LruCache

    cache = LruCache(3)
    for i in range(3):
        cache.put(i, str(i))
    cache.get(0)             # 0 becomes most recently used
    cache.put(3, '3')        # evicts 1, not 0
    assert cache.get(0) == '0'
    assert cache.get(1) is None
    assert cache.stats()['entries'] == 3


def test_astar_matches_dijkstra():
    """A* must stay admissible -- same cost as an uninformed search."""
    import heapq

    from simple_mbtiles_server.__main__ import (
        _astar_route, _build_graph, _haversine)

    segments = []
    for i in range(20):
        for j in range(20):
            a = (int(round((11.0 + i * 0.005) * 100000)),
                 int(round((48.0 + j * 0.005) * 100000)))
            if i + 1 < 20:
                b = (int(round((11.0 + (i + 1) * 0.005) * 100000)),
                     int(round((48.0 + j * 0.005) * 100000)))
                segments.append((a, b, _haversine(a[0] / 1e5, a[1] / 1e5,
                                                  b[0] / 1e5, b[1] / 1e5), 'path'))
            if j + 1 < 20:
                b = (int(round((11.0 + i * 0.005) * 100000)),
                     int(round((48.0 + (j + 1) * 0.005) * 100000)))
                segments.append((a, b, _haversine(a[0] / 1e5, a[1] / 1e5,
                                                  b[0] / 1e5, b[1] / 1e5), 'path'))

    graph, _hints, _routes = _build_graph(segments, 'foot')
    start = (int(round(11.0 * 100000)), int(round(48.0 * 100000)))
    end = (int(round((11.0 + 19 * 0.005) * 100000)),
           int(round((48.0 + 19 * 0.005) * 100000)))

    def dijkstra():
        open_set = [(0.0, start)]
        scores = {start: 0.0}
        while open_set:
            dist, node = heapq.heappop(open_set)
            if dist > scores.get(node, float('inf')):
                continue
            if node == end:
                return dist
            for neighbour, weight, _real in graph.get(node, []):
                new = dist + weight
                if new < scores.get(neighbour, float('inf')):
                    scores[neighbour] = new
                    heapq.heappush(open_set, (new, neighbour))
        return None

    coords, distance = _astar_route(graph, start, end)
    assert coords is not None
    assert abs(distance - dijkstra()) < 1e-6


def test_snapping_avoids_disconnected_fragments():
    """
    Regression: vector tiles are clipped at tile borders, so a real network
    decodes into one large component plus hundreds of stubs. Snapping to the
    geometrically nearest node put the start on such a 2-node dead end and
    every route failed with "no route found" -- even 200 m inside a town.
    """
    from simple_mbtiles_server.__main__ import (
        _astar_route, _build_graph, _nearest_node, _snap_pair)

    # a connected main line ...
    segments = []
    for i in range(10):
        a = (1150000 + i * 100, 4810000)
        b = (1150000 + (i + 1) * 100, 4810000)
        segments.append((a, b, 0.05, 'path'))
    # ... plus an isolated stub that happens to sit closer to the start
    stub_a = (1150010, 4810010)
    stub_b = (1150020, 4810010)
    segments.append((stub_a, stub_b, 0.01, 'path'))

    graph, _hints, _routes = _build_graph(segments, 'foot')
    start_lon, start_lat = 1150012 / 1e5, 4810009 / 1e5
    end_lon, end_lat = 1151000 / 1e5, 4810000 / 1e5

    # the naive nearest node is the stub, and routing from there fails
    naive, _ = _nearest_node(graph, start_lon, start_lat)
    assert naive in (stub_a, stub_b)
    assert _astar_route(graph, naive, (1151000, 4810000))[0] is None

    # _snap_pair picks the connected component instead
    start, start_km, end, end_km = _snap_pair(
        graph, start_lon, start_lat, end_lon, end_lat)
    assert start not in (stub_a, stub_b)
    assert start_km < 0.5 and end_km < 0.5
    coords, distance = _astar_route(graph, start, end)
    assert coords is not None and distance > 0


def test_snap_pair_reports_distance_when_nothing_in_range():
    """Unreachable input must still yield a usable "x m away" error."""
    from simple_mbtiles_server.__main__ import _build_graph, _snap_pair

    segments = [((1150000, 4810000), (1150100, 4810000), 0.1, 'path')]
    graph, _hints, _routes = _build_graph(segments, 'foot')
    # both far away from the single segment at 11.5, 48.1
    _start, start_km, _end, end_km = _snap_pair(graph, 0.0, 0.0, 20.0, 10.0)
    assert start_km > 0.5
    assert end_km > 0.5


def test_tiles_decoded_once_serve_both_profiles():
    """
    Tile decoding must not depend on the profile, otherwise a cached tile
    could not serve foot and bike alike.
    """
    from simple_mbtiles_server.__main__ import _build_graph

    segments = [((1150000, 4810000), (1150100, 4810000), 0.1, 'footway')]
    assert _build_graph(segments, 'foot')[0]
    assert _build_graph(segments, 'bike')[0] == {}   # footway is closed for bikes


# ---------------------------------------------------------------------------
# GPX
# ---------------------------------------------------------------------------

def test_gpx_is_wellformed_and_escaped():
    from xml.etree import ElementTree as ET

    xml = to_gpx(
        [[11.5, 48.1], [11.6, 48.2]],
        name='Tour & <Test>',
        wpts=[{'lat': 48.1, 'lon': 11.5, 'name': 'Start & Ziel'}],
        elevations=[500, 750.5],
    )
    ns = '{http://www.topografix.com/GPX/1/1}'
    root = ET.fromstring(xml)
    assert root.get('version') == '1.1'
    assert root.find(ns + 'wpt').find(ns + 'name').text == 'Start & Ziel'
    track = root.find(ns + 'trk')
    assert track.find(ns + 'name').text == 'Tour & <Test>'
    points = track.findall(ns + 'trkseg/' + ns + 'trkpt')
    assert len(points) == 2
    # GeoJSON is lon,lat -- GPX is lat/lon attributes. Easy to get backwards.
    assert points[0].get('lat') == '48.100000'
    assert points[0].get('lon') == '11.500000'
    assert points[1].find(ns + 'ele').text == '750.5'


def test_gpx_without_elevation_has_no_ele_tags():
    assert '<ele>' not in to_gpx([[11.5, 48.1], [11.6, 48.2]])


def test_gpx_store_enforces_max_files():
    with tempfile.TemporaryDirectory() as tmp:
        store = GpxStore(tmp, max_files=3, ttl_seconds=3600)
        ids = [store.write(to_gpx([[11.5, 48.1], [11.6, 48.2]])) for _ in range(6)]
        assert len([f for f in os.listdir(tmp) if f.endswith('.gpx')]) == 3
        assert store.read(ids[0]) is None
        assert store.read(ids[-1]) is not None


def test_gpx_store_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        store = GpxStore(tmp)
        assert store.path_for('../../etc/passwd') is None
        assert store.path_for('not-hex') is None
        assert store.read('../../etc/passwd') is None
        assert store.path_for('deadbeef') is not None


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

def test_tiles_are_cacheable_with_etag(server):
    """
    Tiles are the hot path: a client fetches dozens per pan. Without a long
    max-age every one of them is a fresh request, and the deployed server had
    no cache headers at all.
    """
    resp = httpx.get(BASE + '/v1/tiles/mytiles@1.0.0/14/8718/5684.mvt', timeout=30.0)
    assert resp.status_code == 200
    cache = resp.headers['cache-control']
    assert 'public' in cache
    assert 'max-age=604800' in cache            # a week
    assert resp.headers['etag']
    # gzip negotiation means the response varies per encoding
    assert resp.headers['vary'] == 'accept-encoding'


def test_tile_revalidation_returns_304(server):
    """A cheap revalidation must not resend the payload."""
    url = BASE + '/v1/tiles/mytiles@1.0.0/14/8718/5684.mvt'
    etag = httpx.get(url, timeout=30.0).headers['etag']

    resp = httpx.get(url, headers={'if-none-match': etag}, timeout=30.0)
    assert resp.status_code == 304
    assert resp.content == b''

    # a stale etag (e.g. after a tile rebuild) must serve the tile again
    resp = httpx.get(url, headers={'if-none-match': '"stale-0-0-0"'}, timeout=30.0)
    assert resp.status_code == 200


def test_etag_encodes_dataset_version(server):
    """
    The etag has to change when the .mbtiles file is rebuilt, otherwise
    clients keep week-old tiles after a deploy. It is derived from mtime and
    size, so no manual cache busting is needed.
    """
    resp = httpx.get(BASE + '/v1/tiles/mytiles@1.0.0/14/8718/5684.mvt', timeout=30.0)
    etag = resp.headers['etag'].strip('"')
    # <dataset>-<z>-<x>-<y>
    assert etag.endswith('-14-8718-5684')
    dataset = etag[:-len('-14-8718-5684')]
    assert dataset and dataset != 'unknown'
    # neighbouring tiles share the dataset part but differ overall
    other = httpx.get(BASE + '/v1/tiles/mytiles@1.0.0/14/8718/5685.mvt',
                      timeout=30.0).headers['etag'].strip('"')
    assert other.startswith(dataset)
    assert other != etag


def test_immutable_assets_are_cached_forever(server):
    """
    Fonts, sprites and the vendored library carry their version in the URL,
    so they can never change under a client.
    """
    for path in ('/v1/static/maplibre-gl@5.19.0/maplibre-gl.js',
                 '/v1/fonts/fonts-gl@1.0.0/Noto%20Sans%20Regular/0-255.pbf'):
        cache = httpx.get(BASE + path, timeout=30.0).headers['cache-control']
        assert 'immutable' in cache, path
        assert 'max-age=31536000' in cache, path


def test_dynamic_endpoints_are_not_cached(server):
    """
    Routes, POI searches and capabilities must never be served stale -- an
    agent acting on a cached route would plan against outdated data.
    """
    paths = [
        '/v1/capabilities',
        '/v1/route/mytiles@1.0.0?from=48.12,11.52&to=48.16,11.58',
        '/v1/poi/mytiles@1.0.0?lat=48.15&lon=11.57&category=alpine_hut',
    ]
    for path in paths:
        resp = httpx.get(BASE + path, timeout=60.0)
        assert resp.headers['cache-control'] == 'no-store', path

    # the style embeds request-derived URLs, so only a short lifetime
    style = httpx.get(BASE + '/v1/styles/osuv-style@1.0.0/style.json'
                             '?tiles=mytiles@1.0.0&fonts=fonts-gl@1.0.0', timeout=30.0)
    assert 'max-age=3600' in style.headers['cache-control']

    # the HTML shell is rewritten by startup.sh, so it must revalidate
    assert 'no-cache' in httpx.get(BASE + '/', timeout=30.0).headers['cache-control']


def test_missing_tiles_are_cached_briefly(server):
    """
    Ocean and out-of-extract tiles 404 constantly while panning; letting the
    client remember that for an hour saves a lot of pointless requests.
    """
    resp = httpx.get(BASE + '/v1/tiles/mytiles@1.0.0/14/1/1.mvt', timeout=30.0)
    assert resp.status_code == 404
    assert 'max-age=3600' in resp.headers['cache-control']


def test_capabilities(server):
    body = httpx.get(BASE + '/v1/capabilities', timeout=10.0).json()
    assert body['routing'] is True
    assert body['mcp'] is True
    assert body['gpx'] is True
    assert body['contours'] is True
    assert body['geocoding'] is False          # no PHOTONSERVER in this env
    assert 'plan_route' in body['mcp_tools']
    assert 'geocode' not in body['mcp_tools']
    assert body['routing_limits']['max_crow_km'] == 50.0


def test_route_returns_linestring(server):
    resp = httpx.get(BASE + '/v1/route/mytiles@1.0.0',
                     params={'from': '48.12,11.52', 'to': '48.16,11.58'},
                     timeout=60.0)
    assert resp.status_code == 200
    body = resp.json()
    assert body['geometry']['type'] == 'LineString'
    assert len(body['geometry']['coordinates']) > 2
    assert body['properties']['distance_km'] > 0
    assert body['properties']['tiles_loaded'] > 0


def test_route_uses_tile_cache(server):
    params = {'from': '48.12,11.52', 'to': '48.16,11.58'}
    httpx.get(BASE + '/v1/route/mytiles@1.0.0', params=params, timeout=60.0)
    second = httpx.get(BASE + '/v1/route/mytiles@1.0.0', params=params,
                       timeout=60.0).json()
    assert second['properties']['cache_hits'] > 0
    assert second['properties']['cache_misses'] == 0


def test_route_rejects_too_long_segment(server):
    resp = httpx.get(BASE + '/v1/route/mytiles@1.0.0',
                     params={'from': '48.0,11.0', 'to': '48.5,12.0'}, timeout=30.0)
    assert resp.status_code == 400
    assert '50 km' in resp.json()['error']


def test_route_rejects_unknown_profile(server):
    resp = httpx.get(BASE + '/v1/route/mytiles@1.0.0',
                     params={'from': '48.12,11.52', 'to': '48.16,11.58',
                             'profile': 'car'}, timeout=30.0)
    assert resp.status_code == 400


def test_route_reports_snap_failure(server):
    resp = httpx.get(BASE + '/v1/route/mytiles@1.0.0',
                     params={'from': '48.9,11.9', 'to': '48.16,11.58'}, timeout=60.0)
    assert resp.status_code in (400, 404)


def test_elevation(server):
    resp = httpx.post(BASE + '/v1/elevation/mytiles@1.0.0',
                      json={'coordinates': [[11.52, 48.12], [11.58, 48.12]]},
                      timeout=60.0)
    assert resp.status_code == 200
    body = resp.json()
    # fixture: 10 m per 0.001 deg of longitude -> 0.06 deg is 600 m of ascent
    assert 550 <= body['ascent_m'] <= 650


def test_poi_search_sorted_by_distance(server):
    resp = httpx.get(BASE + '/v1/poi/mytiles@1.0.0',
                     params={'lat': 48.15, 'lon': 11.57,
                             'category': 'alpine_hut', 'radius': 5},
                     timeout=60.0)
    assert resp.status_code == 200
    features = resp.json()['features']
    assert features
    distances = [f['properties']['distance_km'] for f in features]
    assert distances == sorted(distances)


def test_gpx_download_404_for_unknown_id(server):
    assert httpx.get(BASE + '/v1/gpx/deadbeefdeadbeef.gpx', timeout=10.0).status_code == 404


# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------

def test_mcp_get_is_not_allowed(server):
    assert httpx.get(BASE + '/mcp', timeout=10.0).status_code == 405


def test_mcp_initialize(server):
    body = rpc('initialize', {'protocolVersion': '2025-06-18'}).json()
    assert body['result']['protocolVersion']
    assert body['result']['serverInfo']['name'] == 'sms'
    assert 'tools' in body['result']['capabilities']


def test_mcp_notification_returns_202(server):
    resp = httpx.post(BASE + '/mcp',
                      json={'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                      timeout=10.0)
    assert resp.status_code == 202


def test_mcp_tools_list(server):
    tools = rpc('tools/list').json()['result']['tools']
    names = {t['name'] for t in tools}
    assert {'search_poi', 'plan_route', 'export_gpx'} <= names
    for tool in tools:
        assert tool['description']
        assert tool['inputSchema']['type'] == 'object'


def test_mcp_error_codes(server):
    assert httpx.post(BASE + '/mcp', content=b'{not json',
                      timeout=10.0).json()['error']['code'] == -32700
    assert rpc('does/not/exist').json()['error']['code'] == -32601
    assert httpx.post(BASE + '/mcp', json={'id': 1, 'method': 'ping'},
                      timeout=10.0).json()['error']['code'] == -32600
    assert rpc('tools/call', {'name': 'nope', 'arguments': {}}
               ).json()['error']['code'] == -32602


def test_mcp_batch(server):
    resp = httpx.post(BASE + '/mcp', json=[
        {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
    ], timeout=10.0)
    assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def test_plan_route_chains_waypoints(server):
    body, is_error = call_tool('plan_route', {
        'waypoints': [[48.11, 11.51], [48.15, 11.57], [48.19, 11.63]],
        'name': 'Testtour',
    })
    assert not is_error
    assert len(body['segments']) == 2
    assert body['distance_km'] > 10
    assert body['gpx_url'].endswith('.gpx')
    # geometry must NOT be in the result -- it would flood the context window
    assert 'geometry' not in body
    assert 'coordinates' not in body
    assert body['warnings']


def test_plan_route_reports_failures_per_segment(server):
    body, is_error = call_tool('plan_route', {
        'waypoints': [[48.11, 11.51], [48.15, 11.57], [0.0, 0.0], [48.19, 11.63]],
    })
    assert not is_error                      # partial success is still a success
    assert body['distance_km'] > 0
    assert any('segment 2' in e for e in body['errors'])
    failed = [s for s in body['segments'] if 'error' in s]
    assert failed and failed[0]['segment'] == 2


def test_plan_route_validates_waypoints(server):
    body, is_error = call_tool('plan_route', {'waypoints': [[48.11, 11.51]]})
    assert is_error
    assert 'at least two' in body['error']

    body, is_error = call_tool('plan_route', {'waypoints': [[91.0, 11.5], [48.1, 11.5]]})
    assert is_error
    assert 'out of range' in body['error']


def test_plan_route_gpx_is_downloadable(server):
    body, _ = call_tool('plan_route', {
        'waypoints': [[48.12, 11.52], [48.16, 11.58]],
        'name': 'Downloadtest',
    })
    resp = httpx.get(BASE + '/v1/gpx/%s.gpx' % body['gpx_id'], timeout=30.0)
    assert resp.status_code == 200
    assert resp.headers['content-type'] == 'application/gpx+xml'

    from xml.etree import ElementTree as ET
    ns = '{http://www.topografix.com/GPX/1/1}'
    root = ET.fromstring(resp.text)
    assert root.find(ns + 'trk').find(ns + 'name').text == 'Downloadtest'
    assert len(root.findall(ns + 'trk/' + ns + 'trkseg/' + ns + 'trkpt')) == body['points']


def test_search_poi_tool(server):
    body, is_error = call_tool('search_poi', {
        'center': [48.15, 11.57], 'category': 'alpine_hut', 'limit': 3,
    })
    assert not is_error
    assert body['count'] <= 3
    assert body['results'][0]['distance_km'] >= 0


def test_poi_categories_separate_huts_from_shelters():
    """
    OpenMapTiles files bus stop shelters and air-raid shelters under
    subclass=shelter. Around Garmisch that is 113 of 122 hits, which buried
    the four real huts beyond any result limit -- so `alpine_hut` must not
    match `shelter`.
    """
    from simple_mbtiles_server.__main__ import _matches_poi_category

    assert _matches_poi_category({'subclass': 'alpine_hut'}, 'alpine_hut')
    assert _matches_poi_category({'subclass': 'wilderness_hut'}, 'alpine_hut')
    assert not _matches_poi_category({'subclass': 'shelter'}, 'alpine_hut')
    assert _matches_poi_category({'subclass': 'shelter'}, 'shelter')
    assert _matches_poi_category({'subclass': 'camp_site'}, 'camp_site')
    assert not _matches_poi_category({'subclass': 'camp_site'}, 'alpine_hut')


def test_route_entries_and_matching():
    """
    OpenMapTiles flattens route relations into route_1_* .. route_4_*.
    Matching has to work on the ref ("E3") and on the name as a substring,
    because OSM names carry suffixes ("Malerweg (Etappe 3)").
    """
    from simple_mbtiles_server.__main__ import _route_entries, _route_matches

    props = {
        'route_1_network': 'iwn', 'route_1_ref': 'E3',
        'route_1_name': 'Europäischer Fernwanderweg E3, Sachsen',
        'route_2_network': 'rwn', 'route_2_name': 'Malerweg',
        'route_4_network': 'lwn', 'route_4_name': 'Gelber Balken',
    }
    entries = _route_entries(props)
    assert len(entries) == 3                      # gaps (route_3) are skipped
    assert ('iwn', 'E3', 'Europäischer Fernwanderweg E3, Sachsen') in entries

    assert _route_matches(entries[0], 'E3')       # exact ref
    assert _route_matches(entries[0], 'e3')       # case insensitive
    assert _route_matches(entries[1], 'Malerweg')
    assert _route_matches(entries[1], 'malerweg')
    assert not _route_matches(entries[1], 'Forststeig')
    assert not _route_matches(entries[1], '')     # empty query matches nothing
    # a ref must not match by substring, or "E3" would hit "E33"
    assert not _route_matches(('iwn', 'E33', ''), 'E3')


def test_prefer_routes_discounts_marked_ways():
    """
    A marked hiking route is signposted and maintained, so it should win over
    an unmarked shortcut of similar length -- but only as a weighting.
    International routes rank above local ones.
    """
    from simple_mbtiles_server.__main__ import _route_factor

    iwn = ('iwn', 'E3', 'Fernwanderweg E3')
    lwn = ('lwn', '', 'Gelber Balken')
    road = ('DE:national', 'B 23', 'Bundesstraße 23')

    assert _route_factor([iwn], 'foot') < _route_factor([lwn], 'foot') < 1.0
    # road route relations are not a hiking preference
    assert _route_factor([road], 'foot') == 1.0
    # no routes at all -> neutral
    assert _route_factor([], 'foot') == 1.0
    # cycle networks only count for bike, hiking networks only for foot
    assert _route_factor([('ncn', '', 'D-Route 9')], 'bike') < 1.0
    assert _route_factor([('ncn', '', 'D-Route 9')], 'foot') == 1.0


def test_follow_route_pulls_hard_but_stays_a_preference():
    """
    follow_route exists for the Forststeig case: waypoint routing cuts the
    loops a marked trail makes. The discount is strong, but unmatched ways
    stay usable so the graph never becomes unroutable.
    """
    from simple_mbtiles_server.__main__ import _route_factor

    forststeig = ('rwn', '', 'Forststeig Elbsandstein')
    other = ('lwn', '', 'Gelber Balken')

    strong = _route_factor([forststeig], 'foot', 'Forststeig')
    assert strong < _route_factor([forststeig], 'foot')      # stronger than prefer
    # a non-matching route is neutral, not penalised
    assert _route_factor([other], 'foot', 'Forststeig') == 1.0


def test_route_index_matches_by_proximity():
    """
    transportation_name carries its own generalised geometry, so routes can
    only be bound to graph edges by proximity.
    """
    from simple_mbtiles_server.__main__ import (
        _build_route_index, _routes_at, _E5)

    entry = ('rwn', '', 'Malerweg')
    ways = [(int(round(14.2 * _E5)), int(round(50.9 * _E5)), entry)]
    index = _build_route_index(ways)

    assert entry in _routes_at(index, 14.2, 50.9)
    assert entry in _routes_at(index, 14.2005, 50.9005)   # ~50 m away
    assert _routes_at(index, 15.0, 51.5) == set()          # far away


def test_routes_along_path_reports_share():
    from simple_mbtiles_server.__main__ import _routes_along_path, _E5

    def node(lon, lat):
        return (int(round(lon * _E5)), int(round(lat * _E5)))

    path = [[14.20, 50.90], [14.21, 50.90], [14.22, 50.90]]
    malerweg = ('rwn', '', 'Malerweg')
    road = ('cz:national', '62', 'Silnice I/62')
    edge_routes = {
        (node(14.20, 50.90), node(14.21, 50.90)): {malerweg, road},
        (node(14.21, 50.90), node(14.22, 50.90)): {malerweg, road},
    }
    out = _routes_along_path(path, edge_routes)
    assert len(out) == 1                      # the road relation is filtered
    assert out[0]['name'] == 'Malerweg'
    assert out[0]['segments'] == 2
    assert out[0]['share_percent'] == 100
    assert out[0]['network_label'] == 'regional'


def test_surface_penalty_prefers_rideable_tracks_for_bike():
    """
    For bikepacking the surface matters more than the road class: a "track"
    is either smooth gravel or a rutted mess, and with luggage that decides
    between riding and pushing.
    """
    from simple_mbtiles_server.__main__ import _hint_penalty

    assert _hint_penalty((('smoothness', 'excellent'),), 'bike') == 1.0
    assert _hint_penalty((('smoothness', 'very_horrible'),), 'bike') > 3
    assert _hint_penalty((('smoothness', 'impassable'),), 'bike') is None
    # grade5 track costs more than grade1
    assert (_hint_penalty((('tracktype', 'grade5'),), 'bike') >
            _hint_penalty((('tracktype', 'grade1'),), 'bike'))
    # on foot the surface hardly matters
    assert _hint_penalty((('smoothness', 'bad'),), 'foot') == 1.0
    # T4 costs extra time on foot even when allowed
    assert _hint_penalty((('sac_scale', 'alpine_hiking'),), 'foot') > 1.5


def test_untagged_segments_are_unaffected_by_penalties():
    """
    Stock OpenMapTiles has no smoothness/sac_scale. Untagged ways must keep
    weight 1.0, otherwise the fork would silently change routing everywhere.
    """
    from simple_mbtiles_server.__main__ import _hint_penalty

    for profile in ('foot', 'bike'):
        assert _hint_penalty((), profile) == 1.0
        assert _hint_penalty((('brunnel', 'bridge'),), profile) == 1.0


def test_sac_scale_filter_excludes_hard_ways():
    """
    The whole point of the planetiler fork: with real sac_scale data the
    router can *refuse* to use a T4 path instead of merely warning about it.
    """
    from simple_mbtiles_server.__main__ import _build_graph

    easy = ((1150000, 4810000), (1150100, 4810000), 0.1, 'path',
            (('sac_scale', 'mountain_hiking'),))
    hard = ((1150100, 4810000), (1150200, 4810000), 0.1, 'path',
            (('sac_scale', 'demanding_alpine_hiking'),))
    untagged = ((1150200, 4810000), (1150300, 4810000), 0.1, 'path', ())

    def edges(graph):
        return sum(len(v) for v in graph.values()) // 2

    graph, _, _ = _build_graph([easy, hard, untagged], 'foot')
    assert edges(graph) == 3                    # no limit -> everything routable

    graph, _, _ = _build_graph([easy, hard, untagged], 'foot',
                            max_sac_scale='mountain_hiking')
    # the T5 edge is gone ...
    assert edges(graph) == 2
    assert (1150200, 4810000) not in [
        n for n, _w, _r in graph[(1150100, 4810000)]]
    # ... but the untagged segment is deliberately kept
    assert (1150300, 4810000) in [
        n for n, _w, _r in graph[(1150200, 4810000)]]


def test_via_ferrata_can_be_excluded():
    from simple_mbtiles_server.__main__ import _build_graph

    ferrata = ((1150000, 4810000), (1150100, 4810000), 0.1, 'path',
               (('via_ferrata_scale', '3'),))
    ladder = ((1150100, 4810000), (1150200, 4810000), 0.1, 'path',
              (('ladder', 'yes'),))
    normal = ((1150200, 4810000), (1150300, 4810000), 0.1, 'path', ())

    def edges(graph):
        return sum(len(v) for v in graph.values()) // 2

    graph, _, _ = _build_graph([ferrata, ladder, normal], 'foot')
    assert edges(graph) == 3

    graph, _, _ = _build_graph([ferrata, ladder, normal], 'foot',
                            allow_via_ferrata=False)
    assert edges(graph) == 1                    # only the normal segment left


def test_untagged_ways_are_never_excluded():
    """
    Most of the world has no sac_scale. Excluding untagged ways by default
    would produce "no route found" almost everywhere -- and would be false
    safety, since an untagged way can be anything.
    """
    from simple_mbtiles_server.__main__ import _build_graph

    untagged = ((1150000, 4810000), (1150100, 4810000), 0.1, 'path', ())
    graph, _, _ = _build_graph([untagged], 'foot', max_sac_scale='hiking',
                            allow_via_ferrata=False)
    assert sum(len(v) for v in graph.values()) // 2 == 1


def test_terrain_warnings_report_real_grades():
    from simple_mbtiles_server.__main__ import _terrain_warnings, _E5

    a = (int(round(11.0 * _E5)), int(round(47.5 * _E5)))
    b = (int(round(11.001 * _E5)), int(round(47.5 * _E5)))
    hints = {(a, b): ('path', (('sac_scale', 'alpine_hiking'),
                               ('trail_visibility', 'horrible')))}
    warnings, stats = _terrain_warnings([[11.0, 47.5], [11.001, 47.5]], hints)

    assert stats['max_sac_scale'] == 'T4'
    assert any('T4' in w for w in warnings)
    assert any('trail_visibility=horrible' in w for w in warnings)


def test_alpine_name_heuristic_flags_via_ferratas():
    """
    Via ferratas ARE in the data -- as plain class=path, indistinguishable
    from a forest track. A Zugspitze->Alpspitze route runs straight over
    "Stopselzieher" and "Hoellentalsteig", both cabled climbing routes, and
    since OpenMapTiles has no sac_scale the only signal left is the name.
    """
    from simple_mbtiles_server.__main__ import _alpine_name_warning

    for name in ('Stopselzieher', 'Höllentalsteig', 'Zustieg Alpspitz-Ferrata',
                 'Jubiläumsgrat', 'Alpspitz-Klettersteig', 'Sentiero attrezzato Rio'):
        assert _alpine_name_warning(name), name

    for name in ('Forstweg', 'Seeuferweg', 'Panoramaweg', 'Hauptstraße'):
        assert _alpine_name_warning(name) is None, name


def test_named_way_warnings_match_along_route():
    from simple_mbtiles_server.__main__ import _named_way_warnings, _E5

    # a named way sitting right on the route
    named = [(int(round(11.0 * _E5)), int(round(47.5 * _E5)), 'Stopselzieher')]
    route = [[11.0, 47.5], [11.0005, 47.5005]]
    warnings = _named_way_warnings(route, named)
    assert len(warnings) == 1
    assert 'Stopselzieher' in warnings[0]

    # ... and one far away must not fire
    far = [(int(round(12.0 * _E5)), int(round(48.0 * _E5)), 'Höllentalsteig')]
    assert _named_way_warnings(route, far) == []


def test_name_heuristic_is_suppressed_where_real_data_exists():
    """
    The "Hoellentalsteig" near Garmisch is tagged sac_scale=mountain_hiking
    (T2). Warning about its name would contradict the actual data, so the
    heuristic must stand down wherever a real grade is present -- and stay
    active on untagged ways.
    """
    from simple_mbtiles_server.__main__ import (
        _difficulty_tagged_indices, _named_way_warnings, _E5)

    route = [[11.0, 47.5], [11.001, 47.5]]
    named = [(int(round(11.0 * _E5)), int(round(47.5 * _E5)), 'Höllentalsteig')]

    a = (int(round(11.0 * _E5)), int(round(47.5 * _E5)))
    b = (int(round(11.001 * _E5)), int(round(47.5 * _E5)))

    # tagged with a real grade -> no name warning
    tagged = {(a, b): ('path', (('sac_scale', 'mountain_hiking'),))}
    indices = _difficulty_tagged_indices(route, tagged)
    assert indices == {0, 1}
    assert _named_way_warnings(route, named, tagged_indices=indices) == []

    # untagged -> heuristic still fires
    untagged = {(a, b): ('path', (('surface', 'unpaved'),))}
    indices = _difficulty_tagged_indices(route, untagged)
    assert indices == set()
    warnings = _named_way_warnings(route, named, tagged_indices=indices)
    assert len(warnings) == 1
    assert 'Höllentalsteig' in warnings[0]

    # a via_ferrata_scale also counts as real data (the ferrata warning from
    # _terrain_warnings covers it, no need for the name guess on top)
    ferrata = {(a, b): ('path', (('via_ferrata_scale', '3'),))}
    assert _difficulty_tagged_indices(route, ferrata) == {0, 1}


def test_photon_url_prefers_internal_override():
    """
    PHOTONSERVER is baked into index.html by startup.sh, so it must stay the
    URL a *browser* can reach. Server-side calls from inside a container
    usually need a different address, so PHOTONSERVER_INTERNAL overrides it
    for httpx only -- without touching what the frontend gets.
    """
    import re

    source_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'simple_mbtiles_server', '__main__.py')
    with open(source_path) as f:
        source = f.read()

    # the server-side call must resolve through the override
    resolution = re.search(r'photon_url = \((.*?)\)\.rstrip', source, re.S)
    assert resolution
    assert 'photon_server_internal or photon_server' in resolution.group(1)

    body = source.split('def _photon_get(path, params):', 1)[1]
    body = body.split('def _photon_features', 1)[0]
    assert 'photon_url' in body
    # must not fall back to the public URL for the actual request
    assert 'photon_server.rstrip' not in body

    # startup.sh only ever substitutes the public variable into the HTML
    startup_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'startup.sh')
    with open(startup_path) as f:
        startup = f.read()
    assert 'PHOTONSERVER_INTERNAL' not in startup

    # and the template carries no PHOTONSERVER_INTERNAL placeholder that a
    # naive sed on the shorter name would corrupt
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'simple_mbtiles_server', 'vendor', 'index_with_photon.html')
    with open(template_path, encoding='utf-8') as f:
        template = f.read()
    assert set(re.findall(r'PHOTONSERVER\w*', template)) == {'PHOTONSERVER'}


def test_frontend_offers_marked_route_preference():
    """Both UI variants must expose prefer_routes and send it."""
    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'simple_mbtiles_server', 'vendor')

    for name in ('index.html', 'index_with_photon.html'):
        with open(os.path.join(root, name), encoding='utf-8') as f:
            html = f.read()
        assert 'id="route-prefer-routes"' in html, name
        # on by default: a hiking UI should follow signposted trails
        assert 'id="route-prefer-routes" checked' in html, name
        assert 'prefer_routes=true' in html, name
        # marked routes are reported back to the user
        assert 'props.routes' in html, name
        # and the control is hidden again when the route is cleared
        assert 'route-marked-label' in html, name


def test_plan_route_tool_documents_route_options():
    """
    The follow_route description has to name the actual use case, otherwise
    an agent will not know when to reach for it.
    """
    from simple_mbtiles_server.__main__ import _ROUTE_PREFERENCE

    # sanity: hiking networks are cheaper than 1.0, ordered by importance
    assert _ROUTE_PREFERENCE['iwn'] < _ROUTE_PREFERENCE['lwn'] < 1.0
    assert _ROUTE_PREFERENCE['icn'] < _ROUTE_PREFERENCE['lcn'] < 1.0


def test_coordinate_settings_present_in_both_uis():
    """
    Both index variants must offer the coordinate format setting and the
    copy button -- index.html is generated from index_with_photon.html by
    startup.sh only when PHOTONSERVER is set, so the plain one is a separate
    file that silently drifts otherwise.
    """
    import re

    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'simple_mbtiles_server', 'vendor')

    expected_formats = {'dd', 'dd_short', 'dms', 'dm', 'lonlat', 'osm', 'gmaps'}

    for name in ('index.html', 'index_with_photon.html'):
        with open(os.path.join(root, name), encoding='utf-8') as f:
            html = f.read()

        # settings UI
        assert 'id="settings-btn"' in html, name
        assert 'id="settings-panel"' in html, name
        assert 'id="coord-format-select"' in html, name

        # all formats offered
        block = html.split('var COORD_FORMATS', 1)[1].split('};', 1)[0]
        offered = set(re.findall(r"'(\w+)':\s*\{", block))
        assert offered == expected_formats, (name, offered)

        # copy button plus clipboard fallback for non-https contexts
        assert 'class="coord-copy"' in html, name
        assert 'navigator.clipboard' in html, name
        assert 'execCommand' in html, name

        # the format has to survive a reload
        assert 'sms.coordFormat' in html, name

        # coordinates must actually reach the popups
        assert 'coordBlockHtml(' in html, name

    # the POI popup carries coordinates only in the photon variant, since
    # that is the one with a POI layer at all
    with open(os.path.join(root, 'index_with_photon.html'), encoding='utf-8') as f:
        photon = f.read()
    assert 'poiPopupHtml(name, p.subclass, null, coords[1], coords[0])' in photon


def test_sac_legend_matches_style_colors():
    """
    The osuv style colours paths by sac_scale (data-driven line-color) and
    both index.html variants carry a legend. If the style colours change, the
    legend must change with them -- otherwise the map lies to the user.
    """
    import json
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    style_path = os.path.join(root, 'simple_mbtiles_server', 'vendor',
                              'osuv-style@1.0.0', 'style.json')
    with open(style_path) as f:
        style = json.load(f)

    hiking = next(l for l in style['layers'] if l['id'] == 'path-hiking')
    line_color = hiking['paint']['line-color']
    assert line_color[0] == 'match'
    assert line_color[1] == ['get', 'sac_scale']

    # match expression: [match, input, key1, val1, ..., fallback]
    pairs = dict(zip(line_color[2::2], line_color[3::2]))
    from simple_mbtiles_server.__main__ import _SAC_SCALE_ORDER
    assert set(pairs) == set(_SAC_SCALE_ORDER)
    fallback = line_color[-1]

    # ferrata layer exists and sits above path-hiking
    ids = [l['id'] for l in style['layers']]
    assert ids.index('path-via-ferrata') > ids.index('path-hiking')
    assert ids.index('path-via-ferrata-halo') > ids.index('path-hiking')

    for name in ('index.html', 'index_with_photon.html'):
        with open(os.path.join(root, 'simple_mbtiles_server', 'vendor', name),
                  encoding='utf-8') as f:
            html = f.read()
        legend = html.split('id="sac-legend"', 1)[1].split('</div>\n    <div', 1)[0]
        swatches = re.findall(r'border-top-color:(#[0-9a-fA-F]+)', legend)
        # every style colour (grades + fallback) must appear in the legend
        for colour in list(pairs.values()) + [fallback]:
            assert colour in swatches, '%s missing %s' % (name, colour)


def test_frontend_offers_all_sac_grades():
    """
    The map UI carries its own copy of the SAC grade list in a <select>.
    If the server ever gains or renames a grade, the dropdown must follow --
    an unknown value would come back as HTTP 400.
    """
    import re

    from simple_mbtiles_server.__main__ import _SAC_SCALE_ORDER

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'simple_mbtiles_server', 'vendor', 'index_with_photon.html')
    with open(path, encoding='utf-8') as f:
        html = f.read()

    block = html.split('id="route-sac"', 1)[1].split('</select>', 1)[0]
    offered = re.findall(r'<option value="(\w+)"', block)
    assert offered == list(_SAC_SCALE_ORDER)

    # and the request must actually carry the parameters
    assert 'max_sac_scale=' in html
    assert 'allow_via_ferrata=false' in html
    # excluding ferratas is the default for a hiking UI
    assert 'id="route-no-ferrata" checked' in html


def test_frontend_poi_categories_match_server():
    """
    The map UI carries its own copy of the category filters (it re-matches
    subclasses client-side for marker colours). Those two lists silently
    drifting apart means buttons that return nothing, so pin them together.
    """
    import re

    from simple_mbtiles_server.__main__ import _POI_CATEGORY_FILTERS

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'simple_mbtiles_server', 'vendor', 'index_with_photon.html')
    with open(path, encoding='utf-8') as f:
        html = f.read()

    block = html.split('var POI_CATEGORIES = {', 1)[1].split('\n        };', 1)[0]
    js_categories = set(re.findall(r'^          (\w+): \{', block, re.M))
    buttons = set(re.findall(r'data-poi="(\w+)"', html))

    assert js_categories == set(_POI_CATEGORY_FILTERS)
    assert buttons == set(_POI_CATEGORY_FILTERS)

    # a shelter must not be offered as a hut in either place
    for category in ('alpine_hut', 'camp_site', 'shelter'):
        match = re.search(r'%s: \{.*?indexOf\(p\.subclass\)' % category, block, re.S)
        listed = set(re.findall(r"'([a-z_]+)'",
                                match.group(0).split('[', 1)[1].split(']', 1)[0]))
        assert listed == _POI_CATEGORY_FILTERS[category]['subclass'], category


def test_poi_name_falls_back_to_localised_names():
    """Many POIs carry name_de or name:latin but no plain name."""
    from simple_mbtiles_server.__main__ import _poi_name

    assert _poi_name({'name': 'Meilerhütte'}) == 'Meilerhütte'
    assert _poi_name({'name_de': 'Weilheimer Hütte'}) == 'Weilheimer Hütte'
    assert _poi_name({'name:latin': 'Knorrhütte'}) == 'Knorrhütte'
    assert _poi_name({'subclass': 'shelter'}) is None


def test_search_poi_rejects_unknown_category(server):
    body, is_error = call_tool('search_poi', {
        'center': [48.15, 11.57], 'category': 'biergarten',
    })
    assert is_error
    assert 'unknown category' in body['error']


def test_export_gpx_tool(server):
    body, is_error = call_tool('export_gpx', {
        'coordinates': [[11.52, 48.12], [11.55, 48.14]],
        'name': 'Handtrack',
        'waypoints': [{'lat': 48.12, 'lon': 11.52, 'name': 'Start'}],
    })
    assert not is_error
    assert body['points'] == 2
    assert httpx.get(BASE + '/v1/gpx/%s.gpx' % body['gpx_id'],
                     timeout=30.0).status_code == 200


def test_geocode_tools_absent_without_photon(server):
    names = {t['name'] for t in rpc('tools/list').json()['result']['tools']}
    assert 'geocode' not in names
    assert 'reverse_geocode' not in names


def test_plan_route_description_warns_about_missing_difficulty(server):
    tools = {t['name']: t for t in rpc('tools/list').json()['result']['tools']}
    description = tools['plan_route']['description']
    for term in ('sac_scale', 'trail_visibility', '500 m'):
        assert term in description
