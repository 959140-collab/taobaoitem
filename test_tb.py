import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    page = browser.new_page()
    page.goto('https://detail.tmall.com/item.htm?id=647177941662')
    page.wait_for_timeout(5000) # give it time to load
    # Try to find elements containing the text "品牌" or "型号"
    html = page.evaluate("() => document.body.outerHTML")
    with open('tb_page.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print("Done")
