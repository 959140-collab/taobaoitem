import sys
from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir="./chrome_user_data",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"]
    )
    page = browser.new_page()
    try:
        page.goto('https://detail.tmall.com/item.htm?id=647177941662', timeout=30000)
    except Exception as e:
        print("Timeout or error:", e)
    
    # wait and scroll
    page.wait_for_timeout(2000)
    for _ in range(10):
        page.mouse.wheel(0, 500)
        page.wait_for_timeout(500)
    
    # execute
    v = page.evaluate("""() => {
        let els = document.querySelectorAll('div[class*="ItemProperty--"], div[class*="BasicContent--"], div[class*="skuItem"], div[class*="Attrs--"]');
        let texts = Array.from(els).map(e => e.innerText.trim()).filter(t => t.length > 0);
        return texts;
    }""")
    
    print("Found items:", len(v))
    for x in v[:20]:
        print("->", x.replace('\n', ' '))
        
    browser.close()
