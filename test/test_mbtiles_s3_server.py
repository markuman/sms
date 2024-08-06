from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib
from xml.etree import (
    ElementTree as ET,
)

import httpx
import pytest


@contextmanager
def application(port=8080, max_attempts=500, aws_access_key_id='AKIAIOSFODNN7EXAMPLE'):
    outputs = {}

    put_object_no_raise('', b'')  # Ensures bucket created
    put_object('', '''
        <VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
            <Status>Enabled</Status>
        </VersioningConfiguration>
    '''.encode(), params=(('versioning', ''),))
    delete_all_objects()
    with open('test/counties.mbtiles', 'rb') as f:
        put_object('counties.mbtiles', f.read())

    process_definitions = {
        'web': ['python', '-m', 'mbtiles_s3_server']
    }

    process_outs = {
        name: (tempfile.NamedTemporaryFile(), tempfile.NamedTemporaryFile())
        for name, _ in process_definitions.items()
    }

    processes = {
        name: subprocess.Popen(
            args,
            # stdout=process_outs[name][0],
            # stderr=process_outs[name][1],
            env={
                **os.environ,
                'PORT': str(port),
                'AWS_ACCESS_KEY_ID': aws_access_key_id,
                'AWS_SECRET_ACCESS_KEY': (
                    'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
                ),
                'AWS_REGION': 'us-east-1',
                'MBTILES__1__URL': 'http://127.0.0.1:9000/my-bucket/counties.mbtiles',
                'MBTILES__1__MIN_ZOOM': '2',
                'MBTILES__1__MAX_ZOOM': '16',
                'MBTILES__1__IDENTIFIER': 'mytiles',
                'MBTILES__1__VERSION': '1.1',
                'HTTP_ACCESS_CONTROL_ALLOW_ORIGIN': 'https://my.test/',
            }
        )
        for name, args in process_definitions.items()
    }

    def read_and_close(f):
        f.seek(0)
        contents = f.read()
        f.close()
        return contents

    def stop():
        for _, process in processes.items():
            process.terminate()
        for _, process in processes.items():
            process.wait(timeout=10)
        output_errors = {
            name: (read_and_close(stdout), read_and_close(stderr))
            for name, (stdout, stderr) in process_outs.items()
        }
        return output_errors

    try:
        for i in range(0, max_attempts):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                    break
            except (OSError, ConnectionRefusedError):
                if i == max_attempts - 1:
                    raise
                time.sleep(0.02)

        yield (processes, outputs)
    finally:
        outputs.update(stop())
        delete_all_objects()


@pytest.fixture(name='processes')
def fixture_processes():
    with application() as (processes, outputs):
        yield (processes, outputs)


def test_meta_application_fails():
    # Ensure code coverage even on failing path
    with pytest.raises(ConnectionError):
        application(max_attempts=1).__enter__()


def test_meta_put_many_objects(processes):
    # Ensure code coverage on deleting of > 1000 objects

    for i in range(0, 501):
        put_object(str(i), str(i).encode())
        put_object(str(i), str(i).encode())


def test_tile_exists(processes):
    response_gzip = httpx.get('http://127.0.0.1:8080/v1/tiles/mytiles@1.1/0/0/0.mvt')
    response_gzip.raise_for_status()

    assert response_gzip.headers['access-control-allow-origin'] == 'https://my.test/'
    assert response_gzip.headers['content-encoding'] == 'gzip'
    assert response_gzip.status_code == 200

    response = httpx.get('http://127.0.0.1:8080/v1/tiles/mytiles@1.1/0/0/0.mvt', headers={
        b'accept-encoding': b'identity',
    })
    response.raise_for_status()
    assert 'content-encoding' not in response.headers

    assert response.content == response_gzip.content


def test_tile_does_not_exists(processes):
    response = httpx.get('http://127.0.0.1:8080/v1/tiles/mytiles@1.1/1/9999/9999.mvt')
    assert response.status_code == 404


