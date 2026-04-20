import sqlite3
import json
import re
from contextlib import contextmanager

DB_PATH = "amazon_reviews.db"

STOP_WORDS = {
    "the","a","an","and","or","but","in","on","at","to","for","of","with","by",
    "from","is","was","are","were","be","been","being","have","has","had","do",
    "does","did","will","would","could","should","may","might","it","its","this",
    "that","these","those","i","me","my","we","our","you","your","he","his","she",
    "her","they","their","not","no","so","am","s","t","just","also","very","too",
    "much","more","get","got","use","used","one","two","like","really","quite",
    "even","still","well","bit","lot","time","product","item","amazon","review",
    "order","bought","buy","received","would","can","all","any","some","what",
    "which","who","when","where","why","how","than","then","now","about","up",
    "out","if","as","into","through","before","after","each","other","over",
    "under","its","had","been","only","than","same","both","few","more","most",
    "other","into","through","during","itself","because","while","again","there",
    "where","here","though","although","however","around","between","such","same",
    "re","ve","ll","m","d","didn","doesn","isn","wasn","aren","weren","hasn",
    "haven","hadn","couldn","wouldn","shouldn","won","don","th","nd","rd","st"
}


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


def _filter_clause(sentiment=None, asin=None, category=None, rating=None, days=None):
    conds, params = [], []
    if sentiment:
        conds.append("t.sentiment = ?")
        params.append(sentiment)
    if asin:
        conds.append("r.asin = ?")
        params.append(asin)
    if category:
        conds.append("t.categories LIKE ?")
        params.append(f'%{category}%')
    if rating:
        conds.append("r.rating = ?")
        params.append(int(rating))
    if days and int(days) > 0:
        conds.append("r.review_date >= date('now', ?)")
        params.append(f'-{days} days')
    return (" AND " + " AND ".join(conds)) if conds else "", params


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
               WHERE t.review_id IS NULL AND r.body IS NOT NULL LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_reviews(search=None, sentiment=None, asin=None, category=None,
                    rating=None, days=None, limit=100, offset=0):
    with get_conn() as conn:
        extra, params = _filter_clause(sentiment=sentiment, asin=asin,
                                       category=category, rating=rating, days=days)
        query = f"""
            SELECT r.id, r.asin, p.name as product_name, r.reviewer_name,
                   r.rating, r.title, r.body, r.review_date, r.verified_purchase,
                   t.sentiment, t.sentiment_score, t.categories
            FROM reviews r
            LEFT JOIN products p ON r.asin = p.asin
            LEFT JOIN review_tags t ON r.id = t.review_id
            WHERE 1=1 {extra}
        """
        if search:
            query += " AND (r.title LIKE ? OR r.body LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        query += " ORDER BY r.review_date DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_overview_stats(sentiment=None, asin=None, category=None, rating=None, days=None):
    with get_conn() as conn:
        extra, params = _filter_clause(sentiment=sentiment, asin=asin,
                                       category=category, rating=rating, days=days)
        base = f"""FROM reviews r
                   LEFT JOIN review_tags t ON r.id = t.review_id
                   WHERE 1=1 {extra}"""
        total      = conn.execute(f"SELECT COUNT(*) as n {base}", params).fetchone()["n"]
        avg_rating = conn.execute(f"SELECT AVG(r.rating) as a {base}", params).fetchone()["a"]
        tagged     = conn.execute(
            f"SELECT COUNT(*) as n {base} AND t.sentiment IS NOT NULL", params).fetchone()["n"]
        s_rows = conn.execute(
            f"SELECT t.sentiment, COUNT(*) as n {base} AND t.sentiment IS NOT NULL "
            f"GROUP BY t.sentiment", params).fetchall()
        return {
            "total_reviews": total,
            "tagged_reviews": tagged,
            "avg_rating": round(avg_rating, 2) if avg_rating else 0,
            "sentiment": {row["sentiment"]: row["n"] for row in s_rows}
        }


def get_sentiment_trend(asin=None, category=None, rating=None, days=None):
    with get_conn() as conn:
        extra, params = _filter_clause(asin=asin, category=category, rating=rating, days=days)
        rows = conn.execute(
            f"""SELECT r.review_date as date, t.sentiment, COUNT(*) as n
                FROM reviews r JOIN review_tags t ON r.id = t.review_id
                WHERE r.review_date IS NOT NULL AND t.sentiment IS NOT NULL {extra}
                GROUP BY r.review_date, t.sentiment ORDER BY r.review_date""",
            params
        ).fetchall()
        return [dict(r) for r in rows]


