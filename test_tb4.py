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
    page.goto('https://item.taobao.com/item.htm?id=742501897080')
    
    page.wait_for_timeout(2000)
    for _ in range(12):
        page.mouse.wheel(0, 500)
        page.wait_for_timeout(500)
        
    print("Trying to screenshot the element...")
    # Find the parameter element
    el = page.locator('div:has-text("参数信息")').locator('..').first
    if el.count() > 0:
        el.screenshot(path="params_test.png")
        print("Screenshot saved to params_test.png (method 1)")
    else:
        # Fallback
        el2 = page.locator('.paramsInfoArea')
        if el2.count() > 0:
            el2.screenshot(path="params_test.png")
            print("Screenshot saved to params_test.png (method 2)")
        else:
            print("Element not found")
            
    browser.close()
