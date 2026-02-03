from fastapi import FastAPI, Response

app = FastAPI()

@app.api_route("/standard/{subpath:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def standard_endpoint(subpath: str):
    return Response(status_code=429)

@app.api_route("/priority/{subpath:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def priority_endpoint(subpath: str):
    return Response(status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
