import sqlite3
import json
from contextlib import contextmanager

DB_PATH = "amazon_reviews.db"


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                asin TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT NOT NULL,
                reviewer_name TEXT,
                rating INTEGER,
                title TEXT,
                body TEXT,
                review_date TEXT,
                verified_purchase INTEGER DEFAULT 0,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (asin) REFERENCES products(asin)
            );

            CREATE TABLE IF NOT EXISTS review_tags (
                review_id INTEGER PRIMARY KEY,
                sentiment TEXT,
                sentiment_score REAL,
                categories TEXT,
                tagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (review_id) REFERENCES reviews(id)
            );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_product(asin, name, category):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO products (asin, name, category) VALUES (?, ?, ?)",
            (asin, name, category)
        )


def insert_review(asin, reviewer_name, rating, title, body, review_date, verified):
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM reviews WHERE asin=? AND reviewer_name=? AND title=?",
            (asin, reviewer_name, title)
        ).fetchone()
        if existing:
            return existing["id"]
        cursor = conn.execute(
            """INSERT INTO reviews (asin, reviewer_name, rating, title, body, review_date, verified_purchase)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (asin, reviewer_name, rating, title, body, review_date, int(verified))
        )
        return cursor.lastrowid


def insert_tag(review_id, sentiment, score, categories):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO review_tags (review_id, sentiment, sentiment_score, categories)
               VALUES (?, ?, ?, ?)""",
            (review_id, sentiment, score, json.dumps(categories))
        )


def get_untagged_reviews(limit=50):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.id, r.body, r.rating FROM reviews r
               LEFT JOIN review_tags t ON r.id = t.review_id
               WHERE t.review_id IS NULL AND r.body IS NOT NULL
               LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_reviews(search=None, sentiment=None, asin=None, limit=100, offset=0):
    with get_conn() as conn:
        query = """
            SELECT r.id, r.asin, p.name as product_name, r.reviewer_name,
                   r.rating, r.title, r.body, r.review_date, r.verified_purchase,
                   t.sentiment, t.sentiment_score, t.categories
            FROM reviews r
            LEFT JOIN products p ON r.asin = p.asin
            LEFT JOIN review_tags t ON r.id = t.review_id
            WHERE 1=1
        """
        params = []
        if search:
            query += " AND (r.title LIKE ? OR r.body LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if sentiment:
            query += " AND t.sentiment = ?"
            params.append(sentiment)
        if asin:
            query += " AND r.asin = ?"
            params.append(asin)
        query += " ORDER BY r.scraped_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_overview_stats():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) as n FROM reviews").fetchone()["n"]
        tagged = conn.execute("SELECT COUNT(*) as n FROM review_tags").fetchone()["n"]
        avg_rating = conn.execute("SELECT AVG(rating) as a FROM reviews").fetchone()["a"]
        sentiment_counts = conn.execute(
            """SELECT sentiment, COUNT(*) as n FROM review_tags
               WHERE sentiment IS NOT NULL GROUP BY sentiment"""
        ).fetchall()
        return {
            "total_reviews": total,
            "tagged_reviews": tagged,
            "avg_rating": round(avg_rating, 2) if avg_rating else 0,
            "sentiment": {row["sentiment"]: row["n"] for row in sentiment_counts}
        }


def get_sentiment_trend():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.review_date as date, t.sentiment, COUNT(*) as n
               FROM reviews r
               JOIN review_tags t ON r.id = t.review_id
               WHERE r.review_date IS NOT NULL AND t.sentiment IS NOT NULL
               GROUP BY r.review_date, t.sentiment
               ORDER BY r.review_date"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_category_breakdown():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT categories FROM review_tags WHERE categories IS NOT NULL"
        ).fetchall()
        counts = {}
        for row in rows:
            cats = json.loads(row["categories"])
            for c in cats:
                counts[c] = counts.get(c, 0) + 1
        return counts


def get_products_with_stats():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.asin, p.name, p.category,
                      COUNT(r.id) as review_count,
                      AVG(r.rating) as avg_rating,
                      SUM(CASE WHEN t.sentiment='POSITIVE' THEN 1 ELSE 0 END) as positive,
                      SUM(CASE WHEN t.sentiment='NEGATIVE' THEN 1 ELSE 0 END) as negative
               FROM products p
               LEFT JOIN reviews r ON p.asin = r.asin
               LEFT JOIN review_tags t ON r.id = t.review_id
               GROUP BY p.asin"""
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["avg_rating"] = round(d["avg_rating"], 2) if d["avg_rating"] else 0
            result.append(d)
        return result
