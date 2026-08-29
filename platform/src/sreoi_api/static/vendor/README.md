# Vendored assets

`maplibre-gl` 4.7.1 (BSD-3-Clause) is vendored rather than loaded from a CDN.

Two reasons, both deliberate:

1. **Privacy and residency.** A third-party tile server or CDN sees every map
   viewport a user pans to, which is a signal about their investment interest.
   Under a PDPL-conscious posture that traffic should not leave our control.
2. **Reliability.** The product must work on restricted networks. A map that
   silently renders blank because a CDN is unreachable is worse than no map.

The base layer is our own district geometry from PostGIS, not an external
raster basemap, for the same reasons.

Update by re-downloading the pinned version and committing the change.
