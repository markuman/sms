"""
GPX serialisation and a tiny on-disk store for generated tracks.

Kept dependency free on purpose: the whole point of sms is "one container,
no extras".
"""

import math
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


class GpxError(Exception):
    """Raised for GPX input that cannot be used."""


_LOCAL_NAME_RE = re.compile(r"^\{[^}]*\}")
_DOCTYPE_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def _local(tag):
    """Strip the XML namespace: GPX 1.0, 1.1 and namespace-less files are all
    in the wild, and the element names are identical across them."""
    return _LOCAL_NAME_RE.sub("", tag)


def _child_text(element, name):
    for child in element:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _point(element):
    try:
        lat = float(element.get("lat"))
        lon = float(element.get("lon"))
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    ele = _child_text(element, "ele")
    if ele is not None:
        try:
            ele = float(ele)
            if not math.isfinite(ele):
                ele = None
        except ValueError:
            ele = None
    return lon, lat, ele


def parse_gpx(data, max_points=200000):
    """
    Parse a GPX document (bytes or str).

    Returns {'name', 'points': [[lon, lat, ele_or_None], ...], 'waypoints':
    [{'lat', 'lon', 'name'}], 'tracks': n, 'has_time': bool}.  All track
    segments (and, if there is no track, all routes) are concatenated in
    document order.  Raises GpxError on anything unusable.

    Files with a DOCTYPE or entity declarations are rejected outright: expat
    expands entities, and we do not want to pull in defusedxml for a file
    format that never needs them.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data or not data.strip():
        raise GpxError("empty gpx document")
    if _DOCTYPE_RE.search(data[:4096]):
        raise GpxError("gpx with DOCTYPE or entity declarations is not accepted")

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise GpxError(f"not well-formed xml: {exc}")

    if _local(root.tag) != "gpx":
        raise GpxError(f"root element is <{_local(root.tag)}>, expected <gpx>")

    name = None
    tracks = 0
    has_time = False
    points = []
    waypoints = []

    for child in root:
        tag = _local(child.tag)
        if tag == "metadata" and name is None:
            name = _child_text(child, "name")
        elif tag == "wpt":
            p = _point(child)
            if p is not None:
                waypoints.append(
                    {"lat": p[1], "lon": p[0], "name": _child_text(child, "name") or ""}
                )
        elif tag == "trk":
            tracks += 1
            if name is None:
                name = _child_text(child, "name")
            for seg in child:
                if _local(seg.tag) != "trkseg":
                    continue
                for pt in seg:
                    if _local(pt.tag) != "trkpt":
                        continue
                    p = _point(pt)
                    if p is None:
                        continue
                    if not has_time and _child_text(pt, "time"):
                        has_time = True
                    points.append([p[0], p[1], p[2]])
                    if len(points) > max_points:
                        raise GpxError(f"too many track points (limit {max_points})")

    if not points:
        # no <trk>: fall back to <rte>, which planners like to export
        for child in root:
            if _local(child.tag) != "rte":
                continue
            tracks += 1
            if name is None:
                name = _child_text(child, "name")
            for pt in child:
                if _local(pt.tag) != "rtept":
                    continue
                p = _point(pt)
                if p is not None:
                    points.append([p[0], p[1], p[2]])
                    if len(points) > max_points:
                        raise GpxError(f"too many route points (limit {max_points})")

    if len(points) < 2:
        raise GpxError("gpx contains fewer than two track points")

    return {
        "name": name or "gpx track",
        "points": points,
        "waypoints": waypoints,
        "tracks": tracks,
        "has_time": has_time,
    }


def gpx_ascent_descent(elevations, hysteresis_m=10.0):
    """
    Ascent/descent from a list of elevations (None entries are skipped).

    Plain summing of consecutive differences turns GPS noise into hundreds of
    phantom metres.  This tracks the last *committed* elevation and only
    commits a change once it exceeds *hysteresis_m*, the same idea most
    track editors use.  Returns (ascent_m, descent_m) as ints.
    """
    ascent = descent = 0.0
    anchor = None  # elevation where the current leg started
    extreme = None  # highest (climbing) / lowest (descending) point seen since
    direction = 0  # +1 climbing, -1 descending, 0 undecided
    for ele in elevations:
        if ele is None:
            continue
        if anchor is None:
            anchor = extreme = ele
            continue

        if direction == 0:
            if ele - anchor >= hysteresis_m:
                direction, extreme = 1, ele
            elif anchor - ele >= hysteresis_m:
                direction, extreme = -1, ele
        elif direction == 1:
            if ele > extreme:
                extreme = ele
            elif extreme - ele >= hysteresis_m:
                # the climb is over: book it, start a descent from the top
                ascent += extreme - anchor
                anchor, extreme, direction = extreme, ele, -1
        else:
            if ele < extreme:
                extreme = ele
            elif ele - extreme >= hysteresis_m:
                descent += anchor - extreme
                anchor, extreme, direction = extreme, ele, 1

    if anchor is not None:
        if direction == 1:
            ascent += extreme - anchor
        elif direction == -1:
            descent += anchor - extreme
    return int(round(ascent)), int(round(descent))


def to_gpx(coords, name="sms track", wpts=None, elevations=None):
    """
    Build a GPX 1.1 document.

    coords      -- list of [lon, lat] pairs (GeoJSON order!)
    name        -- track name
    wpts        -- optional list of {'lat': .., 'lon': .., 'name': ..}
    elevations  -- optional list of elevations in metres, same length as coords
    """
    parts = []
    for i, point in enumerate(coords):
        lon, lat = float(point[0]), float(point[1])
        ele = None
        if elevations is not None and i < len(elevations):
            ele = elevations[i]
        if ele is None:
            parts.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"/>')
        else:
            parts.append(
                f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}">'
                f"<ele>{float(ele):.1f}</ele></trkpt>"
            )
    pts = "".join(parts)

    w = "".join(
        f'<wpt lat="{float(p["lat"]):.6f}" lon="{float(p["lon"]):.6f}">'
        f"<name>{escape(str(p.get('name', '')))}</name></wpt>"
        for p in (wpts or [])
    )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="sms" xmlns="http://www.topografix.com/GPX/1/1">'
        f"{w}<trk><name>{escape(str(name))}</name><trkseg>{pts}</trkseg></trk></gpx>"
    )


class GpxStore:
    """
    Writes GPX files into a directory and hands out ids.

    Housekeeping runs on every write: files older than *ttl_seconds* are
    removed, and if more than *max_files* remain the oldest ones go too.
    """

    def __init__(self, directory, max_files=200, ttl_seconds=86400):
        self.directory = directory
        self.max_files = max_files
        self.ttl_seconds = ttl_seconds
        os.makedirs(self.directory, exist_ok=True)

    @staticmethod
    def valid_id(gpx_id):
        return (
            isinstance(gpx_id, str)
            and 8 <= len(gpx_id) <= 40
            and all(c in "0123456789abcdef" for c in gpx_id)
        )

    def path_for(self, gpx_id):
        if not self.valid_id(gpx_id):
            return None
        return os.path.join(self.directory, gpx_id + ".gpx")

    def write(self, xml):
        gpx_id = uuid.uuid4().hex[:16]
        with open(
            os.path.join(self.directory, gpx_id + ".gpx"), "w", encoding="utf-8"
        ) as f:
            f.write(xml)
        self._cleanup()
        return gpx_id

    def read(self, gpx_id):
        path = self.path_for(gpx_id)
        if path is None or not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _cleanup(self):
        try:
            entries = []
            now = time.time()
            for name in os.listdir(self.directory):
                if not name.endswith(".gpx"):
                    continue
                path = os.path.join(self.directory, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if now - mtime > self.ttl_seconds:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                entries.append((mtime, path))

            if len(entries) > self.max_files:
                entries.sort()
                for _, path in entries[: len(entries) - self.max_files]:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        except OSError:
            pass
