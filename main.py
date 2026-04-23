import tkinter as tk
from tkinter import messagebox, scrolledtext
import re
import os
import time
import queue
import tempfile
import shutil
import subprocess
import threading
from io import BytesIO
from PIL import Image, ImageTk
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = lambda x: None
import platform
from pathlib import Path

try:
    import pytesseract
except ImportError:
    pytesseract = None

# ─── 历史记录文件 ──────────────────────────────────────────────
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'urls_history.txt')

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return f.read()
    return ''

def save_history(content):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        f.write(content)

# ─── 运行日志 ──────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'download.log')

class AppLogger:
    """追加写入 download.log，线程安全"""
    _lock = threading.Lock() if False else __import__('threading').Lock()

    def _write(self, level, msg):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{ts}] [{level}] {msg}"
        with self._lock:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')

    def start(self):
        self._write('START', '程序启动')

    def exit(self):
        self._write('EXIT ', '程序退出')

    def begin(self, url):
        self._write('BEGIN', f'开始下载: {url}')

    def success(self, title, save_path):
        self._write('OK   ', f'下载成功: 【{title}】 -> {save_path}')

    def failure(self, url, error):
        self._write('FAIL ', f'下载失败: {url} | 错误: {error}')

app_logger = AppLogger()

# ─── 工具函数 ──────────────────────────────────────────────────
def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|\r\n\t]', '', name).strip()

def play_sound(success=True):
    """播放简短提示音：成功=轻柔短音，失败=低沉报警音"""
    sys_name = platform.system()
    try:
        if sys_name == 'Darwin':
            if success:
                os.system('afplay /System/Library/Sounds/Glass.aiff &')
            else:
                os.system('afplay /System/Library/Sounds/Basso.aiff &')
        elif sys_name == 'Windows':
            import winsound
            winsound.Beep(880 if success else 400, 200)
        else:
            print('\a', end='', flush=True)
    except Exception:
        pass

def system_beep():
    sys_name = platform.system()
    try:
        if sys_name == 'Windows':
            import winsound
            winsound.Beep(1000, 500)
        elif sys_name == 'Darwin':
            os.system('afplay /System/Library/Sounds/Glass.aiff &')
        else:
            print('\a', end='', flush=True)
    except Exception:
        pass

