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
    page.goto('https://item.taobao.com/item.htm?id=769152862476')
    
    page.wait_for_timeout(2000)
    for _ in range(10):
        page.mouse.wheel(0, 500)
        page.wait_for_timeout(500)
    
    html = page.evaluate("() => document.body.innerHTML")
    with open('tb_page3.html', 'w', encoding='utf-8') as f:
        f.write(html)
        
    print("Saved to tb_page3.html")
    browser.close()
