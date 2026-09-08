"""
GPX serialisation and a tiny on-disk store for generated tracks.

Kept dependency free on purpose: the whole point of sms is "one container,
no extras".
"""

import os
import time
import uuid
from xml.sax.saxutils import escape


def to_gpx(coords, name='sms track', wpts=None, elevations=None):
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
                f'<ele>{float(ele):.1f}</ele></trkpt>'
            )
    pts = ''.join(parts)

    w = ''.join(
        f'<wpt lat="{float(p["lat"]):.6f}" lon="{float(p["lon"]):.6f}">'
        f'<name>{escape(str(p.get("name", "")))}</name></wpt>'
        for p in (wpts or [])
    )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="sms" xmlns="http://www.topografix.com/GPX/1/1">'
        f'{w}<trk><name>{escape(str(name))}</name><trkseg>{pts}</trkseg></trk></gpx>'
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
            and all(c in '0123456789abcdef' for c in gpx_id)
        )

    def path_for(self, gpx_id):
        if not self.valid_id(gpx_id):
            return None
        return os.path.join(self.directory, gpx_id + '.gpx')

    def write(self, xml):
        gpx_id = uuid.uuid4().hex[:16]
        with open(
            os.path.join(self.directory, gpx_id + '.gpx'), 'w', encoding='utf-8'
        ) as f:
            f.write(xml)
        self._cleanup()
        return gpx_id

    def read(self, gpx_id):
        path = self.path_for(gpx_id)
        if path is None or not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def _cleanup(self):
        try:
            entries = []
            now = time.time()
            for name in os.listdir(self.directory):
                if not name.endswith('.gpx'):
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
