"""Check isolated headed Chromium before attempting any account login."""
import json
import sys
from playwright.sync_api import sync_playwright
from provider_auth import browser_initialization_diagnostics


def main():
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False, args=['--no-sandbox', '--disable-dev-shm-usage'])
            page = browser.new_page()
            page.goto('about:blank')
            browser.close()
        print('Isolated Chromium startup verified before account login.')
        return 0
    except Exception as exc:
        print(json.dumps({'browser_preflight': 'failed', 'exception': type(exc).__name__,
                          **browser_initialization_diagnostics(exc)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
