"""
cron_cleanup.py — Run daily via Railway cron to delete old contract data.

Schedule in Railway: 0 2 * * * (runs at 2am daily)
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL")

def cleanup_old_jobs():
    """Delete contract text and file data for jobs older than 30 days."""
    cutoff = datetime.now() - timedelta(days=30)

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cur = conn.cursor()

    # Null out contract text and PDF data for old delivered jobs
    cur.execute("""
        UPDATE jobs
        SET contract_text = NULL,
            report_pdf = NULL,
            cms_data = NULL,
            state_data = NULL
        WHERE delivered_at < %s
        AND (contract_text IS NOT NULL OR report_pdf IS NOT NULL)
    """, (cutoff,))

    deleted_count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    print(f"Cleanup complete: nulled data for {deleted_count} jobs older than 30 days")
    return deleted_count

if __name__ == "__main__":
    cleanup_old_jobs()
