"""
FastAPI entry point for the rebar takeoff service.

Stateless: the uploaded PDF is read into memory, analysed, and discarded.
Nothing is written to disk or a database. The frontend single-page app is
served from `/`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from rebar_service import analyze_pdf

app = FastAPI(title="Rebar Takeoff", version="1.0.0")

FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)) -> JSONResponse:
    """Accept a PDF upload and return the takeoff JSON."""
    if file.content_type not in ("application/pdf", "application/octet-stream") \
            and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file.")

    try:
        result = analyze_pdf(pdf_bytes)
    except Exception as exc:  # surface parsing failures as 422 rather than 500
        raise HTTPException(status_code=422, detail=f"Failed to parse PDF: {exc}")

    return JSONResponse(result)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_INDEX)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
