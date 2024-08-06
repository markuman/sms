#!/bin/bash
PORT=8080 \
MBTILES__1__URL=/home/m/osm/planet.mbtiles \
MBTILES__1__MIN_ZOOM=0 \
MBTILES__1__MAX_ZOOM=14 \
MBTILES__1__IDENTIFIER=mytiles \
MBTILES__1__VERSION=1.0.0 \
HTTP_ACCESS_CONTROL_ALLOW_ORIGIN="*" \
python3 -m simple_mbtiles_server
