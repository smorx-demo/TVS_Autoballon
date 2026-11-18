# SmorX AutoBalloon API

Standard FastAPI layout:

```
app/
  api/routes/     # auth, users, autoballoon
  core/           # config, security, database
  models/         # customer, user, license, processing_job
  schemas/        # pydantic request/response
  services/       # business logic + AutoBalloon engine
  utils/          # S3/file handler, logger
```

## Run locally

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 9686
```

Health: `GET http://127.0.0.1:9686/api/health`

## Architecture

```
AutoBalloon.exe → FastAPI → AWS Postgres (metadata)
                         → AWS S3 (PDF files; local fallback)
```

Configure `backend/.env` from `.env.example`.

## Multi-format upload → vector PDF

| Upload | Converter | Verify agent |
|--------|-----------|----------------|
| PDF | passthrough | (optional) |
| STEP/STP | FreeCAD → DXF → PyMuPDF | Claude checks PDF vs STEP manifest |
| DXF | ezdxf → PDF | Claude |
| SVG | PyMuPDF | — |
| PVZ | `SMORX_PVZ_CONVERTER` CLI | Claude |
| Images | raster PDF | — |

After conversion, the **existing balloon / extraction pipeline** runs on the combined PDF.

**Anthropic conversion** (when mechanical tools fail or `SMORX_STEP_USE_ANTHROPIC=1`):

- DXF / SVG / STEP / PVZ → Claude builds **SVG** → PyMuPDF → vector PDF  
- STEP may use **trimesh** preview image (`pip install trimesh`) for vision→SVG  
- `SMORX_ANTHROPIC_REPAIR=1` re-runs Claude if the PDF has no selectable text  

- `FREECAD_CMD` — path to `FreeCADCmd` for STEP (preferred when available)
- `ANTHROPIC_API_KEY` + `SMORX_VERIFY_CONVERSION=1` — post-conversion agents
- `SMORX_PVZ_CONVERTER` — e.g. `"C:\Program Files\...\cvconvert.exe" "{in}" "{out}"`

Job meta stores `conversion_log` (converter + `verification` JSON per file).

## Full 15-point extraction

After balloon (or `POST /api/jobs/{job_id}/extract/full`):

- **All dimensions** — multipass, all zones (`SMORX_FULL_EXTRACT=1`)
- **All text + notes** — every line + numbered notes
- **GD&T FCF** — graphics + text (`gdt_fcf_graphics.py`)
- **Critical / major** — rule engine (`critical_parameters.py`)
- **STEP/PVZ** — `model_manifest.py` on source files

**Master Excel:** `GET /api/jobs/{job_id}/download/master-extraction`  
Sheets: Summary, All_Dimensions, GD&T_FCF, Surface_Finish, All_Notes, All_Text, Critical_Major, Model_3D_PVZ

**Production S3:** `autoballoon-storage-smorx-prod` (us-east-1) — [bucket URL](https://autoballoon-storage-smorx-prod.s3.us-east-1.amazonaws.com/). PDFs are stored per user under `{prefix}/{username}/{job_id}/`. On EC2, leave AWS keys blank and attach an IAM role with `s3:PutObject` / `s3:GetObject`. Health shows `"storage": "s3"`, `"s3_bucket"`, and `"s3_url"` when a bucket is configured.
