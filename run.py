import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Amazon Reviews Pipeline")
    subparsers = parser.add_subparsers(dest="command")

    # scrape
    sp = subparsers.add_parser("scrape", help="Scrape Amazon reviews")
    sp.add_argument("--asins", default="asins.csv", help="CSV file with ASINs")
    sp.add_argument("--pages", type=int, default=5, help="Max pages per product")
    sp.add_argument("--no-headless", action="store_true", help="Show browser window")

    # tag
    tp = subparsers.add_parser("tag", help="Tag reviews with HuggingFace AI")
    tp.add_argument("--batch", type=int, default=20, help="Reviews per run")

    # serve
    svp = subparsers.add_parser("serve", help="Start the dashboard server")
    svp.add_argument("--port", type=int, default=8000)

    # all
    subparsers.add_parser("all", help="Run full pipeline: scrape → tag → serve")

    args = parser.parse_args()

    if args.command == "scrape" or args.command == "all":
        from scraper import run_scraper
        asins = getattr(args, "asins", "asins.csv")
        pages = getattr(args, "pages", 5)
        headless = not getattr(args, "no_headless", False)
        log.info("=== STEP 1: Scraping ===")
        run_scraper(asins_file=asins, max_pages=pages, headless=headless)

    if args.command == "tag" or args.command == "all":
        from tagger import run_tagger
        batch = getattr(args, "batch", 20)
        log.info("=== STEP 2: Tagging ===")
        run_tagger(batch_size=batch)

    if args.command == "serve" or args.command == "all":
        import uvicorn
        port = getattr(args, "port", 8000)
        log.info(f"=== STEP 3: Dashboard at http://localhost:{port} ===")
        uvicorn.run("api:app", host="0.0.0.0", port=port, reload=False)

    if not args.command:
        parser.print_help()


if __name__ == "__main__":
    main()
