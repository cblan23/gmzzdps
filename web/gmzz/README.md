# GMZZ public battle site

The homepage uses `daodao_gmzz_home_simple_v4_upload_help.html` as its layout
reference. It displays the existing uploaded battle history, public character
search, qualified Boss rankings, real profession statistics, and battle details.
Anonymous and incomplete historical records remain browsable; only qualified
records enter rankings. Missing data is shown as unavailable. Curves without
recorded timestamps use sample order instead of invented elapsed seconds.

Run locally:

```powershell
py web/gmzz/dev_server.py --port 8087
```

Open `http://127.0.0.1:8087`. The development server proxies only the public DPS
GET endpoints, matching the production same-origin Nginx setup.

Public data endpoints: `/api/v1/dps/public/statistics`, `/catalog`, `/records`,
`/leaderboards`, `/performance`, and `/encounters/{id}` under the same public root.
`--api-origin` selects a staged API for local verification.

Browser verification uses installed Chrome:

```powershell
.\.venv-build310\Scripts\python.exe -X utf8 tools/verify_gmzz_web.py --url https://gmzz.daodaogame.vip
```
