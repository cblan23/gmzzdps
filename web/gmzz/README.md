# GMZZ public battle site

The homepage uses `daodao_gmzz_home_simple_v4_upload_help.html` as its layout
reference. It displays the existing uploaded battle history, public character
search, qualified Boss rankings, real profession statistics, and battle details.
Captured character names are displayed and searchable. Incomplete historical
records remain browsable; only qualified records enter rankings. Missing data is shown as unavailable. Curves without
recorded timestamps use sample order instead of invented elapsed seconds.
Battle details can export each member's recorded skill cast times as a real
`.xlsx` workbook. Skills without a captured cast time are marked as unrecorded;
damage-hit timestamps are not presented as cast times.
The top navigation's `BOSS 数据` page (`#boss-data`) contains reference data for
五月庄园·城堡（困难）, 困难小丑, 丰饶之树（黄铜书模式）, and 大帝重临. The
latest castle page separates live HP/timing evidence from static client-config
attributes; the other pages retain their supplied source images. Edit
`assets/boss-data/catalog.json` to update these references while keeping each
value's evidence boundary explicit.
Battles containing a projection member (`is_ai` or a captured name ending in
`·投影`) remain visible in public character history and battle detail. Confirmed
victories count as clears and appear in a separate unranked section on the data
page, while these battles stay outside competitive rankings and profession
statistics.

Run locally:

```powershell
py web/gmzz/dev_server.py --port 8087
```

Open `http://127.0.0.1:8087`. The development server proxies only the public DPS
GET endpoints, matching the production same-origin Nginx setup.

Public data endpoints: `/api/v1/dps/public/statistics`, `/catalog`, `/records`,
`/leaderboards`, `/performance`, and `/encounters/{id}` under the same public root.
`--api-origin` selects a staged API for local verification.

Run `tools/prepare_gmzz_web_assets.py` after extracting client icons. Skills,
equipment, and professions use the existing client PNG files; the catalog avoids
requests for missing icons. Career labels follow the [official game selection](https://lom.sparknexa.com/)
and client profession IDs. Crit luck uses captured damage events with known
critical flags; unknown flags are excluded. Team death totals are complete only
when every stored participant has a recorded death count.

Browser verification uses installed Chrome:

```powershell
.\.venv-build310\Scripts\python.exe -X utf8 tools/verify_gmzz_web.py --url https://gmzz.daodaogame.vip
```
