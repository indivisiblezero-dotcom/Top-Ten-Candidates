# AI Video Scout

Every night just after midnight Pacific, this finds YouTube uploads from the day that just ended (12:00 AM – 11:59 PM Pacific) matching **"AI film"**, **"AI video"** and **"AI music video"**, removes anything that isn't a candidate for your Top 10 lists, and publishes a clickable review page.

**What gets removed (and why)**

| Stage | Rule | Cost |
|---|---|---|
| Format | Live/upcoming streams, under 60 seconds, vertical video, `#shorts` | Free |
| Your rules | Blocked channels, excluded title keywords | Free |
| Engagement | Under `min_views` views, or like/view ratio under `min_like_ratio_pct` | Free |
| Classifier | Claude labels each video CREATIVE / TUTORIAL / NEWS / OTHER; TUTORIAL and NEWS are removed | A few cents a day |

Removed videos are still listed (collapsed, with the reason) at the bottom of the page so you can spot false rejections.

---

## One-time setup (about 15 minutes)

### 1. Get a YouTube API key (free)
1. Go to <https://console.cloud.google.com/>, create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → Credentials → Create credentials → API key**. Copy it.
   (Optional: restrict the key to the YouTube Data API v3.)

The free quota is 10,000 units/day; a normal run uses about 900–1,000.

### 2. Get an Anthropic API key
Create one at <https://console.anthropic.com/> and add a small amount of credit.

### 3. Create the GitHub repository
1. Create a new repository on GitHub (public is simplest for free GitHub Pages; private works on paid plans).
2. Upload all files from this folder, keeping the `.github/workflows/` folder structure.

### 4. Add your keys as secrets
Repository → **Settings → Secrets and variables → Actions → Secrets → New repository secret**:
- `YOUTUBE_API_KEY`
- `ANTHROPIC_API_KEY`

### 5. (Optional) Set your thresholds
Same page, **Variables** tab → **New repository variable**:
- `MIN_LIKE_RATIO_PCT` — e.g. `2.5` (means 2.5% of viewers liked it)
- `MIN_VIEWS` — e.g. `100`

If you don't set these, the values in `config.json` are used (2% and 50 views).

### 6. Turn on GitHub Pages
**Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main`, folder: `/docs`** → Save.

### 7. Run it once now
**Actions → Daily AI video scout → Run workflow.** After a minute or two your page is at
`https://<your-username>.github.io/<repo-name>/` — bookmark it.

From then on it runs by itself every night at about 12:15 AM Pacific, year-round (daylight saving is handled automatically). Each page covers one full Pacific calendar day, so the run after Saturday ends, for example, holds everything uploaded on Saturday up to 11:59 PM.

A manual run scans yesterday by default and replaces that day's page. To redo or fill in a specific day, type it in the *Day to scan* box (YYYY-MM-DD). Note that YouTube's search index can lag, so a video uploaded at 11:58 PM may occasionally be missed by the 12:15 AM run; a manual re-run of that day later picks it up.

---

## Changing the like/view threshold

You have three ways, from quickest to most permanent:

1. **On the page** — the *Min like/view* slider hides videos below a higher threshold instantly. It can only raise the threshold, because videos below the run's threshold were already filtered out.
2. **For one run** — Actions → Daily AI video scout → Run workflow, and type a value in *Min like/view ratio*. Good for re-running today with a lower bar.
3. **For every run** — change the `MIN_LIKE_RATIO_PCT` repository variable (step 5). No code edit needed.

**Tip:** videos less than a day old have few views, and the first viewers are usually fans, so ratios run high (often 4–10%) and swing a lot. Start low (around 2%) with a modest view minimum, look at a week of results, then tighten.

## Other settings (`config.json`)

| Setting | What it does |
|---|---|
| `queries` | Search terms. Wrap a term in quotes (`"\"AI film\""`) for exact-phrase matching. |
| `pages_per_query` | 50 results per page; each page costs 100 quota units. |
| `relevance_language` | Prefer results in this language. Set to `""` for all languages. |
| `min_duration_seconds` | Minimum length (default 60). |
| `hidden_likes` | `"include"` keeps videos whose likes are hidden; `"exclude"` removes them. |
| `exclude_title_keywords` | Remove videos whose title contains any of these words, before the classifier runs. |
| `blocked_channels` | Channel names or IDs to always skip. |
| `always_include_channels` | Trusted channels that skip the engagement and classifier checks (format rules still apply). |
| `exclude_categories` | Classifier labels to remove. Add `"OTHER"` to also drop compilations, promos and off-topic videos. |
| `classifier_model` | Claude model used for classification. |

## Running it on your own computer

```bash
pip install -r requirements.txt
export YOUTUBE_API_KEY=...  ANTHROPIC_API_KEY=...
python scout.py            # writes docs/index.html
python scout.py --no-classify   # test the YouTube side without using the classifier
```

## Files

- `scout.py` — the pipeline
- `config.json` — settings
- `template.html` — the review page layout
- `.github/workflows/daily-scout.yml` — the daily schedule
- `docs/` — generated pages (`index.html` = latest, `archive/` = past days, `data/` = raw JSON)

Your shortlist (★ buttons) is saved in your browser and carries over between days, so you can build the week's Top 10 from several daily pages and copy it out at the end.
