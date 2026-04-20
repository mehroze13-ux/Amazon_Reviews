import os
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from database import (
    init_db, get_overview_stats, get_sentiment_trend,
    get_category_breakdown, get_products_with_stats, get_all_reviews
)

app = FastAPI(title="Amazon Reviews Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


@app.get("/")
def serve_dashboard():
    return FileResponse("dashboard/index.html")


@app.get("/api/overview")
def overview():
    return get_overview_stats()


@app.get("/api/sentiment-trend")
def sentiment_trend():
    rows = get_sentiment_trend()
    # Pivot by date
    dates = {}
    for row in rows:
        d = row["date"]
        if d not in dates:
            dates[d] = {"date": d, "POSITIVE": 0, "NEGATIVE": 0, "NEUTRAL": 0}
        sentiment = row["sentiment"]
        if sentiment in dates[d]:
            dates[d][sentiment] = row["n"]
    return sorted(dates.values(), key=lambda x: x["date"])


@app.get("/api/categories")
def categories():
    breakdown = get_category_breakdown()
    return [{"category": k, "count": v} for k, v in sorted(breakdown.items(), key=lambda x: -x[1])]


@app.get("/api/products")
def products():
    return get_products_with_stats()


@app.get("/api/reviews")
def reviews(
    search: str = Query(None),
    sentiment: str = Query(None),
    asin: str = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    rows = get_all_reviews(search=search, sentiment=sentiment, asin=asin, limit=limit, offset=offset)
    # Parse categories JSON string
    for r in rows:
        if r.get("categories") and isinstance(r["categories"], str):
            import json
            try:
                r["categories"] = json.loads(r["categories"])
            except Exception:
                r["categories"] = []
    return rows


@app.get("/api/export")
def export_csv(asin: str = Query(None), sentiment: str = Query(None)):
    import csv, io
    rows = get_all_reviews(sentiment=sentiment, asin=asin, limit=10000)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id", "asin", "product_name", "reviewer_name", "rating",
        "title", "body", "review_date", "verified_purchase", "sentiment", "categories"
    ])
    writer.writeheader()
    for r in rows:
        r["categories"] = ", ".join(r.get("categories") or [])
        writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
    from fastapi.responses import Response
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reviews.csv"}
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
