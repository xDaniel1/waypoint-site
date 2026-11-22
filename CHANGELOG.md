---
title: What's new
permalink: /changelog
---

# What's new

Kova Maps uses one version number across iPhone, Android and the web.

## Site 1.1.1 — November 2026

**Indoor plans** (kovamaps.com/indoor)
- Terminals and stations mapped as multipolygons (JFK Terminals 5, 7 and 8, Penn Station, Grand Central and others) now come with their outlines. The nightly build asked OpenStreetMap for their tags only, so the apps had no shape to draw. Same fix as iPhone build 76.
- A partial answer from OpenStreetMap (a timeout or memory limit, reported as a remark) is retried on another server instead of overwriting a good cell, and so is an answer whose terminals or stations come back without their member list (what the old query returned).
- The nightly build keeps when each cell was fetched in `_data/indoor_fetched.json` and refreshes cells older than 14 days by that time. It used to read the file's date, which every checkout resets, so cells were never refreshed. Cells from the old query are rebuilt on the next runs. A refreshed cell is rewritten even when its map data didn't change, to record the newer snapshot.
- Cells leave out the node id lists OpenStreetMap now sends with every way; no app reads them, and they would have made cells about 20% bigger.
- Overpass gets 60 s per cell instead of 25 s, since answers with outlines are heavier.
- Cells are worked through least recently tried first, so a run cut off by its time limit (or a manual refresh) carries on where the last one stopped, and a cell that keeps failing no longer holds up the rest. Cells are written to a temporary file and renamed, so a failed write never publishes half a file or a leftover temporary file. No request starts that could run past the time limit, and each request gets 75 s in total, connecting included, even from a server that keeps sending slowly. The run shows red when failures match or outnumber successes, or more than a few cells fail. Only runs on main commit cells, replayed on top of main if it moved during the run (retried if main moves again before the push).
- A point OpenStreetMap can't place (written as null) never reaches the apps: a terminal's outline piece is split around it, and a room or shop with one is left out. iPhone builds read a cell's points strictly, so one null would have failed the whole cell. Answers with elements the apps couldn't decode (not a node, way or relation with an id, non-text tags or member roles) are rejected.
- Every nightly run checks that the site serves what is on main, including cells from an earlier run whose site build failed (that build is retried once), and goes red if it can't get them published within 10 minutes.
- Each cell records the OpenStreetMap snapshot it was last confirmed against (`osm_base`), changed or not, and an answer from a mirror lagging behind that snapshot, or not saying which snapshot it is, is refused, so a cell never rolls back. The apps can use it to prefer the newer of two cells.
- Runs always start from the latest main, so a re-run or a queued run builds on what was already pushed.
- Cells that keep failing for a week are listed as a warning on the run. The index of fetch and try times changes every night, so each run commits it and the site rebuilds nightly even when no cell changed.
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
