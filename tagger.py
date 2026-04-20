import os
import json
import logging
import requests
from database import init_db, get_untagged_reviews, insert_tag

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HF_API_TOKEN = os.getenv("HF_API_TOKEN", "")
HF_SENTIMENT_MODEL = "cardiffnlp/twitter-roberta-base-sentiment-latest"
HF_ZERO_SHOT_MODEL = "facebook/bart-large-mnli"
HF_API_BASE = "https://api-inference.huggingface.co/models"

CATEGORY_LABELS = [
    "product quality",
    "delivery and shipping",
    "packaging",
    "price and value",
    "customer service",
    "durability",
    "ease of use",
    "authenticity",
]

CATEGORY_KEYWORDS = {
    "Quality": ["quality", "build", "material", "feel", "looks", "finish", "poor", "excellent", "good", "bad"],
    "Delivery": ["delivery", "shipping", "arrived", "fast", "slow", "delayed", "days", "courier", "dispatch"],
    "Packaging": ["packaging", "packed", "box", "wrap", "damaged", "broken on arrival", "well packed"],
    "Price/Value": ["price", "value", "worth", "expensive", "cheap", "cost", "money", "overpriced", "affordable"],
    "Customer Service": ["service", "support", "help", "return", "refund", "response", "seller", "complaint"],
    "Durability": ["durable", "lasting", "broke", "broken", "wear", "long lasting", "stopped working"],
    "Ease of Use": ["easy", "simple", "difficult", "complicated", "user friendly", "instructions", "setup"],
    "Authenticity": ["original", "fake", "genuine", "authentic", "duplicate", "copy", "counterfeit"],
}


def _hf_headers():
    if not HF_API_TOKEN:
        raise ValueError("HF_API_TOKEN not set. Get a free token at https://huggingface.co/settings/tokens")
    return {"Authorization": f"Bearer {HF_API_TOKEN}"}


def _hf_request(model, payload, retries=3):
    url = f"{HF_API_BASE}/{model}"
    headers = _hf_headers()
    for attempt in range(retries):
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 503:
            # Model loading, wait and retry
            import time
            wait = resp.json().get("estimated_time", 20)
            log.info(f"Model loading, waiting {wait:.0f}s...")
            time.sleep(min(wait, 30))
            continue
        if resp.status_code == 429:
            import time
            log.warning("Rate limited, waiting 10s...")
            time.sleep(10)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"HuggingFace API failed after {retries} retries")


def get_sentiment_hf(text):
    result = _hf_request(HF_SENTIMENT_MODEL, {"inputs": text[:512]})
    if isinstance(result, list) and result:
        scores = result[0] if isinstance(result[0], list) else result
        best = max(scores, key=lambda x: x["score"])
        label = best["label"].upper()
        # Normalize label names
        if label in ("LABEL_2", "POS", "POSITIVE"):
            label = "POSITIVE"
        elif label in ("LABEL_0", "NEG", "NEGATIVE"):
            label = "NEGATIVE"
        else:
            label = "NEUTRAL"
        return label, round(best["score"], 4)
    return "NEUTRAL", 0.5


def get_categories_hf(text):
    result = _hf_request(
        HF_ZERO_SHOT_MODEL,
        {"inputs": text[:512], "parameters": {"candidate_labels": CATEGORY_LABELS}}
    )
    matched = []
    if "scores" in result and "labels" in result:
        for label, score in zip(result["labels"], result["scores"]):
            if score > 0.3:
                # Map back to friendly names
                name = label.title().replace("And ", "& ")
                matched.append(name)
    return matched[:3]  # Top 3 categories


def get_categories_keyword(text):
    text_lower = text.lower()
    matched = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            matched.append(category)
    return matched


def tag_review(text, rating=None):
    use_hf = bool(HF_API_TOKEN)

    if use_hf:
        try:
            sentiment, score = get_sentiment_hf(text)
            categories = get_categories_hf(text)
            if not categories:
                categories = get_categories_keyword(text)
            return sentiment, score, categories
        except Exception as e:
            log.warning(f"HuggingFace API error: {e}. Falling back to local tagging.")

    # Local fallback: VADER sentiment + keyword categories
    return _local_tag(text, rating)


def _local_tag(text, rating=None):
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
        vs = analyzer.polarity_scores(text)
        compound = vs["compound"]
        if compound >= 0.05:
            sentiment, score = "POSITIVE", round(vs["pos"], 4)
        elif compound <= -0.05:
            sentiment, score = "NEGATIVE", round(vs["neg"], 4)
        else:
            sentiment, score = "NEUTRAL", 0.5
    except ImportError:
        # Last resort: use star rating
        if rating is not None:
            if rating >= 4:
                sentiment, score = "POSITIVE", 0.8
            elif rating <= 2:
                sentiment, score = "NEGATIVE", 0.8
            else:
                sentiment, score = "NEUTRAL", 0.5
        else:
            sentiment, score = "NEUTRAL", 0.5

    categories = get_categories_keyword(text)
    return sentiment, score, categories


def run_tagger(batch_size=20):
    init_db()
    reviews = get_untagged_reviews(limit=batch_size)

    if not reviews:
        log.info("No untagged reviews found.")
        return

    log.info(f"Tagging {len(reviews)} reviews...")
    tagged = 0

    for review in reviews:
        try:
            sentiment, score, categories = tag_review(
                text=review["body"],
                rating=review.get("rating")
            )
            insert_tag(
                review_id=review["id"],
                sentiment=sentiment,
                score=score,
                categories=categories
            )
            tagged += 1
            log.info(f"  [{tagged}] Review {review['id']}: {sentiment} | {categories}")
        except Exception as e:
            log.error(f"Failed to tag review {review['id']}: {e}")

    log.info(f"Tagging complete. Tagged {tagged}/{len(reviews)} reviews.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=20, help="Reviews per batch")
    args = parser.parse_args()
    run_tagger(batch_size=args.batch)
