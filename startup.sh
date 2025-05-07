#!/bin/sh

if [ -n "$PHOTONSERVER" ]; then
    echo "PHOTONSERVER is used: $PHOTONSERVER"
    # Hier deine gewünschte Aktion:
    # z. B.: ./starte_photon.sh
    sed "s,PHOTONSERVER,${PHOTONSERVER}," /sms/simple_mbtiles_server/vendor/index_with_photon.html > /sms/simple_mbtiles_server/vendor/index.html
fi

python -m simple_mbtiles_server