# ─── 爬虫核心 ─────────────────────────────────────────────────
class TaobaoScraper:
    def __init__(self, urls, log_queue):
        self.urls = urls
        self.log_queue = log_queue
        self.logger = app_logger
        downloads_dir = os.path.join(str(Path.home()), 'Downloads', 'Taobao_Data')
        os.makedirs(downloads_dir, exist_ok=True)
        self.base_dir = downloads_dir

    def log(self, msg):
        self.log_queue.put(msg)

    def run(self):
        with sync_playwright() as p:
            user_data_dir = os.path.join(os.getcwd(), 'chrome_user_data')
            browser = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
                viewport={'width': 1280, 'height': 800},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars"
                ]
            )
            page = browser.new_page()
            stealth_sync(page)

            total = len(self.urls)
            failed = False
            for idx, url in enumerate(self.urls, 1):
                if not url.strip():
                    continue
                self.log(f"\n[{idx}/{total}] 正在处理: {url}")
                self.logger.begin(url)
                try:
                    self.process_item(page, url.strip())
                except Exception as e:
                    self.log(f"\n❌ 下载失败，程序终止。错误: {e}")
                    self.logger.failure(url, e)
                    play_sound(success=False)
                    failed = True
                    break

            browser.close()
            if not failed:
                self.log(f"\n✅ 全部 {total} 件商品下载完成！文件保存在:\n{self.base_dir}")

    def process_item(self, page, url):
        try:
            # 网络拦截：捕获 .mp4 响应 URL
            captured_videos = []
            def _capture_video(resp):
                u = resp.url
                if '.mp4' in u and u.startswith('http'):
                    captured_videos.append(u)
            page.on('response', _capture_video)

            page.goto(url, timeout=60000)
            self.check_verification(page)
            self.scroll_page(page)

            # 滚动后稍等让视频请求完成
            import time as _t; _t.sleep(2)
            page.remove_listener('response', _capture_video)

            title = page.title().split('-')[0].strip()
            item_id = "unknown"
            match = re.search(r'id=(\d+)', url)
            if match:
                item_id = match.group(1)

            safe_title = sanitize_filename(title)
            if len(safe_title) > 35:
                safe_title = safe_title[:35]

            folder_name = f"{item_id}_{safe_title}"
            item_dir = os.path.join(self.base_dir, folder_name)
            img_dir = os.path.join(item_dir, "images")
            os.makedirs(img_dir, exist_ok=True)

            # 主图
            main_imgs = page.evaluate("""() => {
                let imgs = Array.from(document.querySelectorAll('.tb-gallery img, .mainPic--pic--1lJc90o, #J_ImgBooth, .gallery--img--1X_QkP3, div[class*="mainPic"] img, div[class*="gallery"] img'));
                return imgs.map(img => img.src).filter(src => src && !src.includes('lazy') && !src.includes('10x10.jpg'));
            }""")
            main_imgs = list(set([self.format_url(src) for src in main_imgs]))

            # 主图视频：网络拦截 + JS 双策略
            video_urls_set = set(captured_videos)
            js_videos = page.evaluate(r"""() => {
                let urls = new Set();
                document.querySelectorAll('video').forEach(v => {
                    if (v.src && v.src.startsWith('http')) urls.add(v.src);
                    v.querySelectorAll('source').forEach(s => {
                        if (s.src && s.src.startsWith('http')) urls.add(s.src);
                    });
                });
                document.querySelectorAll('script').forEach(sc => {
                    let text = sc.textContent || '';
                    let matches = text.match(/https?:\/\/[^"'\s,)]+\.mp4[^"'\s,)]*/g);
                    if (matches) matches.forEach(u => urls.add(u));
                });
                return Array.from(urls);
            }""")
            video_urls_set.update(js_videos)
            video_urls = list(video_urls_set)

            # 详情图
            desc_imgs = page.evaluate("""() => {
                let imgs = Array.from(document.querySelectorAll('#description img, #J_DivItemDesc img, div[class*="desc-"] img, div[class*="detail"] img'));
                return imgs.map(img => img.getAttribute('data-src') || img.src).filter(src => src && !src.includes('lazy'));
            }""")
            desc_imgs = list(set([self.format_url(src) for src in desc_imgs]))

            # 属性
            props = page.evaluate("""() => {
                let selectors = [
                    'ul[class*="attributes"] li',
                    'ul[class*="parameter"] li',
                    '#J_AttrUL li',
                    'table[class*="pkg-table"] tr',
                    'div[class*="Attrs--item"]',
                    'div[class*="BasicContent--item"]',
                    'div[class^="ItemProperty--"]',
                    'div[class*="generalParamsInfoItem--"]',
                    'div[class*="emphasisParamsInfoItem--"]'
                ];
                let items = Array.from(document.querySelectorAll(selectors.join(', ')));
                let results = items.map(el => { var s = el.innerText.trim(); return s.split(String.fromCharCode(10)).join('：'); }).filter(t => t.length > 2);
                if (results.length === 0) {
                    let targetPool = [];
                    for (let el of document.querySelectorAll('div')) {
                        if (el.innerText && el.innerText.includes('品牌') && el.innerText.includes('材质') && el.children.length > 3 && el.innerText.length < 2000) {
                            targetPool.push(el);
                        }
                    }
                    if (targetPool.length > 0) {
                        results = Array.from(targetPool[targetPool.length-1].children)
                            .map(el => { var s = el.innerText.trim(); return s.split(String.fromCharCode(10)).join('：'); })
                            .filter(t => t.length > 2);
                    }
                }
                return results;
            }""")

            # OCR 兜底
            if not props and pytesseract:
                self.log("  --> DOM 未找到属性，尝试 OCR 识别...")
                ocr_img_path = os.path.join(item_dir, "temp_ocr.png")
                param_box = page.locator('div:has-text("参数信息")').locator('..').first
                if param_box.count() == 0:
                    param_box = page.locator('.paramsInfoArea').first
                if param_box.count() > 0:
                    try:
                        param_box.screenshot(path=ocr_img_path)
                        text_data = pytesseract.image_to_string(Image.open(ocr_img_path), lang="chi_sim")
                        lines = [l.strip() for l in text_data.split('\n') if len(l.strip()) > 1]
                        for idx2 in range(0, len(lines)-1, 2):
                            if len(lines[idx2]) < 10 and len(lines[idx2+1]) < 30:
                                props.append(f"{lines[idx2]}：{lines[idx2+1]}")
                            else:
                                props.append(lines[idx2])
                                props.append(lines[idx2+1])
                    except Exception as e:
                        self.log(f"  --> OCR 失败: {e}")
                    finally:
                        if os.path.exists(ocr_img_path):
                            os.remove(ocr_img_path)

            self.log(f"  --> 主图 {len(main_imgs)} 张 | 详情图 {len(desc_imgs)} 张 | 视频 {len(video_urls)} 个 | 属性 {len(props)} 条")

            image_candidates = []
            self.log(f"  [图片预加载] 正在获取图片以供挑选...")
            for i, img_url in enumerate(main_imgs):
                body = self.fetch_image_bytes(img_url, page)
                if body:
                    image_candidates.append({
                        "url": img_url,
                        "filename": f"main_{i+1}.jpg",
                        "body": body,
                        "size": len(body),
                        "group": "主图"
                    })

            for i, img_url in enumerate(desc_imgs):
                body = self.fetch_image_bytes(img_url, page)
                if body:
                    image_candidates.append({
                        "url": img_url,
                        "filename": f"desc_{i+1}.jpg",
                        "body": body,
                        "size": len(body),
                        "group": "详情图"
                    })

            # 规格图：在视频缓存之前先点击各 SKU 规格抓图（页面状态更稳定）
            already_urls = set(c["url"] for c in image_candidates)
            sku_candidates, sku_all_names = self.scrape_sku_images(page, already_urls)
            image_candidates.extend(sku_candidates)

            # 生成规格清单文本文件
            if sku_all_names:
                sku_txt_path = os.path.join(item_dir, "sku_names.txt")
                with open(sku_txt_path, "w", encoding="utf-8") as f:
                    f.write("|".join(sku_all_names))
                self.log(f"  [规格清单] 已生成 sku_names.txt，共 {len(sku_all_names)} 个规格")

            if video_urls:
                self.log(f"  [视频] 获取视频并在后台生成封面，共 {len(video_urls)} 个...")
                for i, v_url in enumerate(video_urls):
                    self.log(f"    正在缓存视频 {i+1}/{len(video_urls)}: {v_url[:60]}...")
                    v_item = self.fetch_video_to_temp(v_url, page, f"video_{i+1}.mp4")
                    if v_item:
                        image_candidates.append(v_item)

            if image_candidates:
                self.log(f"  --> 正在等待您在界面确认要保存的图片和视频...")
                user_selected = self.ask_user_for_images(image_candidates)
                
                if user_selected is None:
                    self.log(f"  [⏹️ 终止] 您点击了终止下载，正在放弃当前商品的所有文件。")
                    for item in image_candidates:
                        if item.get("is_video"):
                            try: os.remove(item["temp_path"])
                            except: pass
                    time.sleep(0.5) # ensure handles are free
                    shutil.rmtree(item_dir, ignore_errors=True)
                    self.log(f"  ❌ 当前商品【{safe_title}】已跳过，未保存任何信息。")
                    return # 立刻跳出当前商品的抓取循环
                    
                if user_selected:
                    self.log(f"  [保存选定内容] 您选择了 {len(user_selected)} 个项目，正在保存...")
                    for item in user_selected:
                        if item.get("is_video"):
                            save_path = os.path.join(item_dir, item["filename"])
                            shutil.move(item["temp_path"], save_path)
                            self.log(f"    [+] 视频保存: {item['filename']}")
                        else:
                            save_path = os.path.join(img_dir, item["filename"])
                            body = item["body"]
                            img = Image.open(BytesIO(body))
                            if img.mode in ("RGBA", "P"):
                                img = img.convert("RGB")
                            img.save(save_path, "JPEG", quality=95)
                    
                    # 清理未选中的视频临时文件
                    for item in image_candidates:
                        if item.get("is_video") and item not in user_selected:
                            try: os.remove(item["temp_path"])
                            except: pass
                else:
                    self.log(f"  [放弃] 您没有选择任何内容。")
                    for item in image_candidates:
                        if item.get("is_video"):
                            try: os.remove(item["temp_path"])
                            except: pass
            else:
                self.log(f"  [结果] 未获取到有效内容。")

            self.log(f"  [属性] 写入 info.txt，共 {len(props)} 条...")
            with open(os.path.join(item_dir, "info.txt"), "w", encoding="utf-8") as f:
                f.write(f"原始URL: {url}\n")
                f.write(f"抓取时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("=== 核心属性参数 ===\n")
                written = 0
                for prop in props:
                    if not any(k in prop for k in ['价格', '促销', '销量', '评价', '详情']):
                        f.write(f"{prop}\n")
                        written += 1
                        self.log(f"    属性 {written}: {prop[:50]}")

            self.log(f"  ✅ 【{safe_title}】下载完成")
            self.log(f"     保存路径: {item_dir}")
            self.log(f"OPEN_DIR::{item_dir}")
            self.logger.success(safe_title, item_dir)
            play_sound(success=True)

        except Exception as e:
            self.log(f"  ❌ 处理失败: {e}")
            raise

    def check_verification(self, page):
        while True:
            title = page.title()
            is_blocked = any(k in title for k in ["验证码", "滑块", "安全验证", "登录", "login", "Login", "登录淘宝"])
            if not is_blocked:
                is_blocked = page.locator(".nc-container, #baxia-dialog-content, #login, #J_LoginBox, .baxia-dialog").count() > 0
            if is_blocked:
                system_beep()
                self.log("  --> 检测到安全拦截或需要登录，等待浏览器中手动完成...")
                event = threading.Event()
                self.log_queue.put(("SHOW_TOP_DIALOG", "安全拦截或需要登录", "检测到验证码、滑块或需要手动登录淘宝。\n\n如果当前在登录页，请在弹出的浏览器中登入您的账号；如果出现验证码也请手动处理。\n\n完成后点击「确定」继续任务。", event))
                event.wait()
                time.sleep(2)
            else:
                break

    def scroll_page(self, page):
        page.mouse.wheel(0, 300)
        time.sleep(1)
        for _ in range(25):
            page.mouse.wheel(0, 600)
            time.sleep(0.4)

    def format_url(self, url):
        url = re.sub(r'_\.\d+x\d+[a-zA-Z]*\.jpg$', '', url)
        if url.startswith('//'):
            return 'https:' + url
        elif url.startswith('/'):
            return 'https://' + url
        return url

    def fetch_video_to_temp(self, url, page, filename):
        """把视频下载到临时文件，并用 OpenCV 提取封面，作为候选项"""
        try:
            import urllib.request
            headers = {
                'User-Agent': page.evaluate("navigator.userAgent"),
                'Referer': page.url
            }
            fd, temp_path = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(temp_path, 'wb') as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            
            size_bytes = os.path.getsize(temp_path)
            first_frame_bytes = None
            try:
                import cv2
                cap = cv2.VideoCapture(temp_path)
                ret, frame = cap.read()
                if ret:
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(frame_rgb)
                    # 保存封面为 JPEG 格式字节数据
                    buf = BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    first_frame_bytes = buf.getvalue()
                cap.release()
            except Exception as e:
                self.log(f"    [!] 提取视频封面出错: {e}")

            return {
                "url": url,
                "filename": filename,
                "body": first_frame_bytes,
                "size": size_bytes,
                "group": "视频",
                "is_video": True,
                "temp_path": temp_path
            }
        except Exception as e:
            self.log(f"    [!] 视频临时下载失败: {e}")
            return None

    def fetch_image_bytes(self, url, page):
        try:
            headers = {
                'User-Agent': page.evaluate("navigator.userAgent"),
                'Referer': page.url
            }
            resp = page.context.request.get(url, headers=headers, timeout=15000)
            if resp.ok:
                body = resp.body()
                if len(body) < 10 * 1024:
                    # self.log("  --> 图片小于 10KB，视为无效图片并忽略")
                    return None
                return body
        except Exception as e:
            self.log(f"  --> 图片读取失败: {e}")
        return None

    def scrape_sku_images(self, page, existing_urls):
        """遍历所有规格（SKU）选项，点击并捕获每个规格对应的主图"""
        sku_candidates = []
        all_sku_names  = []   # 所有规格名（不管有没有新图）
        seen_urls = set(existing_urls)

        try:
            # 滚回顶部，确保 SKU 选区可见
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1.0)

            # ── JS：找所有规格选项 ──
            # 真实结构：div[class*="valueItem"] > img.valueItemImg + span.valueItemText[title="规格名"]
            FIND_SKU_JS = """
() => {
    const spans = Array.from(document.querySelectorAll('span[class*="valueItemText"]'));
    return spans.map((el, i) => ({
        idx : i,
        text: (el.getAttribute('title') || el.innerText || '').trim()
    })).filter(it => it.text.length > 0 && it.text.length < 60);
}
"""

            CLICK_SKU_JS = """
(idx) => {
    const spans = Array.from(document.querySelectorAll('span[class*="valueItemText"]'));
    if (!spans[idx]) return;
    // 向上找 div[class*="valueItem"] 然后点它
    const clickTarget = spans[idx].closest('div[class*="valueItem"]') || spans[idx].parentElement;
    if (clickTarget) {
        clickTarget.scrollIntoView({ block: 'center' });
        clickTarget.click();
    }
}
"""

            GET_MAIN_IMG_JS = """
() => {
    const selectors = [
        'div[class*="mainPic"] img',
        'div[class*="MainPic"] img',
        '.tb-gallery img',
        '#J_ImgBooth',
        'div[class*="gallery"] img',
        'div[class*="itemGallery"] img'
    ];
    for (const sel of selectors) {
        const imgs = Array.from(document.querySelectorAll(sel))
            .map(img => img.src)
            .filter(src => src && src.startsWith('http') &&
                          !src.includes('10x10') && !src.includes('lazy'));
        if (imgs.length > 0) return imgs;
    }
    return [];
}
"""

            sku_info = page.evaluate(FIND_SKU_JS)
            if not sku_info:
                self.log("  [规格图] 未发现规格选项，跳过。")
                return []

            self.log(f"  [规格图] 发现 {len(sku_info)} 个规格，逐一点击抓图...")

            for sku in sku_info:
                idx  = sku['idx']
                raw_name  = sku['text'] or f"规格{idx+1}"
                safe_name = sanitize_filename(raw_name)[:30] or f"sku_{idx+1}"
                all_sku_names.append(raw_name)   # 每个规格都记录

                try:
                    page.evaluate(CLICK_SKU_JS, idx)
                    time.sleep(1.2)   # 等主图切换动画完成

                    cur_srcs = page.evaluate(GET_MAIN_IMG_JS)
                    new_imgs = [self.format_url(s) for s in cur_srcs
                                if self.format_url(s) not in seen_urls]

                    fetched = 0
                    for img_url in new_imgs:
                        seen_urls.add(img_url)
                        body = self.fetch_image_bytes(img_url, page)
                        if body:
                            fetched += 1
                            sku_candidates.append({
                                "url"     : img_url,
                                "filename": f"sku_{safe_name}_{fetched}.jpg",
                                "body"    : body,
                                "size"    : len(body),
                                "group"   : f"规格·{safe_name}"
                            })

                    if fetched:
                        self.log(f"    ✔ [{safe_name}] 抓到 {fetched} 张新图")
                    else:
                        self.log(f"    - [{safe_name}] 无新图（与其他规格相同）")

                except Exception as e:
                    self.log(f"    [!] 规格【{safe_name}】处理失败: {e}")
                    continue

            self.log(f"  [规格图] 完成，共采集到 {len(sku_candidates)} 张规格图")

        except Exception as e:
            self.log(f"  [规格图] 全局出错: {e}")

        return sku_candidates, all_sku_names

    def ask_user_for_images(self, candidates):
        event = threading.Event()
        result_box = []
        # 发送特殊指令给主线程，要求弹窗
        self.log_queue.put(("ASK_USER_IMAGES", candidates, event, result_box))
        event.wait()
        return result_box[0] if result_box else []


