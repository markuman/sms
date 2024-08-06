FROM python:3-alpine

RUN pip install --upgrade pip
RUN mkdir /sms
RUN mkdir /data # mount here planet.mbtiles

COPY simple_mbtiles_server/ /sms/simple_mbtiles_server/
COPY requirements.txt /sms/
COPY setup.py /sms/
COPY README.md /sms/

WORKDIR /sms
RUN pip install .

ENV PORT=9000
ENV MBTILES__1__URL=/data/planet.mbtiles
ENV MBTILES__1__MIN_ZOOM=0
ENV MBTILES__1__MAX_ZOOM=14
ENV MBTILES__1__IDENTIFIER=mytiles
ENV MBTILES__1__VERSION=1.0.0
ENV HTTP_ACCESS_CONTROL_ALLOW_ORIGIN="*"

CMD python -m simple_mbtiles_server
