import os
import json
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from database import (
    init_db, get_overview_stats, get_sentiment_trend,
    get_category_breakdown, get_products_with_stats, get_all_reviews,
    get_word_frequencies, get_top_themes, get_available_categories
)

app = FastAPI(title="Amazon Reviews Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
init_db()


@app.get("/")
def serve_dashboard():
    return FileResponse("dashboard/index.html")


@app.get("/api/overview")
def overview(
    sentiment: str = Query(None), asin: str = Query(None),
    category: str = Query(None), rating: int = Query(None),
    days: int = Query(None)
):
    return get_overview_stats(sentiment=sentiment, asin=asin,
                              category=category, rating=rating, days=days)


@app.get("/api/sentiment-trend")
def sentiment_trend(
    asin: str = Query(None), category: str = Query(None),
    rating: int = Query(None), days: int = Query(None)
):
    rows = get_sentiment_trend(asin=asin, category=category, rating=rating, days=days)
    dates = {}
    for row in rows:
        d = row["date"]
        if d not in dates:
            dates[d] = {"date": d, "POSITIVE": 0, "NEGATIVE": 0, "NEUTRAL": 0}
        if row["sentiment"] in dates[d]:
            dates[d][row["sentiment"]] = row["n"]
    return sorted(dates.values(), key=lambda x: x["date"])


@app.get("/api/categories")
def categories(
    sentiment: str = Query(None), asin: str = Query(None),
    rating: int = Query(None), days: int = Query(None)
):
    breakdown = get_category_breakdown(sentiment=sentiment, asin=asin, rating=rating, days=days)
    return [{"category": k, "count": v} for k, v in sorted(breakdown.items(), key=lambda x: -x[1])]


@app.get("/api/products")
def products(days: int = Query(None)):
    return get_products_with_stats(days=days)


@app.get("/api/reviews")
def reviews(
    search: str = Query(None), sentiment: str = Query(None),
    asin: str = Query(None), category: str = Query(None),
    rating: int = Query(None), days: int = Query(None),
    limit: int = Query(50, le=200), offset: int = Query(0)
):
    rows = get_all_reviews(search=search, sentiment=sentiment, asin=asin,
                           category=category, rating=rating, days=days,
                           limit=limit, offset=offset)
    for r in rows:
        if r.get("categories") and isinstance(r["categories"], str):
            try:
                r["categories"] = json.loads(r["categories"])
            except Exception:
                r["categories"] = []
    return rows


@app.get("/api/wordcloud")
def wordcloud(
    sentiment: str = Query(None), asin: str = Query(None),
    category: str = Query(None), days: int = Query(None)
):
    return get_word_frequencies(sentiment=sentiment, asin=asin, category=category, days=days)


@app.get("/api/top-issues")
def top_issues(asin: str = Query(None), days: int = Query(None)):
    return get_top_themes("NEGATIVE", asin=asin, days=days)


@app.get("/api/top-positives")
def top_positives(asin: str = Query(None), days: int = Query(None)):
    return get_top_themes("POSITIVE", asin=asin, days=days)


@app.get("/api/filter-options")
def filter_options():
    from database import get_products_with_stats, get_available_categories
    products = get_products_with_stats()
    categories = get_available_categories()
    return {
        "products": [{"asin": p["asin"], "name": p["name"]} for p in products],
        "categories": categories
    }


@app.get("/api/export")
def export_csv(
    asin: str = Query(None), sentiment: str = Query(None),
    category: str = Query(None), days: int = Query(None)
):
    import csv, io
    rows = get_all_reviews(sentiment=sentiment, asin=asin, category=category,
                           days=days, limit=10000)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id", "asin", "product_name", "reviewer_name", "rating",
        "title", "body", "review_date", "verified_purchase", "sentiment", "categories"
    ])
    writer.writeheader()
    for r in rows:
        if isinstance(r.get("categories"), str):
            try:
                r["categories"] = ", ".join(json.loads(r["categories"]))
            except Exception:
                pass
        elif isinstance(r.get("categories"), list):
            r["categories"] = ", ".join(r["categories"])
        writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
    return Response(
        content=output.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reviews.csv"}
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
