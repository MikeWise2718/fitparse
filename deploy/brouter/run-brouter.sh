#!/bin/bash
# launchd (com.fitmon.brouter): the routing engine for fitmon's planner. Loopback only - only
# fitmon calls it - on 8642, reserved in pokeflute's registry as fitmon-brouter.
# Installed by deploy/brouter/setup-brouter.sh into ~/brouter.
cd ~/brouter
exec ./jre/bin/java -Xmx768m -Xms128m -DmaxRunningTime=60 \
    -cp "$(ls current/brouter-*-all.jar | head -1)" btools.server.RouteServer \
    segments4 current/profiles2 customprofiles 8642 4 127.0.0.1