def test_tile_file_does_not_exists(processes):
    response = httpx.get('http://127.0.0.1:8080/v1/tiles/notmytiles@1.1/0/0/0.mvt')
    assert response.status_code == 404


def test_styles_file(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@1.0.0/'
        'style.json?fonts=fonts-gl@1.0.0&tiles=mytiles@1.1'
    )
    assert response.status_code == 200

    style_dict = json.loads(response.content)
    assert style_dict['name'] == 'Positron'
    assert style_dict['sources'] == {
        'openmaptiles': {
            'type': 'vector',
            'tiles': ['http://127.0.0.1:8080/v1/tiles/mytiles@1.1/{z}/{x}/{y}.mvt'],
            'minzoom': 2,
            'maxzoom': 16,
        },
    }
    assert style_dict['glyphs'] == \
        'http://127.0.0.1:8080/v1/fonts/fonts-gl@1.0.0/{fontstack}/{range}.pbf'
    assert style_dict['sprite'] == \
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@1.0.0/sprite'


def test_styles_file_x_forwarded(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@1.0.0/'
        'style.json?fonts=fonts-gl@1.0.0&tiles=mytiles@1.1',
        headers={
            'host': 'www.mypublicdomain.com',
            'x-forwarded-proto': 'https',
        }
    )
    assert response.status_code == 200

    style_dict = json.loads(response.content)
    assert style_dict['name'] == 'Positron'
    assert style_dict['sources'] == {
        'openmaptiles': {
            'type': 'vector',
            'tiles': ['https://www.mypublicdomain.com/v1/tiles/mytiles@1.1/{z}/{x}/{y}.mvt'],
            'minzoom': 2,
            'maxzoom': 16,
        },
    }
    assert style_dict['glyphs'] == \
        'https://www.mypublicdomain.com/v1/fonts/fonts-gl@1.0.0/{fontstack}/{range}.pbf'
    assert style_dict['sprite'] == \
        'https://www.mypublicdomain.com/v1/styles/positron-gl-style@1.0.0/sprite'


def test_sprites_exists(processes):
    urls = (
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@1.0.0/sprite.json',
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@1.0.0/sprite@2x.json',
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@1.0.0/sprite.png',
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@1.0.0/sprite@2x.png',
    )

    for url in urls:
        response = httpx.get(url)
        response.raise_for_status()
        assert response.status_code == 200
        assert len(response.content) > 10


def test_sprites_not_exist(processes):
    urls = (
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@2.0.0/sprite.json',
        'http://127.0.0.1:8080/v1/styles/positron-gl-style@1.0.0/sprite@3x.png',
        'http://127.0.0.1:8080/v1/styles/maptiler-basic-gl-style@1.0.0/sprite.json',
        'http://127.0.0.1:8080/v1/styles/another-one@1.0.0/sprite.json',
    )

    for url in urls:
        response = httpx.get(url)
        assert response.status_code == 404


def test_styles_file_does_not_exists(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles'
        '/positron-gl-style@1.0.0/notmystyle.json?fonts=fonts-gl&tiles=mytiles@1.1')
    assert response.status_code == 404


def test_styles_file_without_tiles_argument(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles/'
        'positron-gl-style@1.0.0/style.json?fonts=fonts-gl@1.0.0')
    assert response.status_code == 400


def test_styles_file_without_tiles_version(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles/'
        'positron-gl-style@1.0.0/style.json?fonts=fonts-gl@1.0.0&tiles=mytiles')
    assert response.status_code == 400


def test_styles_file_with_tiles_that_does_not_exists(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles'
        '/positron-gl-style@1.0.0/style.json?fonts=fonts-gl@1.0.0&tiles=notmytiles@1.1')
    assert response.status_code == 404


def test_styles_file_without_fonts_argument(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles/'
        'positron-gl-style@1.0.0/style.json?tiles=mytiles@1.1')
    assert response.status_code == 400


def test_styles_file_without_fonts_version(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles/'
        'positron-gl-style@1.0.0/style.json?fonts=fonts-gl&tiles=mytiles@1.1')
    assert response.status_code == 400


