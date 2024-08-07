# simple mbtiles server

Based on https://github.com/uktrade/mbtiles-s3-server, but without S3 dependencies.


## Install Entire Planet

For mbtiles generation and hosting, donations are welcome :)
  * paypal.me/MarkusBergholz
  * bc1qz33cf70vq82gxf8kps06j7lv7m2903hsnjak6k

```
mkdir osm
wget -O osm/planet.mbtiles https://hidrive.ionos.com/api/sharelink/download?id=SYEgScrRe
podman run -ti --rm -p 9000:9000 --name sms -v $(pwd)/osm/:/data/ registry.gitlab.com/markuman/sms:latest
firefox http://localhost:9000
```

### nextcloud GpxPod

1. Deploy the container/service behind a webproxy (_caddy, nginx, traefik,...you name it._) to get a valid SSL version.
2. Goto GpxPod Settings -> Tile Servers
    * Type: Vector
    * Server address: `https://<YOUR_SMS_SERVICE_DEPLOYMENT>/v1/styles/osm-bright-gl-style@1.0.0/style.json?fonts=fonts-gl@1.0.0&tiles=mytiles@1.0.0`


## HELP WANTED

* Improve Style for Nextcloud GxpPod
* Add contour lines

## Example usage

1. Create or obtain an mbtiles file, for example from https://openmaptiles.org/.
2. Start this server, configured with the location of this object and credentials for this user - it's configured using environment variables. You can assign the tiles file any version you like, in this case, `1.0.0`.

   ```bash
      #!/bin/bash
      PORT=8080 \
      MBTILES__1__URL=/home/m/osm/planet.mbtiles \
      MBTILES__1__MIN_ZOOM=0 \
      MBTILES__1__MAX_ZOOM=14 \
      MBTILES__1__IDENTIFIER=mytiles \
      MBTILES__1__VERSION=1.0.0 \
      HTTP_ACCESS_CONTROL_ALLOW_ORIGIN="*" \
      python3 -m simple_mbtiles_server
   ```

3. On your user-facing site, include HTML that loads these tiles from this server, for example to load maps from a server started as above running locally serving OpenMapTiles

   ```html
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8">
        <title>Example map</title>
        <meta name="viewport" content="initial-scale=1,maximum-scale=1,user-scalable=no">
        <script src="http://localhost:8080/v1/static/maplibre-gl@2.1.9/maplibre-gl.js"></script>
        <link href="http://localhost:8080/v1/static/maplibre-gl@2.1.9/maplibre-gl.css" rel="stylesheet">
        <style>
          body, html, #map {margin: 0; padding: 0; height: 100%; width: 100%}
        </style>
      </head>
      <body>
        <div id="map"></div>
        <script>
        var map = new maplibregl.Map({
            container: 'map',
            style: 'http://localhost:8080/v1/styles/osm-bright-gl-style@1.0.0/style.json?fonts=fonts-gl@1.0.0&tiles=mytiles@1.0.0',
            center: [0, 0],
            zoom: 1,
            attributionControl: false
        });
        map.addControl(new maplibregl.AttributionControl({
            customAttribution: '<a href="https://openmaptiles.org/">© OpenMapTiles</a> <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>'
        }), 'bottom-right');
        </script>
      </body>
    </html>
   ```

   This HTML is included in this repository in [example.html](./example.html). A simple server can be started to view it by

   ```bash
   python -m http.server 8081 --bind 127.0.0.1
   ````

   and going to [http://localhost:8081/example.html](http://localhost:8081/example.html)


## Core API

**`GET /v1/tiles/{identifier}@{version}/{z}/{x}/{y}.mvt`**

Fetch a tile in Mapbox Vector Tile (MVT) format

- `identifier`

  An aribtrary identifier for a tileset configued via environment variables when starting the server, for example `MBTILES__1__IDENTIFIER` (see [Example usage](#example-usage)).

- `version`

  A version for a tileset configued via environment variables, for example `MBTILES__1__VERSION` (see [Example usage](#example-usage)). The version is part of the API to encourage releasing a new version of a tileset rather than replacing an existing one.

  An arbitrary version of the tileset identified by the `identifier`.

- `z`, `x`, `y`

  The xyz coordinates of a tile in this tileset


## For the curious, advanced, or developers of this server itself

Hosting your own vector map tiles to show them in a browser requires quite a few components:

1. **JavaScript and CSS**

   A Javascript and CSS library, such as [MapLibre GL](https://github.com/maplibre/maplibre-gl-js), and your own code to run this library, pointing it to a style file

2. **Style file**

   A JSON file that defines how the library should visually style the map data, and where it should find the map tiles, glyphs (fonts), and the sprite. This server transforms the built-in Style files on the fly to be able to refer to any map data.

3. **Glyphs** (fonts)

   Different fonts can be used for different labels and zoom levels, as defined in the Style file. The fonts must Signed Distance Field (SDF) fonts wrapped in a particular Protocol Buffer format. The Style file can refer to "stacks" of fonts; but unlike CSS, the server combines the fonts on the fly in an API where the resulting "font" has at most one glyph from each source font.

4. **Sprite**

   A sprite is actually 4 URLs: a JSON index file and a single PNG file, and a "@2x" JSON index file and PNG files for higher pixel ratio devices (e.g. Retina). The JSON files contains the offsets and sizes of images within corresponding PNG file. The style file refers the common "base" of these. For example, if the style file has `"sprite":"https://my.test/sprite"` then the 4 files must be at `https://my.test/sprite.json`, `https://my.test/sprite.png`, `https://my.test/sprite@2x.json` and `https://my.test/sprite@2x.png`.

5. **Vector map tiles**

   A set of often millions of tiles each covering a different location and different zoom level. These can be distributed as a single mbtiles file, but this is not the format that the Javascript library accepts. This on-the-fly conversion from the mbtiles file to tiles is the main feature of this server.

   The mbtiles file is a SQLite file, containing gzipped Mapbox Vector Tile tiles. This server leaves the un-gzipping to the browser, by sending tiles with a `content-encoding: gzip` header, which results in browser un-gzipping the tile data before it hits the Javascript.


## Licenses

The code of the server itself is released under the MIT license. However, several components included in [mbtiles_s3_server/vendor/](./mbtiles_s3_server/vendor/) are released under different licenses.
