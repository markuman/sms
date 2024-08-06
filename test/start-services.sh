#!/bin/sh

set -e

docker run --rm -p 9000:9000 --name mbtiles-s3-server-minio -d \
  -e 'MINIO_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE' \
  -e 'MINIO_SECRET_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY' \
  -e 'MINIO_REGION=us-east-1' \
  --entrypoint sh \
  minio/minio:RELEASE.2022-07-04T21-02-54Z \
  -c 'mkdir -p /data1 && mkdir -p /data2 && mkdir -p /data3 && mkdir -p /data4 && minio server /data{1...4}'
