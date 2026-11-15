from fastapi import FastAPI

app = FastAPI(title="SWE-Olympus", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "stage": "P1-foundation", "schema_version": "1.0", "model_required": False}
