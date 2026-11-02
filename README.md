# RaceLab

RaceLab is a mobile-friendly, paper-only racing probability experiment.

## GitHub Actions data refresh

The app reads `data/latest.json`. GitHub Actions discovers the daily card once, then checks the clock every five minutes and calls PuntersEdge only once in the final pre-race window. It can also be run manually.

Add the API key once:

1. Open the repository on GitHub.
2. Go to **Settings → Secrets and variables → Actions**.
3. Choose **New repository secret**.
4. Set the name to `PUNTERS_EDGE_API_KEY`.
5. Paste the key and save it.

Do not put the key in a file, commit, issue, or chat message. The workflow only uses it as a GitHub Actions secret.

The free API tier may have credit and freshness limits. The app displays the source timestamp and still treats the output as a research snapshot, not a guaranteed betting signal. RaceLab never places bets.
