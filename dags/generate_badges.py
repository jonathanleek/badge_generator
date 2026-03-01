"""
## Generate Badges DAG

Reads attendee data from a Google Sheet, generates a DXF badge file for each
person who does not yet have a badge, uploads it to Google Cloud Storage, then
writes today's date into the ``badge_creation_date`` column so the row is
skipped on future runs.

**Expected sheet columns (row 1 = headers):**

| Column | Header               | Notes                          |
|--------|----------------------|--------------------------------|
| A      | name                 |                                |
| B      | pronouns             |                                |
| C      | lanyard_hole         | ``TRUE`` / ``FALSE``           |
| D      | badge_creation_date  | Filled in by this DAG          |

**Params:**

- ``spreadsheet_id`` – Google Sheet ID (from the sheet URL)
- ``sheet_range``    – Sheet tab / range (default ``Sheet1``)
- ``gcs_bucket``     – GCS bucket name to upload finished badges into

**Connection required:** ``google_cloud_default`` — a Google service account
with Sheets API and Storage scopes.
"""

from __future__ import annotations

from pathlib import Path

from airflow.sdk import dag, task
from pendulum import datetime

GCP_CONN_ID = "google_cloud_default"
TEMPLATE_PATH = Path(__file__).parents[1] / "include" / "inputs" / "badge_config.json"
DATE_COL = "D"  # badge_creation_date lives in column D

# GCS bucket layout
GCS_PREPARED_BADGES   = "prepared_badges"    # individual badges awaiting sheet layout
GCS_PREPARED_SHEETS   = "prepared_sheets"    # arranged laser-ready sheets
GCS_COMPLETED_BADGES  = "completed_badges"   # badges that have been placed on a sheet


@dag(
    start_date=datetime(2025, 1, 1),
    schedule=None,
    params={
        "spreadsheet_id": "",
        "sheet_range": "Sheet1",
        "gcs_bucket": "",
    },
    doc_md=__doc__,
    default_args={"owner": "badges"},
    tags=["badges"],
)
def generate_badges():

    @task
    def fetch_attendees(**context) -> list[dict]:
        """
        Read all rows from the sheet. Returns only rows that do not yet have a
        badge_creation_date, along with the 1-based sheet row index so we can
        write the date back later.
        """
        from airflow.providers.google.suite.hooks.sheets import GoogleSheetsHook

        params = context["params"]
        hook = GoogleSheetsHook(gcp_conn_id=GCP_CONN_ID)
        rows: list[list[str]] = hook.get_spreadsheet_values(
            spreadsheet_id=params["spreadsheet_id"],
            range_=params["sheet_range"],
        )

        if not rows:
            return []

        headers = [h.lower().strip() for h in rows[0]]
        attendees = []
        for sheet_row, row in enumerate(rows[1:], start=2):  # row 1 = header
            data = dict(zip(headers, row))
            if data.get("badge_creation_date", "").strip():
                continue  # badge already generated for this person
            attendees.append({
                "name": data.get("name", ""),
                "pronouns": data.get("pronouns", ""),
                "lanyard_hole": data.get("lanyard_hole", "TRUE").upper() != "FALSE",
                "sheet_row": sheet_row,
            })

        print(f"Found {len(attendees)} attendee(s) needing badges.")
        return attendees

    @task
    def generate_badge(person: dict, **context) -> int:
        """
        Generate a DXF badge for one person and upload it directly to GCS.
        Returns the sheet row index for use in mark_badges_created.
        """
        import tempfile
        from airflow.providers.google.cloud.hooks.gcs import GCSHook
        from dxf_badges import PersonInfo, build_badge, load_template

        params = context["params"]
        template = load_template(TEMPLATE_PATH)
        pi = PersonInfo(
            name=person["name"],
            pronoun=person["pronouns"],
            lanyard_hole=person["lanyard_hole"],
        )
        doc = build_badge(template, pi)

        safe_name = person["name"].replace(" ", "_")
        object_name = f"{GCS_PREPARED_BADGES}/{safe_name}.dxf"

        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=True) as tmp:
            doc.saveas(tmp.name)
            GCSHook(gcp_conn_id=GCP_CONN_ID).upload(
                bucket_name=params["gcs_bucket"],
                object_name=object_name,
                filename=tmp.name,
            )

        print(f"Uploaded to gs://{params['gcs_bucket']}/{object_name}")
        return person["sheet_row"]

    @task
    def mark_badges_created(row_indices: list[int], **context) -> None:
        """
        Write today's date into the badge_creation_date column (D) for every
        successfully uploaded badge in a single batch API call.
        """
        from airflow.providers.google.suite.hooks.sheets import GoogleSheetsHook
        from pendulum import now

        if not row_indices:
            print("No badges were generated; nothing to mark.")
            return

        params = context["params"]
        today = now().format("YYYY-MM-DD")
        hook = GoogleSheetsHook(gcp_conn_id=GCP_CONN_ID)

        hook.batch_update_spreadsheet_values(
            spreadsheet_id=params["spreadsheet_id"],
            ranges=[f"{DATE_COL}{row}" for row in row_indices],
            values=[[[today]] for _ in row_indices],
        )

        print(f"Marked {len(row_indices)} row(s) with date {today}.")

    # --- Wire up tasks ---
    attendees = fetch_attendees()
    row_indices = generate_badge.expand(person=attendees)
    mark_badges_created(row_indices=row_indices)


generate_badges()
