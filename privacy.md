---
title: Privacy Policy
permalink: /privacy
---

# Kova Maps Privacy Policy

**Effective date:** November 8, 2026

Kova Maps is a maps and navigation app for iPhone, made by Daniel Lantigua ("I", "me"). This policy explains what information Kova Maps uses, where it goes, and the choices you have. The short version:

- **No ads, no tracking, no data sales.** Kova Maps has no advertising or analytics SDKs, does not track you across other companies' apps or websites, and never sells or rents your information.
- **Your location is used to run the map and directions.** It's sent to the map, routing, and data services listed below only when a feature needs it.
- **An account is optional.** Without one, your Favorites and Recents stay on your devices and in your own iCloud. If you sign in, a backup copy is stored on Kova Maps' server, and you can delete the account and that copy at any time from inside the app.

## 1. Information Kova Maps uses

### Location
With your permission, Kova Maps uses your device's precise location to center the map, find places near you, give directions, show speed limits, and guide you during navigation. If you allow "Always" access, location keeps updating while the app is in the background, **but only during active turn-by-turn navigation**. It stops when the trip ends. You can change location access at any time in **Settings > Privacy & Security > Location Services > Kova Maps**.

Kova Maps does not keep a history of where you've been, and it doesn't upload your location to its own server, except for a traffic report you choose to file (see below).

### Searches, Favorites, and Recents
Search text is sent to the search providers listed below to find results. Places you save as Favorites and your recent searches are stored on your device and synced between your own Apple devices through your iCloud account (Apple's iCloud key-value storage, which I can't read).

### Voice search
If you use voice search, Kova Maps uses the microphone and Apple's Speech framework to turn your speech into a search query. Apple may process the audio on its servers to do this, under [Apple's privacy policy](https://www.apple.com/legal/privacy/). Kova Maps doesn't record or store the audio.

### Optional account
You can sign in with **Sign in with Apple** or **Google Sign-In**. If you do, Kova Maps receives an account identifier and the email address the provider shares. With Sign in with Apple, this can be a private relay address that hides your real email. Kova Maps uses the account only to back up and sync your Favorites and Recents, and to attach your traffic reports to you.

### Traffic reports
If you're signed in and report an incident during navigation (for example, an accident or a road closure), Kova Maps stores the report type, its location, the time, and your account identifier. Other Kova Maps users can see the report type and location, but not who filed it. Reports are hidden after about two hours and deleted from the server after about a day.

### Speed camera reports
Speed traps you report in drive mode are stored **only on your device**. They aren't uploaded or shared.

### Crash and performance diagnostics
Kova Maps uses Apple's MetricKit to record crash and hang reports **on your device**, where you can see them under Profile > Diagnostics. Kova Maps doesn't send them anywhere. If you've opted in to share analytics with app developers in iOS settings, Apple may give me anonymous crash reports and usage statistics, under Apple's own terms.

## 2. Services that receive information

To work, Kova Maps sends requests to the services below. Each request includes only what that feature needs. Each service handles that data under its own privacy policy.

| Service | What it receives | Why |
|---|---|---|
| Apple (MapKit, Apple Weather, Sign in with Apple, iCloud, Speech) | Map area, location, searches, voice audio, account sign-in | Maps, search, directions, weather, sign-in, sync, voice search |
| Google Maps Platform (Places, Routes) | Location, search text, route start/end and stops | Place search and details, photos, reviews, traffic-aware driving, transit and bike routes |
| Google Sign-In | Sign-in only, if you choose it | Account sign-in |
| Supabase (Kova Maps' server) | Account ID and email, Favorites and Recents backup, traffic reports | Optional account sync and traffic reports |
| OpenStreetMap (Overpass API) | The map area around your location | Speed limits, lane guidance, and speed camera locations |
| NYC Open Data | Requests for NYC speed limit data | Speed limits in New York City |
| MTA | Requests for the stops and routes you're viewing | Subway and bus arrival times |
| Open-Meteo | Approximate coordinates | Backup weather and air quality |

Privacy policies: [Apple](https://www.apple.com/legal/privacy/) · [Google](https://policies.google.com/privacy) · [Supabase](https://supabase.com/privacy) · [OpenStreetMap Foundation](https://osmfoundation.org/wiki/Privacy_Policy) · [Open-Meteo](https://open-meteo.com/en/terms#privacy)

## 3. How long information is kept

- **On your device:** until you delete it, sign out, or delete the app.
- **Account data on Kova Maps' server:** until you delete your account. Deletion is immediate and permanent.
- **Traffic reports:** deleted from the server after about a day, or straight away when you delete your account.

## 4. Your choices and rights

- **Delete your account:** Profile > Delete Account. This permanently deletes the account, its Favorites and Recents backup, and your traffic reports. Favorites and Recents on your device are kept.
- **Sign out** at any time. Your on-device data stays.
- **Turn off permissions** (location, microphone, speech recognition) in iOS Settings. The features that need them stop working, and the rest of the app keeps working.
- **Ask a question or make a request** about your data by emailing [SUPPORT EMAIL]. I'll respond within 30 days.

If you live in a US state with a consumer privacy law (such as California), you have the right to know about, correct, and delete your personal information, and not to be discriminated against for using those rights. Kova Maps doesn't sell or "share" personal information for targeted advertising as those laws define it.

## 5. Children

Kova Maps isn't directed to children under 13, and I don't knowingly collect personal information from them. If you think a child has created an account, email [SUPPORT EMAIL] and I'll delete it.

## 6. Security

Every connection Kova Maps makes uses HTTPS. On the server, each account can only read and change its own data. This is enforced by the database, not just the app. No system is perfectly secure, but I work to protect what's stored.

## 7. Changes to this policy

If this policy changes, I'll update it here and change the effective date at the top. If the changes are significant, I'll also note them in the app's release notes.

## 8. Contact

Daniel Lantigua, [SUPPORT EMAIL]
