---
title: What's new
permalink: /changelog
---

# What's new

Kova Maps uses one version number across iPhone, Android and the web.

## Site 1.1.1 — November 2026

**Indoor plans** (kovamaps.com/indoor)
- Terminals and stations mapped as multipolygons (JFK Terminals 5, 7 and 8, Penn Station, Grand Central and others) now come with their outlines. The nightly build asked OpenStreetMap for their tags only, so the apps had no shape to draw. Same fix as iPhone build 76.
- A partial answer from OpenStreetMap (a timeout or memory limit, reported as a remark) is retried on another server instead of overwriting a good cell.
- The nightly build keeps when each cell was fetched in `_data/indoor_fetched.json` and refreshes cells older than 14 days by that time. It used to read the file's date, which every checkout resets, so cells were never refreshed. Cells from the old query are rebuilt on the next runs, and a cell whose map data hasn't changed is not rewritten (a refresh commits only changed cells and the index).
- Cells leave out the node id lists OpenStreetMap now sends with every way; no app reads them, and they would have made cells about 20% bigger.
- Overpass gets 60 s per cell instead of 25 s, since answers with outlines are heavier.
- Cells are worked through least recently tried first, so a run cut off by its time limit (or a manual refresh) carries on where the last one stopped, and a cell that keeps failing no longer holds up the rest. Cells are written to a temporary file and renamed, so a failed write never publishes half a file. The run shows red when more than a few cells fail, and the commit is replayed on top of main if main moved during the run.
- The publisher's freshness, scheduling and answer checks have tests, run before every nightly build.

## Site 1.1.0 — November 2026

**kovamaps.com**
- New homepage: leads with indoor maps for 11 airports, live driver reports and one account across devices.
- An indoor maps section lists each supported airport. Three main features, with the rest as a short list.
- A privacy promise (no ads, no tracking, no data sales), matching the privacy policy.
- Kova teal and coral replace the blue gradient, matching the apps.
- New screenshots of the web app as it is now (1.0.6): Kova's layout, the iPhone's map style, teal routes.

## 1.0.1 — November 2026

**Web app** (app.kovamaps.com)
- Loads much faster, especially on phones: smaller downloads, the map engine loads after the page appears, and repeat visits start from a local cache

**This site**
- Screenshots are served as smaller WebP images sized for your screen, and fonts no longer hold up the first paint

## 1.0.0 — November 2026

**Web app** (app.kovamaps.com)
- Search with suggestions, category chips and rated results with photos and hours
- Place details with photos, reviews, hours, website and phone
- Directions for driving with live traffic, transit, walking and cycling, with lanes and speed limits
- Saved places and recents synced with the iPhone app
- Traffic layer, satellite view, drivers' traffic reports and indoor maps
- Service announcements and messages from the Kova team

**iPhone** (1.0.0, build 4, TestFlight beta)
- Turn-by-turn navigation with CarPlay and Live Activities
- Everything above, plus home screen widgets and Siri Shortcuts
