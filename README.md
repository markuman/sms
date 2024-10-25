# 🗺️ OSM - self host the entire planet 🌎 in ~30 minutes 🚀

**s**imple **m**btiles **s**erver

#### TL;DR

```
mkdir osm
wget -O osm/planet.mbtiles https://hidrive.ionos.com/api/sharelink/download?id=SYEgScrRe
podman run -ti --rm -p 9000:9000 --name sms -v "$(pwd)/osm/:/data/" registry.gitlab.com/markuman/sms:latest
firefox http://localhost:9000
```

requirements: 
* `podman` (_or `docker`_)
* 90 GB storage is required (_1 core and 512MB memory are sufficient_)

notes:
* "_~30 minutes_" depends on your bandwidth ...and the hidrive performance of ionos.


# credits

* https://github.com/onthegomap/planetiler is used to generate the planet.mbtiles file
* https://github.com/uktrade/mbtiles-s3-server is the origin code-base of my `sms` project


### nextcloud GpxPod

1. Deploy the container/service behind a webproxy (_caddy, nginx, traefik,...you name it._) to get a valid SSL certificate.
2. Goto GpxPod Settings -> Tile Servers
    * Type: Vector
    * Server address: `https://<YOUR_SMS_SERVICE_DEPLOYMENT>/v1/styles/osm-bright-gl-style@1.0.0/style.json?fonts=fonts-gl@1.0.0&tiles=mytiles@1.0.0`


## HELP WANTED

* Improve Style
  * special for Nextcloud GxpPod
  * provide more style? remove some?
* Add contour lines

## planet.mbtiles

My provided planet.mbtiles is generated by using https://github.com/onthegomap/planetiler  
I just followed this tutorial: https://github.com/onthegomap/planetiler/blob/main/PLANET.md


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

# support

* My Project: https://github.com/markuman/sms
* For the large planet.mbtiles generation and hosting, donations are welcome 🙂
  * paypal.me/MarkusBergholz
  * bc1qz33cf70vq82gxf8kps06j7lv7m2903hsnjak6k