def test_styles_file_with_fonts_that_does_not_exists(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/styles'
        '/positron-gl-style@1.0.0/style.json?fonts=not-exists@1.0.0&tiles=mytiles@1.1')
    assert response.status_code == 404


def test_static_file(processes):
    response = httpx.get('http://127.0.0.1:8080/v1/static'
                         '/maplibre-gl@2.1.9/maplibre-gl.css')
    assert response.status_code == 200


def test_static_file_not_exist(processes):
    response = httpx.get('http://127.0.0.1:8080/v1/static'
                         '/maplibre-gl@2.1.9/maplibre-gl.not')
    assert response.status_code == 404


def test_font_file(processes):
    response_gzip = httpx.get(
        'http://127.0.0.1:8080/v1/fonts/fonts-gl@1.0.0/Metropolis Regular/0-255.pbf')
    assert response_gzip.status_code == 200
    assert response_gzip.headers['content-encoding'] == 'gzip'

    response = httpx.get(
        'http://127.0.0.1:8080/v1/fonts/fonts-gl@1.0.0/Metropolis Regular/0-255.pbf',
        headers={
            b'accept-encoding': b'identity'
        }
    )
    assert response.status_code == 200
    assert 'content-encoding' not in response.headers

    assert response_gzip.content == response.content


def test_font_files(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/fonts'
        '/fonts-gl@1.0.0/Metropolis Regular,Noto Sans Regular/0-255.pbf')
    assert response.status_code == 200
    assert len(response.content) > 1000