def get_category_breakdown(sentiment=None, asin=None, rating=None, days=None):
    with get_conn() as conn:
        extra, params = _filter_clause(sentiment=sentiment, asin=asin, rating=rating, days=days)
        rows = conn.execute(
            f"""SELECT t.categories FROM review_tags t
                JOIN reviews r ON r.id = t.review_id
                WHERE t.categories IS NOT NULL {extra}""",
            params
        ).fetchall()
        counts = {}
        for row in rows:
            for c in json.loads(row["categories"]):
                counts[c] = counts.get(c, 0) + 1
        return counts


def get_products_with_stats(days=None):
    with get_conn() as conn:
        days_cond = "AND r.review_date >= date('now', ?)" if days and int(days) > 0 else ""
        days_param = [f'-{days} days'] if days and int(days) > 0 else []
        rows = conn.execute(
            f"""SELECT p.asin, p.name, p.category,
                       COUNT(r.id) as review_count,
                       AVG(r.rating) as avg_rating,
                       SUM(CASE WHEN t.sentiment='POSITIVE' THEN 1 ELSE 0 END) as positive,
                       SUM(CASE WHEN t.sentiment='NEGATIVE' THEN 1 ELSE 0 END) as negative,
                       SUM(CASE WHEN t.sentiment='NEUTRAL'  THEN 1 ELSE 0 END) as neutral
                FROM products p
                LEFT JOIN reviews r ON p.asin = r.asin {days_cond}
                LEFT JOIN review_tags t ON r.id = t.review_id
                GROUP BY p.asin""",
            days_param
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["avg_rating"] = round(d["avg_rating"], 2) if d["avg_rating"] else 0
            result.append(d)
        return result


def get_word_frequencies(sentiment=None, asin=None, category=None, days=None, limit=100):
    with get_conn() as conn:
        extra, params = _filter_clause(sentiment=sentiment, asin=asin,
                                       category=category, days=days)
        rows = conn.execute(
            f"""SELECT r.body FROM reviews r
                LEFT JOIN review_tags t ON r.id = t.review_id
                WHERE r.body IS NOT NULL {extra}
                LIMIT 500""",
            params
        ).fetchall()

    counts = {}
    for row in rows:
        words = re.findall(r'\b[a-zA-Z]{3,}\b', row["body"].lower())
        for w in words:
            if w not in STOP_WORDS:
                counts[w] = counts.get(w, 0) + 1

    sorted_words = sorted(counts.items(), key=lambda x: -x[1])
    return [[w, c] for w, c in sorted_words[:limit]]


def get_top_themes(sentiment, asin=None, days=None, limit=6):
    with get_conn() as conn:
        extra, params = _filter_clause(sentiment=sentiment, asin=asin, days=days)
        rows = conn.execute(
            f"""SELECT t.categories, r.body, r.rating
                FROM review_tags t JOIN reviews r ON r.id = t.review_id
                WHERE t.sentiment = ? AND t.categories IS NOT NULL {extra}
                ORDER BY r.review_date DESC""",
            [sentiment] + params
        ).fetchall()

    theme_data = {}
    for row in rows:
        cats = json.loads(row["categories"] or "[]")
        for cat in cats:
            if cat not in theme_data:
                theme_data[cat] = {"count": 0, "examples": []}
            theme_data[cat]["count"] += 1
            if len(theme_data[cat]["examples"]) < 2:
                snippet = (row["body"] or "")[:140].strip()
                if snippet:
                    theme_data[cat]["examples"].append(snippet)

    total = sum(v["count"] for v in theme_data.values()) or 1
    sorted_themes = sorted(theme_data.items(), key=lambda x: -x[1]["count"])
    return [
        {"category": k, "count": v["count"],
         "pct": round(v["count"] / total * 100),
         "examples": v["examples"]}
        for k, v in sorted_themes[:limit]
    ]


def get_available_categories():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT categories FROM review_tags WHERE categories IS NOT NULL"
        ).fetchall()
    cats = set()
    for row in rows:
        for c in json.loads(row["categories"]):
            cats.add(c)
    return sorted(cats)
