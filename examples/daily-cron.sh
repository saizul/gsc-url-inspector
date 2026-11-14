#!/bin/sh
# Inspect the URLs of a sitemap every day and keep a dated report.
#
# A run that hits the 2,000 inspections per property per day limit is paused (exit code 3) and the next invocation
# resumes it, so for a large site the same cron line simply keeps going day after day.
#
#   15 3 * * * /opt/gsc/daily-cron.sh >> /var/log/gsc-url-inspector.log 2>&1
set -u

SITE="sc-domain:example.com"
export GSC_CREDENTIALS=/opt/gsc/service-account.json
export GSC_URL_INSPECTOR_DB=/var/lib/gsc/inspections.sqlite
OUT=/var/lib/gsc/reports/$(date +%Y-%m-%d)

gsc-url-inspector inspect --site "$SITE" \
  --sitemap https://www.example.com/sitemap.xml \
  --quiet --format text --report-dir "$OUT" \
  --fail-on regressions
code=$?

case $code in
  0) echo "run complete" ;;
  1) echo "regressions found, see $OUT" ;;
  3) echo "daily quota reached; the run continues tomorrow" ;;
  *) echo "gsc-url-inspector failed with exit code $code" ;;
esac
exit 0