def test_font_pack_that_does_not_exist(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/fonts/not-exist@1.0.0/Metropolis Regular/0-255.pbf')
    assert response.status_code == 404


def test_font_that_does_not_exist(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/fonts/fonts-gl@1.0.0/Jane/0-255.pbf')
    assert response.status_code == 404


def test_font_file_that_does_not_exist(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/fonts/fonts-gl@1.0.0/Metropolis Regular/0-254.pbf')
    assert response.status_code == 404


def test_font_stack_too_large(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/fonts/fonts-gl@1.0.0/a,a,a,a,a,a/0-255.pbf')
    assert response.status_code == 400


def test_font_file_that_does_not_exist_with_dot(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/fonts/fonts-gl@1.0.0/Metropolis..Regular/0-255.pbf')
    assert response.status_code == 404


def test_font_file_range_that_does_not_exist_with_dot(processes):
    response = httpx.get(
        'http://127.0.0.1:8080/v1/fonts/fonts-gl@1.0.0/Metropolis..Regular/0-..255.pbf')
    assert response.status_code == 404


def put_object_no_raise(key, contents, params=()):
    url = f'http://127.0.0.1:9000/my-bucket/{key}'
    body_hash = hashlib.sha256(contents).hexdigest()
    parsed_url = urllib.parse.urlsplit(url)

    headers = aws_sigv4_headers(
        'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        (), 's3', 'us-east-1', parsed_url.netloc, 'PUT', parsed_url.path, params, body_hash,
    )
    httpx.put(url, params=params, data=contents, headers=dict(headers))


def put_object(key, contents, params=()):
    url = f'http://127.0.0.1:9000/my-bucket/{key}'
    body_hash = hashlib.sha256(contents).hexdigest()
    parsed_url = urllib.parse.urlsplit(url)

    headers = aws_sigv4_headers(
        'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        (), 's3', 'us-east-1', parsed_url.netloc, 'PUT', parsed_url.path, params, body_hash,
    )
    response = httpx.put(url, params=params, data=contents, headers=dict(headers))
    response.raise_for_status()


def delete_all_objects():
    def list_keys():
        url = 'http://127.0.0.1:9000/my-bucket/'
        parsed_url = urllib.parse.urlsplit(url)
        namespace = '{http://s3.amazonaws.com/doc/2006-03-01/}'
        key_marker = ''
        version_marker = ''

        def _list(extra_query_items=()):
            nonlocal key_marker, version_marker

            key_marker = ''
            version_marker = ''
            query = (
                ('max-keys', '1000'),
                ('versions', ''),
            ) + extra_query_items

            body = b''
            body_hash = hashlib.sha256(body).hexdigest()
            headers = aws_sigv4_headers(
                'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                (), 's3', 'us-east-1', parsed_url.netloc, 'GET', parsed_url.path, query, body_hash,
            )
            response = httpx.get(url, params=query, headers=dict(headers))
            response.raise_for_status()
            body_bytes = response.content

            for element in ET.fromstring(body_bytes):
                if element.tag in (f'{namespace}Version', f'{namespace}DeleteMarker'):
                    for child in element:
                        if child.tag == f'{namespace}Key':
                            key = child.text
                        if child.tag == f'{namespace}VersionId':
                            version_id = child.text
                    yield key, version_id
                if element.tag == f'{namespace}NextKeyMarker':
                    key_marker = element.text
                if element.tag == f'{namespace}NextVersionIdMarker':
                    version_marker = element.text

        yield from _list()

        while key_marker:
            yield from _list((('key-marker', key_marker), ('version-marker', version_marker)))

    for key, version_id in list_keys():
        url = f'http://127.0.0.1:9000/my-bucket/{key}'
        params = (('versionId', version_id),)
        parsed_url = urllib.parse.urlsplit(url)
        body = b''
        body_hash = hashlib.sha256(body).hexdigest()
        headers = aws_sigv4_headers(
            'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            (), 's3', 'us-east-1', parsed_url.netloc, 'DELETE', parsed_url.path, params, body_hash,
        )
        response = httpx.delete(url, params=params, headers=dict(headers))
        response.raise_for_status()


def aws_sigv4_headers(access_key_id, secret_access_key, pre_auth_headers,
                      service, region, host, method, path, params, body_hash):
    algorithm = 'AWS4-HMAC-SHA256'

    now = datetime.utcnow()
    amzdate = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
    credential_scope = f'{datestamp}/{region}/{service}/aws4_request'

    pre_auth_headers_lower = tuple((
        (header_key.lower(), ' '.join(header_value.split()))
        for header_key, header_value in pre_auth_headers
    ))
    required_headers = (
        ('host', host),
        ('x-amz-content-sha256', body_hash),
        ('x-amz-date', amzdate),
    )
    headers = sorted(pre_auth_headers_lower + required_headers)
    signed_headers = ';'.join(key for key, _ in headers)

    def signature():
        def canonical_request():
            canonical_uri = urllib.parse.quote(path, safe='/~')
            quoted_params = sorted(
                (urllib.parse.quote(key, safe='~'), urllib.parse.quote(value, safe='~'))
                for key, value in params
            )
            canonical_querystring = '&'.join(f'{key}={value}' for key, value in quoted_params)
            canonical_headers = ''.join(f'{key}:{value}\n' for key, value in headers)

            return f'{method}\n{canonical_uri}\n{canonical_querystring}\n' + \
                   f'{canonical_headers}\n{signed_headers}\n{body_hash}'

        def sign(key, msg):
            return hmac.new(key, msg.encode('ascii'), hashlib.sha256).digest()

        string_to_sign = f'{algorithm}\n{amzdate}\n{credential_scope}\n' + \
                         hashlib.sha256(canonical_request().encode('ascii')).hexdigest()

        date_key = sign(('AWS4' + secret_access_key).encode('ascii'), datestamp)
        region_key = sign(date_key, region)
        service_key = sign(region_key, service)
        request_key = sign(service_key, 'aws4_request')
        return sign(request_key, string_to_sign).hex()

    return (
        (b'authorization', (
            f'{algorithm} Credential={access_key_id}/{credential_scope}, '
            f'SignedHeaders={signed_headers}, Signature=' + signature()).encode('ascii')
         ),
        (b'x-amz-date', amzdate.encode('ascii')),
        (b'x-amz-content-sha256', body_hash.encode('ascii')),
    ) + pre_auth_headers
