# GMZZ public battle site

The homepage is based directly on the provided `daodao_home_crisp_v8_share.html`.
Its first-screen layout, embedded artwork, sizing, colors, and ambient animation are
preserved. `site.css` and `site.js` add data views without changing that initial
composition.

Run locally:

```powershell
py web/gmzz/dev_server.py --port 8087
```

Open `http://127.0.0.1:8087`. The development server proxies only the public DPS
GET endpoints, matching the production same-origin Nginx setup.
