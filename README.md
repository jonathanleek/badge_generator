# Badge Generator

An Apache Airflow project that automates the creation of laser-cuttable event badges (DXF format) from attendee data in Google Sheets. Built on the Astronomer platform.

## How It Works

1. **Generate Badges** (`generate_badges` DAG) — Reads new attendees from a Google Sheet, generates individual DXF badge files, uploads them to GCS, and marks the rows as processed.
2. **Arrange Badges** (`arrange_badges` DAG) — Packs individual badge files onto material sheets optimized for laser cutting, uploads the arranged sheets to GCS, and archives the originals.

### Badge Features

- 50mm x 75mm badges with rounded corners
- Auto-scaled name text and pronouns
- Custom logo support (SVG or DXF)
- Optional lanyard holes
- Two-layer output: CUT (red) and ETCH (blue) for laser cutting

## Prerequisites

- [Astro CLI](https://www.astronomer.io/docs/astro/cli/install-cli/)
- Docker
- A Google Cloud service account with access to the Sheets API and Cloud Storage

## Setup

1. Configure a `google_cloud_default` Airflow connection with your service account credentials.

2. Prepare a Google Sheet with these columns:

   | Column | Header | Notes |
   |--------|--------|-------|
   | A | name | Attendee name |
   | B | pronouns | e.g. "he/him" |
   | C | lanyard_hole | TRUE / FALSE |
   | D | badge_creation_date | Left blank — filled by the DAG |

3. Update `include/inputs/badge_config.json` with your event URL and logo path:

   ```json
   {
     "url": "https://your-event.com",
     "logo_uri": "include/inputs/logo_outline.svg"
   }
   ```

4. Start the local environment:

   ```bash
   astro dev start
   ```

   This spins up five containers (Postgres, Scheduler, DAG Processor, API Server, Triggerer). The Airflow UI will be available at http://localhost:8080/.

## DAG Parameters

### `generate_badges`

| Parameter | Description | Default |
|-----------|-------------|---------|
| `spreadsheet_id` | Google Sheet ID | *(required)* |
| `sheet_range` | Sheet tab/range | `Sheet1` |
| `gcs_bucket` | GCS bucket name | *(required)* |

### `arrange_badges`

| Parameter | Description | Default |
|-----------|-------------|---------|
| `gcs_bucket` | GCS bucket name | *(required)* |
| `material_w_in` | Material width (inches) | `24.0` |
| `material_h_in` | Material height (inches) | `12.0` |

## GCS Bucket Layout

| Directory | Contents |
|-----------|----------|
| `prepared_badges/` | Individual badges awaiting layout |
| `prepared_sheets/` | Laser-ready arranged sheets |
| `completed_badges/` | Badges already placed on sheets |

## Project Structure

```
badge_generator/
├── dags/
│   ├── generate_badges.py     # Badge generation DAG
│   └── arrange_badges.py      # Sheet arrangement DAG
├── include/
│   ├── dxf_badges.py          # Core badge generation logic
│   └── inputs/
│       ├── badge_config.json  # Badge template config
│       └── logo_outline.svg   # Logo file
├── tests/
│   └── dags/
│       └── test_dag_example.py
├── Dockerfile
├── requirements.txt
└── airflow_settings.yaml
```

## Running Tests

```bash
astro dev pytest
```

## Deploying to Astronomer

```bash
astro deploy
```

See the [Astronomer docs](https://www.astronomer.io/docs/astro/deploy-code/) for details.