# ─── 单窗口双页面主程序 ──────────────────────────────────────
def run_app():
    root = tk.Tk()
    root.title("淘宝/天猫商品数据抓取助手")
    root.geometry("750x640")
    root.eval('tk::PlaceWindow . center')

    log_queue = queue.Queue()

    # ── 页面容器 ──────────────────────────────────────
    input_frame   = tk.Frame(root)
    progress_frame = tk.Frame(root)

    def show_input_page():
        progress_frame.pack_forget()
        input_frame.pack(fill='both', expand=True)

    def show_progress_page():
        input_frame.pack_forget()
        progress_frame.pack(fill='both', expand=True)

    # ════════════════════════════════════════════════
    # 输入页
    # ════════════════════════════════════════════════
    input_top = tk.Frame(input_frame)
    input_top.pack(fill='x', padx=10, pady=6)
    tk.Label(input_top, text="商品 URL 输入",
             font=("Arial", 13, "bold")).pack(side='left')
    tk.Button(input_top, text="查看进度 ▶", command=show_progress_page,
              font=("Arial", 12), padx=10, pady=3).pack(side='right')

    tk.Label(input_frame, text="请粘贴淘宝或天猫的商品 URL（每行一条）:",
             font=("Arial", 14)).pack(pady=6)

    text_area = tk.Text(input_frame, width=82, height=20)
    text_area.pack(padx=12, pady=6)

    history = load_history()
    if history:
        text_area.insert('1.0', history)

    def on_submit():
        content = text_area.get("1.0", "end-1c").strip()
        if not content:
            return
        save_history(content)
        urls = [l.strip() for l in content.split('\n') if l.strip()]
        # 清空日志框，准备新一轮
        log_box.configure(state='normal')
        log_box.delete('1.0', 'end')
        log_box.configure(state='disabled')
        status_var.set("正在初始化...")
        show_progress_page()
        # 启动爬虫线程
        t = threading.Thread(target=lambda: run_scraper(urls), daemon=True)
        t.start()

    def on_clear():
        text_area.delete('1.0', 'end')
        save_history('')

    def on_paste():
        try:
            clipboard = root.clipboard_get()
        except Exception:
            return
        # 逐行过滤：只保留以 http:// 或 https:// 开头的行
        valid_lines = [l.strip() for l in clipboard.splitlines()
                       if l.strip().startswith(('http://', 'https://'))]
        if not valid_lines:
            return
        # 追加到输入框（末尾换行后插入）
        current = text_area.get('1.0', 'end-1c')
        if current and not current.endswith('\n'):
            text_area.insert('end', '\n')
        text_area.insert('end', '\n'.join(valid_lines))

    def on_clear_paste_submit():
        on_clear()
        on_paste()
        on_submit()

    btn_row = tk.Frame(input_frame)
    btn_row.pack(pady=10)
    tk.Button(btn_row, text="粘贴", command=on_paste,
              font=("Arial", 13), padx=14, pady=4).pack(side='left', padx=10)
    tk.Button(btn_row, text="清空", command=on_clear,
              font=("Arial", 13), padx=14, pady=4).pack(side='left', padx=10)
    btn_one_key = tk.Button(btn_row, text="一键清空粘贴抓取", command=on_clear_paste_submit,
                            font=("Arial", 13, "bold"), padx=14, pady=4)
    btn_one_key.pack(side='left', padx=10)
    tk.Button(btn_row, text="开始抓取 ▶", command=on_submit,
              font=("Arial", 13, "bold"), padx=14, pady=4).pack(side='left', padx=10)

    preview_frame = tk.Frame(input_frame)
    preview_frame.pack(fill='x', padx=12, pady=5)
    tk.Label(preview_frame, text="剪贴板有效网址预览：", font=("Arial", 11), fg="#555").pack(anchor='w')
    preview_text = scrolledtext.ScrolledText(preview_frame, height=4, width=82, state='disabled', bg="#f9f9f9", fg="#333", font=("Courier", 11))
    preview_text.pack(fill='x')

    def update_clipboard():
        try:
            clipboard = root.clipboard_get()
            valid_lines = [l.strip() for l in clipboard.splitlines() if l.strip().startswith(('http://', 'https://'))]
            
            preview_text.configure(state='normal')
            preview_text.delete('1.0', 'end')
            if valid_lines:
                preview_text.insert('1.0', '\n'.join(valid_lines))
                btn_one_key.config(state='normal')
            else:
                preview_text.insert('1.0', '（当前剪贴板中无有效的 http / https 开头的商品网址）')
                btn_one_key.config(state='disabled')
            preview_text.configure(state='disabled')
        except Exception:
            preview_text.configure(state='normal')
            preview_text.delete('1.0', 'end')
            preview_text.insert('1.0', '（无法读取剪贴板内容）')
            btn_one_key.config(state='disabled')
            preview_text.configure(state='disabled')
        root.after(800, update_clipboard)

    update_clipboard()

    text_area.focus_set()

    # ════════════════════════════════════════════════
    # 进度页
    # ════════════════════════════════════════════════
    top_bar = tk.Frame(progress_frame)
    top_bar.pack(fill='x', padx=10, pady=6)
    tk.Button(top_bar, text="◀ 返回输入", command=show_input_page,
              font=("Arial", 12), padx=10, pady=3).pack(side='left')
    tk.Label(top_bar, text="下载进度信息（可直接复制）",
             font=("Arial", 13, "bold")).pack(side='left', padx=16)

    log_box = scrolledtext.ScrolledText(
        progress_frame, width=88, height=26, state='disabled',
        font=("Courier", 11), bg="#1e1e1e", fg="#d4d4d4",
        insertbackground='white'
    )
    log_box.pack(padx=10, pady=4, fill='both', expand=True)

    status_var = tk.StringVar(value="")
    tk.Label(progress_frame, textvariable=status_var,
             font=("Arial", 11), fg="#555").pack(pady=5)

    # ── 日志轮询 ──────────────────────────────────────
    # 链接样式标签（只创建一次）
    _link_tag_counter = [0]

    def open_directory(path):
        import subprocess
        sys_name = platform.system()
        try:
            if sys_name == 'Darwin':
                subprocess.Popen(['open', path])
            elif sys_name == 'Windows':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            pass

    def show_image_selection_dialog(candidates, event, result_box):
        top = tk.Toplevel(root)
        top.title("挑选需要下载的图片")
        top.geometry("750x550")
        top.transient(root)
        top.grab_set()

        tk.Label(top, text="请勾选需要下载的图片（默认全选）：", font=("Arial", 12, "bold")).pack(side="top", pady=10)

        # 底部按钮区优先放到最下面，防止被其它元素挤出屏幕
        btn_frame = tk.Frame(top)
        btn_frame.pack(side="bottom", pady=10)

        container = tk.Frame(top)
        container.pack(side="top", fill="both", expand=True, padx=10, pady=5)

        canvas = tk.Canvas(container, highlightthickness=0, bg=top.cget("bg"))
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=top.cget("bg"))

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def on_mousewheel(event):
            try:
                # Tk 9.0 (Python 3.14+) macOS 使用 <<ScrollWheel>> 虚拟事件
                # delta 是无符号16位整数：正值向下，大值(>32767)是负数(向上)
                delta = event.delta
                if delta > 32767:       # 无符号转有符号
                    delta = delta - 65536
                # 缩放为合理的滚动步长
                steps = max(1, abs(delta) // 40)
                direction = -1 if delta > 0 else 1
                canvas.yview_scroll(direction * steps, "units")
            except Exception:
                pass

        def bind_wheel_recursive(widget):
            """递归绑定到每个子控件（兼容 Tk 9.0）"""
            widget.bind("<<ScrollWheel>>", on_mousewheel)
            for child in widget.winfo_children():
                bind_wheel_recursive(child)

        item_frames = []
        vars_dict = {}
        top.image_refs = [] 

        full_top_ref = [None]
        full_lbl_ref = [None]
        current_idx = [0]
        full_photo_ref = [None]

        def open_gallery(start_idx):
            if full_top_ref[0] and full_top_ref[0].winfo_exists():
                full_top_ref[0].destroy()
                
            ft = tk.Toplevel(top)
            full_top_ref[0] = ft
            ft.transient(top)
            ft.attributes("-topmost", True)
            
            lbl = tk.Label(ft, cursor="hand2")
            lbl.pack()
            full_lbl_ref[0] = lbl
            
            def load_image(idx):
                idx = idx % len(candidates)
                current_idx[0] = idx
                item = candidates[idx]
                
                body_bytes = item['body']
                img_full = Image.open(BytesIO(body_bytes))
                if img_full.mode in ("RGBA", "P"):
                    img_full = img_full.convert("RGB")
                    
                sw = top.winfo_screenwidth() - 100
                sh = top.winfo_screenheight() - 100
                if img_full.width > sw or img_full.height > sh:
                    img_full.thumbnail((sw, sh))
                    
                full_photo_ref[0] = ImageTk.PhotoImage(img_full)
                full_lbl_ref[0].configure(image=full_photo_ref[0])
                
                update_title()
            
            def update_title():
                idx = current_idx[0]
                is_sel = vars_dict[idx].get()
                status = "✅ 已选" if is_sel else "❌ 未选"
                ft.title(f"{status} | [{candidates[idx]['group']}] 查看大图 - 方向键切换，空格选中，ESC关闭")
                
            def on_left(e): load_image(current_idx[0] - 1)
            def on_right(e): load_image(current_idx[0] + 1)
            def on_space(e):
                idx = current_idx[0]
                vars_dict[idx].set(not vars_dict[idx].get())
                update_title()
                
            ft.bind("<Left>", on_left)
            ft.bind("<Right>", on_right)
            ft.bind("<space>", on_space)
            ft.bind("<Escape>", lambda e: ft.destroy())
            lbl.bind("<Button-1>", lambda e: ft.destroy())
            
            load_image(start_idx)
            
            ft.update_idletasks()
            x = (top.winfo_screenwidth() // 2) - (ft.winfo_width() // 2)
            y = (top.winfo_screenheight() // 2) - (ft.winfo_height() // 2)
            ft.geometry(f"+{max(0, x)}+{max(0, y)}")
            
            ft.focus_set()

        for idx, item in enumerate(candidates):
            var = tk.BooleanVar(value=True)
            size_kb = item['size'] / 1024
            text_str = f"[{item['group']}]\n{size_kb:.1f} KB"
            
            cell_frame = tk.Frame(scrollable_frame, bg=top.cget("bg"))
            item_frames.append(cell_frame)

            photo = None
            try:
                img = Image.open(BytesIO(item['body']))
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.thumbnail((120, 120))
                photo = ImageTk.PhotoImage(img)
                top.image_refs.append(photo)
            except Exception:
                pass

            if photo:
                img_lbl = tk.Label(cell_frame, image=photo, width=120, height=120, bg="#e0e0e0", cursor="hand2")
                if item.get("is_video"):
                    img_lbl.bind("<Button-1>", lambda e, p=item['temp_path']: open_directory(p))
                else:
                    img_lbl.bind("<Button-1>", lambda e, i=idx: open_gallery(i))
                img_lbl.grid(row=0, column=0, padx=2, pady=2)
            else:
                img_lbl = tk.Label(cell_frame, text="无图", width=16, height=8, bg="#cccccc")
                img_lbl.grid(row=0, column=0, padx=2, pady=2)

            cb = tk.Checkbutton(cell_frame, text=text_str, variable=var, font=("Arial", 11), justify="center", bg=top.cget("bg"))
            cb.grid(row=1, column=0, pady=3)
            vars_dict[idx] = var

        def reflow_grid(event):
            canvas_width = event.width
            item_width = 140 # approximate width of one thumbnail block + padding
            cols = max(1, canvas_width // item_width)
            
            for i, cell in enumerate(item_frames):
                cell.grid(row=i // cols, column=i % cols, padx=5, pady=5, sticky="n")
                
            top.after(50, lambda: canvas.configure(scrollregion=canvas.bbox("all")))
            # 每次重排后重新递归绑定，因为新 widget 可能尚未绑定
            top.after(80, lambda: bind_wheel_recursive(top))

        canvas.bind("<Configure>", reflow_grid)

        def on_select_all():
            for var in vars_dict.values(): var.set(True)

        def on_invert():
            for var in vars_dict.values(): var.set(not var.get())

        def on_confirm():
            selected = [candidates[i] for i, var in vars_dict.items() if var.get()]
            result_box.append(selected)
            top.destroy()
            event.set()
            
        def on_abort():
            result_box.append(None)
            top.destroy()
            event.set()

        tk.Button(btn_frame, text="全选", command=on_select_all, width=10).pack(side="left", padx=10)
        tk.Button(btn_frame, text="反选", command=on_invert, width=10).pack(side="left", padx=10)
        tk.Button(btn_frame, text="确定下载", command=on_confirm, width=15, bg="#4CAF50", fg="black").pack(side="left", padx=10)
        tk.Button(btn_frame, text="终止下载", command=on_abort, width=15, bg="#f44336", fg="black").pack(side="left", padx=10)
        
        def on_closing():
            result_box.append([]) 
            top.destroy()
            event.set()
        top.protocol("WM_DELETE_WINDOW", on_closing)

    def poll_queue():
        try:
            while True:
                msg = log_queue.get_nowait()
                if isinstance(msg, tuple):
                    if msg[0] == "ASK_USER_IMAGES":
                        show_image_selection_dialog(msg[1], msg[2], msg[3])
                    elif msg[0] == "SHOW_TOP_DIALOG":
                        messagebox.showwarning(msg[1], msg[2], parent=root)
                        msg[3].set()
                    continue
                
                log_box.configure(state='normal')
                if msg.startswith('OPEN_DIR::'):
                    path = msg[len('OPEN_DIR::'):]
                    tag = f"link_{_link_tag_counter[0]}"
                    _link_tag_counter[0] += 1
                    link_text = f"     📂 点击打开目录: {path}"
                    log_box.insert('end', link_text + '\n', tag)
                    log_box.tag_config(tag, foreground='#4fc3f7', underline=True)
                    log_box.tag_bind(tag, '<Button-1>', lambda e, p=path: open_directory(p))
                    log_box.tag_bind(tag, '<Enter>', lambda e: log_box.config(cursor='hand2'))
                    log_box.tag_bind(tag, '<Leave>', lambda e: log_box.config(cursor=''))
                else:
                    log_box.insert('end', msg + '\n')
                log_box.see('end')
                log_box.configure(state='disabled')
                clean = msg.strip()
                if clean and not clean.startswith('OPEN_DIR::'):
                    status_var.set(clean[:90])
        except queue.Empty:
            pass
        root.after(150, poll_queue)

    def run_scraper(urls):
        scraper = TaobaoScraper(urls, log_queue)
        scraper.run()

    root.after(150, poll_queue)
    show_input_page()
    root.mainloop()


# ─── 入口 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    app_logger.start()
    try:
        run_app()
    finally:
        app_logger.exit()
