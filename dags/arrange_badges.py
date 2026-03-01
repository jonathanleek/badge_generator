"""
## Arrange Badges DAG

Downloads all DXF badge files from the ``prepared_badges/`` bucket directory,
packs them onto laser-ready sheets, uploads the sheets to ``prepared_sheets/``,
then moves the source badges to ``completed_badges/``.

Badges are only moved after all sheets have been successfully uploaded, so a
failed upload leaves the source files in place for a safe retry.

**Bucket layout:**

| Directory           | Contents                                   |
|---------------------|--------------------------------------------|
| ``prepared_badges`` | Individual badges awaiting sheet layout    |
| ``prepared_sheets`` | Arranged laser-ready sheets                |
| ``completed_badges``| Badges that have been placed on a sheet    |

**Params:**

- ``gcs_bucket``      – GCS bucket name
- ``material_w_in``   – Material width in inches (default 24)
- ``material_h_in``   – Material height in inches (default 12)

**Connection required:** ``google_cloud_default``
"""

from __future__ import annotations

from pathlib import Path

from airflow.sdk import dag, task
from pendulum import datetime

GCP_CONN_ID = "google_cloud_default"

GCS_PREPARED_BADGES  = "prepared_badges"
GCS_PREPARED_SHEETS  = "prepared_sheets"
GCS_COMPLETED_BADGES = "completed_badges"


@dag(
    start_date=datetime(2025, 1, 1),
    schedule=None,
    params={
        "gcs_bucket": "",
        "material_w_in": 24.0,
        "material_h_in": 12.0,
    },
    doc_md=__doc__,
    default_args={"owner": "badges"},
    tags=["badges"],
)
def arrange_badges():

    @task
    def fetch_badge_objects(**context) -> list[str]:
        """
        List all .dxf files in the prepared_badges/ directory of the bucket.
        Returns GCS object names.
        """
        from airflow.providers.google.cloud.hooks.gcs import GCSHook

        hook = GCSHook(gcp_conn_id=GCP_CONN_ID)
        objects = hook.list(
            bucket_name=context["params"]["gcs_bucket"],
            prefix=f"{GCS_PREPARED_BADGES}/",
        )
        badges = [o for o in (objects or []) if o.endswith(".dxf")]
        print(f"Found {len(badges)} badge(s) in {GCS_PREPARED_BADGES}/.")
        return badges

    @task
    def build_sheets(badge_objects: list[str], **context) -> list[str]:
        """
        Download all prepared badges, arrange them onto sheets using
        arrange_for_laser(), and upload each sheet to prepared_sheets/.
        Returns the list of uploaded sheet object names.
        """
        import tempfile
        from airflow.providers.google.cloud.hooks.gcs import GCSHook
        from dxf_badges import arrange_for_laser

        if not badge_objects:
            print("No badges to arrange.")
            return []

        params = context["params"]
        hook = GCSHook(gcp_conn_id=GCP_CONN_ID)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Download each badge into the temp directory
            for obj in badge_objects:
                hook.download(
                    bucket_name=params["gcs_bucket"],
                    object_name=obj,
                    filename=str(tmp_path / Path(obj).name),
                )

            # Arrange into sheets
            sheets = arrange_for_laser(
                badge_dir=tmp_path,
                material_w_in=float(params["material_w_in"]),
                material_h_in=float(params["material_h_in"]),
            )

            # Upload sheets
            uploaded = []
            for i, sheet in enumerate(sheets, start=1):
                sheet_name = f"sheet_{i:03d}.dxf"
                sheet_path = tmp_path / sheet_name
                sheet.saveas(str(sheet_path))
                object_name = f"{GCS_PREPARED_SHEETS}/{sheet_name}"
                hook.upload(
                    bucket_name=params["gcs_bucket"],
                    object_name=object_name,
                    filename=str(sheet_path),
                )
                uploaded.append(object_name)
                print(f"Uploaded {object_name}")

        print(f"Created {len(sheets)} sheet(s) from {len(badge_objects)} badge(s).")
        return uploaded

    @task
    def move_to_completed(badge_objects: list[str], sheet_objects: list[str], **context) -> None:
        """
        Move each processed badge from prepared_badges/ to completed_badges/.
        sheet_objects is accepted only to enforce that this task runs after
        build_sheets succeeds — badges are not moved if sheet upload failed.
        """
        from airflow.providers.google.cloud.hooks.gcs import GCSHook

        if not badge_objects:
            print("No badges to move.")
            return

        params = context["params"]
        bucket = params["gcs_bucket"]
        hook = GCSHook(gcp_conn_id=GCP_CONN_ID)

        for obj in badge_objects:
            dest = f"{GCS_COMPLETED_BADGES}/{Path(obj).name}"
            hook.copy(
                source_bucket=bucket,
                source_object=obj,
                destination_bucket=bucket,
                destination_object=dest,
            )
            hook.delete(bucket_name=bucket, object_name=obj)
            print(f"Moved {obj} → {dest}")

    # --- Wire up tasks ---
    badge_objects = fetch_badge_objects()
    sheet_objects = build_sheets(badge_objects)
    move_to_completed(badge_objects=badge_objects, sheet_objects=sheet_objects)


arrange_badges()
