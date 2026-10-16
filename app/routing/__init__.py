"""Route planning: geometry helpers, the routing-engine client and the length-fitting planner.

Nothing in here touches the database or Flask; app/services/routelib.py is the library on top.
Coordinates are [lon, lat] or [lon, lat, ele] lists throughout, as in GeoJSON.
"""
